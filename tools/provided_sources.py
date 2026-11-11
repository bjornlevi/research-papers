"""Operator-provided PDFs or links for listed papers that could not be downloaded."""

import argparse
import csv
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from urllib.parse import urljoin, urlsplit

import httpx
import pymupdf

from research_library import collections, literature, releases
from . import literature_ingest as ingest
from . import literature_update as update

AWAITING = {"no_explicitly_licensed_pdf", "licensed_locations_failed"}
MAX_BYTES = ingest.POLICY["max_pdf_bytes"]
MAX_REDIRECTS = 5
BASIS = ("Operator-provided private copy through the local source library on {date}: private local text "
         "extraction and source-linked retrieval only; no public redistribution or original serving; "
         "publisher-version equivalence and scientific review not implied.")
NOT_PDF = ("The link did not return a PDF. Publishers often show a login, cookie or viewer page instead. "
           "Download the PDF in your browser and upload the file.")


def root_of(data_root):
    return Path(data_root).absolute() / "releases/provided-sources"


def registry_of(data_root):
    return Path(data_root).absolute() / "releases/m9-platform"


def updates_of(data_root):
    return Path(data_root).absolute() / "releases/source-updates"


@contextmanager
def ledger_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".ledger.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_ledger(root):
    path = Path(root) / "ledger.json"
    if not path.exists():
        return {"schema_version": 1, "items": {}, "activations": []}
    return ingest.read(path)


def write_ledger(root, ledger):
    collections.atomic_json(Path(root), "ledger.json", ledger)


def listed(data_root, identifier):
    inventory = Path(data_root) / "releases/istopmm-first-tier-20261019/bibliography.csv"
    with inventory.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row["id"] == identifier:
                return row
    return None


def statuses(data_root):
    """Latest provided state per publication, for the library page."""
    root = root_of(data_root)
    if not (root / "ledger.json").exists():
        return {}
    result = {}
    for item in sorted(read_ledger(root)["items"].values(), key=lambda value: value["received_at"]):
        result[item["publication_id"]] = item
    return result


def public_address(host):
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError("The link's host name could not be resolved") from error
    if not infos or not all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos):
        raise ValueError("Links to local or private network addresses are not allowed")


def fetch(url, *, client=None, resolve=public_address):
    """Fetch a public http(s) link without following redirects to private networks."""
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60), follow_redirects=False, headers={
        "User-Agent": "research-library-provided-source/1 (operator-provided private copy)"})
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError("Provide an http(s) link without embedded credentials")
            resolve(parts.hostname)
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("The link redirected without a destination")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise ValueError(f"The link returned HTTP {response.status_code}. "
                                     "Download the PDF in your browser and upload the file.")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("The PDF exceeds the 64 MiB limit")
                return bytes(data), current
        raise ValueError("The link redirected too many times")
    except httpx.HTTPError as error:
        raise ValueError("The link could not be downloaded: " + type(error).__name__) from error
    finally:
        if owned:
            client.close()


def check_pdf(data):
    if len(data) > MAX_BYTES:
        raise ValueError("The PDF exceeds the 64 MiB limit")
    if not data.startswith(b"%PDF"):
        raise ValueError(NOT_PDF)
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.is_encrypted:
                raise ValueError("The PDF is password-protected")
            if not len(document) or len(document) > ingest.POLICY["max_pages"]:
                raise ValueError("The PDF has no pages or too many pages")
    except (RuntimeError, pymupdf.FileDataError) as error:
        raise ValueError("The file is not a readable PDF") from error


def clean_license(value):
    value = " ".join((value or "").split())
    if len(value) > 200:
        raise ValueError("The licence text is too long (200 characters at most)")
    return value or None


