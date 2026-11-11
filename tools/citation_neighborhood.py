"""One-hop iStopMM citation discovery and explicitly licensed PDF acquisition."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import csv
from datetime import date
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx
import pymupdf

from research_library import catalog, literature, releases
from research_library.collections import atomic_json
from .supplements import validate_bytes

FIELDS = ",".join((
    "id", "doi", "ids", "title", "publication_date", "type", "referenced_works",
    "cited_by_count", "locations", "authorships", "primary_location", "language",
))
LICENSES = {"cc0", "public-domain", "cc-by", "cc-by-sa", "cc-by-nd",
            "cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd"}
MAX_PDF_BYTES = 64 * 1024 * 1024
PDF_LOCK = threading.Lock()
SEED_DOI_CORRECTIONS = {
    "10.3389/fimmu.2022.876271/full": "10.3389/fimmu.2022.876271",
}


def doi(value):
    return catalog.alias("doi", value).removeprefix("doi:")


def work_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:https://openalex.org/)?W\d+", value):
        raise ValueError("Invalid OpenAlex work ID")
    return value.rsplit("/", 1)[-1]


def pdf_url(value):
    if not isinstance(value, str):
        raise ValueError("Invalid PDF URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError("PDF acquisition requires an HTTPS URL without credentials")
    return value


class CachedAPI:
    def __init__(self, root, client):
        self.root, self.client = Path(root), client

    def get(self, url, params=None):
        params = params or {}
        key = releases.sha256(releases.encode({"url": url, "params": params}))
        path = self.root / (key + ".json")
        if path.exists():
            saved = releases.read_json(releases.safe_file(self.root, path.name).read_bytes())
            source = saved["source"]
            if source.get("status") != 404:
                if (releases.sha256(source["raw"].encode()) != source["sha256"]
                        or releases.read_json(source["raw"].encode()) != saved["payload"]):
                    raise ValueError("Cached citation response checksum mismatch")
            return saved["payload"], saved["source"]
        try:
            payload, source = self.client.get(url, params)
        except literature.DiscoveryError as error:
            if "HTTP 404)" not in str(error):
                raise
            payload, source = None, {
                "url": str(httpx.URL(url, params=params)), "retrieved_at": literature.utcnow(),
                "status": 404,
            }
        atomic_json(self.root, path.name, {"payload": payload, "source": source})
        return payload, source


def source_ref(source):
    return {key: source[key] for key in ("url", "retrieved_at", "sha256") if key in source}


def openalex_record(value):
    identifier = work_id(value["id"])
    ids = {}
    if value.get("doi"):
        ids["doi"] = value["doi"]
    pmid = value.get("ids", {}).get("pmid")
    if pmid:
        if not re.fullmatch(r"https?://pubmed.ncbi.nlm.nih.gov/\d+/?", pmid):
            raise ValueError("Invalid OpenAlex PMID")
        ids["pmid"] = pmid.rstrip("/").rsplit("/", 1)[-1]
    primary = value.get("primary_location") or {}
    title = value.get("title") or ""
    if not isinstance(title, str):
        raise ValueError("Invalid OpenAlex title")
    types = [value["type"]] if value.get("type") else []
    return {
        "provider": "openalex", "provider_source": "WORK", "provider_id": identifier,
        "identifiers": ids, "title": title, "publication_date": value.get("publication_date") or "",
        "languages": [value["language"]] if value.get("language") else [],
        "authors": [entry["author"]["display_name"] for entry in value.get("authorships", [])
                    if entry.get("author", {}).get("display_name")],
        "journal": (primary.get("source") or {}).get("display_name"),
        "relations": [], "publication_types": types, "query_topics": [],
        "eligibility": {"decision": "needs_review",
                        "reason": "Citation-neighborhood membership; OpenAlex type is not journal-policy approval."},
        "publication_state": "unknown", "source_url": value["id"],
        "availability": {
            "metadata": True, "abstract_reported": False,
            "full_text_reported": any(location.get("pdf_url") for location in value.get("locations", [])),
            "full_text_acquired": False, "redistribution_authorized": False,
        },
    }


def crossref_reference(seed, index, reference):
    ids = {"doi": reference["DOI"]} if reference.get("DOI") else {}
    return {
        "provider": "crossref", "provider_source": "REFERENCE",
        "provider_id": releases.sha256(releases.encode([seed, reference.get("key"), index, reference])),
        "identifiers": ids, "title": reference.get("article-title") or "",
        "authors": [reference["author"]] if reference.get("author") else [],
        "journal": reference.get("journal-title"), "publication_date": str(reference.get("year") or ""),
        "languages": [], "relations": [], "publication_types": [], "query_topics": [],
        "eligibility": {"decision": "needs_review",
                        "reason": "Publisher-deposited reference; bibliographic resolution and type review required."},
        "publication_state": "unknown", "source_url": "https://doi.org/" + seed,
        "availability": {"metadata": True, "abstract_reported": False, "full_text_reported": False,
                         "full_text_acquired": False, "redistribution_authorized": False},
    }


def discover(root, manifest, api, *, project_scope=False, directions=("background", "impact")):
    directions = frozenset(directions)
    if not directions or not directions <= {"background", "impact"}:
        raise ValueError("Select references, citing papers, or both")
    root = Path(root)
    with Path(manifest).open(newline="") as stream:
        seeds = list(csv.DictReader(stream))
    identifiers = [doi(row["doi"]) for row in seeds]
    if not seeds or len(set(identifiers)) != len(seeds):
        raise ValueError("Expected nonempty distinct DOI-bearing seed publications")
    if not project_scope and len(seeds) != 45:
        raise ValueError("Expected 45 distinct DOI-bearing iStopMM main publications")
    nodes, edges, seed_reports = {}, {}, []
    unresolved, resolution_issues = [], []

    def add_work(work, source):
        record = openalex_record(work)
        key = catalog.provider_key(record)
        entry = nodes.setdefault(key, {"record": record, "work": work, "sources": []})
        if entry["record"] != record:
            raise ValueError("Provider changed a citation work within the snapshot")
        ref = source_ref(source)
        if ref not in entry["sources"]:
            entry["sources"].append(ref)
        return key

    def add_edge(seed, direction, target, source):
        key = (seed, direction, target)
        entry = edges.setdefault(key, {"seed_doi": seed, "direction": direction, "target_provider_record": target,
                                       "sources": []})
        ref = source_ref(source)
        if ref not in entry["sources"]:
            entry["sources"].append(ref)

    pending = {}
    for row in seeds:
        original = doi(row["doi"])
        seed = original if project_scope else SEED_DOI_CORRECTIONS.get(original, original)
        report = {"seed_doi": seed, "number": row["number"], "title": row["title"],
                  "original_seed_doi": original, "existing_filename": row["filename"], "providers": {}}
        if seed != original:
            corrected, correction_source = api.get("https://api.crossref.org/works/" + seed)
            titles = corrected["message"].get("title", []) if corrected else []
            normalize_title = lambda value: re.sub(r"\W+", "", value).casefold()
            if (not corrected or doi(corrected["message"]["DOI"]) != seed
                    or not any(normalize_title(title) == normalize_title(row["title"]) for title in titles)):
                raise ValueError("Seed DOI correction requires exact Crossref title/DOI confirmation")
            report["identity_correction"] = {
                "reason": "The inventory DOI includes the publisher URL suffix /full.",
                "source": source_ref(correction_source),
            }
        seed_reports.append(report)
        try:
            work, origin = api.get("https://api.openalex.org/works/https://doi.org/" + seed, {"select": FIELDS})
            if work is None:
                report["providers"]["openalex"] = {"status": "seed_not_indexed", "source": origin}
            else:
                if doi(work.get("doi")) != seed:
                    raise ValueError("OpenAlex seed DOI mismatch")
                add_work(work, origin)
                seed_id = work_id(work["id"])
                refs = work.get("referenced_works")
                if not isinstance(refs, list) or len(refs) != len(set(refs)):
                    raise ValueError("Invalid OpenAlex reference identifiers")
                for value in refs if "background" in directions else []:
                    pending.setdefault(work_id(value), []).append((seed, "background", origin))
                page, count, total = 1, 0, None
                cursor = "*" if api.client.openalex_api_key else None
                seen_cursors = set()
                while "impact" in directions:
                    payload, source = api.get("https://api.openalex.org/works", {
                        "filter": "cites:" + seed_id, "select": FIELDS, "per-page": 100,
                        **({"cursor": cursor} if cursor is not None else {"page": page}),
                    })
                    if not payload or not isinstance(payload.get("results"), list):
                        raise ValueError("Invalid citing-work page")
                    expected = payload["meta"]["count"]
                    if total is not None and total != expected:
                        raise ValueError("OpenAlex citing count changed during pagination")
                    total = expected
                    for citing in payload["results"]:
                        target = add_work(citing, source)
                        if (seed, "impact", target) in edges:
                            raise ValueError("Repeated citing publication across pages")
                        if work["id"] not in citing["referenced_works"]:
                            raise ValueError("Citing result does not reference seed")
                        add_edge(seed, "impact", target, source)
                    count += len(payload["results"])
                    if count == total:
                        break
                    if not payload["results"] or count > total:
                        raise ValueError("Nonadvancing OpenAlex citation pagination")
                    if cursor is not None:
                        seen_cursors.add(cursor)
                        next_cursor = payload["meta"].get("next_cursor")
                        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                            raise ValueError("Nonadvancing OpenAlex citation cursor")
                        cursor = next_cursor
                    elif page * 100 >= 10000:
                        raise ValueError("OpenAlex numbered-page ceiling reached; credentialed cursor acquisition required")
                    page += 1
                report["providers"]["openalex"] = {
                    "status": "complete_indexed_neighborhood", "work_id": seed_id,
                    "references_reported": len(refs), "citations_retrieved": count,
                    "citations_reported": total, "source": source_ref(origin),
                    "requested_directions": sorted(directions),
                }
        except (literature.DiscoveryError, ValueError, KeyError, TypeError) as error:
            report["providers"]["openalex"] = {"status": "failed", "error": str(error)}
            print(json.dumps({"event": "citation_provider_failed", "seed": seed, "provider": "openalex",
                              "error": str(error)}), file=sys.stderr, flush=True)
        if "background" not in directions:
            atomic_json(root, "discovery-progress.json", {"seeds": seed_reports, "nodes": len(nodes), "edges": len(edges)})
            continue
        try:
            payload, source = api.get("https://api.crossref.org/works/" + seed)
            if payload is None:
                report["providers"]["crossref"] = {"status": "seed_not_indexed", "source": source}
            else:
                message = payload["message"]
                if doi(message["DOI"]) != seed:
                    raise ValueError("Crossref seed DOI mismatch")
                references = message.get("reference", [])
                for index, reference in enumerate(references):
                    record = crossref_reference(seed, index, reference)
                    key = catalog.provider_key(record)
                    nodes[key] = {"record": record, "sources": [source_ref(source)], "reference": reference}
                    add_edge(seed, "background", key, source)
                report["providers"]["crossref"] = {
                    "status": "complete_deposited_references" if len(references) == message.get("reference-count", 0)
                              else "deposited_reference_gap",
                    "references_retrieved": len(references), "references_reported": message.get("reference-count"),
                    "source": source_ref(source),
                }
        except (literature.DiscoveryError, ValueError, KeyError, TypeError) as error:
            report["providers"]["crossref"] = {"status": "failed", "error": str(error)}
            print(json.dumps({"event": "citation_provider_failed", "seed": seed, "provider": "crossref",
                              "error": str(error)}), file=sys.stderr, flush=True)
        atomic_json(root, "discovery-progress.json", {"seeds": seed_reports, "nodes": len(nodes), "edges": len(edges)})
        print(json.dumps({"event": "citation_seed", "number": row["number"], "nodes": len(nodes),
                          "edges": len(edges)}), file=sys.stderr, flush=True)
    identifiers = sorted(pending)
    for start in range(0, len(identifiers), 50):
        batch = identifiers[start:start + 50]
        payload, source = api.get("https://api.openalex.org/works", {
            "filter": "openalex_id:" + "|".join(batch), "select": FIELDS, "per-page": 100,
        })
        if not payload or not isinstance(payload.get("results"), list):
            raise ValueError("Invalid reference-work resolution page")
        resolved = set()
        for work in payload["results"]:
            identifier = work_id(work["id"])
            if identifier not in batch or identifier in resolved:
                raise ValueError("Unexpected reference resolution identity")
            resolved.add(identifier)
            key = add_work(work, source)
            for seed, direction, origin in pending[identifier]:
                add_edge(seed, direction, key, origin)
        for identifier in set(batch) - resolved:
            for seed, direction, origin in pending[identifier]:
                unresolved.append({"seed_doi": seed, "direction": direction, "openalex_id": identifier,
                                   "source": source_ref(origin), "resolution_source": source_ref(source)})
        print(json.dumps({"event": "citation_reference_batch", "resolved": start + len(batch),
                          "total": len(identifiers)}), file=sys.stderr, flush=True)
    # Resolve deposited DOI references absent from the OpenAlex identifier graph.
    known_dois = {value.removeprefix("doi:") for node in nodes.values()
                  if node["record"]["provider"] == "openalex"
                  for value in catalog.record_aliases(node["record"])[0] if value.startswith("doi:")}
    missing = {}
    for key, entry in nodes.items():
        record = entry["record"]
        if record["provider"] == "crossref" and record["identifiers"].get("doi"):
            try:
                identifier = doi(record["identifiers"]["doi"])
            except releases.ReleaseError:
                continue
            if identifier not in known_dois:
                missing[identifier] = key
    values = sorted(missing)
    for start in range(0, len(values), 25):
        payload, source = api.get("https://api.openalex.org/works", {
            "filter": "doi:" + "|".join("https://doi.org/" + value for value in values[start:start + 25]),
            "select": FIELDS, "per-page": 100,
        })
        if not payload or not isinstance(payload.get("results"), list):
            raise ValueError("Invalid deposited-DOI resolution")
        for work in payload["results"]:
            if doi(work["doi"]) not in values[start:start + 25]:
                resolution_issues.append({
                    "kind": "provider_doi_resolution_mismatch", "requested": values[start:start + 25],
                    "returned_work": work["id"], "returned_doi": work["doi"], "source": source_ref(source),
                    "action": "Not imported: differing DOI is not an authorized identity merge.",
                })
                continue
            add_work(work, source)
    result = {
        "schema_version": 1, "acquired_at": literature.utcnow(),
        "scope": (f"Exactly one hop from {len(seeds)} project core publication DOIs. No neighbor expansion."
                  if project_scope else
                  "Exactly one hop from the 45 iStopMM main-publication DOIs. No neighbor expansion."),
        "limitations": [
            "Requested directions: " + ", ".join(sorted(directions)) + ". Unrequested directions were not searched.",
            "Provider-indexed citations only; zero results do not prove no citations.",
            "Crossref reference deposits and OpenAlex indexing may omit publications or unstructured references.",
            "Citation edges show reference links, not agreement, independent evidence or causal impact.",
            "No disease/topic restriction: all first-hop fields are retained.",
        ],
        "seeds": seed_reports, "nodes": nodes, "edges": list(edges.values()), "unresolved": unresolved,
        "resolution_issues": resolution_issues,
    }
    atomic_json(root, "neighborhood.json", result)
    return result


def publish_metadata(root, *, project_scope=False):
    graph = releases.read_json((root / "neighborhood.json").read_bytes())
    identifier = releases.sha256(releases.encode(graph))
    run = {"id": identifier, "provider": "openalex_crossref", "query": graph["scope"],
           "policy_version": "project-first-tier-1" if project_scope else "istopmm-first-tier-1",
           "policy_sha256": releases.sha256(releases.encode(graph["limitations"])),
           "from_date": "1000-01-01", "through_date": date.today().isoformat(),
           "started_at": graph["acquired_at"], "updated_at": graph["acquired_at"],
           "status": "incomplete" if graph["unresolved"] or any(
               any(p["status"] not in {"complete_indexed_neighborhood", "complete_deposited_references"}
                   for p in seed["providers"].values()) for seed in graph["seeds"]) else "complete",
           "disease": "citation_neighborhood"}
    output = root / "catalog-001.sqlite"
    if output.exists():
        raise ValueError("Catalog output exists; use a new version directory")
    with tempfile.TemporaryDirectory() as temporary:
        with closing(sqlite3.connect(Path(temporary) / "spool.sqlite")) as connection:
            spool = catalog.Occurrences(connection)
            for node in graph["nodes"].values():
                requests = [{"url": source["url"], "retrieved_at": source["retrieved_at"],
                             "response_sha256": source["sha256"]} for source in node["sources"]]
                for start in range(0, len(requests), 20):
                    spool.add({"record": node["record"], "run_id": identifier,
                               "page_sha256": releases.sha256(releases.encode(node)),
                               "requests": requests[start:start + 20]})
            report = catalog.write_sqlite(
                output, spool, [run], as_of=date.today().isoformat(),
                rationale=("Project reference/citation metadata; not scientific approval or full-text evidence. "
                           + " ".join(text for text in graph["limitations"] if text.startswith("Incomplete coverage:"))
                           if project_scope else
                           "First-tier iStopMM background/impact metadata; not scientific approval or full-text evidence."),
                authorization_basis="User-approved private local evaluation; remote redistribution requires review.",
                istopmm_dois=[seed["seed_doi"] for seed in graph["seeds"]], relationships=())
    snapshot = catalog.open_snapshot(output, validate_records=True)
    owners = {}
    with snapshot.connect() as connection:
        for (data,) in connection.execute("SELECT data FROM publications"):
            record = releases.read_json(data.encode())
            for origin in record["provenance"]:
                owners[origin["provider_record"]] = record["id"]
    memberships = {}
    for edge in graph["edges"]:
        canonical = owners[edge["target_provider_record"]]
        item = memberships.setdefault(canonical, {"id": canonical, "background_of": [], "impact_on": []})
        field = "background_of" if edge["direction"] == "background" else "impact_on"
        if edge["seed_doi"] not in item[field]:
            item[field].append(edge["seed_doi"])
    inventory = {"schema_version": 1, "catalog_id": report["catalog_id"], "coverage": report,
                 "membership_count": len(memberships), "memberships": sorted(memberships.values(), key=lambda r: r["id"]),
                 "seed_coverage": graph["seeds"], "unresolved": graph["unresolved"],
                 "resolution_issues": graph["resolution_issues"], "limitations": graph["limitations"]}
    atomic_json(root, "inventory.json", inventory)
    return inventory


def licensed_locations(work):
    result = []
    for location in work.get("locations", []):
        if location.get("license") not in LICENSES:
            continue
        value = location.get("pdf_url")
        if not value and pmcid(location.get("landing_page_url")):
            value = location["landing_page_url"]
        try:
            url = pdf_url(value)
        except ValueError:
            continue
        result.append({"url": url, "license": location["license"], "license_id": location.get("license_id"),
                       "landing_page_url": location.get("landing_page_url"),
                       "version": location.get("version"), "provider_work_id": work["id"]})
    return sorted(result, key=lambda item: (
        "ncbi.nlm.nih.gov" not in item["url"], "europepmc.org" not in item["url"], item["url"]))


def pmcid(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.hostname not in {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "europepmc.org"}:
        return None
    match = re.search(r"/(PMC\d+)(?:/|$)", parsed.path, flags=re.I)
    return match[1].upper() if match else None


def distribution_response(client, url, pace):
    pace()
    with client.stream("GET", url) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("PMC distribution metadata exceeds byte limit")
    return bytes(body), {"url": url, "retrieved_at": literature.utcnow(),
                         "sha256": hashlib.sha256(body).hexdigest(), "raw": body.decode("utf-8")}


def official_pmc_location(client, candidate, record, pace):
    identifier = pmcid(candidate["url"]) or pmcid(candidate.get("landing_page_url"))
    if not identifier:
        return candidate
    base = "https://pmc-oa-opendata.s3.amazonaws.com/"
    listing_url = str(httpx.URL(base, params={"list-type": "2", "prefix": identifier + ".", "max-keys": 1000}))
    body, listing_source = distribution_response(client, listing_url, pace)
    listing = ET.fromstring(body)
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    if listing.findtext("s:IsTruncated", namespaces=ns) != "false":
        raise ValueError("PMC distribution listing is incomplete")
    pattern = re.compile(rf"{identifier}\.(\d+)/{identifier}\.\1\.json")
    versions = sorted((int(match[1]), key.text) for key in listing.findall("s:Contents/s:Key", ns)
                      if key.text and (match := pattern.fullmatch(key.text)))
    if not versions:
        raise ValueError("No official PMC OA distribution found")
    version, key = versions[-1]
    body, metadata_source = distribution_response(client, base + key, pace)
    metadata = releases.read_json(body)
    license_code = metadata.get("license_code", "").lower().replace(" ", "-")
    if (metadata.get("pmcid") != identifier or metadata.get("version") != version
            or metadata.get("is_pmc_openaccess") is not True or license_code not in LICENSES):
        raise ValueError("PMC distribution identity/open-access/license validation failed")
    aliases = {catalog.alias(namespace, str(metadata[namespace]))
               for namespace in ("doi", "pmid", "pmcid") if metadata.get(namespace)}
    for namespace in ("doi:", "pmid:", "pmcid:"):
        declared = {value for value in record["aliases"] if value.startswith(namespace)}
        official = {value for value in aliases if value.startswith(namespace)}
        if declared and official and declared != official:
            raise ValueError("PMC distribution strong identifier mismatch")
    key = f"{identifier}.{version}/{identifier}.{version}.pdf"
    supplied = urlsplit(metadata.get("pdf_url") or "")
    if supplied.scheme != "s3" or supplied.netloc != "pmc-oa-opendata" or supplied.path != "/" + key:
        raise ValueError("Official PMC distribution does not supply the expected main PDF")
    return {**candidate, "download_url": base + key, "official_pmc": {
        "pmcid": identifier, "license": license_code, "listing": listing_source,
        "metadata": metadata_source,
    }}


def download(client, url, sleep=time.sleep, pace=None, on_bytes=None):
    url = pdf_url(url)
    for attempt in range(4):
        sleep(0.5)
        response = None
        try:
            current, redirects = url, []
            for _ in range(6):
                if pace:
                    pace()
                with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = pdf_url(str(response.url.join(response.headers["location"])))
                        redirects.append(current)
                        continue
                    if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                        break
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if on_bytes:
                            on_bytes(len(chunk))
                        data.extend(chunk)
                        if len(data) > MAX_PDF_BYTES:
                            raise ValueError("PDF exceeds acquisition byte limit")
                    with PDF_LOCK:
                        validation = validate_bytes(bytes(data), ".pdf")
                    return bytes(data), {"final_url": str(response.url), "redirects": redirects,
                                         "content_type": response.headers.get("content-type"), **validation}
            else:
                raise ValueError("Too many PDF redirects")
        except httpx.TransportError:
            if attempt == 3:
                raise
        if attempt == 3:
            if response is not None:
                response.raise_for_status()
            raise ValueError("PDF server unavailable after bounded retries")
        delay = 2 ** attempt
        if response is not None and response.headers.get("retry-after"):
            raw = response.headers["retry-after"]
            if not raw.isdigit() or int(raw) > 120:
                raise httpx.HTTPStatusError("PDF provider requires a later retry",
                                            request=response.request, response=response)
            delay = int(raw)
        sleep(delay)
    raise ValueError("PDF acquisition did not complete")


class RequestPacer:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0

    def __call__(self):
        with self.lock:
            time.sleep(max(0, 0.5 - (time.monotonic() - self.last)))
            self.last = time.monotonic()


def acquire_one(client, record, candidates, pace, on_bytes=None):
    entry = {"id": record["id"], "title": record["title"], "citation_url": record["citation_url"],
             "status": "no_explicitly_licensed_pdf", "attempts": [], "acquired_at": None}
    if record["istopmm_overlap"] == "matched_doi":
        entry["status"] = "existing_seed_collection"
        entry["note"] = "Already in the original iStopMM inventory; not downloaded again or newly licensed."
        return entry, None
    if record["identity_status"] != "resolved":
        entry["status"] = "identity_review_required"
        return entry, None
    unique = {candidate["url"]: candidate for candidate in candidates}
    for candidate in unique.values():
        try:
            candidate = official_pmc_location(client, candidate, record, pace)
            data, response = download(client, candidate.get("download_url", candidate["url"]),
                                      pace=pace, on_bytes=on_bytes)
        except (httpx.HTTPError, ValueError, KeyError, ET.ParseError, pymupdf.FileDataError) as error:
            failure = {**candidate, "status": "failed", "error": str(error)}
            if isinstance(error, httpx.HTTPStatusError):
                failure["http_status"] = error.response.status_code
            elif isinstance(error, httpx.TransportError):
                failure["transport_error"] = True
            entry["attempts"].append(failure)
            continue
        entry.update(status="downloaded", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                     acquired_at=literature.utcnow(), acquisition={**candidate, **response},
                     redistribution_authorized=False,
                     license_verification="Explicit location license reported by OpenAlex; not independently adjudicated.",
                     identity_verification="Provider identifier/location association; document contents not scientifically reviewed.")
        return entry, data
    if unique:
        entry["status"] = "licensed_locations_failed"
    return entry, None


def save_pdf(papers, data, digest, hashes):
    filename = hashes.get(digest, digest + ".pdf")
    path = papers / filename
    if path.is_symlink():
        raise ValueError("Symlinked PDF output")
    if path.exists():
        if catalog.file_digest(path) != digest:
            raise ValueError("Content-addressed PDF differs")
    else:
        descriptor, temporary = tempfile.mkstemp(dir=papers, prefix=".pdf-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
            catalog.sync_directory(papers)
        finally:
            Path(temporary).unlink(missing_ok=True)
    hashes[digest] = filename
    return filename


def acquire(root, papers, retry_pmc=False):
    graph = releases.read_json((root / "neighborhood.json").read_bytes())
    inventory = releases.read_json((root / "inventory.json").read_bytes())
    snapshot = catalog.open_snapshot(root / "catalog-001.sqlite")
    members = {item["id"] for item in inventory["memberships"]}
    node_to_id = {}
    with snapshot.connect() as connection:
        for (data,) in connection.execute("SELECT data FROM publications"):
            record = releases.read_json(data.encode())
            for origin in record["provenance"]:
                node_to_id[origin["provider_record"]] = record["id"]
    candidates = {}
    for key, node in graph["nodes"].items():
        identifier = node_to_id[key]
        if identifier in members and node.get("work"):
            candidates.setdefault(identifier, []).extend(licensed_locations(node["work"]))
    state_path = root / "acquisition.json"
    state = releases.read_json(state_path.read_bytes()) if state_path.exists() else {
        "schema_version": 1, "catalog_id": snapshot.release_id,
        "policy": "Explicit OpenAlex location CC/public-domain license; private acquisition only. "
                  "No inferred right from free-to-read or is_oa alone; no access-control bypass.",
        "items": {},
    }
    if state["catalog_id"] != snapshot.release_id:
        raise ValueError("Acquisition state belongs to a different catalog")
    papers = Path(papers)
    if papers.is_symlink():
        raise ValueError("Symlinked PDF destination")
    papers.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for entry in state["items"].values():
        if entry["status"] == "downloaded":
            path = releases.safe_file(papers, entry["filename"])
            if catalog.file_digest(path) != entry["sha256"]:
                raise ValueError("Previously acquired PDF checksum differs")
            hashes[entry["sha256"]] = entry["filename"]
    with httpx.Client(timeout=httpx.Timeout(60), follow_redirects=False,
                      headers={"User-Agent": "research-library-first-tier/1 (private-research-acquisition)"}) as client:
        pace = RequestPacer()
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [
                identifier for identifier in sorted(members) if identifier not in state["items"] or
                (retry_pmc and state["items"][identifier]["status"] in {
                    "no_explicitly_licensed_pdf", "licensed_locations_failed"
                } and any(pmcid(item["url"]) or pmcid(item.get("landing_page_url"))
                          for item in candidates.get(identifier, [])))
            ]
            jobs = {pool.submit(acquire_one, client, snapshot.get(identifier), candidates.get(identifier, []), pace):
                    identifier for identifier in pending}
            for job in as_completed(jobs):
                identifier = jobs.pop(job)
                entry, data = job.result()
                if data is not None:
                    entry["filename"] = save_pdf(papers, data, entry["sha256"], hashes)
                if identifier in state["items"]:
                    previous = state["items"][identifier]
                    entry["attempts"] = previous["attempts"] + entry["attempts"]
                    entry["previous_statuses"] = previous.get("previous_statuses", []) + [{
                        "status": previous["status"], "retried_at": literature.utcnow(),
                    }]
                state["items"][identifier] = entry
                atomic_json(root, "acquisition.json", state)
                print(json.dumps({"event": "citation_pdf", "processed": len(state["items"]), "total": len(members),
                                  "status": entry["status"]}), file=sys.stderr, flush=True)
    return dict(Counter(item["status"] for item in state["items"].values()))


def bibliographic_resolution(record):
    if record["aliases"]:
        return "strong_identifier"
    if any(origin["provider_record"].startswith("openalex:WORK:") for origin in record["provenance"]):
        return "provider_work_identifier_only"
    return "unresolved_reference_occurrence"


def verify_and_export(root, papers):
    graph = releases.read_json((root / "neighborhood.json").read_bytes())
    inventory = releases.read_json((root / "inventory.json").read_bytes())
    acquisition = releases.read_json((root / "acquisition.json").read_bytes())
    snapshot = catalog.open_snapshot(root / "catalog-001.sqlite", validate_records=True)
    if (releases.sha256(releases.encode(graph)) != snapshot.document["runs"][0]["id"]
            or inventory["catalog_id"] != snapshot.release_id
            or acquisition["catalog_id"] != snapshot.release_id):
        raise ValueError("Graph, catalog, inventory and acquisition identities differ")
    seeds = {seed["seed_doi"] for seed in graph["seeds"]}
    if len(seeds) != 45:
        raise ValueError("Invalid seed set")
    memberships = {item["id"]: item for item in inventory["memberships"]}
    if set(memberships) != set(acquisition["items"]):
        raise ValueError("Acquisition pass has not assigned every first-tier record a status")
    records = {identifier: snapshot.get(identifier) for identifier in snapshot.publication_keys()}
    owners = {origin["provider_record"]: record["id"] for record in records.values()
              for origin in record["provenance"]}
    expected, canonical_edges = {}, set()
    for edge in graph["edges"]:
        if edge["seed_doi"] not in seeds or edge["direction"] not in {"background", "impact"}:
            raise ValueError("Edge is outside declared first-tier scope")
        target = edge["target_provider_record"]
        identifier = owners[target]
        canonical_edges.add((edge["seed_doi"], edge["direction"], identifier))
        item = expected.setdefault(identifier, {"id": identifier, "background_of": set(), "impact_on": set()})
        item["background_of" if edge["direction"] == "background" else "impact_on"].add(edge["seed_doi"])
    if set(expected) != set(memberships) or any(
        set(memberships[key][field]) != item[field] for key, item in expected.items()
        for field in ("background_of", "impact_on")
    ):
        raise ValueError("Memberships do not match directed citation edges")
    licenses = {}
    for key, node in graph["nodes"].items():
        if node.get("work"):
            licenses.setdefault(owners[key], []).extend(licensed_locations(node["work"]))
    downloaded = {}
    for identifier, entry in acquisition["items"].items():
        if entry["id"] != identifier:
            raise ValueError("Acquisition entry identity differs")
        if entry["status"] == "downloaded":
            path = releases.safe_file(papers, entry["filename"])
            if catalog.file_digest(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
                raise ValueError("Acquired PDF size/checksum differs")
            with PDF_LOCK:
                validation = validate_bytes(path.read_bytes(), ".pdf")
            if validation["pdf_pages"] != entry["acquisition"]["pdf_pages"]:
                raise ValueError("Acquired PDF page count differs")
            if not any(all(entry["acquisition"].get(key) == candidate[key] for key in candidate)
                       for candidate in licenses.get(identifier, [])):
                raise ValueError("Acquired PDF lacks its recorded explicit location-license basis")
            official = entry["acquisition"].get("official_pmc")
            if official:
                for source in (official["listing"], official["metadata"]):
                    if releases.sha256(source["raw"].encode()) != source["sha256"]:
                        raise ValueError("Official PMC license metadata checksum differs")
                metadata = releases.read_json(official["metadata"]["raw"].encode())
                if (metadata["pmcid"] != official["pmcid"] or metadata["is_pmc_openaccess"] is not True
                        or metadata["license_code"].lower().replace(" ", "-") != official["license"]
                        or official["license"] not in LICENSES):
                    raise ValueError("Official PMC license/identity basis differs")
            downloaded[entry["filename"]] = entry["bytes"]
    actual_files = {path.name for path in papers.glob("*.pdf")}
    if actual_files != set(downloaded):
        raise ValueError("PDF folder and acquisition inventory differ")
    fields = ["id", "bibliographic_resolution", "title", "authors", "publication_date", "journal", "doi", "citation_url",
              "background_of", "impact_on", "istopmm_overlap", "identity_status", "acquisition_status",
              "pdf_filename", "pdf_sha256", "pdf_source_url", "pdf_final_url", "license",
              "acquired_at", "metadata_sources", "unstructured_references"]
    # CSV quoting preserves bibliographic text; consumers should import columns as text.
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix=".bibliography-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for identifier, member in sorted(memberships.items()):
                record, entry = records[identifier], acquisition["items"][identifier]
                location = entry.get("acquisition", {})
                provider_keys = {origin["provider_record"] for origin in record["provenance"]}
                writer.writerow({
                    "id": identifier, "bibliographic_resolution": bibliographic_resolution(record),
                    "title": record["title"], "authors": "; ".join(record["authors"]),
                    "publication_date": record["publication_date"], "journal": record["journal"],
                    "doi": "; ".join(alias[4:] for alias in record["aliases"] if alias.startswith("doi:")),
                    "citation_url": record["citation_url"], "background_of": "; ".join(member["background_of"]),
                    "impact_on": "; ".join(member["impact_on"]), "istopmm_overlap": record["istopmm_overlap"],
                    "identity_status": record["identity_status"], "acquisition_status": entry["status"],
                    "pdf_filename": entry.get("filename"), "pdf_sha256": entry.get("sha256"),
                    "pdf_source_url": location.get("url"), "pdf_final_url": location.get("final_url"),
                    "license": location.get("license"), "acquired_at": entry["acquired_at"],
                    "metadata_sources": "; ".join(sorted({request["url"] for origin in record["provenance"]
                                                         for request in origin["requests"]})),
                    "unstructured_references": "; ".join(
                        graph["nodes"][key].get("reference", {}).get("unstructured", "")
                        for key in sorted(provider_keys) if graph["nodes"][key].get("reference", {}).get("unstructured")),
                })
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "bibliography.csv")
        catalog.sync_directory(root)
    finally:
        Path(temporary).unlink(missing_ok=True)
    background = {key for key, member in memberships.items() if member["background_of"]}
    impact = {key for key, member in memberships.items() if member["impact_on"]}
    report = {
        "verified_at": literature.utcnow(), "catalog_id": snapshot.release_id,
        "seed_count": len(seeds), "catalog_records_including_seeds": len(records),
        "first_tier_records": len(memberships), "background_records": len(background),
        "impact_records": len(impact), "both_directions": len(background & impact),
        "strong_identifier_records": sum(bool(records[key]["aliases"]) for key in memberships),
        "provider_identifier_only_records": sum(
            bibliographic_resolution(records[key]) == "provider_work_identifier_only" for key in memberships),
        "unresolved_reference_occurrences": sum(
            bibliographic_resolution(records[key]) == "unresolved_reference_occurrence" for key in memberships),
        "provider_edges": len(graph["edges"]), "canonical_edges": len(canonical_edges),
        "unresolved_openalex_reference_links": len(graph["unresolved"]),
        "doi_resolution_mismatches": len(graph["resolution_issues"]),
        "acquisition_status": dict(Counter(entry["status"] for entry in acquisition["items"].values())),
        "unique_pdf_files": len(downloaded), "pdf_bytes": sum(downloaded.values()),
        "all_acquired_pdf_hashes_and_parsing_verified": True,
        "global_citation_completeness_claimed": False,
        "limitations": graph["limitations"],
    }
    atomic_json(root, "verification.json", report)
    rows = []
    for identifier, member in sorted(memberships.items()):
        record, entry = records[identifier], acquisition["items"][identifier]
        title = record["title"] or "[Unresolved or missing title: see bibliography.csv]"
        link = escape(title)
        if urlsplit(record["citation_url"]).scheme in {"http", "https"}:
            link = f'<a href="{escape(record["citation_url"], quote=True)}">{link}</a>'
        local = ""
        if entry["status"] == "downloaded":
            relative = os.path.relpath(papers / entry["filename"], root).replace(os.sep, "/")
            local = f'<a href="{escape(relative, quote=True)}">PDF</a>'
        rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in (
            link, escape(record["publication_date"]), escape("; ".join(member["background_of"])),
            escape("; ".join(member["impact_on"])), escape(entry["status"]), local,
            escape(entry.get("acquisition", {}).get("license", "")),
        )) + "</tr>")
    page = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>iStopMM first-tier sources</title>'
        "<h1>iStopMM first-tier sources</h1><p>Provisional provider-indexed references and citers of 45 seeds. "
        "No second-hop expansion or scientific approval. Missing records and access failures remain explicit. "
        "PDF licenses are reported by the providers; downloads do not authorize service redistribution.</p>"
        '<p><a href="bibliography.csv">Full citation and acquisition list (CSV)</a> | '
        '<a href="verification.json">Verification and counts</a></p>'
        "<table><thead><tr><th>Publication</th><th>Date</th><th>Background of (seed DOI)</th>"
        "<th>Impact on (seed DOI)</th><th>Acquisition</th><th>Local file</th><th>Reported license</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></html>"
    )
    path = root / "inventory.html"
    if path.is_symlink():
        raise ValueError("Symlinked HTML inventory")
    path.write_text(page, encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover")
    discovery.add_argument("--manifest", default=Path("papers/manifest.csv"), type=Path)
    commands.add_parser("catalog")
    acquisition = commands.add_parser("acquire")
    acquisition.add_argument("--papers", default=Path("papers/citation-neighborhood"), type=Path)
    acquisition.add_argument("--retry-pmc", action="store_true",
                             help="Retry unresolved records with explicitly licensed PMC locations")
    verification = commands.add_parser("verify")
    verification.add_argument("--papers", default=Path("papers/citation-neighborhood"), type=Path)
    args = parser.parse_args()
    try:
        if args.root.is_symlink():
            raise ValueError("Symlinked citation workspace")
        if args.command == "discover":
            if (args.root / "neighborhood.json").exists():
                raise ValueError("Discovery snapshot exists; select a new version directory")
            with closing(literature.MetadataClient()) as client:
                result = discover(args.root, args.manifest, CachedAPI(args.root / "responses", client))
            print(json.dumps({"seeds": len(result["seeds"]), "nodes": len(result["nodes"]),
                              "edges": len(result["edges"]), "unresolved": len(result["unresolved"])}))
        elif args.command == "catalog":
            result = publish_metadata(args.root)
            print(json.dumps({"catalog_id": result["catalog_id"], "memberships": result["membership_count"]}))
        elif args.command == "acquire":
            print(json.dumps(acquire(args.root, args.papers, retry_pmc=args.retry_pmc)))
        else:
            print(json.dumps(verify_and_export(args.root, args.papers)))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, httpx.HTTPError) as error:
        print(f"Citation acquisition failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
