"""Draft and activate retrieval boundaries without changing published text."""

from copy import deepcopy
from pathlib import Path
import re
import tempfile

from research_library import chunks, collections, passage_context, releases, retrieval
from . import literature_update, project_edit, project_library, project_text

CHECK_LIMIT = 40
CHECK_DEPTH = 5


def draft_name(source_id):
    return releases.sha256(releases.encode(source_id)) + ".json"


def load_draft(root, source_id):
    return _load(root, "chunk-drafts", source_id)


def load_checks(root, source_id):
    return _load(root, "chunk-checks", source_id) or []


def _load(root, directory, source_id):
    folder = Path(root) / directory
    name = draft_name(source_id)
    if not (folder / name).exists():
        return None
    return releases.read_json(releases.safe_file(folder, name).read_bytes())


def suggestions(blocks):
    if not blocks:
        return []
    ranges = []
    start = 1
    length = 0
    page_count = 0
    previous = None
    previous_end = 0
    previous_context = None
    for index, (unit, offset, end, context) in enumerate(blocks, 1):
        size = end - offset
        if size > 16000:
            return []
        new_page = previous is not None and previous["id"] != unit["id"]
        section_or_element = previous_context is not None and (
            previous_context[0] != context[0] or previous_context[1] or context[1])
        continuation = new_page and not section_or_element and (
            previous["text"][:previous_end].rstrip()
            and not previous["text"][:previous_end].rstrip().endswith((".", "!", "?", ":")))
        if index > start and (section_or_element or (length >= 1600 and not continuation)
                              or length + size + new_page > 16000
                              or page_count + new_page > chunks.MAX_CHUNK_PAGES):
            ranges.append([start, index - 1])
            start, length, page_count = index, 0, 0
        if not page_count or new_page:
            page_count += 1
        length += size + (new_page and index != start)
        previous, previous_end, previous_context = unit, end, context
    ranges.append([start, len(blocks)])
    return ranges if len(ranges) <= chunks.MAX_DOCUMENT_CHUNKS else []


def parse(units, form):
    if "ranges" not in form:
        raise ValueError("Reload the RAG/MCP tab to edit document ranges")
    value = form["ranges"].strip()
    label_text = form.get("labels", "")
    if len(label_text) > chunks.MAX_DOCUMENT_CHUNKS * 211:
        raise ValueError("Editorial labels are too long")
    labels = {}
    for line in label_text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*([1-9][0-9]{0,6})\s*:\s*(.+?)\s*", line)
        if not match or match[1] in labels:
            raise ValueError("Use one editorial label per line, like 23: Cohort counts")
        labels[match[1]] = match[2]
    if not value:
        if labels:
            raise ValueError("Editorial labels require document ranges")
        return {}
    if len(value) > 36000:
        raise ValueError("Document ranges are too long")
    ranges = []
    for field in value.split(","):
        match = re.fullmatch(r"\s*([1-9][0-9]{0,6})(?:\s*-\s*([1-9][0-9]{0,6}))?\s*", field)
        if not match:
            raise ValueError("Enter ranges like 1-3, 3-5, 6, or leave the ranges blank")
        first, last = match.groups()
        ranges.append([int(first), int(last or first)])
    return {units[0]["id"]: {"ranges": ranges, **({"labels": labels} if labels else {})}}


def combined(snapshot, units, plans):
    selected = {unit["id"] for unit in units}
    return {key: value for key, value in snapshot.chunk_plans.items() if key not in selected} | plans


def passage_anchors(blocks, ranges):
    anchors = {}
    for first, last in ranges:
        parts = [(unit["citation"]["locator"]["pdf_page"], unit["text"][start:end].strip())
                 for unit, start, end, _ in blocks[first - 1:last]]
        if parts:
            anchors[str(first)] = {
                "digest": releases.sha256(releases.encode(parts)),
                "excerpt": " ".join(" ".join(text for _, text in parts).split())[:240],
                "page": parts[0][0],
            }
    return anchors


