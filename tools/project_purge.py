"""Rebuild private project data before permanently removing a core paper."""

from copy import deepcopy
import csv
from pathlib import Path
import os
import shutil
import tempfile

from research_library import collections, releases
from . import citation_neighborhood, literature_update


def _doi(value):
    return collections.publication(value).removeprefix("doi:")


def _safe_tree(root):
    if root.resolve() != root.absolute() or not root.is_dir():
        raise ValueError("Purge requires a real project directory without symlinked ancestors")
    cwd = Path.cwd().resolve()
    if root == Path(root.anchor) or cwd == root or cwd.is_relative_to(root):
        raise ValueError("Refusing to purge a filesystem root or workspace ancestor")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("Remove symlinks from project data before purging a paper")


def _filtered_graph(graph, remaining):
    filtered = dict(graph)
    filtered["limitations"] = list(graph["limitations"])
    nodes = graph["nodes"]
    edges = [edge for edge in graph["edges"] if _doi(edge["seed_doi"]) in remaining]
    target_dois = {_doi(nodes[edge["target_provider_record"]]["record"]["identifiers"].get("doi", ""))
                   for edge in edges}
    keep = {edge["target_provider_record"] for edge in edges}
    retained_targets = (target_dois | remaining) - {""}
    keep.update(key for key, node in nodes.items()
                if node["record"].get("provider_source") != "REFERENCE"
                and _doi(node["record"]["identifiers"].get("doi", "")) in retained_targets)
    filtered["nodes"] = {key: value for key, value in nodes.items() if key in keep}
    filtered["edges"] = edges
    filtered["seeds"] = [seed for seed in graph["seeds"] if _doi(seed["seed_doi"]) in remaining]
    filtered["unresolved"] = [item for item in graph["unresolved"] if _doi(item["seed_doi"]) in remaining]
    retained_dois = {_doi(node["record"]["identifiers"].get("doi", "")) for node in filtered["nodes"].values()} - {""}
    filtered["resolution_issues"] = [
        issue for issue in graph.get("resolution_issues", [])
        if all(_doi(doi) in retained_dois for doi in issue.get("requested", []))]
    filtered["scope"] = (f"Exactly one hop from {len(filtered['seeds'])} project core publication DOIs. "
                         "No neighbor expansion.")
    return filtered