def lookup_license(doi, *, client=None):
    """Licence reported by OpenAlex for a DOI; None when none is reported or the lookup fails."""
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(20), headers={
        "User-Agent": "research-library-provided-source/1 (licence lookup)"})
    try:
        response = client.get("https://api.openalex.org/works/https://doi.org/" + doi,
                              params={"select": "best_oa_location,primary_location,locations"})
        if response.status_code == 404:
            return {"license": None, "license_source": "openalex", "license_checked_at": literature.utcnow()}
        response.raise_for_status()
        work = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owned:
            client.close()
    locations = [work.get("best_oa_location"), work.get("primary_location"), *(work.get("locations") or [])]
    found = next((location["license"] for location in locations if location and location.get("license")), None)
    return {"license": found, "license_source": "openalex", "license_checked_at": literature.utcnow()}


def resolve_licenses(root, lookup=None):
    """Look up licences for queued items without an operator-given licence (outside the writer locks)."""
    lookup = lookup or lookup_license
    with ledger_lock(root):
        todo = {key: item["publication_id"] for key, item in read_ledger(root)["items"].items()
                if item["status"] in {"received", "processing"} and not item.get("license_source")}
    found = {key: lookup(identifier.removeprefix("doi:")) for key, identifier in todo.items()}
    with ledger_lock(root):
        ledger = read_ledger(root)
        for key, result in found.items():
            if result and not ledger["items"][key].get("license_source"):
                ledger["items"][key].update(result)
        write_ledger(root, ledger)


def receive(data_root, identifier, data, *, method, url=None, final_url=None, filename=None, registry=None,
            license=None):
    """Validate and store a provided PDF; adding it to the library is a separate step.

    An operator-given licence is kept as is; otherwise the licence is looked up by DOI when the copy is added.
    """
    data_root = Path(data_root).absolute()
    license = clean_license(license)
    row = listed(data_root, identifier)
    if row is None:
        raise ValueError("Only papers listed in the source library can be provided")
    if not identifier.startswith("doi:"):
        raise ValueError("Only papers with a DOI can currently be added")
    if row["acquisition_status"] not in AWAITING:
        raise ValueError("This paper is already downloaded or in the original collection")
    current = update.store_at(Path(registry or registry_of(data_root))).current().snapshots["literature"]
    doi = identifier.removeprefix("doi:")
    if any(source["doi"].lower() == doi.lower() for source in current._bundle["sources"]):
        raise ValueError("This paper is already in the library")
    check_pdf(data)
    digest = releases.sha256(data)
    root = root_of(data_root)
    with ledger_lock(root):
        ledger = read_ledger(root)
        for item in ledger["items"].values():
            if item["publication_id"] == identifier and item["status"] in {"received", "processing", "added"}:
                return item
            if item["sha256"] == digest and item["publication_id"] != identifier:
                raise ValueError("This PDF was already provided for a different paper")
        papers = root / "papers"
        papers.mkdir(parents=True, exist_ok=True)
        target = papers / (digest + ".pdf")
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(dir=papers, prefix=".pdf-")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        key = identifier + "|" + digest
        item = {
            "publication_id": identifier, "title": row["title"], "publication_date": row["publication_date"],
            "sha256": digest, "bytes": len(data), "filename": target.name, "method": method,
            "url": url, "final_url": final_url, "original_filename": (filename or "")[:256] or None,
            "received_at": literature.utcnow(), "status": "received", "error": None,
            "license": license, "license_source": "operator" if license else None,
        }
        ledger["items"][key] = item
        write_ledger(root, ledger)
    return item


def source_for(item, basis):
    doi = item["publication_id"].removeprefix("doi:")
    location = {"kind": "user_provided", "method": "uploaded file" if item["method"] == "upload" else "link",
                "url": item["url"], "final_url": item["final_url"] or "https://doi.org/" + doi,
                "license": item.get("license"), "license_source": item.get("license_source")}
    return {
        "publication_id": item["publication_id"], "source_id": "literature:" + item["sha256"],
        "sha256": item["sha256"], "filename": item["filename"], "bytes": item["bytes"], "format": "pdf",
        "title": item["title"], "publication_date": item["publication_date"],
        "aliases": [item["publication_id"]], "types": [], "membership": {}, "curation_disease": None,
        "acquisition": {"acquisition": location, "acquired_at": item["received_at"]},
        "permissions": {"local_storage": True, "private_passages": True, "private_tables": True,
                        "public_remote_serving": False, "original_serving": False, "basis": basis},
    }


