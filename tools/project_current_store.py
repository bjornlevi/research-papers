"""Atomic, per-paper current-state storage for private project evidence."""

from copy import deepcopy
from contextlib import ExitStack
from collections import OrderedDict
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

from research_library import collections, releases
from . import literature_update, project_chunks, project_edit, project_library, project_state, project_text

_MARKDOWN_TRANSACTIONS = threading.local()
_VALIDATED_PAPERS = OrderedDict()
_VALIDATED_PAPERS_LOCK = threading.Lock()
_VALIDATED_PAPERS_LIMIT = 2048


def _snapshot_header(snapshot):
    if snapshot is None:
        return None
    bundle = snapshot["bundle"]
    if any(bundle[section] for section in ("claims", "issues", "relationships")):
        raise ValueError("Per-paper storage does not yet support curated cross-paper dependencies")
    return {"schema_version": bundle["schema_version"], "caveats": bundle["caveats"]}


def _paper_snapshot(snapshot, source_id):
    if snapshot is None:
        return None
    bundle = snapshot["bundle"]
    source = next((item for item in bundle["sources"] if item["id"] == source_id), None)
    if source is None:
        return None
    units = [item for item in bundle["units"] if item["citation"]["source_id"] == source_id]
    ids = {item["id"] for item in units}
    return {
        "source": source, "units": units,
        "tables": [item for item in bundle["tables"] if item["citation"]["source_id"] == source_id],
        "authorization": snapshot["authorization"][source_id],
        "chunk_plans": {key: value for key, value in snapshot["chunk_plans"].items() if key in ids},
    }


def _bundle(header, papers, section):
    if header is None:
        if any(paper[section] is not None for paper in papers.values()):
            raise ValueError("Paper evidence has no corresponding project state")
        return None
    if not isinstance(header, dict) or set(header) != {"schema_version", "caveats"}:
        raise ValueError("Invalid current-state snapshot header")
    bundle = {"schema_version": header["schema_version"], "caveats": header["caveats"],
              **{name: [] for name in releases.SECTIONS}}
    authorization, plans = {}, {}
    for source_id, paper in papers.items():
        value = paper[section]
        if value is None:
            continue
        bundle["sources"].append(value["source"])
        bundle["units"].extend(value["units"])
        bundle["tables"].extend(value["tables"])
        authorization[source_id] = value["authorization"]
        if plans.keys() & value["chunk_plans"].keys():
            raise ValueError("Duplicate retrieval plan identity")
        plans.update(value["chunk_plans"])
    for name in ("sources", "units", "tables"):
        bundle[name].sort(key=lambda value: value["id"])
    return {"bundle": bundle, "authorization": authorization, "chunk_plans": plans}


def _validate_paper(paper, headers):
    if (not isinstance(paper, dict)
            or set(paper) != {"source_id", "editorial", "published", "rag", "audit"}
            or not isinstance(paper["source_id"], str)):
        raise ValueError("Invalid current paper record")
    source_id = paper["source_id"]
    if paper["editorial"] is None:
        raise ValueError("Current paper requires an editorial draft")
    for section in ("editorial", "published"):
        value = paper[section]
        if value is None:
            continue
        if (headers[section] is None or not isinstance(value, dict)
                or set(value) != {"source", "units", "tables", "authorization", "chunk_plans"}
                or not isinstance(value["source"], dict) or value["source"].get("id") != source_id
                or not isinstance(value["units"], list) or not isinstance(value["tables"], list)
                or not isinstance(value["chunk_plans"], dict)):
            raise ValueError("Invalid current paper evidence")
        bundle = {"schema_version": headers[section]["schema_version"],
                  "caveats": headers[section]["caveats"],
                  **{name: [] for name in releases.SECTIONS}}
        bundle.update(sources=[value["source"]], units=value["units"], tables=value["tables"])
        releases.validate_bundle(bundle)
        if any(item["citation"]["source_id"] != source_id for item in value["units"] + value["tables"]):
            raise ValueError("Paper evidence refers to another source")
        grant = value["authorization"]
        if (not isinstance(grant, dict) or set(grant) != {"scopes", "basis"}
                or not isinstance(grant["scopes"], list)
                or not all(isinstance(scope, str) for scope in grant["scopes"])
                or not {"curated", "context"} <= set(grant["scopes"])
                or not isinstance(grant["basis"], str) or not grant["basis"].strip()):
            raise releases.AccessDeniedError("Invalid source authorization")
        if (value["source"]["omission"] != "original_not_exported" or "asset_id" in value["source"]
                or any(item["citation"]["availability"] != "metadata_only"
                       for item in value["units"] + value["tables"])):
            raise ValueError("Invalid private paper availability")
        releases.chunk_records(bundle, value["chunk_plans"])
    if (paper["published"] is not None
            and paper["published"]["source"]["sha256"] != paper["editorial"]["source"]["sha256"]):
        raise ValueError("Published paper and draft refer to different originals")
    published = _bundle(headers["published"], {source_id: paper}, "published")
    state = {"rag": {source_id: paper["rag"]} if paper["rag"] is not None else {}}
    project_state._validate_rag(state, {source_id}, published)
    if paper["audit"] is not None:
        if (not isinstance(paper["audit"], dict)
                or not set(paper["audit"]) <= {"draft", "publication", "unpublication"}
                or any(not isinstance(event, dict) or event.get("source_id") != source_id
                       or not isinstance(event.get("at"), str)
                       or (kind in {"publication", "unpublication"} and event.get("scope") != kind)
                       or (kind == "draft" and event.get("scope") in {"publication", "unpublication"})
                       for kind, event in paper["audit"].items())):
            raise ValueError("Invalid current paper audit")


def initialize(state, destination):
    """Create a new store from validated current state without changing the source project."""
    root = Path(destination).absolute()
    with literature_update.writer_lock(root):
        if root.exists() or root.is_symlink():
            raise ValueError("Current paper store already exists")
        headers = {name: _snapshot_header(state[name]) for name in ("editorial", "published")}
        source_ids = {source["id"] for name in ("editorial", "published") if state[name]
                      for source in state[name]["bundle"]["sources"]}
        papers = {source_id: {
            "source_id": source_id,
            "editorial": _paper_snapshot(state["editorial"], source_id),
            "published": _paper_snapshot(state["published"], source_id),
            "rag": state["rag"].get(source_id), "audit": state["audit"].get(source_id),
        } for source_id in source_ids}
        manifest = {"schema_version": 1, "project": state["project"], "headers": headers,
                    "created_at": datetime.now(timezone.utc).date().isoformat(),
                    "papers": sorted(source_ids)}
        stage = Path(tempfile.mkdtemp(prefix="." + root.name + "-current-", dir=root.parent))
        try:
            project_state._write_private_json(stage, "manifest.json", manifest)
            for source_id, paper in papers.items():
                project_state._write_private_json(stage / "papers", project_state._paper_filename(source_id), paper)
            if _read(stage) != state:
                raise ValueError("Current paper store does not reproduce the source state")
            stage.rename(root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"directory": str(root), "papers": len(papers)}