def term_review(root, source_id, draft, identity):
    if not draft:
        return []
    pending = list(draft.get("unassigned_terms", []))
    if draft["content_identity"] == identity:
        return pending
    plan = next((p for p in draft["plans"].values() if isinstance(p, dict) and p.get("labels")), None)
    if plan is None:
        return pending
    anchors = draft.get("term_anchors")
    if anchors is None:
        # Older drafts did not store anchors. Resolve their content version, not their old numbers.
        event = next((event for event in project_edit.history(root, source_id)
                      if (event.get("before") or {}).get("content_identity") == draft["content_identity"]
                      and event.get("scope") in {"publication", "unpublication"}
                      and event.get("parent_snapshot")), None)
        anchors = {}
        if event:
            registry = project_library.store(root)
            try:
                old = registry.load(event["parent_snapshot"]).snapshots["core"]
            except collections.SnapshotUnavailable:
                pass
            else:
                source = old._lookup("sources", source_id)
                blocks = passage_context.document_blocks(old.document_units(source_id),
                                                         title=source["bibliography"]["title"])
                anchors = passage_anchors(blocks, plan["ranges"])
    for first, terms in plan["labels"].items():
        entry = {"terms": terms, **anchors.get(first, {})}
        if entry not in pending:
            pending.append(entry)
    return pending


def suggest_terms(blocks, ranges, pending):
    anchors = passage_anchors(blocks, ranges)
    attached = {}
    unassigned = []
    for entry in pending:
        matches = [first for first, anchor in anchors.items()
                   if anchor["digest"] == entry.get("digest")]
        if len(matches) == 1 and (matches[0] not in attached or attached[matches[0]] == entry["terms"]):
            attached[matches[0]] = entry["terms"]
        else:
            unassigned.append(entry)
    return attached, unassigned


def preview_snapshot(snapshot, units, plans):
    plans = combined(snapshot, units, plans)
    context = {}
    replacements = releases.chunk_records(snapshot._bundle, plans, document_context=context)
    with tempfile.TemporaryDirectory() as temporary:
        index = releases._index(Path(temporary) / "index.sqlite", snapshot._bundle, replacements,
                                labels=releases._editorial_labels(context))
    return releases.Snapshot(snapshot._manifest, snapshot._bundle, {
        "evidence/index.sqlite": index, **releases.chunk_payloads(plans),
    })


def search_hits(snapshot, source, query):
    return snapshot.call("search_evidence", {"query": query, "paper": source["doi"],
                                            "limit": CHECK_DEPTH})["result"]["items"]


def preview(snapshot, source, units, plans, query):
    candidate = preview_snapshot(snapshot, units, plans)
    hits = search_hits(candidate, source, query)
    return [(hit, candidate.call("get_source_context", {"unit_id": hit["id"]})["result"],
             candidate._lookup("units", hit["id"])["text"]) for hit in hits]


def normalized(text):
    return " ".join(text.split())


def check_rank(snapshot, source, check):
    for rank, hit in enumerate(search_hits(snapshot, source, check["query"]), 1):
        context = hit.get("passage_context", {})
        exact = hit.get("exact_evidence")
        spans = context.get("source_spans")
        if spans:
            page_texts = [snapshot._lookup("units", span["unit_id"])["text"][span["start"]:span["end"]]
                          for span in spans if span["citation"]["locator"]["pdf_page"] == check["page"]]
        elif hit["citation"]["locator"]["pdf_page"] == check["page"]:
            page_texts = [snapshot._lookup("units", hit["id"])["text"]]
        else:
            page_texts = []
        if (any(check["excerpt"] in normalized(text) for text in page_texts)
                and (exact is None or check["excerpt"] in normalized(exact["text"]))):
            return rank
    return None


def expected_present(units, page, excerpt):
    return any(unit["citation"]["locator"]["pdf_page"] == page
               and excerpt in normalized(unit["text"]) for unit in units)


