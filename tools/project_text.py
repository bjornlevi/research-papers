"""Private editorial versions and explicit publication of clear text for retrieval."""

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from research_library import collections, releases
from . import literature_update


def editorial_store(root):
    root = Path(root)
    path = root / "editorial"
    if not (path / "active.json").exists():
        path = root / "registry"
    return collections.CollectionStore(path, allowed_profiles=["private_context"],
                                       allowed_collections=["core"], defaults=["core"])


def empty_bundle():
    return {"schema_version": 1, "caveats": ["Only explicitly published clear text is available for retrieval."],
            **{section: [] for section in releases.SECTIONS}}


def attached_bundle(source, result):
    """Keep an attachable document and page anchors even without usable extraction."""
    location = source["acquisition"]["acquisition"]
    record = {
        "id": source["source_id"], "doi": source["publication_id"].removeprefix("doi:"),
        "sha256": source["sha256"], "format": "pdf", "url": location["final_url"],
        "provenance": "Operator-provided private PDF. Draft text is not published or human verified.",
        "rights": "restricted", "omission": "original_not_exported",
        "bibliography": {"title": source["title"], "year": None, "acquired_from": location["url"],
                         "acquisition_method": "user_provided_private_copy",
                         "acquired_at": source["acquisition"]["acquired_at"]},
    }
    bundle = empty_bundle()
    bundle["sources"] = [record]
    for page in result["pages"]:
        bundle["units"].append({
            "id": record["id"] + f':p{page["pdf_page"]}', "text": page.get("text", ""), "truncated": False,
            "citation": {"source_id": record["id"], "doi": record["doi"], "source_sha256": record["sha256"],
                         "source_url": record["url"], "provenance": record["provenance"],
                         "availability": "metadata_only", "locator": {"kind": "pdf", "pdf_page": page["pdf_page"]}},
            "verification_status": "machine_extracted", "human_verification": "not_reviewed",
            "caveats": ["No usable extraction at attachment; an admin must provide and check clear text."],
            "source_issue_ids": [],
        })
    return {"bundle": bundle, "grants": {record["id"]: {"scopes": ["curated", "context"],
            "basis": source["permissions"]["basis"]}}, "ocr": None}


def activate(root, bundle, grants, *, editorial=False):
    """Install a validated immutable release, then activate its registry last."""
    from . import project_library
    root = Path(root)
    registry = collections.CollectionStore(root / ("editorial" if editorial else "registry"),
        allowed_profiles=["private_context"], allowed_collections=["core"], defaults=["core"])
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "evidence"
        release_id = releases.export(bundle, output, profile="private_context", authorization=grants)
        registry.releases.install(output)
    previous = registry.current() if (registry.root / "active.json").exists() else None
    document = deepcopy(previous.document) if previous else {"schema_version": 1, "collections": []}
    document["collections"] = [entry for entry in document["collections"] if entry["id"] != "core"]
    document["collections"].append({
        "id": "core", "title": project_library.load(root)["name"], "version": release_id[:16],
        "updated_at": datetime.now(timezone.utc).date().isoformat(),
        "coverage": ("Private attached sources and editorial drafts; not authorized as published RAG text."
                     if editorial else "Explicitly published clear text; transcription approval is not scientific validation."),
        "release_id": release_id, "publications": {},
    })
    return {"snapshot": registry.publish(document), "release": release_id}


def ensure_editorial(root):
    from . import project_library
    root = Path(root)
    if (root / "editorial/active.json").exists():
        return
    if (root / "registry/active.json").exists():
        original = project_library.store(root).current().snapshots["core"]
        activate(root, original._bundle, original._manifest["authorization"], editorial=True)
    else:
        activate(root, empty_bundle(), {}, editorial=True)
        activate(root, empty_bundle(), {})