def initialize_empty(destination, *, name, authorization, rows=None):
    """Create initialized empty draft and publication states without release snapshots."""
    project = {
        "schema_version": 1, "name": name.strip() if isinstance(name, str) else name,
        "authorization": authorization.strip() if isinstance(authorization, str) else authorization,
        "papers": project_library.validated_papers(rows)[0] if rows is not None else {},
        "git_markdown_tracking": False,
    }
    project_library.validate_current_project(project)
    empty = {"bundle": project_text.empty_bundle(), "authorization": {}, "chunk_plans": {}}
    return initialize({"schema_version": 1, "project": project, "editorial": deepcopy(empty),
                       "published": deepcopy(empty), "rag": {}, "audit": {}}, destination)


def initialize_from_preserved(store, project_root, destination, *, name, authorization, rows):
    """Create a new current store with selected preserved drafts, never published text."""
    store, project_root, destination = (Path(path).absolute() for path in (store, project_root, destination))
    if (store.resolve() != store or project_root.resolve() != project_root
            or destination.parent.resolve() != destination.parent):
        raise ValueError("Preserved and destination paths cannot contain symlinks")
    paths = (store, project_root, destination)
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise ValueError("Preserved store, PDF project and destination must be separate")
    project = {
        "schema_version": 1, "name": name.strip() if isinstance(name, str) else name,
        "authorization": authorization.strip() if isinstance(authorization, str) else authorization,
        "papers": project_library.validated_papers(rows)[0], "git_markdown_tracking": False,
    }
    project_library.validate_current_project(project)
    with ExitStack() as locks:
        for root in sorted((store, project_root)):
            locks.enter_context(literature_update.writer_lock(root))
        if destination.exists() or destination.is_symlink():
            raise ValueError("Current paper store already exists")
        original = _read(store)
        if original["editorial"] is None or original["published"] is None:
            raise ValueError("Preserved evidence is not initialized")
        sources = original["editorial"]["bundle"]["sources"]
        selected = [source for source in sources
                    if collections.publication(source["doi"]).removeprefix("doi:") in project["papers"]]
        if len({collections.publication(source["doi"]) for source in selected}) != len(selected):
            raise ValueError("Multiple preserved sources match one DOI")
        paper_root = project_root / "papers"
        if paper_root.is_symlink() or paper_root.resolve() != paper_root:
            raise ValueError("Preserved PDF directory cannot contain symlinks")

        def verify_pdfs():
            for source in selected:
                if source["format"] != "pdf":
                    raise ValueError("Preserved source is not a PDF")
                path = releases.safe_file(paper_root, source["sha256"] + ".pdf")
                if releases.sha256(path.read_bytes()) != source["sha256"]:
                    raise ValueError("Preserved PDF checksum mismatch; draft was not restored")

        verify_pdfs()
        ids = {source["id"] for source in selected}
        editorial = deepcopy(original["editorial"])
        for section in ("sources", "units", "tables"):
            editorial["bundle"][section] = [item for item in editorial["bundle"][section]
                                            if (item["id"] if section == "sources"
                                                else item["citation"]["source_id"]) in ids]
        editorial["authorization"] = {key: value for key, value in editorial["authorization"].items() if key in ids}
        unit_ids = {unit["id"] for unit in editorial["bundle"]["units"]}
        editorial["chunk_plans"] = {key: value for key, value in editorial["chunk_plans"].items() if key in unit_ids}
        empty_published = deepcopy(original["published"])
        for section in releases.SECTIONS:
            empty_published["bundle"][section] = []
        empty_published["authorization"], empty_published["chunk_plans"] = {}, {}
        audit = {key: {"draft": value["draft"]} for key, value in original["audit"].items()
                 if key in ids and "draft" in value}
        state = {"schema_version": 1, "project": project, "editorial": editorial,
                 "published": empty_published, "rag": {}, "audit": audit}
        stage = Path(tempfile.mkdtemp(prefix="." + destination.name + "-preserved-", dir=destination.parent))
        try:
            candidate = stage / "store"
            initialize(state, candidate)
            if _read(store) != original:
                raise ValueError("Preserved store changed during draft reuse")
            verify_pdfs()
            if destination.exists() or destination.is_symlink():
                raise ValueError("Current paper store already exists")
            candidate.rename(destination)
        finally:
            shutil.rmtree(stage)
    return {"directory": str(destination), "restored_drafts": len(selected),
            "published": 0, "pdf_project": str(project_root)}


def _quality_files(root):
    """Validate and capture every quality record without following symlinks."""
    from research_library import quality
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError("Quality directory cannot contain symlinked paths")
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ValueError("Invalid quality directory")
    files = {}
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or folder.name not in {"inventory", "reviews", "problems"}:
            raise ValueError("Unexpected entry in quality directory")
        for path in folder.iterdir():
            if (path.is_symlink() or not path.is_file()
                    or not re.fullmatch(r"[0-9a-f]{64}\.json", path.name)):
                raise ValueError("Invalid quality record path")
            path = releases.safe_file(folder, path.name)
            if path.stat().st_size > releases.MAX_PAYLOAD_BYTES:
                raise ValueError("Quality record exceeds size limit")
            data = path.read_bytes()
            if len(data) > releases.MAX_PAYLOAD_BYTES:
                raise ValueError("Quality record exceeds size limit")
            record = releases.read_json(data)
            if folder.name == "inventory":
                if not isinstance(record, dict) or not all(
                        isinstance(record.get(key), str) for key in ("source_id", "source_sha256", "format")):
                    raise ValueError("Invalid quality inventory")
                source = {"id": record["source_id"], "sha256": record["source_sha256"], "format": record["format"]}
                if path.stem != quality.source_key(source) or record["format"] not in {"pdf", "docx", "xlsx"}:
                    raise ValueError("Invalid quality inventory identity")
                quality._inventory(root, source)
            files[(folder.name, path.name)] = data
    for section in ("reviews", "problems"):
        quality._events(root, section)
    return files


def _write_quality_files(root, files):
    for (section, name), data in files.items():
        folder = Path(root) / section
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (folder / name).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    if _quality_files(root) != files:
        raise ValueError("Copied quality records failed verification")