def pending(data_root):
    root = root_of(data_root)
    return [key for key, item in read_ledger(root)["items"].items()
            if item["status"] in {"received", "processing"}] if (root / "ledger.json").exists() else []


def fail_pending(data_root, error):
    """Mark queued items failed after an unexpected error so they are not retried forever."""
    root = root_of(data_root)
    with ledger_lock(root):
        ledger = read_ledger(root)
        for item in ledger["items"].values():
            if item["status"] in {"received", "processing"}:
                item.update(status="failed", error=error, processed_at=literature.utcnow())
        write_ledger(root, ledger)


def apply(data_root, registry=None, lookup=None):
    """Add received PDFs as a new validated literature snapshot; previous snapshots are retained."""
    data_root = Path(data_root).absolute()
    registry = Path(registry or registry_of(data_root))
    root = root_of(data_root)
    if not pending(data_root):
        return {"added": [], "failed": [], "waiting": 0, "snapshot": None}
    resolve_licenses(root, lookup)
    try:
        with update.writer_lock(updates_of(data_root)), update.writer_lock(registry):
            return apply_locked(data_root, registry, root)
    except ValueError as error:
        if str(error).startswith("Another update writer holds"):
            return {"added": [], "failed": [], "waiting": len(pending(data_root)), "snapshot": None}
        raise


def apply_locked(data_root, registry, root):
    with ledger_lock(root):
        ledger = read_ledger(root)
        keys = [key for key, item in ledger["items"].items() if item["status"] in {"received", "processing"}]
        for key in keys:
            ledger["items"][key].update(status="processing", error=None)
        write_ledger(root, ledger)
    store = update.store_at(registry)
    current = store.current()
    parent = current.snapshots["literature"]
    bundle = deepcopy(parent._bundle)
    grants = deepcopy(parent._manifest["authorization"])
    existing = {source["sha256"] for source in bundle["sources"]}
    name = "provided-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    folder = root / "runs" / collections.identifier(name)
    papers = root / "papers"
    basis = BASIS.format(date=literature.utcnow()[:10])
    outcomes = {}
    for key in keys:
        item = ledger["items"][key]
        try:
            source = source_for(item, basis)
            ingest.source_path(papers, source)
            if item["sha256"] in existing:
                raise ValueError("This exact PDF is already in the library")
            result = ingest.extract_pdf(papers / item["filename"], source)
            result.update(publication_id=item["publication_id"], source_sha256=item["sha256"],
                          policy=ingest.POLICY, extracted_at=literature.utcnow())
            built = update.source_bundle(folder / ("export-" + item["sha256"]), papers,
                                         item["publication_id"], source, result)
            if built["bundle"] is None:
                raise ValueError("No text could be extracted, even with OCR; the PDF may be image-only "
                                 "without recognizable text")
            for section in releases.SECTIONS:
                bundle[section].extend(built["bundle"][section])
            grants.update(built["grants"])
            existing.add(item["sha256"])
            status = result["status"]
            if built["ocr"]:
                status = built["ocr"]["status"]
            outcomes[key] = {"status": "added", "extraction": status, "pages": len(result["pages"]),
                             "ocr": built["ocr"]}
        except (ValueError, RuntimeError, OSError, pymupdf.FileDataError) as error:
            outcomes[key] = {"status": "failed", "error": str(error)[:500]}
    added = [key for key, outcome in outcomes.items() if outcome["status"] == "added"]
    sid = evidence_id = None
    if added:
        releases.validate_bundle(bundle)
        evidence_id = releases.export(bundle, folder / "evidence", profile="private_context",
                                      authorization=grants)
        document = deepcopy(current.document)
        for entry in document["collections"]:
            if entry["id"] == "literature":
                entry["release_id"] = store.releases.install(folder / "evidence")
                entry.update(version=name, updated_at=literature.utcnow()[:10])
        if store.current().release_id != current.release_id:
            raise ValueError("Registry changed while adding provided PDFs")
        sid = store.publish(document)
        if store.current().snapshots["istopmm"].release_id != current.snapshots["istopmm"].release_id:
            raise ValueError("iStopMM collection changed while adding provided PDFs")
        update.rebase_pending_run(updates_of(data_root), current.release_id, sid)
    with ledger_lock(root):
        ledger = read_ledger(root)
        for key, outcome in outcomes.items():
            ledger["items"][key].update(outcome, processed_at=literature.utcnow(),
                                        snapshot=sid if outcome["status"] == "added" else None,
                                        evidence_release=evidence_id if outcome["status"] == "added" else None)
        if sid:
            ledger["activations"].append({"run": name, "baseline": current.release_id, "snapshot": sid,
                                          "items": added, "activated_at": literature.utcnow()})
        write_ledger(root, ledger)
    return {"added": [ledger["items"][key]["publication_id"] for key in added],
            "failed": [{"publication_id": ledger["items"][key]["publication_id"], "error": outcome["error"]}
                       for key, outcome in outcomes.items() if outcome["status"] == "failed"],
            "waiting": 0, "snapshot": sid}


