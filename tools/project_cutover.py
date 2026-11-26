"""Compare snapshot-backed project state with a separate current store."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import subprocess

from research_library import releases
from . import literature_text, literature_update, project_current_store, project_state, project_text


def _sources(state):
    identifiers = set(state["rag"]) | set(state["audit"])
    for section in ("editorial", "published"):
        snapshot = state[section]
        if snapshot is not None:
            identifiers.update(source["id"] for source in snapshot["bundle"]["sources"])
    return identifiers


def _section_for_source(state, section, source_id):
    return project_current_store._paper_snapshot(state[section], source_id)


def _pdf_check(project_root, source):
    directory = project_root / "papers"
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("Project PDF directory cannot contain symlinked paths")
    filename = source["sha256"] + ".pdf"
    path = directory / filename
    if not path.exists() and not path.is_symlink():
        return "missing"
    path = releases.safe_file(directory, filename)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    current = path.stat()
    def version(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if version(before) != version(after) or version(after) != version(current):
        raise ValueError("A project PDF changed during comparison; retry")
    return "verified" if digest.hexdigest() == source["sha256"] else "checksum_mismatch"


def _markdown_check(state, repository):
    repository = Path(repository).absolute()
    if repository.resolve() != repository or not repository.is_dir():
        raise ValueError("Markdown repository must be a real directory without symlinked paths")
    result = subprocess.run(["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, check=False)
    if result.returncode or Path(result.stdout.strip()).resolve() != repository:
        raise ValueError("Markdown destination must be a Git repository root")
    output = repository / "project-markdown"
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise ValueError("Invalid Git Markdown output directory")
    editorial, published = state["editorial"], state["published"]
    expected = {}
    published_ids = set()
    for section, snapshot in (("draft", editorial), ("published", published)):
        if snapshot is None:
            continue
        bundle = snapshot["bundle"]
        known = literature_text.evidence(bundle["units"])
        for source in bundle["sources"]:
            name = literature_text.slug(source) + "/" + section + ".md"
            if name in expected:
                raise ValueError("Multiple sources map to the same Markdown path")
            expected[name] = (source["id"], project_text._markdown_version(bundle, source["id"], section, known))
            if section == "published":
                published_ids.add(source["id"])
    if editorial is not None:
        for source in editorial["bundle"]["sources"]:
            if source["id"] not in published_ids:
                name = literature_text.slug(source) + "/published.md"
                if name in expected:
                    raise ValueError("Multiple sources map to the same Markdown path")
                expected[name] = (source["id"], None)
    counts = {"matching": 0, "missing": 0, "different_generated": 0, "unmanaged": 0,
              "withdrawal_needed": 0, "withdrawn_or_absent": 0}
    differences = []
    withdrawn = (project_text._MARKDOWN_GENERATED
                 + "# No published clear text\n\nThis paper is not currently published for retrieval.\n")
    for name, (source_id, content) in sorted(expected.items()):
        folder, target = output / name.split("/")[0], output / name
        if folder.is_symlink() or target.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise ValueError("Git Markdown path cannot contain symlinks or invalid directories")
        if target.exists() and not target.is_file():
            raise ValueError("Git Markdown target must be a regular file")
        if not target.exists():
            status = "withdrawn_or_absent" if content is None else "missing"
        else:
            text = target.read_text(encoding="utf-8")
            if not text.startswith(project_text._MARKDOWN_GENERATED):
                status = "unmanaged"
            elif content is None:
                status = "withdrawn_or_absent" if text == withdrawn else "withdrawal_needed"
            else:
                status = "matching" if text == content else "different_generated"
        counts[status] += 1
        if status not in {"matching", "withdrawn_or_absent"}:
            differences.append({"source_id": source_id, "path": name, "status": status})
    return {"counts": counts, "differences": differences,
            "scope": "Current draft and published views only; index, historical exports, Git staging and rights are not checked."}


def compare(project_root, current_store, *, quality_root=None, markdown_repository=None):
    """Report differences without changing either data store or promising cutover readiness."""
    project_root, current_store = Path(project_root).absolute(), Path(current_store).absolute()
    if (project_root.resolve() != project_root or current_store.resolve() != current_store
            or project_root == current_store or project_root.is_relative_to(current_store)
            or current_store.is_relative_to(project_root)):
        raise ValueError("Project and current store must be separate, real paths without symlinks")
    with ExitStack() as locks:
        for root in sorted((project_root, current_store)):
            locks.enter_context(literature_update.writer_lock(root))
        normal = project_state.current(project_root)
        current = project_current_store.read(current_store)
        normal_sources, current_sources = _sources(normal), _sources(current)
        differences = []
        for source_id in sorted(normal_sources | current_sources):
            changed = []
            for section in ("editorial", "published"):
                if _section_for_source(normal, section, source_id) != _section_for_source(current, section, source_id):
                    changed.append(section)
            for section in ("rag", "audit"):
                if normal[section].get(source_id) != current[section].get(source_id):
                    changed.append(section)
            if changed:
                differences.append({"source_id": source_id, "sections": changed})
        source_records = {}
        for state in (normal, current):
            if state["editorial"] is not None:
                for source in state["editorial"]["bundle"]["sources"]:
                    source_records[(source["id"], source["sha256"])] = source
        pdfs = [{"source_id": source["id"], "sha256": source["sha256"],
                 "status": _pdf_check(project_root, source)}
                for source in source_records.values() if source["format"] == "pdf"]
        quality_source = (Path(quality_root).absolute() if quality_root else
                          project_root / "releases/data-quality")
        normal_quality = project_current_store._quality_files(quality_source)
        current_quality = project_current_store._quality_files(current_store / "quality")
        sections = {name: normal[name] == current[name]
                    for name in ("project", "editorial", "published", "rag", "audit")}
        result = {
            "sections_match": sections,
            "project_fields_differ": sorted(
                name for name in set(normal["project"]) | set(current["project"])
                if normal["project"].get(name) != current["project"].get(name)),
            "paper_differences": differences,
            "normal_source_count": len(normal_sources),
            "current_source_count": len(current_sources),
            "pdfs": pdfs,
            "quality_records_match": normal_quality == current_quality,
            "normal_quality_records": len(normal_quality),
            "current_quality_records": len(current_quality),
            "scope": ("Current paper state, project settings, project PDFs and selected quality records only. "
                      "Discovery, metadata catalogs, shared PDFs, batch queues, Markdown, other settings, "
                      "active readers and a verified full recovery are not assessed."),
            "cutover_authorized": False,
        }
        if markdown_repository is not None:
            result["markdown"] = _markdown_check(current, markdown_repository)
        return result