def prepare(project_root, destination, *, without_git_markdown=False, quality_root=None,
            confirm_quality_writers_stopped=False):
    """Create a separate experimental store and verify its recovery before exposing it."""
    project_root = Path(project_root).absolute()
    destination = Path(destination).absolute()
    if (project_root.resolve() != project_root or destination.parent.resolve() != destination.parent
            or destination.is_relative_to(project_root) or project_root.is_relative_to(destination)):
        raise ValueError("Current store must be outside the project with no symlinked parent path")
    quality_root = Path(quality_root).absolute() if quality_root else project_root / "releases/data-quality"
    if quality_root == destination or quality_root.is_relative_to(destination):
        raise ValueError("Quality source must be outside the new store")
    with literature_update.writer_lock(project_root):
        state = project_state.current(project_root)
        if state["editorial"] is None or state["published"] is None:
            raise ValueError("Attach a paper and initialize editorial and published evidence before preparation")
        if without_git_markdown:
            state["project"]["git_markdown_tracking"] = False
        with literature_update.writer_lock(destination):
            if destination.exists() or destination.is_symlink():
                raise ValueError("Current paper store already exists")
            stage = Path(tempfile.mkdtemp(prefix="." + destination.name + "-prepare-", dir=destination.parent))
            try:
                candidate = stage / "store"
                result = initialize(state, candidate)
                quality_files = _quality_files(quality_root)
                if quality_files and not confirm_quality_writers_stopped:
                    raise ValueError("Stop all quality record writers and confirm before copying quality records")
                _write_quality_files(candidate / "quality", quality_files)
                restored = stage / "restored"
                project_state.restore_state(read(candidate), restored)
                if project_state.current(restored) != state:
                    raise ValueError("Current store recovery verification failed")
                if _quality_files(quality_root) != quality_files:
                    raise ValueError("Quality records changed during preparation; retry with writers stopped")
                if destination.exists() or destination.is_symlink():
                    raise ValueError("Current paper store already exists")
                candidate.rename(destination)
            finally:
                shutil.rmtree(stage)
    return {"directory": str(destination), "papers": result["papers"], "recovery_verified": True,
            "connected_to_project": False, "quality_records_copied": len(quality_files),
            "git_markdown_tracking": state["project"].get("git_markdown_tracking", False)}


def restore_project(store, destination):
    """Recover a separate project from current per-paper state without changing the store."""
    store, root = Path(store).absolute(), Path(destination).absolute()
    if (store.resolve() != store or root.parent.resolve() != root.parent
            or root.is_relative_to(store) or store.is_relative_to(root)):
        raise ValueError("Restore destination must be separate and have no symlinked paths")
    with literature_update.reader_lock(store), literature_update.writer_lock(root):
        if root.exists() or root.is_symlink():
            raise ValueError("Restore destination already exists")
        state = _read(store)
        quality_files = _quality_files(store / "quality")
        stage = Path(tempfile.mkdtemp(prefix="." + root.name + "-current-restore-", dir=root.parent))
        try:
            candidate = stage / "project"
            result = project_state.restore_state(state, candidate)
            _write_quality_files(candidate / "releases/data-quality", quality_files)
            if project_state.current(candidate) != state:
                raise ValueError("Restored current state failed verification")
            if _quality_files(store / "quality") != quality_files:
                raise ValueError("Quality records changed during recovery; retry with writers stopped")
            if root.exists() or root.is_symlink():
                raise ValueError("Restore destination already exists")
            candidate.rename(root)
        finally:
            shutil.rmtree(stage)
    return {**result, "project": str(root), "quality_records_restored": len(quality_files)}


def sync_git_markdown(root, repository, *, source_id=None, revision=None):
    """Rebuild authorized Git Markdown views from one consistent current store."""
    root = Path(root).absolute()
    if (source_id is None) != (revision is None):
        raise ValueError("A paper Markdown refresh requires both source ID and revision")
    with literature_update.writer_lock(root):
        state = _read(root)
        if not state["project"].get("git_markdown_tracking", False):
            raise ValueError("Git Markdown tracking is not enabled for this current store")
        if source_id is not None:
            _, current = read_paper(root, source_id)
            if current != revision:
                raise ValueError("Paper changed; reload before refreshing Markdown")
        versions = {}
        for section in ("editorial", "published"):
            snapshot = state[section]
            versions[section] = ({"bundle": snapshot["bundle"],
                                  "revision": releases.sha256(releases.encode(snapshot))}
                                 if snapshot is not None else None)
        count = project_text.sync_git_markdown_views(
            repository, editorial=versions["editorial"], published=versions["published"], source_id=source_id)
        return {"exported": count}


def _manifest(root, *, allow_pending=False):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Current paper store must be a real directory")
    pending = root / "membership-transaction.json"
    if not allow_pending and (pending.exists() or pending.is_symlink()):
        raise ValueError("Interrupted paper membership update; recover the current store before reading")
    pending = root / "markdown-transaction.json"
    if ((pending.exists() or pending.is_symlink())
            and root not in getattr(_MARKDOWN_TRANSACTIONS, "held", set())):
        raise ValueError("Interrupted Git Markdown update; recover the current store before reading")
    manifest = releases.read_json(releases.safe_file(root, "manifest.json").read_bytes())
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "project", "headers", "papers", "created_at"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or not isinstance(manifest["created_at"], str)
            or not isinstance(manifest["headers"], dict)
            or set(manifest["headers"]) != {"editorial", "published"}
            or not isinstance(manifest["papers"], list)
            or not all(isinstance(item, str) for item in manifest["papers"])
            or len(set(manifest["papers"])) != len(manifest["papers"])):
        raise ValueError("Invalid current paper store manifest")
    try:
        if date.fromisoformat(manifest["created_at"]).isoformat() != manifest["created_at"]:
            raise ValueError("Noncanonical date")
    except ValueError as error:
        raise ValueError("Invalid current paper store creation date") from error
    project_library.validate_current_project(manifest["project"])
    return manifest


def membership_revision(root):
    """Return a revision for changes to the store's paper membership or project metadata."""
    root = Path(root).absolute()
    with literature_update.reader_lock(root):
        manifest = _manifest(root)
        _read_papers(root, manifest, {})
        return releases.sha256(releases.encode(manifest))


def project_view(root):
    """Read project settings and their membership revision together."""
    root = Path(root).absolute()
    with literature_update.reader_lock(root):
        manifest = _manifest(root)
        _read_papers(root, manifest, {})
        return deepcopy(manifest["project"]), releases.sha256(releases.encode(manifest))


def register_core_papers(root, rows, *, expected):
    """Register Core papers without changing attached evidence or retrieval."""
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        manifest = _manifest(root)
        if releases.sha256(releases.encode(manifest)) != expected:
            raise ValueError("Project settings changed; reload before adding Core papers")
        _read_papers(root, manifest, {})
        project = deepcopy(manifest["project"])
        project["papers"], added = project_library.validated_papers(rows, project["papers"])
        result = update_project(root, project=project, expected=expected)
        return {"added": added, "revision": result["revision"]}


