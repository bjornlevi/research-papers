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
    if set(document) != {"schema_version", "name", "authorization", "papers"} or document["schema_version"] != 1:
        raise ValueError("Invalid project configuration")
    return document


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


def save_named_pdf(pdf_data_root, data, digest, title):
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")[:140].rstrip("-") or "paper"
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


def find_pdf(root, doi, pdf_data_root, *, title=None):
    import httpx
    root, pdf_data_root = Path(root).absolute(), Path(pdf_data_root).absolute()
    doi = citation_neighborhood.doi(doi)
    paper = library_paper(root, doi, title)
    if pdf_data_root.resolve() != pdf_data_root:
        raise ValueError("PDF data folder cannot contain a symlinked path")
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
    with literature_update.writer_lock(root):
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


def configure_metadata(root, *, progress=None):
    root = Path(root)
    if progress:
        progress("Reading saved discovery searches")
    with literature_update.writer_lock(root):
        project = load(root)
        registry = store(root)
        if not (root / "registry/active.json").exists():
            raise ValueError("Attach at least one project PDF before configuring the metadata catalog")
        current = registry.current()
        discovery = root / "discovery"
        if discovery.resolve() != discovery.absolute():
            raise ValueError("Discovery directory cannot contain a symlinked path")
        latest = {}
        for folder in sorted(discovery.iterdir()) if discovery.exists() else []:
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
        if graph["unresolved"]:
            reasons.append(f"{len(graph['unresolved'])} provider reference identifiers could not be resolved")
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
    root = Path(root)
    if root.exists() or root.is_symlink():
        raise ValueError("Project directory already exists; choose a new directory")
    if not name.strip() or len(name) > 256 or not authorization.strip():
        raise ValueError("Project name and private-use authorization are required")
    papers = validated_papers(rows)[0] if rows is not None else {}
    collections.atomic_json(root, "project.json", {
        "schema_version": 1, "name": name.strip(), "authorization": authorization.strip(), "papers": papers})


def reset(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Project root is missing or is a symlink")
    resolved = root.resolve()
    if resolved != root.absolute():
        raise ValueError("Refusing to reset a project root containing a symlinked path")
    cwd = Path.cwd().resolve()
    if resolved == Path(resolved.anchor) or resolved == cwd or cwd.is_relative_to(resolved):
        raise ValueError("Refusing to reset a filesystem root or an ancestor of the working directory")
    load(root)
    shutil.rmtree(root)


def register(root, rows):
    with literature_update.writer_lock(root):
        project = load(root)
        project["papers"], added = validated_papers(rows, project["papers"])
        collections.atomic_json(Path(root), "project.json", project)
        return added


def attach(root, doi, pdf, *, identity_confirmed, license=None, title=None, expected_sha256=None):
    if not identity_confirmed:
        raise ValueError("Confirm that the PDF matches the registered title and DOI")
    root = Path(root)
    doi = citation_neighborhood.doi(doi)
    with literature_update.writer_lock(root):
        project = load(root)
        paper = library_paper(root, doi, title)
        registry = store(root)
        current = registry.current() if (root / "registry/active.json").exists() else None
        if current and any(s["doi"].lower() == doi for s in current.snapshots["core"]._bundle["sources"]):
            raise ValueError("This DOI already has extracted evidence; replacement requires a separate workflow")
        if Path(pdf).stat().st_size > provided_sources.MAX_BYTES:
            raise ValueError("The PDF exceeds the 64 MiB limit")
        data = Path(pdf).read_bytes()
        provided_sources.check_pdf(data)
        digest = releases.sha256(data)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("PDF changed after confirmation; inspect and confirm it again")
        if current and any(s["sha256"] == digest for s in current.snapshots["core"]._bundle["sources"]):
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
        result = literature_ingest.extract_pdf(target, source)
        result.update(publication_id=item["publication_id"], source_sha256=digest,
                      policy=literature_ingest.POLICY, extracted_at=now)
        runs = root / "runs"
        runs.mkdir(exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="import-", dir=runs))
        built = literature_update.source_bundle(run / "source", papers,
                                                     item["publication_id"], source, result)
        if built["bundle"] is None:
            raise ValueError("No text extracted, including OCR; inspect the PDF")
        identity_note = " Operator confirmed the registered title and DOI; identity not independently verified."
        for entry in built["bundle"]["sources"]:
            entry["provenance"] += identity_note
        for unit in built["bundle"]["units"]:
            unit["citation"]["provenance"] += identity_note
            if built["ocr"] and (built["ocr"]["unresolved_pages"] or built["ocr"]["low_confidence_pages"]):
                unit["caveats"].append(
                    "needs_visual_review: OCR contains unresolved or low-confidence pages; inspect the original PDF.")
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
        write_text_views(run / "text", bundle)
        destination = run / "evidence"
        evidence_id = releases.export(bundle, destination, profile="private_context", authorization=grants)
        installed = registry.releases.install(destination)
        version = "core-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        document = {"schema_version": 1, "collections": [
            *([deepcopy(entry) for entry in current.document["collections"] if entry["id"] != "core"]
              if current else []), {
            "id": "core", "title": project["name"], "version": version,
            "updated_at": now[:10], "coverage": "Privately attached project papers, including core and discovered papers; not exhaustively reviewed.",
            "release_id": installed, "publications": {},
        }]}
        snapshot = registry.publish(document)
        return {"doi": doi, "snapshot": snapshot, "release": evidence_id, "pages": len(result["pages"]),
                "extraction": result["status"], "ocr": built["ocr"],
                "text_views": str(run / "text" / "index.md"),
                "identity": "operator-confirmed; not independently verified"}


def status(root):
    project = load(root)
    registry = store(root)
    current = registry.current() if (Path(root) / "registry/active.json").exists() else None
    bundle = current.snapshots["core"]._bundle if current else {"sources": [], "units": []}
    sources = {s["doi"].lower(): s for s in bundle["sources"]}
    def state(doi):
        if doi not in sources:
            return "awaiting_pdf"
        caveats = [caveat for unit in bundle["units"] if unit["citation"]["source_id"] == sources[doi]["id"]
                   for caveat in unit["caveats"]]
        if any("needs_visual_review" in caveat for caveat in caveats):
            return "needs_visual_review"
        if any(caveat.startswith("Separate local OCR") for caveat in caveats):
            return "extracted_with_ocr"
        return "extracted"
    return {"name": project["name"], "snapshot": current.release_id if current else None,
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


def discover(root, output, *, directions=None):
    with literature_update.writer_lock(root):
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
    project_purge.discovery_index(output, graph)
    csv_path = export_citations(output)
    failures = sum(any(provider["status"] not in {"complete_indexed_neighborhood", "complete_deposited_references"}
                       for provider in seed["providers"].values()) for seed in graph["seeds"])
    return {"seeds": len(graph["seeds"]), "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
            "unresolved": len(graph["unresolved"]), "seeds_with_provider_gaps": failures,
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
    commands.add_parser("status")
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
                            license=args.license)
        elif args.command == "status":
            result = status(args.root)
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