def run_checks(live, source, units, plans, checks):
    if not checks:
        return []
    draft = preview_snapshot(live, units, plans)
    results = []
    for check in checks:
        found = expected_present(units, check["page"], check["excerpt"])
        results.append({"id": check["id"], "expected_present": found,
                        "live": check_rank(live, source, check) if found else None,
                        "draft": check_rank(draft, source, check) if found else None})
    return results


def update_check(root, *, source_id, snapshot_id, form, remove=False):
    root = Path(root)
    with literature_update.writer_lock(root):
        current = project_library.store(root).current()
        if current.release_id != snapshot_id:
            raise ValueError("Published retrieval changed; reload before editing search checks")
        snapshot = current.snapshots["core"]
        units = snapshot.document_units(source_id)
        checks = load_checks(root, source_id)
        if remove:
            check_id = form.get("check_id", "")
            if not any(check["id"] == check_id for check in checks):
                raise ValueError("That search check no longer exists")
            checks = [check for check in checks if check["id"] != check_id]
        else:
            query = form.get("query", "").strip()
            retrieval.search_expression(query, "plain")
            excerpt = normalized(form.get("expected_excerpt", ""))
            page_number = form.get("expected_page", "").strip()
            if not re.fullmatch(r"[1-9][0-9]{0,6}", page_number) or not 8 <= len(excerpt) <= 500:
                raise ValueError("Enter a PDF page and an excerpt of 8 to 500 characters")
            page_number = int(page_number)
            if not expected_present(units, page_number, excerpt):
                raise ValueError("The expected excerpt must appear on the selected published PDF page")
            check = {"query": query, "page": page_number, "excerpt": excerpt}
            check["id"] = releases.sha256(releases.encode(check))
            if any(item["id"] == check["id"] for item in checks):
                raise ValueError("This search check is already saved")
            if len(checks) >= CHECK_LIMIT:
                raise ValueError(f"Save up to {CHECK_LIMIT} checks per paper")
            checks = [*checks, check]
        collections.atomic_json(root / "chunk-checks", draft_name(source_id), checks)


def update(root, *, source_id, snapshot_id, form, activate):
    root = Path(root)
    with literature_update.writer_lock(root):
        registry = project_library.store(root)
        current = registry.current()
        if current.release_id != snapshot_id:
            raise ValueError("Published retrieval changed; reload before saving or activating chunks")
        snapshot = current.snapshots["core"]
        units = snapshot.document_units(source_id)
        if not units:
            raise ValueError("Publish the paper before configuring retrieval chunks")
        plans = parse(units, form)
        all_plans = combined(snapshot, units, plans)
        releases.chunk_records(snapshot._bundle, all_plans)
        identity = project_text.content_identity(snapshot._bundle, source_id)
        draft = load_draft(root, source_id)
        pending = term_review(root, source_id, draft, identity)
        plan = plans.get(units[0]["id"], {})
        labels = plan.get("labels", {})
        source = snapshot._lookup("sources", source_id)
        blocks = passage_context.document_blocks(units, title=source["bibliography"]["title"])
        anchors = passage_anchors(blocks, [pair for pair in plan.get("ranges", []) if str(pair[0]) in labels])
        assigned = set(labels.values())
        collections.atomic_json(root / "chunk-drafts", draft_name(source_id), {
            "content_identity": identity,
            "plans": plans,
            "term_anchors": anchors,
            "unassigned_terms": [entry for entry in pending if entry["terms"] not in assigned],
        })
        if activate and all_plans != snapshot.chunk_plans:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "release"
                release_id = releases.export(snapshot._bundle, output, profile="private_context",
                    authorization=snapshot._manifest["authorization"], chunk_plans=all_plans)
                registry.releases.install(output)
            document = deepcopy(current.document)
            entry = next(entry for entry in document["collections"] if entry["id"] == "core")
            entry.update(release_id=release_id, version="chunks-" + release_id[:16])
            registry.publish(document)