def update_project(root, *, project, expected):
    """Replace project metadata without modifying paper, publication, or RAG records."""
    project_library.validate_current_project(project)
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        manifest = _manifest(root)
        if releases.sha256(releases.encode(manifest)) != expected:
            raise ValueError("Project settings changed; reload before saving")
        _read_papers(root, manifest, {})
        if project.get("git_markdown_tracking", False) != manifest["project"].get("git_markdown_tracking", False):
            raise ValueError("Git Markdown tracking requires its explicit authorization and export workflow")
        updated = deepcopy(manifest)
        updated["project"] = deepcopy(project)
        collections.atomic_json(root, "manifest.json", updated)
        return {"revision": releases.sha256(releases.encode(updated))}


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_membership(root):
    path = root / "membership-transaction.json"
    if not path.exists() and not path.is_symlink():
        return False
    transaction = releases.read_json(releases.safe_file(root, path.name).read_bytes())
    if (not isinstance(transaction, dict)
            or set(transaction) != {"operation", "source_id", "before", "after", "paper_sha256"}
            or transaction["operation"] not in {"add", "remove"}
            or not isinstance(transaction["source_id"], str)):
        raise ValueError("Invalid paper membership transaction")
    for key in ("before", "after", "paper_sha256"):
        collections.digest(transaction[key])
    manifest = _manifest(root, allow_pending=True)
    revision = releases.sha256(releases.encode(manifest))
    if revision not in {transaction["before"], transaction["after"]}:
        raise ValueError("Paper membership transaction does not match the manifest")
    source_id = transaction["source_id"]
    committed = revision == transaction["after"]
    should_exist = (transaction["operation"] == "add") == committed
    if (source_id in manifest["papers"]) != should_exist:
        raise ValueError("Paper membership transaction disagrees with the manifest")
    folder = root / "papers"
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Invalid current paper directory")
    name = project_state._paper_filename(source_id)
    target = folder / name
    exists = target.exists() or target.is_symlink()
    if exists:
        data = releases.safe_file(folder, name).read_bytes()
        if releases.sha256(data) != transaction["paper_sha256"]:
            raise ValueError("Paper changed during membership recovery")
    elif should_exist:
        raise ValueError("Paper missing during membership recovery")
    expected = {project_state._paper_filename(item) for item in manifest["papers"]}
    actual = {item.name for item in folder.iterdir()} if folder.exists() else set()
    if actual != expected | ({name} if exists and not should_exist else set()):
        raise ValueError("Current paper files do not match membership transaction")
    if exists and not should_exist:
        target.unlink()
        _sync_directory(folder)
    _read_papers(root, manifest, {})
    path.unlink()
    _sync_directory(root)
    return True


def recover_membership(root):
    """Complete or roll back an interrupted paper membership change."""
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        return _recover_membership(root)


def add_paper(root, *, editorial, expected):
    """Add already-extracted editorial evidence without publishing or selecting it as Core."""
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        manifest = _manifest(root)
        if (manifest["project"].get("git_markdown_tracking", False)
                and root not in getattr(_MARKDOWN_TRANSACTIONS, "held", set())):
            raise ValueError("Adding papers with Git Markdown tracking is not yet supported")
        if releases.sha256(releases.encode(manifest)) != expected:
            raise ValueError("Paper membership changed; reload before attaching")
        existing = _read_papers(root, manifest, {})
        if not isinstance(editorial, dict) or not isinstance(editorial.get("source"), dict):
            raise ValueError("Invalid attached paper evidence")
        source_id = editorial["source"].get("id")
        if not isinstance(source_id, str) or source_id in existing:
            raise ValueError("Paper is already attached or has an invalid source ID")
        paper = {"source_id": source_id, "editorial": deepcopy(editorial), "published": None,
                 "rag": None, "audit": None}
        _validate_paper(paper, manifest["headers"])
        source = paper["editorial"]["source"]
        for _, other in existing.values():
            record = other["editorial"]["source"]
            if collections.publication(record["doi"]) == collections.publication(source["doi"]):
                raise ValueError("This DOI already has an attached source")
            if record["sha256"] == source["sha256"]:
                raise ValueError("This PDF is already associated with another paper")
        papers = {key: value[1] for key, value in existing.items()} | {source_id: paper}
        releases.validate_bundle(_bundle(manifest["headers"]["editorial"], papers, "editorial")["bundle"])
        updated = deepcopy(manifest)
        updated["papers"] = sorted([*updated["papers"], source_id])
        transaction = {"operation": "add", "source_id": source_id, "before": expected,
                       "after": releases.sha256(releases.encode(updated)),
                       "paper_sha256": releases.sha256(releases.encode(paper))}
        collections.atomic_json(root, "membership-transaction.json", transaction)
        collections.atomic_json(root / "papers", project_state._paper_filename(source_id), paper)
        collections.atomic_json(root, "manifest.json", updated)
        _recover_membership(root)
        return {"source_id": source_id, "revision": transaction["paper_sha256"],
                "membership_revision": transaction["after"]}


def remove_paper(root, *, source_id, revision, expected, confirmed=False):
    """Remove a paper record only; this does not purge PDFs, discovery, or project metadata."""
    if not confirmed:
        raise ValueError("Confirm removal of this paper record")
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        manifest = _manifest(root)
        if manifest["project"].get("git_markdown_tracking", False):
            raise ValueError("Removing papers with Git Markdown tracking is not yet supported")
        if releases.sha256(releases.encode(manifest)) != expected:
            raise ValueError("Paper membership changed; reload before removal")
        existing = _read_papers(root, manifest, {})
        if source_id not in existing:
            raise ValueError("Paper is not registered in the current store")
        digest, paper = existing[source_id]
        if releases.sha256(releases.encode(paper)) != revision:
            raise ValueError("Paper changed; reload before removal")
        updated = deepcopy(manifest)
        updated["papers"].remove(source_id)
        transaction = {"operation": "remove", "source_id": source_id, "before": expected,
                       "after": releases.sha256(releases.encode(updated)), "paper_sha256": digest}
        collections.atomic_json(root, "membership-transaction.json", transaction)
        collections.atomic_json(root, "manifest.json", updated)
        _recover_membership(root)
        return {"source_id": source_id, "membership_revision": transaction["after"]}


def read(root):
    """Read current records; returned data is detached from the store."""
    root = Path(root).absolute()
    with literature_update.reader_lock(root):
        return _read(root)


def read_editor_state(root):
    """Capture a consistent detached editor state and the revisions of its paper records."""
    root = Path(root).absolute()
    with literature_update.reader_lock(root):
        manifest = _manifest(root)
        records = {key: value[1] for key, value in _read_papers(root, manifest, {}).items()}
        state = {"schema_version": 1, "project": manifest["project"],
                 "editorial": _bundle(manifest["headers"]["editorial"], records, "editorial"),
                 "published": _bundle(manifest["headers"]["published"], records, "published"),
                 "rag": {key: value["rag"] for key, value in records.items() if value["rag"] is not None},
                 "audit": {key: value["audit"] for key, value in records.items() if value["audit"] is not None}}
        revisions = {key: releases.sha256(releases.encode(value)) for key, value in records.items()}
        return deepcopy(state), revisions


def published_snapshot(root):
    """Build a detached retrieval view from current published papers, without retaining a release."""
    return _published_snapshot(read(root))


