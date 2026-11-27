"""Export current private editorial and retrieval state without derived indexes."""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import shutil
import tempfile

from research_library import collections, releases
from . import literature_update, project_chunks, project_edit, project_library, project_text


def _snapshot(store):
    if not (store.root / "active.json").is_file():
        return None
    snapshot = store.current().snapshots["core"]
    return {
        "bundle": snapshot._bundle,
        "authorization": snapshot._manifest["authorization"],
        "chunk_plans": snapshot.chunk_plans,
    }


def current(root):
    """Capture current text and retrieval settings, not PDFs or discovery caches."""
    root = Path(root)
    with literature_update.writer_lock(root):
        project = project_library.load(root)
        published = _snapshot(project_library.store(root))
        editorial = (_snapshot(project_text.editorial_store(root))
                     if (root / "editorial" / "active.json").is_file() else None)
        if editorial is None and published is not None:
            raise ValueError("Separate editorial state before exporting current project state")
        source_ids = {source["id"] for snapshot in (editorial, published) if snapshot
                      for source in snapshot["bundle"]["sources"]}
        names = {project_chunks.draft_name(source_id) for source_id in source_ids}
        for folder in ("chunk-drafts", "chunk-checks"):
            directory = root / folder
            if directory.is_symlink():
                raise ValueError(f"Symlinked {folder} directory")
            if directory.exists():
                if not directory.is_dir():
                    raise ValueError(f"Invalid {folder} directory")
                if any(path.is_symlink() or not path.is_file() or path.name not in names
                       for path in directory.iterdir()):
                    raise ValueError(f"Unassociated or invalid files in {folder}; review before export")
        rag = {}
        audit = {}
        for source_id in sorted(source_ids):
            draft = project_chunks.load_draft(root, source_id)
            checks = project_chunks.load_checks(root, source_id)
            if draft is not None or checks:
                rag[source_id] = {"draft": draft, "checks": checks}
            events = project_edit.history(root, source_id)
            latest = {}
            for event in events:
                scope = event.get("scope", "draft")
                kind = scope if scope in {"publication", "unpublication"} else "draft"
                if kind not in latest:
                    latest[kind] = event
            if latest:
                audit[source_id] = latest
        return {"schema_version": 1, "project": project, "editorial": editorial,
                "published": published, "rag": rag, "audit": audit}


def export(root, destination):
    """Write a private current-state export; never overwrite existing files."""
    state = current(root)
    path = Path(destination)
    if path.is_symlink():
        raise ValueError("Current-state export cannot be a symlink")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    completed = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(releases.encode(state))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        completed = True
    finally:
        if not completed:
            path.unlink(missing_ok=True)
    return {"file": str(path), "published": len(state["published"]["bundle"]["sources"])
            if state["published"] else 0,
            "editorial": len(state["editorial"]["bundle"]["sources"]) if state["editorial"] else 0}


def _paper_filename(source_id):
    return releases.sha256(releases.encode(source_id)) + ".json"


def _write_private_json(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _split_snapshot(snapshot, papers, name):
    if snapshot is None:
        return None
    bundle = snapshot["bundle"]
    result = {"bundle": deepcopy(bundle), "authorization": [], "chunk_plans": []}
    for section in ("sources", "units", "tables"):
        result["bundle"][section] = [item["id"] for item in bundle[section]]
    for source in bundle["sources"]:
        source_id = source["id"]
        units = [unit for unit in bundle["units"] if unit["citation"]["source_id"] == source_id]
        tables = [table for table in bundle["tables"] if table["citation"]["source_id"] == source_id]
        unit_ids = {unit["id"] for unit in units}
        papers[source_id][name] = {
            "source": source, "units": units, "tables": tables,
            "authorization": snapshot["authorization"][source_id],
            "chunk_plans": {key: value for key, value in snapshot["chunk_plans"].items() if key in unit_ids},
        }
    for key in ("authorization", "chunk_plans"):
        result[key] = list(snapshot[key])
    return result


def export_papers(root, destination):
    """Write a new private per-paper current-state directory without overwriting data."""
    state = current(root)
    root = Path(destination).absolute()
    if root.exists() or root.is_symlink():
        raise ValueError("Current-state destination already exists")
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="." + root.name + "-export-", dir=root.parent))
    try:
        source_ids = {source["id"] for snapshot in (state["editorial"], state["published"]) if snapshot
                      for source in snapshot["bundle"]["sources"]}
        papers = {source_id: {"source_id": source_id, "editorial": None, "published": None,
                              "rag": state["rag"].get(source_id), "audit": state["audit"].get(source_id)}
                  for source_id in source_ids}
        manifest = {"schema_version": 1, "project": state["project"], "papers": sorted(source_ids),
                    "editorial": _split_snapshot(state["editorial"], papers, "editorial"),
                    "published": _split_snapshot(state["published"], papers, "published")}
        for source_id, paper in papers.items():
            _write_private_json(stage / "papers", _paper_filename(source_id), paper)
        _write_private_json(stage, "manifest.json", manifest)
        if _read_papers(stage) != state:
            raise ValueError("Per-paper export cannot represent the current state exactly")
        stage.rename(root)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"directory": str(root), "papers": len(papers)}


