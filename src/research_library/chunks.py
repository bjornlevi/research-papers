"""Page-bound retrieval spans over immutable source text."""

import re


PAYLOAD = "evidence/chunks-v1.json"
DOCUMENT_PAYLOAD = "evidence/chunks-v2.json"
MAX_PER_PAGE = 100
MAX_DOCUMENT_CHUNKS = 2000
MAX_CHUNK_PAGES = 32
_BREAK = re.compile(r"\n(?:[ \t]*\n)+(?=\S|[ \t]+\S)")
_CAPTION = re.compile(r"(?:\*{0,2}(?:FIGURE|TABLE)\s+\d|\*Source:)", re.I)


def paragraphs(text):
    """Return offsets at paragraph boundaries, retaining every source character."""
    candidates = [0, *(match.end() for match in _BREAK.finditer(text)), len(text)]
    starts = [0]
    for previous, start, end in zip(candidates, candidates[1:], candidates[2:]):
        before, after = text[previous:start].strip(), text[start:end].lstrip()
        if (re.match(r"^#{1,6}\s", before) or
                (after.startswith(("![", "|")) and _CAPTION.match(before)) or
                (_CAPTION.match(after) and before.startswith(("![", "|", "*Source:")))):
            continue
        starts.append(start)
    return list(zip(starts, starts[1:] + [len(text)]))


def records(bundle, plans, *, identity, error, document_context=None):
    if not isinstance(plans, dict):
        raise error("Invalid retrieval chunk plans")
    units = {unit["id"]: unit for unit in bundle["units"]}
    occupied = {row["id"] for section in ("units", "claims", "tables") for row in bundle[section]}
    occupied.update(row["record"]["id"] for section in ("issues", "relationships") for row in bundle[section])
    replacements = {}
    for unit_id, ranges in plans.items():
        unit = units.get(unit_id)
        if unit is None or unit["citation"]["locator"]["kind"] != "pdf":
            raise error("Retrieval chunks must reference a retained PDF page")
        if isinstance(ranges, dict):
            _document_records(bundle, plans, unit, ranges, replacements, occupied,
                              identity=identity, error=error, context=document_context)
            continue
        spans = paragraphs(unit["text"])
        if not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_PER_PAGE:
            raise error(f"Provide 1 to {MAX_PER_PAGE} chunks per page")
        previous_start, end = 0, 0
        page_identity = identity([unit_id, unit["text"]])
        replacements[unit_id] = []
        for pair in ranges:
            if not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair):
                raise error("Use integer paragraph ranges")
            first, last = pair
            if not (previous_start < first <= end + 1 and end < last <= len(spans) and first <= last):
                raise error("Ranges must cover the page in order, without gaps or repeated chunks")
            start_offset, end_offset = spans[first - 1][0], spans[last - 1][1]
            text = unit["text"][start_offset:end_offset]
            if len(text) > 16000:
                raise error("A retrieval chunk exceeds 16,000 characters; split it into smaller ranges")
            identifier = "chunk:" + identity([page_identity, first, last])
            if identifier in occupied:
                raise error("Retrieval chunk ID collides with retained evidence")
            occupied.add(identifier)
            replacements[unit_id].append({**unit, "id": identifier, "text": text})
            previous_start, end = first, last
        if end != len(spans):
            raise error("Ranges must cover the final paragraph of each page")
    return replacements


def _document_records(bundle, plans, anchor, plan, replacements, occupied, *, identity, error, context):
    from .passage_context import describe, document_blocks

    source_id = anchor["citation"]["source_id"]
    pages = sorted((unit for unit in bundle["units"] if unit["citation"]["source_id"] == source_id),
                   key=lambda unit: (unit["citation"]["locator"]["pdf_page"], unit["id"]))
    if pages[0]["id"] != anchor["id"] or any(unit["id"] in plans for unit in pages[1:]):
        raise error("A document chunk plan must anchor at its first page and cannot mix with page plans")
    ranges = plan.get("ranges")
    if set(plan) - {"ranges", "labels"} or not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_DOCUMENT_CHUNKS:
        raise error(f"Provide 1 to {MAX_DOCUMENT_CHUNKS} document chunk ranges")
    labels = plan.get("labels", {})
    if not isinstance(labels, dict) or len(labels) > len(ranges) or any(
        not isinstance(label, str) or not 1 <= len(label) <= 200 or label != label.strip()
        or not label.isprintable() or not re.search(r"\w", label) for label in labels.values()
    ):
        raise error("Editorial labels must be printable, single-line text up to 200 characters")
    source = next(source for source in bundle["sources"] if source["id"] == source_id)
    title = (source.get("bibliography") or {}).get("title")
    blocks = document_blocks(pages, title=title)
    details = [block[3] for block in blocks]
    document_id = identity([(unit["id"], unit["citation"]["locator"], unit["text"]) for unit in pages])
    for unit in pages:
        replacements[unit["id"]] = []
    previous_start, previous_end = 0, 0
    starts = set()
    for pair in ranges:
        if not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair):
            raise error("Use integer document block ranges")
        first, last = pair
        starts.add(str(first))
        if not (previous_start < first <= previous_end + 1 and previous_end < last <= len(blocks)):
            raise error("Ranges must cover the document in order, without gaps or repeated chunks")
        spans = []
        for unit, start, end, _ in blocks[first - 1:last]:
            if spans and spans[-1][0]["id"] == unit["id"]:
                spans[-1] = (unit, spans[-1][1], end)
            else:
                spans.append((unit, start, end))
        if len(spans) > MAX_CHUNK_PAGES or sum(end - start for _, start, end in spans) + len(spans) - 1 > 16000:
            raise error("Split chunks longer than 16,000 characters or spanning more than 32 PDF pages")
        text = "\n".join(unit["text"][start:end] for unit, start, end in spans)
        identifier = "chunk:" + identity(["document-v2", document_id, first, last])
        if identifier in occupied:
            raise error("Retrieval chunk ID collides with retained evidence")
        occupied.add(identifier)
        first_unit = spans[0][0]
        statuses = [unit["verification_status"] for unit, _, _ in spans]
        status = min(statuses, key=("not_reviewed", "machine_extracted", "source_checked_by_ai",
                                    "source_checked_by_human").index)
        replacements[first_unit["id"]].append({**first_unit, "id": identifier, "text": text,
            "truncated": any(unit["truncated"] for unit, _, _ in spans),
            "verification_status": status,
            "human_verification": "reviewed" if all(unit["human_verification"] == "reviewed"
                                                     for unit, _, _ in spans) else "not_reviewed",
            "caveats": list(dict.fromkeys(value for unit, _, _ in spans for value in unit["caveats"]))[:100],
            "source_issue_ids": list(dict.fromkeys(value for unit, _, _ in spans
                                                   for value in unit["source_issue_ids"]))[:100]})
        if context is not None:
            context[identifier] = describe(title, details, first, last) | {
                "block_scope": "document",
                "source_spans": [{"unit_id": unit["id"], "citation": unit["citation"], "start": start, "end": end}
                                 for unit, start, end in spans],
            }
            if str(first) in labels:
                context[identifier]["editorial_label"] = labels[str(first)]
        previous_start, previous_end = first, last
    if previous_end != len(blocks):
        raise error("Ranges must cover the final document block")
    if labels.keys() - starts:
        raise error("Each editorial label must identify the first block of a chunk")