def editorial_collection(root):
    """Build a pinned operator view of current drafts and their paper revisions."""
    with literature_update.reader_lock(root):
        state, revisions = read_editor_state(root)
        created_at = _manifest(Path(root).absolute())["created_at"]
    evidence = _published_snapshot({"published": state["editorial"]})
    entry = {
        "id": "core", "title": state["project"]["name"],
        "version": "draft-" + evidence.release_id[:16], "updated_at": created_at,
        "coverage": "Current private editorial drafts; not published evidence.",
        "release_id": evidence.release_id, "publications": {},
    }
    document = {"schema_version": 1, "collections": [entry]}
    return collections.CollectionSnapshot(document, {"core": evidence}, {"core"}, ["core"]), revisions


def _published_snapshot(state):
    published = state["published"]
    if published is None:
        raise ValueError("Published evidence store is not initialized")
    bundle = published["bundle"]
    plans = published["chunk_plans"]
    releases.validate_bundle(bundle)
    context = {}
    records = releases.chunk_records(bundle, plans, document_context=context)
    with tempfile.TemporaryDirectory(prefix="current-evidence-index-") as temporary:
        index = releases._index(Path(temporary) / "index.sqlite", bundle, records,
                                labels=releases._editorial_labels(context))
    payloads = {"evidence/index.sqlite": index, **releases.chunk_payloads(plans)}
    manifest = {
        "release_id": releases.sha256(releases.encode(published)),
        "profile": "private_context", "assets": {}, "authorization": published["authorization"],
    }
    return releases.Snapshot(manifest, bundle, payloads)


class CurrentCollectionStore(collections.CollectionEndpoint):
    """Read-only current-state endpoint; old registry IDs are not retained."""

    def __init__(self, root, *, allowed_profiles, allow_metadata=False):
        self.root = Path(root).absolute()
        if set(allowed_profiles) != {"private_context"}:
            raise releases.AccessDeniedError("Current project evidence requires the private_context profile")
        if type(allow_metadata) is not bool:
            raise ValueError("Current metadata authorization must be a boolean")
        self.allow_metadata = allow_metadata
        self._lock = threading.Lock()
        self._manifest = None
        self._papers = {}
        self._snapshot = None

    def current(self):
        try:
            with self._lock:
                with literature_update.reader_lock(self.root):
                    manifest = _manifest(self.root)
                    cache = (self._papers if self._manifest is not None
                             and self._manifest["headers"] == manifest["headers"] else {})
                    papers = _read_papers(self.root, manifest, cache)
                    metadata = project_library.current_metadata(self.root) if self.allow_metadata else None
                project = manifest["project"]
                same_published = (
                    self._snapshot is not None
                    and self._manifest["headers"]["published"] == manifest["headers"]["published"]
                    and self._papers.keys() == papers.keys()
                    and all(self._papers[key][1]["published"] == value[1]["published"]
                            for key, value in papers.items())
                )
                evidence = (self._snapshot.snapshots["core"] if same_published else
                            _published_snapshot({"published": _bundle(
                                manifest["headers"]["published"],
                                {key: value[1] for key, value in papers.items()}, "published")}))
                document = {"schema_version": 1, "collections": [{
                    "id": "core", "title": project["name"],
                    "version": "current-" + evidence.release_id[:16], "updated_at": manifest["created_at"],
                    "coverage": "Current published project evidence; date indicates current-store initialization.",
                    "release_id": evidence.release_id, "publications": {},
                }]}
                snapshots = {"core": evidence}
                if metadata:
                    entry, metadata_snapshot = metadata
                    document["collections"].append(entry)
                    snapshots["metadata"] = metadata_snapshot
                collections.CollectionStore.validate_document(document)
                result = (self._snapshot if same_published and self._snapshot.document == document else
                          collections.CollectionSnapshot(document, snapshots, set(snapshots), ["core"]))
                self._manifest, self._papers, self._snapshot = manifest, papers, result
                return result
        except ValueError as error:
            raise releases.ReleaseError("Invalid or unavailable current project state") from error

    def load(self, snapshot_id):
        collections.digest(snapshot_id)
        current = self.current()
        if current.release_id != snapshot_id:
            raise collections.SnapshotUnavailable("Requested current-state snapshot is no longer available")
        return current


def _read_papers(root, manifest, cache):
    folder = root / "papers"
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Invalid current paper directory")
    expected = {project_state._paper_filename(source_id) for source_id in manifest["papers"]}
    actual = {item.name for item in folder.iterdir()} if folder.exists() else set()
    if expected != actual:
        raise ValueError("Current paper files do not match the manifest")
    papers = {}
    headers_digest = releases.sha256(releases.encode(manifest["headers"]))
    for source_id in manifest["papers"]:
        data = releases.safe_file(folder, project_state._paper_filename(source_id)).read_bytes()
        digest = releases.sha256(data)
        previous = cache.get(source_id)
        if previous is not None and previous[0] == digest:
            value = previous[1]
        else:
            value = releases.read_json(data)
            if not isinstance(value, dict) or value.get("source_id") != source_id:
                raise ValueError("Current paper identity mismatch")
            key = (source_id, headers_digest, digest)
            with _VALIDATED_PAPERS_LOCK:
                validated = key in _VALIDATED_PAPERS
                if validated:
                    _VALIDATED_PAPERS.move_to_end(key)
            if not validated:
                _validate_paper(value, manifest["headers"])
                with _VALIDATED_PAPERS_LOCK:
                    _VALIDATED_PAPERS[key] = None
                    _VALIDATED_PAPERS.move_to_end(key)
                    if len(_VALIDATED_PAPERS) > _VALIDATED_PAPERS_LIMIT:
                        _VALIDATED_PAPERS.popitem(last=False)
        papers[source_id] = (digest, value)
    return papers


def _read(root):
    manifest = _manifest(root)
    papers = {key: value[1] for key, value in _read_papers(root, manifest, {}).items()}
    state = {"schema_version": 1, "project": manifest["project"],
             "editorial": _bundle(manifest["headers"]["editorial"], papers, "editorial"),
             "published": _bundle(manifest["headers"]["published"], papers, "published"),
             "rag": {key: value["rag"] for key, value in papers.items() if value["rag"] is not None},
             "audit": {key: value["audit"] for key, value in papers.items() if value["audit"] is not None}}
    return deepcopy(state)


def replace(root, paper, *, expected):
    """Atomically replace one paper after checking its previously read revision."""
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        manifest = _manifest(root)
        if not isinstance(paper, dict) or paper.get("source_id") not in manifest["papers"]:
            raise ValueError("Paper is not registered in the current store")
        name = project_state._paper_filename(paper["source_id"])
        folder = root / "papers"
        existing = releases.read_json(releases.safe_file(folder, name).read_bytes())
        if not isinstance(existing, dict) or existing.get("source_id") != paper["source_id"]:
            raise ValueError("Current paper identity mismatch")
        if releases.sha256(releases.encode(existing)) != expected:
            raise ValueError("Paper changed; reload before saving")
        _validate_paper(paper, manifest["headers"])
        if (manifest["project"].get("git_markdown_tracking", False)
                and root not in getattr(_MARKDOWN_TRANSACTIONS, "held", set())):
            def markdown_content(value):
                return ({key: value[key] for key in ("source", "units", "tables")} if value is not None else None)
            if any(markdown_content(existing.get(section)) != markdown_content(paper.get(section))
                   for section in ("editorial", "published")):
                raise ValueError("Paper changes require the Git Markdown tracked edit workflow")
        collections.atomic_json(folder, name, paper)
        return releases.sha256(releases.encode(paper))