def _join_snapshot(manifest, papers, name):
    if manifest is None:
        if any(paper[name] is not None for paper in papers.values()):
            raise ValueError("Unexpected paper snapshot")
        return None
    if not isinstance(manifest, dict) or set(manifest) != {"bundle", "authorization", "chunk_plans"}:
        raise ValueError("Invalid snapshot manifest")
    bundle = deepcopy(manifest["bundle"])
    sections = {section: {} for section in ("sources", "units", "tables")}
    grants, plans = {}, {}
    for source_id, paper in papers.items():
        item = paper[name]
        if item is None:
            continue
        if (not isinstance(item, dict)
                or set(item) != {"source", "units", "tables", "authorization", "chunk_plans"}
                or not isinstance(item["source"], dict) or item["source"].get("id") != source_id
                or not isinstance(item["units"], list) or not isinstance(item["tables"], list)
                or not isinstance(item["chunk_plans"], dict)):
            raise ValueError("Invalid per-paper snapshot")
        for section, values in (("sources", [item["source"]]), ("units", item["units"]),
                                ("tables", item["tables"])):
            for value in values:
                if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
                        or value["id"] in sections[section]
                        or (section != "sources" and
                            (not isinstance(value.get("citation"), dict)
                             or value["citation"].get("source_id") != source_id))):
                    raise ValueError("Invalid or duplicate per-paper evidence")
                sections[section][value["id"]] = value
        unit_ids = {unit["id"] for unit in item["units"]}
        if not set(item["chunk_plans"]) <= unit_ids or set(plans) & set(item["chunk_plans"]):
            raise ValueError("Invalid per-paper chunk plan identities")
        grants[source_id] = item["authorization"]
        plans.update(item["chunk_plans"])
    for section, values in sections.items():
        identifiers = bundle.get(section)
        if (not isinstance(identifiers, list) or not all(isinstance(value, str) for value in identifiers)
                or len(identifiers) != len(set(identifiers)) or set(identifiers) != set(values)):
            raise ValueError("Per-paper evidence does not match its manifest")
        bundle[section] = [values[identifier] for identifier in identifiers]
    for key, values in (("authorization", grants), ("chunk_plans", plans)):
        identifiers = manifest[key]
        if (not isinstance(identifiers, list) or not all(isinstance(value, str) for value in identifiers)
                or len(identifiers) != len(set(identifiers)) or set(identifiers) != set(values)):
            raise ValueError("Per-paper settings do not match their manifest")
    return {"bundle": bundle, "authorization": {key: grants[key] for key in manifest["authorization"]},
            "chunk_plans": {key: plans[key] for key in manifest["chunk_plans"]}}


