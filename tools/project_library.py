"""Initialize and populate an independent private core evidence collection."""

import argparse
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unicodedata

from research_library import collections, literature, releases
from . import citation_neighborhood, literature_ingest, literature_text, literature_update, provided_sources


def load(root):
    document = literature_ingest.read(Path(root) / "project.json")
    validate_project(document)
    return document


def validate_project(document):
    if (set(document) - {"schema_version", "name", "authorization", "papers", "git_markdown_tracking"}
            or not {"schema_version", "name", "authorization", "papers"} <= set(document)
            or document["schema_version"] != 1
            or type(document.get("git_markdown_tracking", False)) is not bool):
        raise ValueError("Invalid project configuration")


def validate_current_project(document):
    if not isinstance(document, dict):
        raise ValueError("Invalid project configuration")
    validate_project(document)
    if (not isinstance(document["name"], str) or not document["name"].strip()
            or len(document["name"]) > 256 or not isinstance(document["authorization"], str)
            or not document["authorization"].strip() or not isinstance(document["papers"], dict)):
        raise ValueError("Invalid project configuration")
    papers = document["papers"]
    if papers:
        if any(not isinstance(doi, str) or not isinstance(row, dict) or set(row) != {"title"}
               or not isinstance(row["title"], str) for doi, row in papers.items()):
            raise ValueError("Invalid Core papers in project configuration")
        validated, _ = validated_papers([{"doi": doi, "title": row["title"]} for doi, row in papers.items()])
        if validated != papers:
            raise ValueError("Invalid Core papers in project configuration")


def store(root):
    return collections.CollectionStore(
        Path(root) / "registry", allowed_profiles=["private_context"],
        allowed_collections=["core"], defaults=["core"])


def openalex_key_path(root):
    return Path(root).absolute().parent / "openalex-api-key"


def metadata_client_options(root):
    key_path = openalex_key_path(root)
    if not key_path.exists() and not key_path.is_symlink():
        return {}
    if key_path.is_symlink() or key_path.stat().st_mode & 0o077:
        raise ValueError("OpenAlex API key file must be private (chmod 600) and not a symlink")
    key = key_path.read_text(encoding="utf-8").strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("OpenAlex API key file must contain one nonempty key")
    return {"openalex_api_key": key}


def pdf_title_stem(title):
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")[:140].rstrip("-") or "paper"


def save_named_pdf(pdf_data_root, data, digest, title):
    stem = pdf_title_stem(title)
    filename = stem + ".pdf"
    counter = 0
    while True:
        path = pdf_data_root / filename
        if path.is_symlink():
            raise ValueError("Symlinked PDF output")
        if not path.exists() or releases.sha256(path.read_bytes()) == digest:
            return citation_neighborhood.save_pdf(pdf_data_root, data, digest, {digest: filename})
        counter += 1
        suffix = digest[:12] + (f"-{counter}" if counter > 1 else "")
        filename = f"{stem}-{suffix}.pdf"


def local_pdf_files(pdf_data_root):
    """Enumerate local PDFs recursively without following symlinks."""
    root = Path(pdf_data_root).absolute()
    if root.resolve() != root:
        raise ValueError("PDF data folder cannot contain a symlinked path")
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ValueError("PDF data folder must be a directory")
    files = {}
    def scan_error(error):
        raise error
    for folder, directories, names in os.walk(root, followlinks=False, onerror=scan_error):
        directories[:] = sorted(name for name in directories if not (Path(folder) / name).is_symlink())
        for name in sorted(names):
            path = Path(folder) / name
            if path.suffix.casefold() == ".pdf" and not path.is_symlink() and path.is_file():
                files[path.relative_to(root).as_posix()] = path
    return files