def read_paper(root, source_id):
    """Read one paper and a revision token for stale-write detection."""
    root = Path(root).absolute()
    with literature_update.reader_lock(root):
        manifest = _manifest(root)
        if source_id not in manifest["papers"]:
            raise ValueError("Paper is not registered in the current store")
        path = releases.safe_file(root / "papers", project_state._paper_filename(source_id))
        paper = releases.read_json(path.read_bytes())
        if not isinstance(paper, dict) or paper.get("source_id") != source_id:
            raise ValueError("Current paper identity mismatch")
        _validate_paper(paper, manifest["headers"])
        return deepcopy(paper), releases.sha256(releases.encode(paper))


def _markdown_transaction(root, repository, *, operation=None, arguments=None):
    root = Path(root).absolute()
    repository = Path(repository).resolve()
    journal = root / "markdown-transaction.json"
    with literature_update.writer_lock(root):
        held = getattr(_MARKDOWN_TRANSACTIONS, "held", set())
        if operation is None:
            if not journal.exists() and not journal.is_symlink():
                return {"recovered": False}
            entry = releases.read_json(releases.safe_file(root, journal.name).read_bytes())
            if not isinstance(entry, dict) or entry.get("repository") != str(repository):
                raise ValueError("Markdown recovery requires the original Git repository")
            if entry.get("operation") == "enable":
                if set(entry) != {"operation", "repository", "before", "after"}:
                    raise ValueError("Invalid Markdown settings transaction")
                for key in ("before", "after"):
                    collections.digest(entry[key])
                _MARKDOWN_TRANSACTIONS.held = held | {root}
                try:
                    manifest = _manifest(root)
                    _read_papers(root, manifest, {})
                    current = releases.sha256(releases.encode(manifest))
                    if current not in {entry["before"], entry["after"]}:
                        raise ValueError("Markdown settings transaction does not match the manifest")
                    updated = deepcopy(manifest)
                    updated["project"]["git_markdown_tracking"] = True
                    if releases.sha256(releases.encode(updated)) != entry["after"]:
                        raise ValueError("Markdown settings transaction has inconsistent project settings")
                    if current == entry["before"]:
                        collections.atomic_json(root, "manifest.json", updated)
                    count = sync_git_markdown(root, repository)["exported"]
                    journal.unlink()
                    _sync_directory(root)
                    return {"recovered": True, "enabled": True, "files": count}
                finally:
                    _MARKDOWN_TRANSACTIONS.held = held
            if not isinstance(entry.get("source_id"), str):
                raise ValueError("Invalid Markdown transaction")
            if entry.get("operation") == "add":
                if set(entry) != {"operation", "source_id", "repository", "before", "after", "paper_sha256"}:
                    raise ValueError("Invalid Markdown attachment transaction")
                for key in ("before", "after", "paper_sha256"):
                    collections.digest(entry[key])
                _MARKDOWN_TRANSACTIONS.held = held | {root}
                try:
                    membership_path = root / "membership-transaction.json"
                    if membership_path.exists() or membership_path.is_symlink():
                        membership = releases.read_json(releases.safe_file(root, membership_path.name).read_bytes())
                        expected_membership = {key: entry[key] for key in
                                               ("operation", "source_id", "before", "after", "paper_sha256")}
                        if membership != expected_membership:
                            raise ValueError("Markdown and membership attachment transactions do not match")
                    _recover_membership(root)
                    manifest = _manifest(root)
                    _read_papers(root, manifest, {})
                    revision = releases.sha256(releases.encode(manifest))
                    if revision == entry["before"]:
                        if entry["source_id"] in manifest["papers"]:
                            raise ValueError("Markdown attachment transaction has inconsistent membership")
                    elif revision == entry["after"]:
                        _, paper_revision = read_paper(root, entry["source_id"])
                        if paper_revision != entry["paper_sha256"]:
                            raise ValueError("Paper changed during Markdown attachment recovery")
                        sync_git_markdown(root, repository, source_id=entry["source_id"], revision=paper_revision)
                    else:
                        raise ValueError("Markdown attachment transaction does not match the manifest")
                    journal.unlink()
                    _sync_directory(root)
                    return {"recovered": True, "attached": revision == entry["after"]}
                finally:
                    _MARKDOWN_TRANSACTIONS.held = held
            if set(entry) != {"source_id", "repository", "before"}:
                raise ValueError("Invalid Markdown transaction")
            collections.digest(entry["before"])
            source_id = entry["source_id"]
        else:
            manifest = _manifest(root)
            if not manifest["project"].get("git_markdown_tracking", False):
                raise ValueError("Git Markdown tracking is not enabled for this current store")
            source_id = arguments["source_id"]
            _, revision = read_paper(root, source_id)
            if revision != arguments["revision"]:
                raise ValueError("Paper changed; reload before saving")
            collections.atomic_json(root, journal.name, {
                "source_id": source_id, "repository": str(repository), "before": revision})
        _MARKDOWN_TRANSACTIONS.held = held | {root}
        try:
            if operation is not None:
                try:
                    result = operation(root, **arguments)
                except ValueError:
                    _, current = read_paper(root, source_id)
                    if current == revision:
                        journal.unlink()
                        _sync_directory(root)
                    raise
            else:
                result = {"recovered": True}
            _, current = read_paper(root, source_id)
            sync_git_markdown(root, repository, source_id=source_id, revision=current)
            journal.unlink()
            _sync_directory(root)
            return result
        finally:
            _MARKDOWN_TRANSACTIONS.held = held


def edit_with_git_markdown(root, repository, *, action, **arguments):
    """Apply one paper edit and leave a recoverable journal until Markdown is refreshed."""
    operations = {"save": save_draft, "publish": publish, "unpublish": unpublish}
    if action not in operations:
        raise ValueError("Unknown Markdown-tracked paper action")
    return _markdown_transaction(root, repository, operation=operations[action], arguments=arguments)


