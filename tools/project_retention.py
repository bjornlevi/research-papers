"""Inspect or explicitly prune inactive project releases."""

from pathlib import Path
import shutil
import tempfile

from research_library import collections, releases
from . import literature_update, project_library, project_purge, project_state, project_text


def _size(path):
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _store_inventory(store):
    if not (store.root / "active.json").is_file():
        return None
    active = store.current()
    retained = {"releases": set(), "catalogs": set()}
    for entry in active.document["collections"]:
        kind = "catalogs" if entry.get("kind") == "catalog" else "releases"
        retained[kind].add(entry["release_id"])
    historical = []
    for kind in ("registries", "releases", "catalogs"):
        folder = store.root / kind
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise ValueError(f"Expected {folder} to be a directory")
        for path in sorted(folder.iterdir()):
            identifier = path.name if kind == "releases" else path.stem
            collections.digest(identifier)
            if kind == "registries":
                if not path.is_file() or path.suffix != ".json":
                    raise ValueError(f"Unexpected registry entry: {path.name}")
                keep = identifier == active.release_id
            elif kind == "catalogs":
                if not path.is_file() or path.suffix != ".sqlite":
                    raise ValueError(f"Unexpected {kind} entry: {path.name}")
                keep = identifier in retained[kind]
            else:
                if not path.is_dir():
                    raise ValueError(f"Unexpected {kind} entry: {path.name}")
                keep = identifier in retained[kind]
            if not keep:
                historical.append({"path": str(path.relative_to(store.root)),
                                   "bytes": _size(path)})
    return {"active_snapshot": active.release_id,
            "active_releases": {key: sorted(value) for key, value in retained.items()},
            "historical": historical}


def preview(root):
    """List inactive releases without deleting or promising recovery from Git."""
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        project_purge._safe_tree(root)
        project_library.load(root)
        published = _store_inventory(project_library.store(root))
        editorial = (_store_inventory(project_text.editorial_store(root))
                     if (root / "editorial" / "active.json").is_file() else None)
        return {"published": published, "editorial": editorial}


def prune(root, backup, *, readers_stopped=False):
    """Prune inactive stores only after creating and testing a current-state backup."""
    root = Path(root).absolute()
    backup = Path(backup).absolute()
    if not readers_stopped:
        raise ValueError("Stop the website and all MCP readers and confirm they are stopped")
    if backup.is_relative_to(root) or backup.parent.resolve() != backup.parent:
        raise ValueError("Backup must be outside the project and have no symlinked parent path")
    with literature_update.writer_lock(root):
        report = preview(root)
        project_state.export_papers(root, backup)
        with tempfile.TemporaryDirectory(prefix="research-retention-check-") as temporary:
            restored = Path(temporary) / "restored"
            project_state.restore(backup, restored)
            if project_state.current(restored) != project_state.current(root):
                raise ValueError("Backup verification failed; no historical data was removed")
        removed = 0
        freed = 0
        for key, name in (("editorial", "editorial"), ("published", "registry")):
            inventory = report[key]
            if inventory is None:
                continue
            directory = root / name
            pointer = releases.read_json(releases.safe_file(directory, "active.json").read_bytes())
            if pointer["active"] != inventory["active_snapshot"]:
                raise ValueError("Active snapshot changed during retention; no further data was removed")
            if pointer["previous"] is not None:
                collections.atomic_json(directory, "active.json",
                                        {"active": pointer["active"], "previous": None})
            for item in inventory["historical"]:
                path = directory / item["path"]
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.is_file() and not path.is_symlink():
                    path.unlink()
                else:
                    raise ValueError(f"Historical entry changed during retention: {path}")
                removed += 1
                freed += item["bytes"]
        return {"backup": str(backup), "removed_entries": removed, "removed_bytes": freed,
                "runs_removed": False, "old_snapshot_links_retained": False}