def find_local_pdf(doi, title, pdf_data_root):
    """Suggest a local copy using recorded acquisition, filename or first-page metadata."""
    import pymupdf
    files = local_pdf_files(pdf_data_root)
    known = downloaded_pdf_metadata(Path(pdf_data_root))
    normalized_title = re.sub(r"[^a-z0-9]", "", unicodedata.normalize(
        "NFKD", title).encode("ascii", "ignore").decode().lower())
    title_stem = pdf_title_stem(title)
    doi_pattern = re.compile(re.escape(doi) + r"(?=$|[\s,;:)\]}]|\.(?:\s|$))", re.IGNORECASE)
    correction_pattern = re.compile(
        r"^\s*(?:(?:author|publisher)\s+correction|correction|corrigendum|erratum)\b", re.IGNORECASE | re.MULTILINE)
    correction_requested = bool(correction_pattern.search(title))
    attempts = []
    for filename, path in sorted(files.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
        if path.stat().st_size > provided_sources.MAX_BYTES:
            attempts.append({"filename": filename, "error": "Local PDF exceeds the 64 MiB limit"})
            continue
        digest = releases.sha256(path.read_bytes())
        acquisition = known.get(("doi:" + doi, digest))
        basis = "recorded acquisition DOI and checksum" if acquisition else None
        stem = path.stem.casefold()
        if basis is None and title_stem and (stem == title_stem or stem.startswith(title_stem + "-")
                and re.fullmatch(r"[a-f0-9]{12}(?:-\d+)?", stem[len(title_stem) + 1:])):
            basis = "title filename"
        try:
            with pymupdf.open(path) as document:
                if document.is_encrypted or not len(document):
                    attempts.append({"filename": filename, "error": "Local PDF is encrypted or empty"})
                    continue
                first_page = document[0].get_text()[:100_000]
                metadata = document.metadata or {}
                correction_pdf = bool(correction_pattern.search(
                    first_page + "\n" + (metadata.get("title") or "")))
                if correction_pdf and not correction_requested:
                    if basis is not None or doi_pattern.search(first_page):
                        attempts.append({"filename": filename,
                                         "error": "Correction notice is not the original paper; its cited DOI is not PDF identity"})
                    continue
                if basis is None and doi_pattern.search(first_page + "\n" + " ".join(
                        value for value in metadata.values() if isinstance(value, str))):
                    basis = "DOI in first page or PDF metadata"
                if basis is None and len(normalized_title) >= 25:
                    text = re.sub(r"[^a-z0-9]", "", unicodedata.normalize(
                        "NFKD", first_page + "\n" + (metadata.get("title") or "")
                        ).encode("ascii", "ignore").decode().lower())
                    if normalized_title in text:
                        basis = "title in first page or PDF metadata"
        except pymupdf.FileDataError:
            attempts.append({"filename": filename, "error": "Local file is not a readable PDF"})
            continue
        if basis:
            return {
                "status": "local_found", "doi": doi, "filename": filename, "sha256": digest,
                "match_basis": basis, "attempts": attempts,
                "identity_note": "Suggested local match; operator must verify title, DOI and private-use permission.",
            }, attempts
    return None, attempts


def library_paper(root, doi, title=None):
    root = Path(root)
    doi = citation_neighborhood.doi(doi)
    project = load(root)
    if doi in project["papers"]:
        return project["papers"][doi]
    from . import project_purge
    for folder in sorted((root / "discovery").iterdir()) if (root / "discovery").exists() else []:
        if folder.is_symlink() or not (folder / "job.json").is_file() or not (folder / "neighborhood.json").is_file():
            continue
        index = project_purge.discovery_index(folder)
        keys = [key for key, (identifier, seeds) in index["targets"].items()
                if identifier == doi and set(project["papers"]).intersection(seeds)]
        if not keys:
            continue
        if not title:
            graph = releases.read_json(releases.safe_file(folder, "neighborhood.json").read_bytes())
            title = next((graph["nodes"][key]["record"].get("title") for key in keys
                          if graph["nodes"][key]["record"].get("title")), None)
        if not title:
            raise ValueError("This discovered paper needs a title before its PDF can be confirmed")
        papers, _ = validated_papers([{"doi": doi, "title": title}], {})
        return papers[doi]
    raise ValueError("Select a core paper or a DOI connected to the current core library in saved discovery")


def current_library_paper(root, store, doi, title, revision):
    from . import project_current_store, project_index
    _, current_revision = project_current_store.project_view(store)
    if current_revision != revision:
        raise ValueError("Paper membership changed; reload and try again")
    paper = project_index.paper_detail(root, "doi:" + doi, include_metadata=False, current_store=store)
    if project_current_store.membership_revision(store) != revision:
        raise ValueError("Paper membership changed; reload and try again")
    if not paper["title"]:
        raise ValueError("This paper needs a title before its PDF can be confirmed")
    if title != paper["title"]:
        raise ValueError("Paper title changed; reload and confirm the PDF again")
    return paper


def find_pdf(root, doi, pdf_data_root, *, title=None, current_store=None, membership_revision=None):
    import httpx
    root, pdf_data_root = Path(root).absolute(), Path(pdf_data_root).absolute()
    doi = citation_neighborhood.doi(doi)
    def selected_paper():
        return (current_library_paper(root, current_store, doi, title, membership_revision)
                if current_store else library_paper(root, doi, title))

    paper = selected_paper()
    if pdf_data_root.resolve() != pdf_data_root:
        raise ValueError("PDF data folder cannot contain a symlinked path")
    local, local_attempts = find_local_pdf(doi, paper["title"], pdf_data_root)
    if local is not None:
        if current_store:
            selected_paper()
        return local
    with closing(literature.MetadataClient(**metadata_client_options(root))) as client:
        work, source = client.get("https://api.openalex.org/works/https://doi.org/" + doi,
                                  {"select": citation_neighborhood.FIELDS})
    if citation_neighborhood.doi(work.get("doi") or "") != doi:
        raise ValueError("OpenAlex returned a different DOI; PDF identity requires review")
    candidates = citation_neighborhood.licensed_locations(work)
    record = {"id": "doi:" + doi, "title": paper["title"],
              "citation_url": "https://doi.org/" + doi, "aliases": ["doi:" + doi],
              "identity_status": "resolved", "istopmm_overlap": "not_matched"}
    with httpx.Client(timeout=httpx.Timeout(60), follow_redirects=False,
                      headers={"User-Agent": "research-project/1 (private-open-access-acquisition)"}) as client:
        entry, data = citation_neighborhood.acquire_one(
            client, record, candidates, citation_neighborhood.RequestPacer())
    entry.update(doi=doi, year=(work.get("publication_date") or "")[:4],
                 metadata_source=citation_neighborhood.source_ref(source))
    entry["local_attempts"] = local_attempts
    with literature_update.writer_lock(root):
        if current_store:
            selected_paper()
        else:
            library_paper(root, doi, paper["title"])
        pdf_data_root.mkdir(parents=True, exist_ok=True)
        if data is not None:
            entry["filename"] = save_named_pdf(pdf_data_root, data, entry["sha256"], record["title"])
            entry["final_url"] = entry["acquisition"]["final_url"]
            entry["retrieved_at"] = entry["acquired_at"]
            entry["version"] = entry["acquisition"].get("version")
            entry["version_note"] = entry["version"]
            entry["license"] = entry["acquisition"]["license"]
            entry["rights_note"] = entry["license_verification"]
        log_name = "project-" + releases.sha256(doi.encode()) + "-downloads.json"
        log_path = pdf_data_root / log_name
        previous = (releases.read_json(releases.safe_file(pdf_data_root, log_name).read_bytes())
                    if log_path.exists() else [])
        collections.atomic_json(pdf_data_root, log_name, [*previous, entry])
    return entry


def downloaded_pdf_metadata(pdf_data_root):
    records = {}
    if not pdf_data_root.exists():
        return records
    if pdf_data_root.resolve() != pdf_data_root.absolute():
        raise ValueError("PDF data folder cannot contain a symlinked path")
    for path in sorted(pdf_data_root.glob("*-downloads.json")):
        for item in releases.read_json(releases.safe_file(pdf_data_root, path.name).read_bytes()):
            if item.get("status") == "downloaded":
                key = (collections.publication(item["doi"]), item["sha256"])
                records[key] = item
    return records


def save_openalex_key(root, key):
    key = key.strip()
    if not key or len(key) > 512 or not key.isascii() or any(character.isspace() for character in key):
        raise ValueError("Enter a nonempty OpenAlex API key without whitespace (maximum 512 characters)")
    path = openalex_key_path(root)
    if path.parent.resolve() != path.parent or path.is_symlink():
        raise ValueError("OpenAlex API key path cannot contain symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".openalex-key-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def metadata_collections(root):
    registry = Path(root) / "registry"
    if not (registry / "active.json").exists():
        return []
    current = store(root).current()
    return [entry["id"] for entry in current.document["collections"] if entry.get("kind") == "catalog"]


def current_metadata(root):
    from research_library import catalog
    root = Path(root).absolute()
    with literature_update.reader_lock(root):
        pointer = root / "metadata-current.json"
        if not pointer.exists() and not pointer.is_symlink():
            return None
        entry = releases.read_json(releases.safe_file(root, pointer.name).read_bytes())
        collections.CollectionStore.validate_document({"schema_version": 1, "collections": [entry]})
        if entry["id"] != "metadata" or entry.get("kind") != "catalog" or entry["publications"]:
            raise ValueError("Invalid current metadata catalog pointer")
        if (root / "catalogs").is_symlink():
            raise releases.ReleaseError("Symlinked current metadata catalog directory")
        path = catalog.stored_path(root, entry["release_id"])
        snapshot = catalog.open_snapshot(path)
        if snapshot.release_id != entry["release_id"]:
            raise releases.ReleaseError("Current metadata catalog checksum mismatch")
        return entry, snapshot


def configure_metadata(root, *, progress=None, current_store=None):
    root = Path(root)
    if progress:
        progress("Reading saved discovery searches")
    with literature_update.writer_lock(root):
        if current_store:
            from . import project_current_store, project_index
            project, revision = project_current_store.project_view(current_store)
            discovery_signature = project_index._state(
                root, current_state=project_current_store.read(current_store), current_store=current_store)
            folders = project_index.discovery_runs(root, current_store)
            registry = current = None
        else:
            project = load(root)
            registry = store(root)
            if not (root / "registry/active.json").exists():
                raise ValueError("Attach at least one project PDF before configuring the metadata catalog")
            current = registry.current()
            discovery = root / "discovery"
            if discovery.resolve() != discovery.absolute():
                raise ValueError("Discovery directory cannot contain a symlinked path")
            folders = {folder.name: folder for folder in sorted(discovery.iterdir())} if discovery.exists() else {}
        latest = {}
        for folder in folders.values():
            if (not folder.is_dir() or folder.is_symlink() or len(folder.name) != 32
                    or any(c not in "0123456789abcdef" for c in folder.name)
                    or not (folder / "job.json").is_file()):
                continue
            job = releases.read_json(releases.safe_file(folder, "job.json").read_bytes())
            if job["status"] == "complete":
                saved = releases.read_json(releases.safe_file(folder, "neighborhood.json").read_bytes())
                scope = (job["direction"], tuple(sorted(seed["seed_doi"] for seed in saved["seeds"])))
                if scope not in latest or saved["acquired_at"] > latest[scope]["acquired_at"]:
                    latest[scope] = saved
        graphs = list(latest.values())
        if not graphs:
            raise ValueError("Complete a Find related papers search before configuring the metadata catalog")
        graph = {
            "schema_version": 1, "acquired_at": max(g["acquired_at"] for g in graphs),
            "scope": "Saved project reference/citation searches: " + "; ".join(sorted({g["scope"] for g in graphs})),
            "limitations": sorted({text for g in graphs for text in g["limitations"]}),
            "seeds": [seed for g in graphs for seed in g["seeds"]],
            "nodes": {}, "edges": [edge for g in graphs for edge in g["edges"]],
            "unresolved": [item for g in graphs for item in g["unresolved"]],
            "resolution_issues": [item for g in graphs for item in g["resolution_issues"]],
        }
        reasons = []
        for saved in graphs:
            for seed in saved["seeds"]:
                for provider, result in seed["providers"].items():
                    if result["status"] not in {"complete_indexed_neighborhood", "complete_deposited_references"}:
                        reasons.append(f"{seed['seed_doi']} ({provider}): {result.get('error', result['status'])}")
        unresolved = citation_neighborhood.unresolved_references(graph)
        if unresolved:
            reasons.append(f"{len(unresolved)} provider reference identifiers could not be resolved")
        graph["limitations"].extend("Incomplete coverage: " + reason for reason in reasons)
        for saved in graphs:
            for key, node in saved["nodes"].items():
                merged = graph["nodes"].setdefault(key, deepcopy(node))
                for source in node["sources"]:
                    if source not in merged["sources"]:
                        merged["sources"].append(source)
        if progress:
            progress(f"Building catalog from {len(graphs)} searches and {len(graph['nodes'])} provider records")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            collections.atomic_json(output, "neighborhood.json", graph)
            report = citation_neighborhood.publish_metadata(output, project_scope=True)
            if progress:
                progress("Validating and installing the catalog")
            from research_library import catalog
            if current_store:
                with literature_update.writer_lock(current_store):
                    if project_current_store.membership_revision(current_store) != revision:
                        raise ValueError("Project changed while building the catalog; retry")
                    if project_index._state(
                            root, current_state=project_current_store.read(current_store),
                            current_store=current_store) != discovery_signature:
                        raise ValueError("Discovery data changed while building the catalog; retry")
                    release_id = catalog.install(current_store, output / "catalog-001.sqlite")
                    now = datetime.now(timezone.utc)
                    entry = {
                        "id": "metadata", "kind": "catalog", "title": project["name"] + " publication metadata",
                        "version": release_id[:16], "updated_at": now.date().isoformat(),
                        "coverage": "Saved provider-indexed references and citations; coverage may be incomplete.",
                        "release_id": release_id, "publications": {},
                    }
                    if progress:
                        progress("Activating the updated metadata catalog")
                    collections.atomic_json(Path(current_store), "metadata-current.json", entry)
                return {"searches": len(graphs), "catalog_id": report["catalog_id"]}
            release_id = catalog.install(root / "registry", output / "catalog-001.sqlite")
        document = deepcopy(current.document)
        document["collections"] = [entry for entry in document["collections"] if entry["id"] != "metadata"]
        now = datetime.now(timezone.utc)
        document["collections"].append({
            "id": "metadata", "kind": "catalog", "title": project["name"] + " publication metadata",
            "version": release_id[:16], "updated_at": now.date().isoformat(),
            "coverage": "Saved provider-indexed references and citations; coverage may be incomplete.",
            "release_id": release_id, "publications": {},
        })
        if progress:
            progress("Activating the updated metadata catalog")
        registry.publish(document)
        return {"searches": len(graphs), "catalog_id": report["catalog_id"]}


def write_text_views(root, bundle):
    root.mkdir()
    grouped = defaultdict(list)
    for unit in bundle["units"]:
        grouped[unit["citation"]["source_id"]].append(unit)
    known = literature_text.evidence(bundle["units"])
    index = ["# Project paper text\n\n",
             "Formatting-only views; cite the original PDF page and source hash. Do not edit.\n\n"]
    for source in bundle["sources"]:
        units = grouped[source["id"]]
        if not units:
            continue
        name = literature_text.slug(source)
        folder = root / name
        folder.mkdir()
        (folder / "text.md").write_text(literature_text.render(source, units, {}, known), encoding="utf-8")
        title = (source.get("bibliography") or {}).get("title") or source["doi"]
        index.append(f"- [{literature_text.escape_markdown(title)}]({name}/text.md)\n")
    (root / "index.md").write_text("".join(index), encoding="utf-8")


def validated_papers(rows, existing=None):
    papers = dict(existing or {})
    added = []
    for row in rows:
        doi = citation_neighborhood.doi(row["doi"])
        title = row["title"].strip()
        if not title or len(title) > 1024:
            raise ValueError("Each paper needs a nonempty title of at most 1024 characters")
        if doi in papers:
            raise ValueError("Duplicate core paper DOI: " + doi)
        papers[doi] = {"title": title}
        added.append(doi)
    if not added:
        raise ValueError("No papers to register")
    return papers, added


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        return read_csv_stream(stream)


def read_csv_bytes(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV must be UTF-8 encoded") from error
    return read_csv_stream(io.StringIO(text, newline=""))


def read_csv_stream(stream):
    reader = csv.DictReader(stream)
    if not reader.fieldnames or not {"doi", "title"} <= set(reader.fieldnames):
        raise ValueError("CSV must have doi and title columns")
    return list(reader)


def read_manifest(path):
    return read_manifest_data(literature_ingest.read(path))


def read_manifest_bytes(data):
    return read_manifest_data(releases.read_json(data))


def read_manifest_data(document):
    if (not isinstance(document, dict) or set(document) != {"schema_version", "papers"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["papers"], list)):
        raise ValueError("Invalid core paper manifest")
    if any(not isinstance(row, dict) or set(row) != {"doi", "title"} for row in document["papers"]):
        raise ValueError("Manifest papers require only doi and title")
    return document["papers"]


def export_manifest(root, path):
    with literature_update.writer_lock(root):
        project = load(root)
        document = {"schema_version": 1, "papers": [
            {"doi": doi, "title": item["title"]} for doi, item in sorted(project["papers"].items())
        ]}
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise ValueError("Manifest destination already exists")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return len(document["papers"])


def initialize(root, name, authorization, rows=None):
    from . import project_preservation
    root = Path(root)
    if root.exists() or root.is_symlink():
        raise ValueError("Project directory already exists; choose a new directory")
    if not name.strip() or len(name) > 256 or not authorization.strip():
        raise ValueError("Project name and private-use authorization are required")
    papers = validated_papers(rows)[0] if rows is not None else {}
    with literature_update.writer_lock(root):
        if root.exists() or root.is_symlink():
            raise ValueError("Project directory already exists; choose a new directory")
        root.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="." + root.name + "-init-", dir=root.parent))
        try:
            collections.atomic_json(stage, "project.json", {
                "schema_version": 1, "name": name.strip(), "authorization": authorization.strip(),
                "papers": papers, "git_markdown_tracking": False})
            project_preservation.restore(stage, papers, preserved_for=root)
            stage.rename(root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def check_reset_root(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Project root is missing or is a symlink")
    resolved = root.resolve()
    if resolved != root.absolute():
        raise ValueError("Refusing to reset a project root containing a symlinked path")
    cwd = Path.cwd().resolve()
    if resolved == Path(resolved.anchor) or resolved == cwd or cwd.is_relative_to(resolved):
        raise ValueError("Refusing to reset a filesystem root or an ancestor of the working directory")


def reset(root):
    root = Path(root)
    check_reset_root(root)
    load(root)
    shutil.rmtree(root)


def register(root, rows):
    from . import project_preservation
    with literature_update.writer_lock(root):
        project = load(root)
        project["papers"], added = validated_papers(rows, project["papers"])
        project_preservation.restore(root, added)
        collections.atomic_json(Path(root), "project.json", project)
        return added


def set_git_markdown_tracking(root, enabled):
    from . import project_text
    root = Path(root)
    if type(enabled) is not bool:
        raise ValueError("Git Markdown tracking must be enabled or disabled")
    with literature_update.writer_lock(root):
        project = load(root)
        if project.get("git_markdown_tracking", False) == enabled:
            return {"enabled": enabled, "files": 0}
        project["git_markdown_tracking"] = enabled
        if enabled:
            count = project_text.backfill_git_markdown(root)
        else:
            count = 0
        collections.atomic_json(root, "project.json", project)
        return {"enabled": enabled, "files": count}


def attach(root, doi, pdf, *, identity_confirmed, license=None, title=None, expected_sha256=None, extract=True,
           current_store=None, membership_revision=None, markdown_repository=None):
    if not identity_confirmed:
        raise ValueError("Confirm that the PDF matches the registered title and DOI")
    root = Path(root)
    doi = citation_neighborhood.doi(doi)
    with literature_update.writer_lock(root):
        from . import project_text
        if current_store:
            from . import project_current_store
            project, revision = project_current_store.project_view(current_store)
            if revision != membership_revision:
                raise ValueError("Paper membership changed; reload before attaching")
            if project.get("git_markdown_tracking", False) and not markdown_repository:
                raise ValueError("Git Markdown attachment requires the authorized Markdown repository")
            if not project.get("git_markdown_tracking", False) and markdown_repository:
                raise ValueError("Git Markdown tracking is not enabled for this current store")
            paper = current_library_paper(root, current_store, doi, title, membership_revision)
            state = project_current_store.read(current_store)
            sources = state["editorial"]["bundle"]["sources"]
            registry = current = None
        else:
            project = load(root)
            paper = library_paper(root, doi, title)
            registry = project_text.editorial_store(root)
            current = registry.current() if (registry.root / "active.json").exists() else None
            sources = current.snapshots["core"]._bundle["sources"] if current else []
        if any(s["doi"].lower() == doi for s in sources):
            raise ValueError("This DOI already has an attached source; replacement requires a separate workflow")
        if Path(pdf).stat().st_size > provided_sources.MAX_BYTES:
            raise ValueError("The PDF exceeds the 64 MiB limit")
        data = Path(pdf).read_bytes()
        provided_sources.check_pdf(data)
        digest = releases.sha256(data)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("PDF changed after confirmation; inspect and confirm it again")
        if any(s["sha256"] == digest for s in sources):
            raise ValueError("This PDF is already associated with another paper")
        license = provided_sources.clean_license(license)
        papers = root / "papers"
        papers.mkdir(parents=True, exist_ok=True)
        target = papers / (digest + ".pdf")
        if target.exists() and releases.sha256(target.read_bytes()) != digest:
            raise ValueError("Stored PDF checksum differs")
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(dir=papers, prefix=".pdf-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "publication_id": "doi:" + doi, "title": paper["title"],
            "publication_date": "", "sha256": digest, "bytes": len(data), "filename": target.name,
            "method": "upload", "url": None, "final_url": None, "received_at": now,
            "license": license, "license_source": "operator" if license else None,
        }
        source = provided_sources.source_for(item, project["authorization"])
        if extract:
            result = literature_ingest.extract_pdf(target, source)
        else:
            import pymupdf
            with pymupdf.open(target) as document:
                if document.is_encrypted or not 1 <= len(document) <= literature_ingest.POLICY["max_pages"]:
                    raise ValueError("PDF is encrypted, empty or exceeds the page limit")
                result = {"status": "not_extracted",
                          "pages": [{"pdf_page": number, "text": ""} for number in range(1, len(document) + 1)]}
        result.update(publication_id=item["publication_id"], source_sha256=digest,
                      policy=literature_ingest.POLICY, extracted_at=now)
        runs = root / "runs"
        runs.mkdir(exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="import-", dir=runs))
        activated = False
        try:
            if extract:
                with tempfile.TemporaryDirectory(prefix="extraction-", dir=run) as workspace:
                    built = literature_update.source_bundle(
                        Path(workspace), papers, item["publication_id"], source, result)
            else:
                built = project_text.attached_bundle(source, result)
            if built["bundle"] is None:
                built = project_text.attached_bundle(source, result)
            else:
                caveat_updates = {
                    literature_ingest.EXTRACTION_CHECKPOINT_CAVEAT:
                        "Paragraph layout is machine-inferred from PDF text blocks; inspect the original PDF.",
                    literature_ingest.OCR_CHECKPOINT_CAVEAT:
                        "Separate local OCR; not human verified. Inspect the original PDF.",
                }
                for item_with_caveats in [built["bundle"], *built["bundle"]["units"], *built["bundle"]["tables"]]:
                    item_with_caveats["caveats"] = [
                        caveat_updates.get(caveat, caveat)
                        for caveat in item_with_caveats["caveats"]]
            present = {unit["citation"]["locator"]["pdf_page"] for unit in built["bundle"]["units"]}
            missing = project_text.attached_bundle(source, result)
            for unit in missing["bundle"]["units"]:
                unit["citation"]["provenance"] = built["bundle"]["sources"][0]["provenance"]
            built["bundle"]["units"].extend(unit for unit in missing["bundle"]["units"]
                                            if unit["citation"]["locator"]["pdf_page"] not in present)
            identity_note = " Operator confirmed the registered title and DOI; identity not independently verified."
            for entry in built["bundle"]["sources"]:
                entry["provenance"] += identity_note
            for unit in built["bundle"]["units"]:
                unit["citation"]["provenance"] += identity_note
                if built["ocr"] and (built["ocr"]["unresolved_pages"] or built["ocr"]["low_confidence_pages"]):
                    unit["caveats"].append(
                        "needs_visual_review: OCR contains unresolved or low-confidence pages; inspect the original PDF.")
            if current_store:
                source_id = built["bundle"]["sources"][0]["id"]
                editorial = project_current_store._paper_snapshot({
                    "bundle": built["bundle"], "authorization": built["grants"], "chunk_plans": {},
                }, source_id)
                added = (project_current_store.add_paper_with_git_markdown(
                    current_store, markdown_repository, editorial=editorial, expected=membership_revision)
                    if markdown_repository else
                    project_current_store.add_paper(current_store, editorial=editorial, expected=membership_revision))
                return {"doi": doi, "source_id": source_id, "revision": added["revision"],
                        "pages": len(result["pages"]), "extraction": result["status"], "ocr": built["ocr"],
                        "text_status": "draft", "retrievable": False,
                        "identity": "operator-confirmed; not independently verified"}
            project_text.ensure_editorial(root)
            registry = project_text.editorial_store(root)
            current = registry.current()
            if current:
                parent = current.snapshots["core"]
                bundle = deepcopy(parent._bundle)
                grants = deepcopy(parent._manifest["authorization"])
                for section in releases.SECTIONS:
                    bundle[section].extend(built["bundle"][section])
                grants.update(built["grants"])
            else:
                bundle, grants = built["bundle"], built["grants"]
            releases.validate_bundle(bundle)
            source_id = built["bundle"]["sources"][0]["id"]
            write_text_views(run / "text", project_text.source_bundle(bundle, source_id))
            destination = run / "evidence"
            evidence_id = releases.export(bundle, destination, profile="private_context", authorization=grants,
                                         chunk_plans=parent.chunk_plans if current else None)
            installed = registry.releases.install(destination)
            version = "core-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            document = {"schema_version": 1, "collections": [
                *([deepcopy(entry) for entry in current.document["collections"] if entry["id"] != "core"]
                  if current else []), {
                "id": "core", "title": project["name"], "version": version,
                "updated_at": now[:10], "coverage": "Privately attached project papers, including core and discovered papers; not exhaustively reviewed.",
                "release_id": installed, "publications": {},
            }]}
            collections.atomic_json(run, "import.json", {
                "schema_version": 1, "source_id": source_id, "doi": doi,
                "source_sha256": digest, "release": evidence_id,
            })
            snapshot = registry.publish(document)
            activated = True
            shutil.rmtree(destination)
            project_text.export_git_markdown(root, bundle, source_id=source_id,
                                             kind="extracted", release_id=installed)
            return {"doi": doi, "snapshot": snapshot, "release": evidence_id, "pages": len(result["pages"]),
                    "extraction": result["status"], "ocr": built["ocr"],
                    "text_status": "draft", "retrievable": False,
                    "text_views": str(run / "text" / "index.md"),
                    "identity": "operator-confirmed; not independently verified"}
        finally:
            if not activated:
                shutil.rmtree(run)


