"""Private, byte-verified copies of project and PDF directories for recovery."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from research_library import collections, releases
from . import literature_update, project_current_store, project_library, project_state


def _tree(root):
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError(f"Backup input must be a real directory without symlinks: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"Backup cannot include a symlink: {path}")
        if path.is_dir():
            result[name] = None
        elif path.is_file():
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    digest.update(block)
            result[name] = {"size": size, "sha256": digest.hexdigest()}
        else:
            raise ValueError(f"Backup cannot include a special file: {path}")
    return result


def _copy(root, destination, entries):
    destination.mkdir(mode=0o700, parents=True)
    for name, metadata in entries.items():
        source, target = root / name, destination / name
        if metadata is None:
            target.mkdir(mode=0o700)
        else:
            with source.open("rb") as reader, target.open("xb") as writer:
                os.fchmod(writer.fileno(), 0o600)
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())


def _semantic(project, current):
    state = project_state.current(project)
    current_state = project_current_store.read(current) if current else None
    if current:
        project_current_store._quality_files(current / "quality")
        project_current_store.project_library.current_metadata(current)
    return {"project": releases.sha256(releases.encode(state)),
            "current_state": releases.sha256(releases.encode(current_state)) if current else None}


def verify(backup):
    """Verify all bytes and validate the captured active paper states."""
    root = Path(backup).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Backup must be a real directory without symlinked paths")
    document = releases.read_json(releases.safe_file(root, "manifest.json").read_bytes())
    if (not isinstance(document, dict) or set(document) != {"schema_version", "trees", "state"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["trees"], dict)
            or set(document["trees"]) not in ({"project", "pdf-data"}, {"project", "pdf-data", "current-state"})):
        raise ValueError("Invalid full-backup manifest")
    locks = {".project.writer.lock"} | ({".current-state.writer.lock"}
                                         if "current-state" in document["trees"] else set())
    if {path.name for path in root.iterdir()} != {"manifest.json", *document["trees"], *locks}:
        raise ValueError("Unexpected full-backup contents")
    for name in locks:
        if releases.safe_file(root, name).stat().st_size:
            raise ValueError("Invalid backup lock file")
    for name, expected in document["trees"].items():
        if not isinstance(expected, dict) or _tree(root / name) != expected:
            raise ValueError(f"Backup tree differs from manifest: {name}")
    state = _semantic(root / "project", root / "current-state" if "current-state" in document["trees"] else None)
    if state != document["state"]:
        raise ValueError("Backup active paper state differs from manifest")
    return {"backup": str(root), "trees": sorted(document["trees"]), "verified": True}


def create(project, pdf_data, backup, *, current_state=None, writers_stopped=False):
    """Create a new private backup after the operator stops all data writers."""
    if not writers_stopped:
        raise ValueError("Stop the website, MCP servers and all project/PDF writers and confirm they are stopped")
    roots = {"project": Path(project).absolute(), "pdf-data": Path(pdf_data).absolute()}
    if current_state:
        roots["current-state"] = Path(current_state).absolute()
    destination = Path(backup).absolute()
    all_paths = [*roots.values(), destination]
    if destination.parent.resolve() != destination.parent:
        raise ValueError("Backup destination cannot have symlinked ancestors")
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for index, a in enumerate(all_paths) for b in all_paths[index + 1:]):
        raise ValueError("Backup and input directories must be separate and non-overlapping")
    with ExitStack() as locks:
        for root in sorted([roots["project"], *([roots["current-state"]] if current_state else [])]):
            locks.enter_context(literature_update.writer_lock(root))
        if destination.exists() or destination.is_symlink():
            raise ValueError("Backup destination already exists")
        state = _semantic(roots["project"], roots.get("current-state"))
        trees = {name: _tree(root) for name, root in roots.items()}
        stage = Path(tempfile.mkdtemp(prefix="." + destination.name + "-backup-", dir=destination.parent))
        try:
            candidate = stage / "backup"
            candidate.mkdir(mode=0o700)
            for name in (["project", "current-state"] if current_state else ["project"]):
                (candidate / ("." + name + ".writer.lock")).touch(mode=0o600, exist_ok=False)
            for name, root in roots.items():
                _copy(root, candidate / name, trees[name])
            if {name: _tree(root) for name, root in roots.items()} != trees:
                raise ValueError("Source data changed during backup; stop writers and retry")
            if _semantic(roots["project"], roots.get("current-state")) != state:
                raise ValueError("Project state changed during backup")
            collections.atomic_json(candidate, "manifest.json",
                                    {"schema_version": 1, "trees": trees, "state": state})
            verify(candidate)
            if destination.exists() or destination.is_symlink():
                raise ValueError("Backup destination already exists")
            candidate.rename(destination)
        finally:
            shutil.rmtree(stage)
    return {"backup": str(destination), "trees": sorted(roots), "verified": True,
            "external_settings_included": False}


def restore(backup, destination):
    """Restore a verified backup into a new, separate parent directory."""
    backup, destination = Path(backup).absolute(), Path(destination).absolute()
    if (destination.parent.resolve() != destination.parent or destination == backup
            or destination.is_relative_to(backup) or backup.is_relative_to(destination)):
        raise ValueError("Restore destination must be separate without symlinked ancestors")
    verify(backup)
    document = releases.read_json(releases.safe_file(backup, "manifest.json").read_bytes())
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore destination already exists")
    stage = Path(tempfile.mkdtemp(prefix="." + destination.name + "-restore-", dir=destination.parent))
    try:
        candidate = stage / "restored"
        candidate.mkdir(mode=0o700)
        for name, entries in document["trees"].items():
            _copy(backup / name, candidate / name, entries)
        for name, entries in document["trees"].items():
            if _tree(candidate / name) != entries:
                raise ValueError(f"Restored tree differs from backup: {name}")
        if _semantic(candidate / "project", candidate / "current-state"
                     if "current-state" in document["trees"] else None) != document["state"]:
            raise ValueError("Restored current state differs from backup")
        verify(backup)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Restore destination already exists")
        candidate.rename(destination)
    finally:
        shutil.rmtree(stage)
    return {"directory": str(destination), "trees": sorted(document["trees"]), "verified": True}


def start_from_preserved(backup, destination, *, name, authorization, rows):
    """Create an isolated new project with selected unpublished drafts from a verified backup."""
    backup, destination = Path(backup).absolute(), Path(destination).absolute()
    if (destination.parent.resolve() != destination.parent or destination == backup
            or destination.is_relative_to(backup) or backup.is_relative_to(destination)):
        raise ValueError("New project destination must be separate without symlinked ancestors")
    verified = verify(backup)
    if "current-state" not in verified["trees"]:
        raise ValueError("Backup must include a current-state store")
    if destination.exists() or destination.is_symlink():
        raise ValueError("New project destination already exists")
    stage = Path(tempfile.mkdtemp(prefix="." + destination.name + "-restart-", dir=destination.parent))
    try:
        candidate = stage / "project-set"
        candidate.mkdir(mode=0o700)
        store, project = candidate / "current-state", candidate / "project"
        result = project_current_store.initialize_from_preserved(
            backup / "current-state", backup / "project", store,
            name=name, authorization=authorization, rows=rows)
        state = project_current_store.read(store)
        project_library.initialize(project, state["project"]["name"], state["project"]["authorization"], rows)
        for source in state["editorial"]["bundle"]["sources"]:
            filename = source["sha256"] + ".pdf"
            original = releases.safe_file(backup / "project" / "papers", filename)
            folder = project / "papers"
            folder.mkdir(mode=0o700, exist_ok=True)
            with original.open("rb") as reader, (folder / filename).open("xb") as writer:
                os.fchmod(writer.fileno(), 0o600)
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            copied = releases.safe_file(folder, filename)
            if releases.sha256(copied.read_bytes()) != source["sha256"]:
                raise ValueError("Copied preserved PDF checksum mismatch")
        verify(backup)
        if destination.exists() or destination.is_symlink():
            raise ValueError("New project destination already exists")
        candidate.rename(destination)
    finally:
        shutil.rmtree(stage)
    return {"directory": str(destination), "project": str(destination / "project"),
            "current_state": str(destination / "current-state"),
            "restored_drafts": result["restored_drafts"], "published": 0}