def add_paper_with_git_markdown(root, repository, *, editorial, expected):
    """Attach a draft and regenerate its Markdown using a recoverable journal."""
    root = Path(root).absolute()
    repository = Path(repository).resolve()
    with literature_update.writer_lock(root):
        manifest = _manifest(root)
        if not manifest["project"].get("git_markdown_tracking", False):
            raise ValueError("Git Markdown tracking is not enabled for this current store")
        if releases.sha256(releases.encode(manifest)) != expected:
            raise ValueError("Paper membership changed; reload before attaching")
        if not isinstance(editorial, dict) or not isinstance(editorial.get("source"), dict):
            raise ValueError("Invalid attached paper evidence")
        source_id = editorial["source"].get("id")
        if not isinstance(source_id, str) or source_id in manifest["papers"]:
            raise ValueError("Paper is already attached or has an invalid source ID")
        paper = {"source_id": source_id, "editorial": deepcopy(editorial), "published": None,
                 "rag": None, "audit": None}
        _validate_paper(paper, manifest["headers"])
        updated = deepcopy(manifest)
        updated["papers"] = sorted([*updated["papers"], source_id])
        journal = root / "markdown-transaction.json"
        collections.atomic_json(root, journal.name, {
            "operation": "add", "source_id": source_id, "repository": str(repository),
            "before": expected, "after": releases.sha256(releases.encode(updated)),
            "paper_sha256": releases.sha256(releases.encode(paper)),
        })
        held = getattr(_MARKDOWN_TRANSACTIONS, "held", set())
        _MARKDOWN_TRANSACTIONS.held = held | {root}
        try:
            try:
                result = add_paper(root, editorial=editorial, expected=expected)
            except ValueError:
                membership = root / "membership-transaction.json"
                if not membership.exists() and not membership.is_symlink():
                    journal.unlink()
                    _sync_directory(root)
                raise
            sync_git_markdown(root, repository, source_id=source_id, revision=result["revision"])
            journal.unlink()
            _sync_directory(root)
            return result
        finally:
            _MARKDOWN_TRANSACTIONS.held = held


def set_git_markdown_tracking(root, repository, *, enabled, expected, rights_confirmed=False):
    """Toggle Markdown tracking; enabling is journaled until the backfill completes."""
    if type(enabled) is not bool:
        raise ValueError("Git Markdown tracking must be enabled or disabled")
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        manifest = _manifest(root)
        if releases.sha256(releases.encode(manifest)) != expected:
            raise ValueError("Project settings changed; reload before saving")
        _read_papers(root, manifest, {})
        if manifest["project"].get("git_markdown_tracking", False) == enabled:
            return {"enabled": enabled, "files": 0}
        updated = deepcopy(manifest)
        updated["project"]["git_markdown_tracking"] = enabled
        if not enabled:
            collections.atomic_json(root, "manifest.json", updated)
            return {"enabled": False, "files": 0}
        if not rights_confirmed or repository is None:
            raise ValueError("Confirm authorization for the selected Git repository and its remote")
        repository = Path(repository).resolve()
        journal = root / "markdown-transaction.json"
        collections.atomic_json(root, journal.name, {
            "operation": "enable", "repository": str(repository), "before": expected,
            "after": releases.sha256(releases.encode(updated)),
        })
        result = recover_git_markdown(root, repository)
        return {"enabled": True, "files": result["files"]}


def recover_git_markdown(root, repository):
    """Complete an interrupted paper Markdown refresh using its original Git repository."""
    return _markdown_transaction(root, repository)


def editor_view(root, source_id):
    """Return one consistent editor state and revision without opening a library snapshot."""
    paper, revision = read_paper(root, source_id)
    editorial = paper["editorial"]
    published = paper["published"]
    identity = None
    status = "draft"
    pending = []
    if published is not None:
        bundle = project_text.empty_bundle()
        bundle.update(sources=[published["source"]], units=published["units"], tables=published["tables"])
        identity = project_text.content_identity(bundle, source_id)
        bundle.update(sources=[editorial["source"]], units=editorial["units"], tables=editorial["tables"])
        status = "published" if project_text.content_identity(bundle, source_id) == identity else "draft_revision"
        pending = project_chunks.review_current_terms(
            paper["rag"]["draft"] if paper["rag"] else None, identity)
    return {
        "revision": revision, "source_id": source_id,
        "editorial": editorial, "published": published, "rag": paper["rag"], "publication_state": status,
        "audit": paper["audit"], "published_content_identity": identity, "pending_terms": pending,
    }


def paragraph_draft(root, project_root, *, source_id, unit_id, revision):
    """Repair a PDF page as an unsaved draft using a current paper revision."""
    paper, current = read_paper(root, source_id)
    if current != revision:
        raise ValueError("Paper changed; reload the editor before repairing layout")
    editorial = paper["editorial"]
    bundle = project_text.empty_bundle()
    bundle.update(sources=[editorial["source"]], units=editorial["units"], tables=editorial["tables"])
    return project_edit._paragraph_draft(Path(project_root), bundle, source_id=source_id, unit_id=unit_id)


def _rag_preview_state(root, source_id, revision):
    root = Path(root).absolute()
    with literature_update.reader_lock(root):
        manifest = _manifest(root)
        papers = _read_papers(root, manifest, {})
        if source_id not in papers:
            raise ValueError("Paper is not registered in the current store")
        paper = papers[source_id][1]
        if releases.sha256(releases.encode(paper)) != revision:
            raise ValueError("Paper changed; reload before previewing retrieval")
        if paper["published"] is None:
            raise ValueError("Publish the paper before configuring retrieval chunks")
        published = _bundle(manifest["headers"]["published"],
                            {key: value[1] for key, value in papers.items()}, "published")
        checks = deepcopy(paper["rag"]["checks"]) if paper["rag"] else []
    snapshot = _published_snapshot({"published": published})
    units = snapshot.document_units(source_id)
    source = snapshot._lookup("sources", source_id)
    if source["format"] != "pdf":
        raise ValueError("Retrieval chunks require a PDF source")
    if not units:
        raise ValueError("Publish the paper before configuring retrieval chunks")
    return snapshot, source, units, checks