def _read_papers(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Per-paper state must be a real directory")
    manifest = releases.read_json(releases.safe_file(directory, "manifest.json").read_bytes())
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "project", "papers", "editorial", "published"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or not isinstance(manifest["papers"], list)
            or not all(isinstance(value, str) for value in manifest["papers"])
            or len(manifest["papers"]) != len(set(manifest["papers"]))):
        raise ValueError("Invalid per-paper state manifest")
    folder = directory / "papers"
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Invalid per-paper state directory")
    expected = {_paper_filename(source_id) for source_id in manifest["papers"]}
    actual = {path.name for path in folder.iterdir()} if folder.exists() else set()
    if actual != expected:
        raise ValueError("Per-paper state files do not match the manifest")
    papers = {}
    for source_id in manifest["papers"]:
        paper = releases.read_json(releases.safe_file(folder, _paper_filename(source_id)).read_bytes())
        if (not isinstance(paper, dict)
                or set(paper) != {"source_id", "editorial", "published", "rag", "audit"}
                or paper["source_id"] != source_id):
            raise ValueError("Invalid per-paper state identity")
        papers[source_id] = paper
    return {"schema_version": 1, "project": manifest["project"],
            "editorial": _join_snapshot(manifest["editorial"], papers, "editorial"),
            "published": _join_snapshot(manifest["published"], papers, "published"),
            "rag": {key: paper["rag"] for key, paper in papers.items() if paper["rag"] is not None},
            "audit": {key: paper["audit"] for key, paper in papers.items() if paper["audit"] is not None}}


def _restore_snapshot(root, value, *, editorial):
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"bundle", "authorization", "chunk_plans"}:
        raise ValueError("Invalid exported snapshot")
    store = collections.CollectionStore(
        root / ("editorial" if editorial else "registry"), allowed_profiles=["private_context"],
        allowed_collections=["core"], defaults=["core"])
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "evidence"
        release_id = releases.export(value["bundle"], output, profile="private_context",
                                     authorization=value["authorization"], chunk_plans=value["chunk_plans"])
        store.releases.install(output)
    document = {"schema_version": 1, "collections": [{
        "id": "core", "title": project_library.load(root)["name"], "version": release_id[:16],
        "updated_at": date.today().isoformat(),
        "coverage": ("Restored private editorial drafts; not authorized as published RAG text."
                     if editorial else
                     "Restored explicitly published clear text; transcription approval is not scientific validation."),
        "release_id": release_id, "publications": {},
    }]}
    store.publish(document)
    snapshot = store.current().snapshots["core"]
    if (snapshot._bundle != value["bundle"] or snapshot._manifest["authorization"] != value["authorization"]
            or snapshot.chunk_plans != value["chunk_plans"]):
        raise ValueError("Restored snapshot does not match the exported state")


def _validate_rag(state, editorial_ids, published):
    rag = state["rag"]
    if not isinstance(rag, dict) or not set(rag) <= editorial_ids:
        raise ValueError("Invalid RAG source identities")
    published_ids = {source["id"] for source in published["bundle"]["sources"]} if published else set()
    for source_id, saved in rag.items():
        if not isinstance(saved, dict) or set(saved) != {"draft", "checks"}:
            raise ValueError("Invalid saved RAG state")
        draft, checks = saved["draft"], saved["checks"]
        if draft is not None:
            if (not isinstance(draft, dict) or not {"content_identity", "plans"} <= set(draft)
                    or not isinstance(draft["content_identity"], str) or not isinstance(draft["plans"], dict)
                    or not isinstance(draft.get("unassigned_terms", []), list)
                    or (draft.get("term_anchors") is not None and not isinstance(draft["term_anchors"], dict))):
                raise ValueError("Invalid saved RAG draft")
            labels = any(isinstance(plan, dict) and plan.get("labels") for plan in draft["plans"].values())
            identity = (project_text.content_identity(published["bundle"], source_id)
                        if source_id in published_ids else None)
            if labels and draft.get("term_anchors") is None:
                raise ValueError("Older RAG draft requires term anchors before it can be restored safely")
            for plan in draft["plans"].values():
                if not isinstance(plan, dict) or not plan.get("labels"):
                    continue
                if not isinstance(plan["labels"], dict):
                    raise ValueError("Invalid saved RAG labels")
                for key in plan["labels"]:
                    anchor = draft["term_anchors"].get(key)
                    if (not isinstance(anchor, dict) or set(anchor) != {"digest", "excerpt", "page"}
                            or not isinstance(anchor["excerpt"], str)
                            or type(anchor["page"]) is not int or anchor["page"] < 1):
                        raise ValueError("Saved RAG term anchor is missing or invalid")
                    collections.digest(anchor["digest"])
            if draft["content_identity"] == identity:
                source_bundle = project_text.source_bundle(published["bundle"], source_id)
                releases.chunk_records(source_bundle, draft["plans"])
        if not isinstance(checks, list) or len(checks) > project_chunks.CHECK_LIMIT:
            raise ValueError("Invalid saved search checks")
        seen = set()
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {"id", "query", "page", "excerpt"}
                    or not isinstance(check["query"], str) or not isinstance(check["excerpt"], str)
                    or type(check["page"]) is not int or check["page"] < 1
                    or not 8 <= len(check["excerpt"]) <= 500):
                raise ValueError("Invalid saved search check")
            expected = releases.sha256(releases.encode(
                {"query": check["query"], "page": check["page"], "excerpt": check["excerpt"]}))
            if check["id"] != expected or expected in seen:
                raise ValueError("Invalid or duplicate saved search check identity")
            seen.add(expected)