def status(root):
    from . import project_text
    project = load(root)
    registry = store(root)
    published = registry.current() if (Path(root) / "registry/active.json").exists() else None
    workspace = project_text.editorial_store(root)
    current = workspace.current() if (workspace.root / "active.json").exists() else None
    bundle = current.snapshots["core"]._bundle if current else {"sources": [], "units": []}
    published_bundle = published.snapshots["core"]._bundle if published else project_text.empty_bundle()
    sources = {s["doi"].lower(): s for s in bundle["sources"]}
    def state(doi):
        if doi not in sources:
            return "awaiting_pdf"
        return project_text.publication_state(bundle, published_bundle, sources[doi]["id"])
    return {"name": project["name"], "snapshot": published.release_id if published else None,
            "editorial_snapshot": current.release_id if current else None,
            "papers": [{"doi": doi, "title": (sources[doi].get("bibliography", {}).get("title") or row["title"])
                        if doi in sources else row["title"],
                        "status": state(doi)}
                       for doi, row in sorted(project["papers"].items())],
            "discovered": [{"doi": doi, "title": source.get("bibliography", {}).get("title") or doi,
                            "status": state(doi)}
                           for doi, source in sorted(sources.items()) if doi not in project["papers"]]}


def export_citations(output):
    output = Path(output)
    graph = literature_ingest.read(output / "neighborhood.json")
    target = output / "citation-edges.csv"
    if target.exists() or target.is_symlink():
        raise ValueError("Citation CSV already exists; preserve the existing export")
    fields = ["seed_doi", "direction", "provider_record", "title", "doi", "publication_date",
              "authors", "unstructured_reference", "edge_sources"]
    descriptor, temporary = tempfile.mkstemp(dir=output, prefix=".citations-")
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for edge in graph["edges"]:
                node = graph["nodes"][edge["target_provider_record"]]
                record = node["record"]
                row = {
                    "seed_doi": edge["seed_doi"], "direction": edge["direction"],
                    "provider_record": edge["target_provider_record"], "title": record["title"],
                    "doi": record["identifiers"].get("doi", ""), "publication_date": record["publication_date"],
                    "authors": "; ".join(record["authors"]),
                    "unstructured_reference": node.get("reference", {}).get("unstructured", ""),
                    "edge_sources": json.dumps(edge["sources"], ensure_ascii=False),
                }
                writer.writerow({key: "'" + value if value.lstrip().startswith(("=", "+", "-", "@"))
                                 or value.startswith(("\t", "\r"))
                                 else value for key, value in row.items()})
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def discover(root, output, *, directions=None, current_store=None, membership_revision=None):
    with literature_update.writer_lock(root):
        if current_store:
            from . import project_current_store
            project, revision = project_current_store.project_view(current_store)
            if revision != membership_revision:
                raise ValueError("Core-paper selection changed; reload before starting discovery")
        else:
            project = load(root)
        rows = [{"number": str(index), "doi": doi, "title": paper["title"], "filename": ""}
                for index, (doi, paper) in enumerate(sorted(project["papers"].items()), 1)]
    if not rows:
        raise ValueError("Register at least one core paper before discovery")
    output = Path(output)
    if output.is_symlink() or (output / "neighborhood.json").exists():
        raise ValueError("Discovery output exists or is a symlink; choose a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "seeds.csv"
    if manifest.exists():
        with manifest.open(newline="", encoding="utf-8") as stream:
            if list(csv.DictReader(stream)) != rows:
                raise ValueError("Cached discovery belongs to a different seed list; use a new output directory")
    else:
        with manifest.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["number", "doi", "title", "filename"])
            writer.writeheader()
            writer.writerows(rows)
    client_options = metadata_client_options(root)
    with closing(literature.MetadataClient(**client_options)) as client:
        graph = citation_neighborhood.discover(
            output, manifest, citation_neighborhood.CachedAPI(output / "responses", client), project_scope=True,
            **({"directions": directions} if directions is not None else {}))
    from . import project_purge
    if not current_store:
        from . import project_preservation
        with literature_update.writer_lock(root):
            project_preservation.restore(root, {
                node["record"]["identifiers"]["doi"] for node in graph["nodes"].values()
                if node["record"]["identifiers"].get("doi")})
    project_purge.discovery_index(output, graph)
    csv_path = export_citations(output)
    failures = sum(any(provider["status"] not in {"complete_indexed_neighborhood", "complete_deposited_references"}
                       for provider in seed["providers"].values()) for seed in graph["seeds"])
    return {"seeds": len(graph["seeds"]), "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
            "unresolved": len(citation_neighborhood.unresolved_references(graph)),
            "seeds_with_provider_gaps": failures,
            "broken_reference_links": sum(item.get("resolution_status") == "not_found"
                                          for item in graph["unresolved"]),
            "output": str(output), "citation_edges": str(csv_path), "limitations": graph["limitations"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("releases/project"))
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--name", required=True)
    init.add_argument("--authorization", required=True, help="Basis for private text extraction and retrieval")
    input_list = init.add_mutually_exclusive_group()
    input_list.add_argument("--papers", type=Path, help="Initial CSV with doi and title columns")
    input_list.add_argument("--manifest", type=Path, help="Initial exported JSON core paper manifest")
    add = commands.add_parser("add")
    add.add_argument("--doi", required=True)
    add.add_argument("--title", required=True)
    batch = commands.add_parser("import-csv")
    batch.add_argument("file", type=Path, help="CSV with doi and title columns")
    pdf = commands.add_parser("attach-pdf")
    pdf.add_argument("--doi", required=True)
    pdf.add_argument("--pdf", type=Path, required=True)
    pdf.add_argument("--confirm-identity", action="store_true", help="Confirm PDF matches the registered DOI and title")
    pdf.add_argument("--license")
    pdf.add_argument("--no-extract", action="store_true", help="Attach the PDF with empty page drafts for manual text import")
    migration = commands.add_parser("migrate-text-workflow", help="Separate editorial drafts from retrieval")
    migration.add_argument("--withdraw-existing", action="store_true",
                           help="Withdraw active text pending admin publication; historical snapshots remain")
    text = commands.add_parser("import-text", help="Save a UTF-8 page-marked clear-text draft")
    text.add_argument("--doi", required=True)
    text.add_argument("--file", type=Path, required=True)
    text.add_argument("--reviewer", required=True)
    text.add_argument("--reason", required=True)
    export_text = commands.add_parser("export-text", help="Export a page-marked UTF-8 draft for external editing")
    export_text.add_argument("--doi", required=True)
    export_text.add_argument("--file", type=Path, required=True)
    publish = commands.add_parser("publish-text", help="Explicitly publish admin-checked clear text for RAG")
    publish.add_argument("--doi", required=True)
    publish.add_argument("--snapshot-id", required=True, help="Editorial snapshot from status")
    publish.add_argument("--reviewer", required=True)
    publish.add_argument("--reason", required=True)
    publish.add_argument("--confirm-checked", action="store_true")
    commands.add_parser("status")
    commands.add_parser("retention-preview", help="Inspect inactive project releases without deleting them")
    retention = commands.add_parser("retention-prune",
                                    help="Remove inactive releases after verifying a current-state backup")
    retention.add_argument("--backup", required=True, type=Path,
                           help="New private per-paper backup directory outside the project")
    retention.add_argument("--confirm-readers-stopped", action="store_true",
                           help="Confirm website and all MCP readers have been stopped")
    full_backup = commands.add_parser("backup-project",
                                      help="Create a verified private copy of project and PDF directories")
    full_backup.add_argument("directory", type=Path, help="New backup directory")
    full_backup.add_argument("--pdf-data-root", required=True, type=Path, help="Shared PDF directory to copy")
    full_backup.add_argument("--current-state", type=Path, help="Optional separate current-state directory")
    full_backup.add_argument("--confirm-writers-stopped", action="store_true",
                             help="Confirm website, MCP and all project and PDF writers have stopped")
    backup_verify = commands.add_parser("verify-project-backup", help="Verify a private project backup")
    backup_verify.add_argument("directory", type=Path)
    backup_restore = commands.add_parser("restore-project-backup",
                                         help="Restore a verified backup into a new directory")
    backup_restore.add_argument("directory", type=Path, help="Existing backup directory")
    backup_restore.add_argument("--destination", required=True, type=Path, help="New parent directory")
    preserved_start = commands.add_parser(
        "start-from-preserved", help="Create a separate current-state project with drafts from a verified backup")
    preserved_start.add_argument("backup", type=Path, help="Verified backup that includes a current-state store")
    preserved_start.add_argument("--destination", required=True, type=Path, help="New parent directory")
    preserved_start.add_argument("--name", required=True)
    preserved_start.add_argument("--authorization", required=True)
    preserved_inputs = preserved_start.add_mutually_exclusive_group(required=True)
    preserved_inputs.add_argument("--papers", type=Path, help="Core papers CSV")
    preserved_inputs.add_argument("--manifest", type=Path, help="Core papers JSON manifest")
    current_export = commands.add_parser("export-current-state",
                                         help="Export private current text and RAG state without PDFs")
    current_export.add_argument("file", type=Path, help="New private JSON file; must not already exist")
    paper_export = commands.add_parser("export-paper-state",
                                       help="Export private current state as separate per-paper files")
    paper_export.add_argument("directory", type=Path, help="New private directory; must not already exist")
    current_restore = commands.add_parser("restore-current-state",
                                          help="Restore current text and RAG state into a new project")
    current_restore.add_argument("file", type=Path, help="Current-state JSON file or per-paper directory")
    current_prepare = commands.add_parser("prepare-current-store",
                                          help="Create and recovery-test an experimental per-paper store")
    current_prepare.add_argument("directory", type=Path, help="New private directory outside the project")
    current_prepare.add_argument("--without-git-markdown", action="store_true",
                                 help="Disable Git Markdown tracking only in the separate experimental copy")
    current_prepare.add_argument("--quality-root", type=Path,
                                 help="Existing quality directory (default: project/releases/data-quality)")
    current_prepare.add_argument("--confirm-quality-writers-stopped", action="store_true",
                                 help="Confirm all writers of existing quality records are stopped")
    current_compare = commands.add_parser(
        "compare-current-store", help="Compare current project paper state with a separate current-state store")
    current_compare.add_argument("directory", type=Path, help="Existing current-state store")
    current_compare.add_argument("--quality-root", type=Path,
                                 help="Normal-project quality directory, if not at the default path")
    current_compare.add_argument("--markdown-repository", type=Path,
                                 help="Compare current generated views against this existing Git repository")
    store_restore = commands.add_parser("restore-current-store",
                                        help="Restore an experimental per-paper store into a new project")
    store_restore.add_argument("directory", type=Path, help="Experimental per-paper store directory")
    store_recover = commands.add_parser("recover-current-store",
                                        help="Recover an interrupted experimental paper membership update")
    store_recover.add_argument("directory", type=Path, help="Experimental per-paper store directory")
    markdown_recover = commands.add_parser("recover-current-markdown",
                                          help="Recover an interrupted current-store Markdown refresh")
    markdown_recover.add_argument("directory", type=Path, help="Experimental per-paper store directory")
    markdown_recover.add_argument("--repository", required=True, type=Path,
                                  help="Original authorized Git repository used for the refresh")
    soft = commands.add_parser("soft-reset", help="Preserve all material and return to Core papers setup")
    soft.add_argument("--confirm", action="store_true", help="Confirm restarting the active project without deletion")
    manifest = commands.add_parser("export-manifest", help="Export DOI/title metadata only")
    manifest.add_argument("file", type=Path)
    discovery = commands.add_parser("discover")
    discovery.add_argument("--output", type=Path, required=True)
    export = commands.add_parser("export-citations")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            rows = read_csv(args.papers) if args.papers else read_manifest(args.manifest) if args.manifest else None
            initialize(args.root, args.name, args.authorization, rows)
            result = {"project": str(args.root), "status": "initialized", "papers": len(rows) if rows else 0}
        elif args.command == "add":
            result = {"added": register(args.root, [{"doi": args.doi, "title": args.title}])}
        elif args.command == "import-csv":
            result = {"added": register(args.root, read_csv(args.file))}
        elif args.command == "attach-pdf":
            result = attach(args.root, args.doi, args.pdf, identity_confirmed=args.confirm_identity,
                            license=args.license, extract=not args.no_extract)
        elif args.command == "status":
            result = status(args.root)
        elif args.command == "retention-preview":
            from . import project_retention
            result = project_retention.preview(args.root)
        elif args.command == "retention-prune":
            from . import project_retention
            result = project_retention.prune(args.root, args.backup,
                                             readers_stopped=args.confirm_readers_stopped)
        elif args.command == "backup-project":
            from . import project_backup
            result = project_backup.create(args.root, args.pdf_data_root, args.directory,
                                           current_state=args.current_state,
                                           writers_stopped=args.confirm_writers_stopped)
        elif args.command == "verify-project-backup":
            from . import project_backup
            result = project_backup.verify(args.directory)
        elif args.command == "restore-project-backup":
            from . import project_backup
            result = project_backup.restore(args.directory, args.destination)
        elif args.command == "start-from-preserved":
            from . import project_backup
            rows = read_csv(args.papers) if args.papers else read_manifest(args.manifest)
            result = project_backup.start_from_preserved(
                args.backup, args.destination, name=args.name, authorization=args.authorization, rows=rows)
        elif args.command == "export-current-state":
            from . import project_state
            result = project_state.export(args.root, args.file)
        elif args.command == "export-paper-state":
            from . import project_state
            result = project_state.export_papers(args.root, args.directory)
        elif args.command == "restore-current-state":
            from . import project_state
            result = project_state.restore(args.file, args.root)
        elif args.command == "prepare-current-store":
            from . import project_current_store
            result = project_current_store.prepare(
                args.root, args.directory, without_git_markdown=args.without_git_markdown,
                quality_root=args.quality_root, confirm_quality_writers_stopped=args.confirm_quality_writers_stopped)
        elif args.command == "compare-current-store":
            from . import project_cutover
            result = project_cutover.compare(args.root, args.directory, quality_root=args.quality_root,
                                             markdown_repository=args.markdown_repository)
        elif args.command == "restore-current-store":
            from . import project_current_store
            result = project_current_store.restore_project(args.directory, args.root)
        elif args.command == "recover-current-store":
            from . import project_current_store
            result = {"directory": str(args.directory), "recovered": project_current_store.recover_membership(
                args.directory)}
        elif args.command == "recover-current-markdown":
            from . import project_current_store
            result = project_current_store.recover_git_markdown(args.directory, args.repository)
        elif args.command == "soft-reset":
            from . import project_preservation
            if not args.confirm:
                raise ValueError("Use --confirm to restart the project while preserving its material")
            result = project_preservation.soft_reset(args.root)
        elif args.command == "migrate-text-workflow":
            from . import project_text
            result = project_text.migrate(args.root, withdraw=args.withdraw_existing)
        elif args.command in {"publish-text", "import-text", "export-text"}:
            from . import project_text, project_edit
            current = project_text.editorial_store(args.root).current()
            bundle = current.snapshots["core"]._bundle
            source = next((s for s in bundle["sources"] if s["doi"] == citation_neighborhood.doi(args.doi)), None)
            if source is None:
                raise ValueError("Attach the source PDF before providing clear text")
            if args.command == "export-text":
                units = project_edit.document_units(bundle, source["id"])
                with args.file.open("x", encoding="utf-8") as stream:
                    stream.write(project_edit.document_draft(units))
                result = {"file": str(args.file), "editorial_snapshot": current.release_id, "status": "draft"}
            elif args.command == "publish-text":
                result = project_text.publish(args.root, snapshot_id=args.snapshot_id, source_id=source["id"],
                    reviewer=args.reviewer, reason=args.reason, checked=args.confirm_checked)
            else:
                units = project_edit.document_units(bundle, source["id"])
                if not units:
                    raise ValueError("No retained pages; add missing pages before importing text")
                tables = []
                for table in bundle["tables"]:
                    if table["citation"]["source_id"] == source["id"]:
                        page = next(u for u in units if u["citation"]["locator"] == table["citation"]["locator"])
                        tables.append({"id": table["id"], "unit_id": page["id"],
                                       **{key: table[key] for key in project_edit.TABLE_FIELDS}})
                try:
                    text = args.file.read_text(encoding="utf-8-sig")
                except UnicodeDecodeError as error:
                    raise ValueError("Clear text must be UTF-8 encoded") from error
                result = project_edit.correct(args.root, snapshot_id=current.release_id, source_id=source["id"],
                    unit_id=units[0]["id"], text=text, whole_document=True,
                    bibliography={k: v or "" for k, v in source["bibliography"].items()},
                    source_url=source["url"] or "", tables=tables, reviewer=args.reviewer,
                    reason=args.reason, checked=False)
        elif args.command == "export-manifest":
            result = {"manifest": str(args.file), "papers": export_manifest(args.root, args.file)}
        elif args.command == "discover":
            result = discover(args.root, args.output)
        else:
            result = {"citation_edges": str(export_citations(args.output))}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, releases.ReleaseError) as error:
        print(f"Project operation failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
