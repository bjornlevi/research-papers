"""Initialize and populate an independent private core evidence collection."""

import argparse
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from istopmm_evidence import collections, literature, releases
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


def write_text_views(root, bundle):
    root.mkdir()
    grouped = defaultdict(list)
    for unit in bundle["units"]:
        grouped[unit["citation"]["source_id"]].append(unit)
    known = literature_text.evidence(bundle["units"])
    index = ["# Core paper text\n\n",
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
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"doi", "title"} <= set(reader.fieldnames):
            raise ValueError("CSV must have doi and title columns")
        return list(reader)


def read_manifest(path):
    document = literature_ingest.read(path)
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


def register(root, rows):
    with literature_update.writer_lock(root):
        project = load(root)
        project["papers"], added = validated_papers(rows, project["papers"])
        collections.atomic_json(Path(root), "project.json", project)
        return added


def attach(root, doi, pdf, *, identity_confirmed, license=None):
    if not identity_confirmed:
        raise ValueError("Confirm that the PDF matches the registered title and DOI")
    root = Path(root)
    doi = citation_neighborhood.doi(doi)
    with literature_update.writer_lock(root):
        project = load(root)
        if doi not in project["papers"]:
            raise ValueError("Register the paper before providing its PDF")
        registry = store(root)
        current = registry.current() if (root / "registry/active.json").exists() else None
        if current and any(s["doi"].lower() == doi for s in current.snapshots["core"]._bundle["sources"]):
            raise ValueError("This DOI already has core evidence; replacement requires a separate workflow")
        if Path(pdf).stat().st_size > provided_sources.MAX_BYTES:
            raise ValueError("The PDF exceeds the 64 MiB limit")
        data = Path(pdf).read_bytes()
        provided_sources.check_pdf(data)
        digest = releases.sha256(data)
        if current and any(s["sha256"] == digest for s in current.snapshots["core"]._bundle["sources"]):
            raise ValueError("This PDF is already associated with another core paper")
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
            "publication_id": "doi:" + doi, "title": project["papers"][doi]["title"],
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
        document = {"schema_version": 1, "collections": [{
            "id": "core", "title": project["name"], "version": version,
            "updated_at": now[:10], "coverage": "Operator-selected core papers; not exhaustively reviewed.",
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
            "papers": [{"doi": doi, "title": row["title"],
                        "status": state(doi)}
                       for doi, row in sorted(project["papers"].items())]}


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


def discover(root, output):
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
    with closing(literature.MetadataClient()) as client:
        graph = citation_neighborhood.discover(
            output, manifest, citation_neighborhood.CachedAPI(output / "responses", client), project_scope=True)
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