def migrate(root, *, withdraw=False):
    """Preserve previous releases; optionally withdraw unapproved active evidence."""
    from . import project_library
    root = Path(root)
    with literature_update.writer_lock(root):
        ensure_editorial(root)
        old = project_library.store(root).current()
        if withdraw:
            result = activate(root, empty_bundle(), {})
        else:
            result = {"snapshot": old.release_id,
                      "release": old.snapshots["core"].release_id}
        result.update(editorial_snapshot=editorial_store(root).current().release_id,
                      previous_snapshot=old.release_id, withdrawn=withdraw)
        return result


def source_bundle(bundle, source_id):
    selected = deepcopy(bundle)
    selected["sources"] = [source for source in selected["sources"] if source["id"] == source_id]
    if not selected["sources"]:
        raise ValueError("Select an attached source")
    for section in ("units", "tables"):
        selected[section] = [item for item in selected[section] if item["citation"]["source_id"] == source_id]
    for section in ("claims", "issues", "relationships"):
        if selected[section]:
            raise ValueError("Curated dependencies require a dependency-aware clear-text publication")
    return selected


def content_identity(bundle, source_id):
    """Compare document content independently of verification/provenance labels."""
    selected = source_bundle(bundle, source_id)
    source = selected["sources"][0]
    return releases.sha256(releases.encode({
        "source": {key: source.get(key) for key in ("id", "sha256", "bibliography", "url")},
        "units": sorted((item["id"], item["text"], item["citation"]["locator"]) for item in selected["units"]),
        "tables": sorted((item["id"], {key: item[key] for key in
                        ("title", "headers", "units", "rows", "footnotes")}) for item in selected["tables"]),
    }))


def publication_state(editorial, published, source_id):
    if not any(source["id"] == source_id for source in published["sources"]):
        return "draft"
    return ("published" if content_identity(editorial, source_id) == content_identity(published, source_id)
            else "draft_revision")