def rollback(data_root, registry=None):
    """Restore the snapshot active before the most recent provided-PDF addition."""
    data_root = Path(data_root).absolute()
    registry = Path(registry or registry_of(data_root))
    root = root_of(data_root)
    with update.writer_lock(updates_of(data_root)), update.writer_lock(registry), ledger_lock(root):
        ledger = read_ledger(root)
        active = [entry for entry in ledger["activations"] if not entry.get("rolled_back_at")]
        if not active:
            raise ValueError("No provided-PDF addition to roll back")
        last = active[-1]
        store = update.store_at(registry)
        if store.current().release_id != last["snapshot"]:
            raise ValueError("The library has changed since this addition; refusing an unrelated rollback")
        restored = store.activate(last["baseline"])
        last["rolled_back_at"] = literature.utcnow()
        for key in last["items"]:
            ledger["items"][key].update(status="rolled_back")
        write_ledger(root, ledger)
        return {"snapshot": restored, "removed": [ledger["items"][key]["publication_id"] for key in last["items"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("RESEARCH_LIBRARY_DATA_ROOT", ".")))
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Provide a PDF file or link for a listed paper and add it")
    add.add_argument("publication_id", help="Library identifier, for example doi:10.1000/example")
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", type=Path)
    source.add_argument("--url")
    add.add_argument("--license", help="Licence of this copy, for example cc-by or 'publisher permission'; "
                                       "looked up by DOI when omitted")
    commands.add_parser("apply", help="Add received PDFs that are still waiting")
    commands.add_parser("status", help="Show provided PDFs and their state")
    commands.add_parser("rollback", help="Undo the most recent provided-PDF addition")
    args = parser.parse_args()
    try:
        if args.command == "add":
            if args.pdf:
                receive(args.data_root, args.publication_id, args.pdf.read_bytes(), method="upload",
                        filename=args.pdf.name, license=args.license)
            else:
                data, final = fetch(args.url)
                receive(args.data_root, args.publication_id, data, method="link", url=args.url, final_url=final,
                        license=args.license)
            result = apply(args.data_root)
        elif args.command == "apply":
            result = apply(args.data_root)
        elif args.command == "rollback":
            result = rollback(args.data_root)
        else:
            result = {key: {field: item.get(field) for field in ("status", "extraction", "pages", "license",
                                                                 "license_source", "error", "snapshot")}
                      for key, item in statuses(args.data_root).items()}
        if args.command in {"add", "apply", "rollback"}:
            from . import literature_text
            literature_text.build(args.data_root)
    except (ValueError, OSError, releases.ReleaseError) as error:
        print(f"Provided source failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