def preview_rag(root, *, source_id, revision, form, query):
    """Preview unsaved ranges against one detached published retrieval state."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Enter a test query before previewing draft search")
    snapshot, source, units, _ = _rag_preview_state(root, source_id, revision)
    return project_chunks.preview(snapshot, source, units, project_chunks.parse(units, form), query.strip())


def check_rag(root, *, source_id, revision, form):
    """Run saved checks against live and unsaved draft ranges without changing either."""
    snapshot, source, units, checks = _rag_preview_state(root, source_id, revision)
    return project_chunks.run_checks(snapshot, source, units, project_chunks.parse(units, form), checks)


def save_draft(root, *, revision, source_id, unit_id, text, bibliography, source_url, tables,
               reviewer, reason, missing_locator=None, whole_document=False):
    """Save a validated draft without changing published evidence or live RAG."""
    bibliography, source_url, reviewer, reason = project_edit._validate_correction(
        text=text, bibliography=bibliography, source_url=source_url, tables=tables, reviewer=reviewer,
        reason=reason, missing_locator=missing_locator, whole_document=whole_document)
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        paper, current = read_paper(root, source_id)
        if current != revision:
            raise ValueError("Paper changed; reload before saving")
        manifest = _manifest(root)
        snapshot = _bundle(manifest["headers"]["editorial"], {source_id: paper}, "editorial")
        bundle, source, unit, unit_id, units, before, new_tables = project_edit._apply_correction(
            snapshot["bundle"], source_id=source_id, unit_id=unit_id, text=text, bibliography=bibliography,
            source_url=source_url, tables=tables, missing_locator=missing_locator, whole_document=whole_document)
        updated = deepcopy(paper)
        updated["editorial"] = _paper_snapshot(
            {"bundle": bundle, "authorization": snapshot["authorization"],
             "chunk_plans": snapshot["chunk_plans"]}, source_id)
        event = {"schema_version": 1, "at": datetime.now(timezone.utc).isoformat(),
                 "reviewer": reviewer, "reason": reason, "status": "draft", "source_id": source_id,
                 "source_sha256": source["sha256"], "unit_id": unit_id, "parent_revision": current,
                 "before": {"sha256": releases.sha256(releases.encode(before))},
                 "after": {"sha256": releases.sha256(releases.encode({
                     "text": project_edit.document_draft(units) if whole_document else unit["text"],
                     "bibliography": bibliography, "source_url": source_url, "tables": new_tables}))}}
        if whole_document:
            event.update(scope="document", unit_ids=[item["id"] for item in units])
        updated["audit"] = {**(updated["audit"] or {}), "draft": event}
        new_revision = replace(root, updated, expected=current)
        return {"revision": new_revision, "source_id": source_id}


def publish(root, *, revision, source_id, reviewer, reason, checked):
    """Approve a paper's draft for retrieval without applying its RAG draft."""
    reviewer, reason = reviewer.strip(), reason.strip()
    if not reviewer or len(reviewer) > 200 or not reason or len(reason) > 2000:
        raise ValueError("Provide a reviewer name and publication reason")
    if not checked:
        raise ValueError("Confirm that the clear text was checked against the original source before publication")
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        paper, current = read_paper(root, source_id)
        if current != revision:
            raise ValueError("Paper changed; reload before publishing")
        manifest = _manifest(root)
        if manifest["headers"]["published"] is None:
            raise ValueError("Published evidence store is not initialized")
        draft = _bundle(manifest["headers"]["editorial"], {source_id: paper}, "editorial")
        selected = project_text._published_source(draft["bundle"], source_id)
        previous = _bundle(manifest["headers"]["published"], {source_id: paper}, "published")
        previous_identity = (project_text.content_identity(previous["bundle"], source_id)
                             if paper["published"] is not None else None)
        identity = project_text.content_identity(selected, source_id)
        old_plans = paper["published"]["chunk_plans"] if paper["published"] is not None else {}
        chunks_stale = bool(old_plans) and previous_identity != identity
        updated = deepcopy(paper)
        updated["published"] = _paper_snapshot({
            "bundle": selected, "authorization": draft["authorization"],
            "chunk_plans": {} if chunks_stale else old_plans}, source_id)
        event = {"schema_version": 1, "at": datetime.now(timezone.utc).isoformat(),
                 "reviewer": reviewer, "reason": reason, "scope": "publication",
                 "source_id": source_id, "source_sha256": selected["sources"][0]["sha256"], "unit_id": None,
                 "parent_revision": current, "chunks_stale": chunks_stale,
                 "before": {"content_identity": previous_identity}, "after": {"content_identity": identity}}
        updated["audit"] = {**(updated["audit"] or {}), "publication": event}
        new_revision = replace(root, updated, expected=current)
        return {"revision": new_revision, "source_id": source_id, "chunks_stale": chunks_stale}


def unpublish(root, *, revision, source_id, reviewer, reason, checked):
    """Withdraw published evidence without removing the draft or saved RAG settings."""
    reviewer, reason = reviewer.strip(), reason.strip()
    if not reviewer or len(reviewer) > 200 or not reason or len(reason) > 2000:
        raise ValueError("Provide your name and an unpublication reason")
    if not checked:
        raise ValueError("Confirm removal from active AI retrieval")
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        paper, current = read_paper(root, source_id)
        if current != revision:
            raise ValueError("Paper changed; reload before unpublishing")
        if paper["published"] is None:
            raise ValueError("Paper is not published")
        manifest = _manifest(root)
        published = _bundle(manifest["headers"]["published"], {source_id: paper}, "published")
        event = {
            "schema_version": 1, "at": datetime.now(timezone.utc).isoformat(),
            "reviewer": reviewer, "reason": reason, "scope": "unpublication", "source_id": source_id,
            "source_sha256": paper["published"]["source"]["sha256"], "unit_id": None,
            "parent_revision": current,
            "before": {"content_identity": project_text.content_identity(published["bundle"], source_id)},
            "after": {"content_identity": None},
        }
        updated = deepcopy(paper)
        updated["published"] = None
        updated["audit"] = {**(updated["audit"] or {}), "unpublication": event}
        new_revision = replace(root, updated, expected=current)
        return {"revision": new_revision, "source_id": source_id}


def _published_document(root, source_id, revision):
    paper, current = read_paper(root, source_id)
    if current != revision:
        raise ValueError("Paper changed; reload before editing retrieval settings")
    if paper["published"] is None:
        raise ValueError("Publish the paper before configuring retrieval chunks")
    manifest = _manifest(root)
    published = _bundle(manifest["headers"]["published"], {source_id: paper}, "published")
    source = paper["published"]["source"]
    if source["format"] != "pdf":
        raise ValueError("Retrieval chunks require a PDF source")
    units = sorted(paper["published"]["units"],
                   key=lambda unit: (unit["citation"]["locator"]["pdf_page"], unit["id"]))
    if not units:
        raise ValueError("Publish the paper before configuring retrieval chunks")
    return paper, current, published, units


def update_rag(root, *, source_id, revision, form, activate=False):
    """Save retrieval settings and, only when requested, apply them to published evidence."""
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        paper, current, published, units = _published_document(root, source_id, revision)
        plans = project_chunks.parse(units, form)
        releases.chunk_records(published["bundle"], plans)
        saved = paper["rag"] or {"draft": None, "checks": []}
        identity = project_text.content_identity(published["bundle"], source_id)
        pending = project_chunks.review_current_terms(saved["draft"], identity)
        updated = deepcopy(paper)
        updated["rag"] = {
            "draft": project_chunks.build_draft(published["bundle"], source_id, units, plans, pending),
            "checks": saved["checks"],
        }
        if activate:
            updated["published"]["chunk_plans"] = plans
        new_revision = replace(root, updated, expected=current)
        return {"revision": new_revision, "source_id": source_id, "activated": activate}


def update_check(root, *, source_id, revision, form, remove=False):
    """Add or remove a saved search check against the published text."""
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        paper, current, _, units = _published_document(root, source_id, revision)
        saved = paper["rag"] or {"draft": None, "checks": []}
        updated = deepcopy(paper)
        updated["rag"] = {
            "draft": saved["draft"],
            "checks": project_chunks.edited_checks(units, saved["checks"], form, remove=remove),
        }
        new_revision = replace(root, updated, expected=current)
        return {"revision": new_revision, "source_id": source_id}
