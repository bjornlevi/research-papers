"""Non-destructive project restart and checksum-verified reuse of editorial material."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile

from research_library import collections, releases
from . import literature_update, project_text


def archive_root(root):
    root = Path(root).absolute()
    return root.parent / (root.name + "-preserved")


def archives(root):
    parent = archive_root(root)
    if parent.is_symlink() or parent.resolve() != parent:
        raise ValueError("Preserved project folder cannot contain symlinks")
    if not parent.exists():
        return []
    result = []
    for path in sorted(parent.iterdir(), reverse=True):
        if path.is_symlink() or not path.is_dir() or not (path / "project.json").is_file():
            raise ValueError("Unexpected preserved project entry; inspect it before reusing material")
        result.append(path)
    return result


def soft_reset(root):
    from . import project_library
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        project_library.check_reset_root(root)
        project_library.load(root)
        parent = archive_root(root)
        archives(root)
        parent.mkdir(mode=0o700, exist_ok=True)
        # Reserve a unique destination beside the project, on the same filesystem.
        destination = Path(tempfile.mkdtemp(
            prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-"), dir=parent))
        destination.rmdir()
        root.rename(destination)
        return {"archive": str(destination), "status": "awaiting_core_papers",
                "preserved": "Entire project, including PDFs, text, history and discovery"}


def restore(root, dois, *, preserved_for=None):
    """Reuse previously attached exact DOI/PDF versions as drafts, never publication."""
    from . import project_edit
    root = Path(root)
    wanted = set(dois)
    retained = archives(preserved_for or root)
    if not wanted or not retained:
        return []
    current_store = project_text.editorial_store(root)
    current = current_store.current() if (current_store.root / "active.json").exists() else None
    bundle = deepcopy(current.snapshots["core"]._bundle) if current else project_text.empty_bundle()
    grants = deepcopy(current.snapshots["core"]._manifest["authorization"]) if current else {}
    existing_dois = {collections.publication(s["doi"]).removeprefix("doi:") for s in bundle["sources"]}
    wanted -= existing_dois
    if not wanted:
        return []
    selected = []
    hashes = {s["sha256"] for s in bundle["sources"]}
    for archive in retained:
        workspace = project_text.editorial_store(archive)
        if not (workspace.root / "active.json").exists():
            continue
        evidence = workspace.current().snapshots["core"]
        for source in evidence._bundle["sources"]:
            doi = collections.publication(source["doi"]).removeprefix("doi:")
            if doi not in wanted:
                continue
            pdf = releases.safe_file(archive / "papers", source["sha256"] + ".pdf")
            if releases.sha256(pdf.read_bytes()) != source["sha256"]:
                raise ValueError("Preserved PDF checksum mismatch; material was not restored: " + doi)
            if source["sha256"] in hashes:
                raise ValueError("Preserved PDF is already associated with another paper: " + doi)
            material = project_text.source_bundle(evidence._bundle, source["id"])
            for section in ("sources", "units", "tables"):
                bundle[section].extend(material[section])
            grants[source["id"]] = deepcopy(evidence._manifest["authorization"][source["id"]])
            selected.append((doi, archive, source, pdf))
            hashes.add(source["sha256"])
            wanted.remove(doi)
        if not wanted:
            break
    if not selected:
        return []
    releases.validate_bundle(bundle)
    project_text.ensure_editorial(root)
    papers = root / "papers"
    papers.mkdir(exist_ok=True)
    for doi, archive, source, pdf in selected:
        destination = papers / pdf.name
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or releases.sha256(destination.read_bytes()) != source["sha256"]:
                raise ValueError("Existing PDF checksum mismatch; material was not restored: " + doi)
        else:
            shutil.copyfile(pdf, destination)
        for event in project_edit.history(archive, source["id"]):
            collections.atomic_json(root / "corrections", releases.sha256(releases.encode(event)) + ".json", event)
    project_text.activate(root, bundle, grants, editorial=True)
    return [doi for doi, _, _, _ in selected]