def _file_version(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def discovery_index(folder, graph=None):
    """Cache only target identities and core connections, not provider payloads."""
    path = releases.safe_file(folder, "neighborhood.json")
    version = _file_version(path)
    cached = folder / "purge-index.json"
    if graph is None and cached.exists():
        index = releases.read_json(releases.safe_file(folder, cached.name).read_bytes())
        if index.get("schema_version") == 1 and index.get("version") == version:
            return index
    if graph is None:
        graph = releases.read_json(path.read_bytes())
    targets = {}
    for edge in graph["edges"]:
        key = edge["target_provider_record"]
        if key not in targets:
            identifier = _doi(graph["nodes"][key]["record"]["identifiers"].get("doi", ""))
            targets[key] = [identifier, set()]
        targets[key][1].add(_doi(edge["seed_doi"]))
    index = {"schema_version": 1, "version": version,
             "targets": {key: [identifier, sorted(seeds)] for key, (identifier, seeds) in targets.items()}}
    if _file_version(path) != version:
        raise ValueError("Discovery data changed while preparing deletion preview; try again")
    collections.atomic_json(folder, cached.name, index)
    return index


def plan(root, doi, pdf_data_root, *, prepare=True):
    from . import project_library
    root = Path(root).absolute()
    _safe_tree(root)
    if any((path / "deleted-data").exists() for path in root.parent.glob("." + root.name + "-purge-*")):
        raise ValueError("An earlier purge has undeleted recovery data beside the project; review it before another purge")
    if any(path.name not in {"project.json", "runs", "registry", "papers", "discovery", "metadata-job.json",
                             "purge-job.json", "pdf-job.json", "batch-pdf.json", "retained-text",
                             "library-index.sqlite", "corrections"} for path in root.iterdir()):
        raise ValueError("Unexpected project files; review them before rebuilding project data")
    project = project_library.load(root)
    doi = citation_neighborhood.doi(doi)
    if doi not in project["papers"]:
        raise ValueError("Select an existing core paper")
    remaining = set(project["papers"]) - {doi}
    graphs, graph_versions = [], []
    all_dois, keep_dois, all_keys, keep_keys = set(), set(remaining), set(), set()
    discovery = root / "discovery"
    if discovery.exists():
        for folder in sorted(discovery.iterdir()):
            if not folder.is_dir() or not (folder / "job.json").is_file():
                raise ValueError("Unexpected discovery data; inspect it before purging")
            job = releases.read_json((folder / "job.json").read_bytes())
            if not (folder / "neighborhood.json").exists():
                continue
            if prepare:
                graph = releases.read_json((folder / "neighborhood.json").read_bytes())
                index = discovery_index(folder, graph)
                graphs.append((folder.name, job, _filtered_graph(graph, remaining)))
            else:
                index = discovery_index(folder)
            graph_versions.append([folder.name, index["version"]])
            for key, (identifier, seeds) in index["targets"].items():
                all_keys.add(key)
                if identifier:
                    all_dois.add(identifier)
                if remaining.intersection(seeds) and identifier != doi:
                    keep_keys.add(key)
                    if identifier:
                        keep_dois.add(identifier)
    removed_dois = (all_dois - keep_dois) | {doi}
    # A paper explicitly purged from core must not survive as a discovered record.
    for _, _, graph in graphs:
        removed = {key for key, node in graph["nodes"].items()
                   if _doi(node["record"]["identifiers"].get("doi", "")) in removed_dois}
        graph["nodes"] = {key: node for key, node in graph["nodes"].items() if key not in removed}
        graph["edges"] = [edge for edge in graph["edges"] if edge["target_provider_record"] not in removed]
        graph["limitations"].append("Project purge removed a core seed and records connected only to it; raw response caches were deleted.")
    downloads = {(_doi(identifier), digest): item
                 for (identifier, digest), item in project_library.downloaded_pdf_metadata(
                     Path(pdf_data_root).absolute()).items()}
    protected_hashes = {digest for (identifier, digest) in downloads if identifier not in removed_dois}
    digests = {digest for (identifier, digest) in downloads if identifier in removed_dois}
    registry = project_library.store(root)
    current, snapshot = None, None
    if (root / "registry/active.json").exists():
        state = releases.read_json(releases.safe_file(root / "registry", "active.json").read_bytes())
        snapshot = collections.digest(state["active"])
        document = releases.read_json(releases.safe_file(
            root / "registry/registries", snapshot + ".json").read_bytes())
        if releases.sha256(releases.encode(document)) != snapshot:
            raise ValueError("Registry identity mismatch")
        registry.validate_document(document)
        if any(entry["id"] != "core" and entry.get("kind") != "catalog" for entry in document["collections"]):
            raise ValueError("Purge supports one core collection plus metadata catalogs; review custom evidence collections first")
        if prepare:
            current = registry.current()
        for path in (root / "registry/releases").iterdir():
            if prepare:
                sources = releases.validate(path, allowed_profiles=["private_context"])._bundle["sources"]
            else:
                # Preview needs source checksums, not validation of every historical search index.
                manifest = releases.read_json(releases.safe_file(path, "manifest.json").read_bytes())
                records = releases.safe_file(path, "records.json").read_bytes()
                if releases.sha256(records) != manifest["files"]["records.json"]["sha256"]:
                    raise ValueError("Evidence source inventory checksum mismatch")
                sources = releases.read_json(records)["sources"]
            for source in sources:
                (digests if _doi(source["doi"]) in removed_dois else protected_hashes).add(source["sha256"])
    digests -= protected_hashes
    shared = []
    pdf_root = Path(pdf_data_root).absolute()
    if pdf_root.exists():
        if pdf_root.resolve() != pdf_root:
            raise ValueError("PDF data folder cannot contain a symlinked path")
        for path in sorted(pdf_root.iterdir()):
            if path.suffix.casefold() == ".pdf" and path.is_file():
                if path.is_symlink():
                    raise ValueError("Remove symlinked PDFs before purging")
                if releases.sha256(path.read_bytes()) in digests:
                    shared.append(path)
    return {
        "root": root, "doi": doi, "project": project, "remaining": remaining, "graphs": graphs,
        "removed_dois": removed_dois, "digests": digests, "shared": shared, "current": current,
        "snapshot": snapshot, "graph_versions": graph_versions,
        "related_removed": len(all_keys - keep_keys),
    }


def summary(plan):
    return {"doi": plan["doi"], "title": plan["project"]["papers"][plan["doi"]]["title"],
            "related_removed": plan["related_removed"], "pdf_copies": [str(path) for path in plan["shared"]]}


def fingerprint(plan):
    return releases.sha256(releases.encode({
        "project": plan["project"], "snapshot": plan["snapshot"],
        "removed_dois": sorted(plan["removed_dois"]), "digests": sorted(plan["digests"]),
        "shared": [str(path) for path in plan["shared"]],
        "graphs": plan["graph_versions"],
    }))


def purge(root, doi, pdf_data_root, *, progress=None, expected_plan=None):
    from . import project_library
    root = Path(root).absolute()
    with literature_update.writer_lock(root):
        data = plan(root, doi, pdf_data_root, prepare=False)
        if expected_plan is not None and fingerprint(data) != expected_plan:
            raise ValueError("Project data changed after confirmation; review a new purge preview")
        if progress:
            progress("Rebuilding retained core evidence and discovery results")
        data = plan(root, doi, pdf_data_root)
        if expected_plan is not None and fingerprint(data) != expected_plan:
            raise ValueError("Project data changed after confirmation; review a new purge preview")
        transaction = Path(tempfile.mkdtemp(prefix="." + root.name + "-purge-", dir=root.parent))
        stage, backup = transaction / "replacement", transaction / "deleted-data"
        committed = False
        try:
            project = deepcopy(data["project"])
            del project["papers"][data["doi"]]
            collections.atomic_json(stage, "project.json", project)
            registry = project_library.store(stage)
            old = data["current"]
            if old:
                # Imported projects have only source/page units; refuse unhandled curated dependencies.
                original = old.snapshots["core"]
                bundle = deepcopy(original._bundle)
                if any(bundle[key] for key in ("claims", "issues", "relationships")):
                    raise ValueError("This core collection has curated dependencies; purge requires a dependency-aware migration")
                bundle["sources"] = [source for source in bundle["sources"]
                                     if _doi(source["doi"]) not in data["removed_dois"]]
                ids = {source["id"] for source in bundle["sources"]}
                bundle["units"] = [unit for unit in bundle["units"] if unit["citation"]["source_id"] in ids]
                bundle["tables"] = [table for table in bundle["tables"] if table["citation"]["source_id"] in ids]
                if bundle["sources"]:
                    destination = stage / "retained-evidence"
                    grants = {key: value for key, value in original._manifest["authorization"].items() if key in ids}
                    releases.export(bundle, destination, profile="private_context", authorization=grants)
                    installed = registry.releases.install(destination)
                    entry = deepcopy(next(entry for entry in old.document["collections"] if entry["id"] == "core"))
                    entry["release_id"] = installed
                    entry["version"] = "purged-" + installed[:16]
                    registry.publish({"schema_version": 1, "collections": [entry]})
                    shutil.rmtree(destination)
                    project_library.write_text_views(stage / "retained-text", bundle)
                # Retain historical evidence releases only when they have no purged source.
                safe_releases = set()
                for path in (root / "registry/releases").iterdir():
                    snapshot = releases.validate(path, allowed_profiles=["private_context"])
                    if all(_doi(source["doi"]) not in data["removed_dois"] for source in snapshot._bundle["sources"]):
                        registry.releases.install(path)
                        safe_releases.add(snapshot.release_id)
                for path in (root / "registry/registries").glob("*.json"):
                    document = releases.read_json(path.read_bytes())
                    if all(entry.get("kind") != "catalog" and entry["release_id"] in safe_releases
                           for entry in document["collections"]):
                        collections.atomic_json(stage / "registry/registries", path.name, document)
            if (root / "runs").exists():
                for path in (root / "runs").iterdir():
                    if not (path / "evidence/manifest.json").is_file():
                        raise ValueError("An incomplete PDF import needs review before purge")
                    snapshot = releases.validate(path / "evidence", allowed_profiles=["private_context"])
                    if all(_doi(source["doi"]) not in data["removed_dois"] for source in snapshot._bundle["sources"]):
                        shutil.copytree(path, stage / "runs" / path.name)
            if old:
                from . import project_edit
                for source in old.snapshots["core"]._bundle["sources"]:
                    if _doi(source["doi"]) not in data["removed_dois"]:
                        for event in project_edit.history(root, source["id"]):
                            collections.atomic_json(stage / "corrections",
                                releases.sha256(releases.encode(event)) + ".json", event)
            if (root / "papers").exists():
                for path in (root / "papers").iterdir():
                    if path.is_file() and releases.sha256(path.read_bytes()) not in data["digests"]:
                        (stage / "papers").mkdir(exist_ok=True)
                        shutil.copy2(path, stage / "papers" / path.name)
            for name, job, graph in data["graphs"]:
                if not graph["seeds"]:
                    continue
                output = stage / "discovery" / name
                collections.atomic_json(output, "neighborhood.json", graph)
                discovery_index(output, graph)
                project_library.export_citations(output)
                job = deepcopy(job)
                job["status"] = "complete"
                job["report"] = {
                    "edges": len(graph["edges"]), "unresolved": len(graph["unresolved"]),
                    "seeds_with_provider_gaps": sum(any(
                        result["status"] not in {"complete_indexed_neighborhood", "complete_deposited_references"}
                        for result in seed["providers"].values()) for seed in graph["seeds"])}
                collections.atomic_json(output, "job.json", job)
                rows = [{"number": str(index), "doi": seed["seed_doi"], "title": seed["title"], "filename": ""}
                        for index, seed in enumerate(graph["seeds"], 1)]
                with (output / "seeds.csv").open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=["number", "doi", "title", "filename"])
                    writer.writeheader()
                    writer.writerows(rows)
            if (stage / "registry/active.json").exists() and (stage / "discovery").exists() and any(
                    entry.get("kind") == "catalog" for entry in old.document["collections"]):
                if progress:
                    progress("Rebuilding the metadata catalog without purged papers")
                project_library.configure_metadata(stage)
            # Validate the replacement before changing the live project.
            project_library.status(stage)
            if progress:
                progress("Activating rebuilt data and deleting old copies")
            os.rename(root, backup)
            try:
                os.rename(stage, root)
            except OSError:
                os.rename(backup, root)
                raise
            committed = True
            for path in data["shared"]:
                if path.is_symlink() or releases.sha256(path.read_bytes()) not in data["digests"]:
                    raise ValueError("A shared PDF changed during purge; remaining deletion requires review")
                path.unlink()
            pdf_root = Path(pdf_data_root).absolute()
            if pdf_root.exists():
                for path in sorted(pdf_root.glob("*-downloads.json")):
                    records = releases.read_json(releases.safe_file(pdf_root, path.name).read_bytes())
                    retained = [item for item in records if _doi(item.get("doi", "")) not in data["removed_dois"]]
                    collections.atomic_json(pdf_root, path.name, retained)
            shutil.rmtree(backup)
            return summary(data)
        finally:
            if backup.exists():
                raise ValueError("Purge cleanup is incomplete; inspect retained deletion data at " + str(backup))
            if not committed and stage.exists():
                shutil.rmtree(stage)
            (transaction / ".replacement.writer.lock").unlink(missing_ok=True)
            transaction.rmdir()