def _git_markdown_directory(root):
    result = subprocess.run(
        ["git", "-C", str(Path(root).resolve()), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("Git Markdown tracking requires the project to be inside a Git repository")
    repository = Path(result.stdout.strip()).resolve()
    destination = repository / "project-markdown"
    if destination.is_symlink():
        raise ValueError("Git Markdown output directory cannot be a symlink")
    destination.mkdir(exist_ok=True)
    return destination


def _markdown_cell(value):
    return str(value if value is not None else "").replace("|", r"\|").replace("\r\n", "\n").replace("\n", "<br>")


def _markdown_version(bundle, source_id, kind, release_id, known):
    from . import literature_text

    source = next((item for item in bundle["sources"] if item["id"] == source_id), None)
    if source is None:
        raise ValueError("Cannot export Markdown for a missing source")
    units = [item for item in bundle["units"] if item["citation"]["source_id"] == source_id]
    content = [f"> Version type: **{kind}**<br>\n> Release: `{release_id}`\n\n",
               literature_text.render(source, units, {}, known)]
    tables = [table for table in bundle["tables"] if table["citation"]["source_id"] == source_id]
    if tables:
        content.append("\n## Structured tables\n\n")
    for table in tables:
        page = table["citation"]["locator"].get("pdf_page")
        content.append(f"### {_markdown_cell(table['title'])}\n\n")
        if page is not None:
            content.append(f"PDF page: {page}\n\n")
        headers = [header + (f" ({unit})" if unit else "")
                   for header, unit in zip(table["headers"], table["units"])]
        content.extend([
            "| " + " | ".join(_markdown_cell(value) for value in headers) + " |\n",
            "| " + " | ".join("---" for _ in headers) + " |\n",
        ])
        for row in table["rows"]:
            content.append("| " + " | ".join(_markdown_cell(value) for value in row) + " |\n")
        if table["footnotes"]:
            content.append("\n" + "\n".join(f"- {_markdown_cell(note)}" for note in table["footnotes"]) + "\n")
        content.append("\n")
    lines = []
    for line in "".join(content).splitlines():
        trailing = len(line) - len(line.rstrip(" \t"))
        lines.append(line.rstrip(" \t") + ("<br>" if trailing >= 2 else ""))
    return "\n".join(lines).rstrip() + "\n"


def export_git_markdown(root, bundle, *, source_id, kind, release_id, force=False,
                        known=None, refresh_index=True, output_root=None, stage=True):
    from . import literature_text, project_library

    if not force and not project_library.load(root).get("git_markdown_tracking", False):
        return None
    if kind not in {"extracted", "draft", "published"} or not re.fullmatch(r"[0-9a-f]{64}", release_id):
        raise ValueError("Invalid Git Markdown version identity")
    source = next((item for item in bundle["sources"] if item["id"] == source_id), None)
    if source is None:
        raise ValueError("Cannot export Markdown for a missing source")
    if known is None:
        known = literature_text.evidence(bundle["units"])
    directory = (output_root or _git_markdown_directory(root)) / literature_text.slug(source)
    if directory.is_symlink():
        raise ValueError("Git Markdown paper directory cannot be a symlink")
    directory.mkdir(exist_ok=True)
    target = directory / f"{kind}-{release_id}.md"
    if target.is_symlink():
        raise ValueError("Git Markdown version cannot be a symlink")
    content = _markdown_version(bundle, source_id, kind, release_id, known)
    if target.exists():
        if target.read_text(encoding="utf-8") == content:
            if refresh_index:
                _write_git_markdown_index(directory.parent)
            if stage:
                paths = [target]
                if refresh_index:
                    paths.append(directory.parent / "index.md")
                _stage_git_markdown(directory.parent, paths)
            return target
    descriptor, temporary = tempfile.mkstemp(prefix=".markdown-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if refresh_index:
        _write_git_markdown_index(directory.parent)
    if stage:
        paths = [target]
        if refresh_index:
            paths.append(directory.parent / "index.md")
        _stage_git_markdown(directory.parent, paths)
    return target


def _write_git_markdown_index(directory):
    lines = ["# Project paper Markdown\n\n",
             "Generated from immutable project releases. Commit these files to retain their history in Git.\n\n"]
    for paper in sorted(path for path in directory.iterdir() if path.is_dir()):
        if paper.is_symlink():
            raise ValueError("Git Markdown paper directory cannot be a symlink")
        versions = sorted(path for path in paper.glob("*.md") if path.name != "index.md")
        if not versions:
            continue
        lines.append(f"## {paper.name}\n\n")
        for version in versions:
            if version.is_symlink():
                raise ValueError("Git Markdown version cannot be a symlink")
            lines.append(f"- [{version.stem}]({paper.name}/{version.name})\n")
        lines.append("\n")
    target = directory / "index.md"
    if target.is_symlink():
        raise ValueError("Git Markdown index cannot be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".index-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(lines).rstrip() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _stage_git_markdown(directory, paths):
    repository = directory.parent
    names = [path.relative_to(repository).as_posix() for path in paths]
    result = subprocess.run(
        ["git", "-C", str(repository), "add", "--", *names],
        capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("Could not stage generated Git Markdown: " + result.stderr.strip())


def backfill_git_markdown(root):
    from . import literature_text, project_library

    count = 0
    output = _git_markdown_directory(root)
    staged = []
    workspace = editorial_store(root)
    if (workspace.root / "active.json").is_file():
        snapshot = workspace.current().snapshots["core"]
        known = literature_text.evidence(snapshot._bundle["units"])
        for source in snapshot._bundle["sources"]:
            export_git_markdown(root, snapshot._bundle, source_id=source["id"],
                                kind="draft", release_id=snapshot.release_id, force=True,
                                known=known, refresh_index=False, output_root=output, stage=False)
            staged.append(output / literature_text.slug(source) /
                          f"draft-{snapshot.release_id}.md")
            count += 1
    published_store = project_library.store(root)
    if (published_store.root / "active.json").is_file():
        snapshot = published_store.current().snapshots["core"]
        known = literature_text.evidence(snapshot._bundle["units"])
        for source in snapshot._bundle["sources"]:
            export_git_markdown(root, snapshot._bundle, source_id=source["id"],
                                kind="published", release_id=snapshot.release_id, force=True,
                                known=known, refresh_index=False, output_root=output, stage=False)
            staged.append(output / literature_text.slug(source) /
                          f"published-{snapshot.release_id}.md")
            count += 1
    if count:
        _write_git_markdown_index(output)
        _stage_git_markdown(output, [*staged, output / "index.md"])
    return count


def publish(root, *, snapshot_id, source_id, reviewer, reason, checked):
    from . import project_library, project_edit
    root = Path(root)
    reviewer, reason = reviewer.strip(), reason.strip()
    if not reviewer or len(reviewer) > 200 or not reason or len(reason) > 2000:
        raise ValueError("Provide a reviewer name and publication reason")
    if not checked:
        raise ValueError("Confirm that the clear text was checked against the original source before publication")
    with literature_update.writer_lock(root):
        ensure_editorial(root)
        current = editorial_store(root).current()
        if current.release_id != snapshot_id:
            raise ValueError("Editorial text changed; reload before publishing")
        evidence = current.snapshots["core"]
        selected = source_bundle(evidence._bundle, source_id)
        if not selected["units"] or not any(unit["text"].strip() for unit in selected["units"]):
            raise ValueError("Provide clear text before publishing")
        if any(unit["truncated"] for unit in selected["units"]):
            raise ValueError("Resolve incomplete page text before publishing")
        source = selected["sources"][0]
        source["provenance"] = (
            source["provenance"]
            .replace("Admin-edited draft; original extraction retained in prior snapshots; not published for retrieval.",
                     "Admin-edited transcription; original extraction retained in prior snapshots.")
            .replace("Draft text is not published or human verified.",
                     "Clear text explicitly approved by the admin for retrieval; not scientific validation.")
        )
        publication_note = " Clear text explicitly published after admin source comparison; see local publication audit."
        if publication_note not in source["provenance"]:
            source["provenance"] += publication_note
        for item in [*selected["units"], *selected["tables"]]:
            item["citation"]["provenance"] = source["provenance"]
            item.update(verification_status="source_checked_by_human", human_verification="reviewed")
            if project_edit.CORRECTION_CAVEAT not in item["caveats"]:
                item["caveats"].append(project_edit.CORRECTION_CAVEAT)
        published = project_library.store(root).current()
        original = published.snapshots["core"]
        bundle = deepcopy(original._bundle)
        if any(bundle[key] for key in ("claims", "issues", "relationships")):
            raise ValueError("Curated dependencies require a dependency-aware clear-text publication")
        for section in ("sources", "units", "tables"):
            bundle[section] = [item for item in bundle[section]
                if (item["id"] if section == "sources" else item["citation"]["source_id"]) != source_id]
            bundle[section].extend(selected[section])
        grants = deepcopy(original._manifest["authorization"])
        grants[source_id] = evidence._manifest["authorization"][source_id]
        (root / "runs").mkdir(exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="correction-publication-", dir=root / "runs"))
        activated = False
        try:
            # Audit and immutable release are staged before the retrieval registry changes.
            release_id = releases.export(bundle, run / "evidence", profile="private_context", authorization=grants)
            project_library.store(root).releases.install(run / "evidence")
            project_library.write_text_views(run / "text", selected)
            event = {"schema_version": 1, "at": datetime.now(timezone.utc).isoformat(),
                     "reviewer": reviewer, "reason": reason, "scope": "publication", "source_id": source_id,
                     "source_sha256": selected["sources"][0]["sha256"], "unit_id": None,
                     "parent_snapshot": published.release_id, "parent_release": original.release_id,
                     "editorial_snapshot": current.release_id, "release": release_id,
                     "before": {"content_identity": (content_identity(original._bundle, source_id)
                                if any(s["id"] == source_id for s in original._bundle["sources"]) else None)},
                     "after": {"content_identity": content_identity(selected, source_id)}}
            collections.atomic_json(run, "correction.json", event)
            document = deepcopy(published.document)
            entry = next(entry for entry in document["collections"] if entry["id"] == "core")
            entry.update(release_id=release_id, version="published-" + release_id[:16],
                         coverage="Admin-published clear text; transcription approval is not scientific validation.")
            # Export a published Markdown version only after retrieval activation succeeds.
            snapshot = project_library.store(root).publish(document)
            activated = True
            export_git_markdown(root, selected, source_id=source_id,
                                kind="published", release_id=release_id)
            return {"snapshot": snapshot, "release": release_id, "audit": str(run / "correction.json")}
        finally:
            if not activated:
                shutil.rmtree(run)


def publication(root, source_id, *, current=None):
    """Describe the active retrieval version, not a newer editorial draft."""
    from . import project_edit, project_library
    registry = project_library.store(root)
    if not (registry.root / "active.json").is_file():
        return None
    current = current if current is not None else registry.current()
    bundle = current.snapshots["core"]._bundle
    if not any(source["id"] == source_id for source in bundle["sources"]):
        return None
    identity = content_identity(bundle, source_id)
    event = next((event for event in project_edit.history(root, source_id)
                  if event.get("scope") == "publication"
                  and event.get("after", {}).get("content_identity") == identity), None)
    return {"snapshot": current.release_id, "content_identity": identity, "event": event}


def unpublish(root, *, snapshot_id, source_id, reviewer, reason, checked):
    """Withdraw one source from active retrieval without removing editorial/history data."""
    from . import project_library
    root = Path(root)
    reviewer, reason = reviewer.strip(), reason.strip()
    if not reviewer or len(reviewer) > 200 or not reason or len(reason) > 2000:
        raise ValueError("Provide your name and an unpublication reason")
    if not checked:
        raise ValueError("Confirm removal from active AI retrieval")
    with literature_update.writer_lock(root):
        registry = project_library.store(root)
        current = registry.current()
        if current.release_id != snapshot_id:
            raise ValueError("Published content changed; reload before unpublishing")
        original = current.snapshots["core"]
        selected = source_bundle(original._bundle, source_id)
        ensure_editorial(root)
        bundle = deepcopy(original._bundle)
        for section in ("sources", "units", "tables"):
            bundle[section] = [item for item in bundle[section]
                              if (item["id"] if section == "sources" else item["citation"]["source_id"]) != source_id]
        grants = deepcopy(original._manifest["authorization"])
        grants.pop(source_id, None)
        (root / "runs").mkdir(exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="correction-unpublication-", dir=root / "runs"))
        activated = False
        try:
            release_id = releases.export(bundle, run / "evidence", profile="private_context", authorization=grants)
            registry.releases.install(run / "evidence")
            event = {"schema_version": 1, "at": datetime.now(timezone.utc).isoformat(),
                     "reviewer": reviewer, "reason": reason, "scope": "unpublication", "source_id": source_id,
                     "source_sha256": selected["sources"][0]["sha256"], "unit_id": None,
                     "parent_snapshot": current.release_id, "parent_release": original.release_id,
                     "release": release_id,
                     "before": {"content_identity": content_identity(selected, source_id)},
                     "after": {"content_identity": None}}
            collections.atomic_json(run, "correction.json", event)
            document = deepcopy(current.document)
            entry = next(entry for entry in document["collections"] if entry["id"] == "core")
            entry.update(release_id=release_id, version="published-" + release_id[:16])
            snapshot = registry.publish(document)
            activated = True
            return {"snapshot": snapshot, "release": release_id, "audit": str(run / "correction.json")}
        finally:
            if not activated:
                shutil.rmtree(run)