def restore(source, destination):
    """Rebuild current evidence in a new project; never overwrite existing data."""
    source, root = Path(source), Path(destination).absolute()
    if source.is_symlink():
        raise ValueError("Current-state export cannot be a symlink")
    if source.is_dir():
        state = _read_papers(source)
    elif source.is_file():
        state = releases.read_json(source.read_bytes())
    else:
        raise ValueError("Current-state export must be a file or per-paper directory")
    return restore_state(state, destination)


def restore_state(state, destination):
    """Rebuild validated state in a new project; never overwrite existing data."""
    root = Path(destination).absolute()
    if (not isinstance(state, dict)
            or set(state) != {"schema_version", "project", "editorial", "published", "rag", "audit"}
            or type(state["schema_version"]) is not int or state["schema_version"] != 1
            or not isinstance(state["project"], dict) or not isinstance(state["audit"], dict)):
        raise ValueError("Invalid current-state export")
    if state["editorial"] is None and state["published"] is not None:
        raise ValueError("Published state requires separate editorial state")
    with literature_update.writer_lock(root):
        if root.exists() or root.is_symlink():
            raise ValueError("Restore destination already exists")
        stage = Path(tempfile.mkdtemp(prefix="." + root.name + "-restore-", dir=root.parent))
        try:
            collections.atomic_json(stage, "project.json", state["project"])
            project = project_library.load(stage)
            project_library.validate_current_project(project)
            _restore_snapshot(stage, state["editorial"], editorial=True)
            _restore_snapshot(stage, state["published"], editorial=False)
            editorial = state["editorial"]
            published = state["published"]
            editorial_ids = {source["id"] for source in editorial["bundle"]["sources"]} if editorial else set()
            published_ids = {source["id"] for source in published["bundle"]["sources"]} if published else set()
            if not published_ids <= editorial_ids:
                raise ValueError("Published sources must also be present in editorial state")
            _validate_rag(state, editorial_ids, published)
            for source_id, saved in state["rag"].items():
                if saved["draft"] is not None:
                    collections.atomic_json(stage / "chunk-drafts", project_chunks.draft_name(source_id),
                                            saved["draft"])
                if saved["checks"]:
                    collections.atomic_json(stage / "chunk-checks", project_chunks.draft_name(source_id),
                                            saved["checks"])
            if not set(state["audit"]) <= editorial_ids:
                raise ValueError("Invalid audit source identities")
            for source_id, events in state["audit"].items():
                if not isinstance(events, dict) or not set(events) <= {"draft", "publication", "unpublication"}:
                    raise ValueError("Invalid saved audit events")
                for kind, event in events.items():
                    if (not isinstance(event, dict) or event.get("source_id") != source_id
                            or not isinstance(event.get("at"), str)
                            or (kind in {"publication", "unpublication"} and event.get("scope") != kind)
                            or (kind == "draft" and event.get("scope") in {"publication", "unpublication"})):
                        raise ValueError("Invalid saved audit event")
                    collections.atomic_json(stage / "corrections",
                                            releases.sha256(releases.encode(event)) + ".json", event)
            stage.rename(root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"project": str(root), "editorial": len(editorial_ids), "published": len(published_ids),
            "pdfs_restored": False, "discovery_restored": False}
