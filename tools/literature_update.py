"""On-demand source updates with saved provenance and reproducible snapshots."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import fcntl
from functools import wraps
import os
from pathlib import Path
import platform
import resource
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

import httpx
import pymupdf
from jsonschema import ValidationError

from research_library import __version__, catalog, collections, literature, releases, retrieval
from . import citation_neighborhood as citation
from . import literature_ingest as ingest
from . import literature_ocr

POLICY = {
    "version": "source-updates-1", "overlap_days": 30,
    "max_pdfs": 25, "max_bytes": 250 * 1024 * 1024,
    "workers": 2, "min_free_bytes": 1024 * 1024 * 1024,
    "metadata_batch_size": 25, "max_metadata_pages": 200,
    "retention": "Retain all historical releases and reports; no automatic deletion.",
    "activation": "Explicit operator activation only after validation.",
    "load_budget": {"max_seconds": 600, "max_peak_rss_mib": 2048,
                    "max_mean_query_seconds": 2, "min_queries": 10},
}
NOTICE_KINDS = {"corrects", "retracts", "concerns"}
NOTICE_LINKS = {"erratumfor", "erratumin", "retractionof", "retractionin",
                "expressionofconcernfor", "expressionofconcernin", "updateof", "updatein",
                "retractedandrepublishedfrom", "retractedandrepublishedin"}
_LOCKS = threading.local()
# Native extraction outcomes that yield an acquired, staged source version.
EXTRACTED = {"extracted", "partial", "ocr_required", "no_native_text"}


@contextmanager
def writer_lock(root):
    root = Path(root).resolve()
    held = getattr(_LOCKS, "held", set())
    if root in held:
        yield
        return
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / ("." + root.name + ".writer.lock")).open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another update writer holds: " + str(root)) from error
        _LOCKS.held = held | {root}
        try:
            yield
        finally:
            _LOCKS.held = held
            fcntl.flock(stream, fcntl.LOCK_UN)


def single_writer(function):
    @wraps(function)
    def call(root, *args, **kwargs):
        with writer_lock(root):
            return function(root, *args, **kwargs)
    return call


def read(path):
    return ingest.read(Path(path))


def write(root, name, document):
    collections.atomic_json(Path(root), name, document)


def checked(document, path):
    value = read(path)
    if ingest.identity(value) != document:
        raise ValueError("Checkpoint/artifact checksum mismatch: " + str(path))
    return value


def say(message):
    """Human-readable progress on stderr; stdout stays the final JSON result."""
    print(f"[update {datetime.now().strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def store_at(path):
    return collections.CollectionStore(
        Path(path), allowed_profiles=["private_context"],
        allowed_collections=["istopmm", "literature", "catalog"],
        defaults=["istopmm", "literature"])


def tree_hashes(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected a real directory")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinked update/backup artifact")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = catalog.file_digest(path)
    return result


@single_writer
def initialize(root, acquisition, extraction, papers, registry, authorization, policy=None):
    root = Path(root)
    if root.exists() or root.is_symlink() or not authorization.strip():
        raise ValueError("Initialize a new workspace with explicit private-use authorization")
    policy = deepcopy(policy or POLICY)
    validate_policy(policy)
    acquisition, extraction, papers, registry = (
        Path(value).absolute() for value in (acquisition, extraction, papers, registry))
    original = store_at(registry).current()
    graph = read(acquisition / "neighborhood.json")
    state = read(acquisition / "acquisition.json")
    manifest = read(extraction / "pilot.json")
    if state["catalog_id"] != manifest["catalog_id"]:
        raise ValueError("Baseline acquisition and extraction do not match")
    if graph["scope"] != "Exactly one hop from the 45 iStopMM main-publication DOIs. No neighbor expansion.":
        raise ValueError("Only the approved first-tier scope is supported")
    if len(graph["seeds"]) != 45:
        raise ValueError("Expected 45 first-tier seeds")
    for source in manifest["sources"]:
        ingest.source_path(papers, source)
    config = {
        "schema_version": 1, "created_at": literature.utcnow(), "policy": policy,
        "authorization": authorization, "acquisition_root": str(acquisition),
        "extraction_root": str(extraction), "papers": str(papers), "registry": str(registry),
        "baseline_snapshot": original.release_id, "baseline_manifest": ingest.identity(manifest),
        "baseline_graph": ingest.identity(graph), "baseline_acquisition": ingest.identity(state),
        "baseline_releases": {cid: snap.release_id for cid, snap in original.snapshots.items()},
    }
    write(root, "config.json", config)
    return config


def validate_policy(policy):
    if set(policy) != set(POLICY):
        raise ValueError("Update policy has missing/unknown fields")
    for name, low, high in (
        ("overlap_days", 1, 365),
        ("max_pdfs", 1, 25), ("max_bytes", 1, 250 * 1024 * 1024), ("workers", 1, 2),
        ("min_free_bytes", 1024 * 1024, 1024 ** 4), ("metadata_batch_size", 1, 25),
        ("max_metadata_pages", 1, 200),
    ):
        if type(policy[name]) is not int or not low <= policy[name] <= high:
            raise ValueError("Policy exceeds approved budget: " + name)
    if policy["load_budget"] != POLICY["load_budget"]:
        raise ValueError("Load budget must remain the documented engineering acceptance budget")
    if any(policy[key] != POLICY[key] for key in ("version", "retention", "activation")):
        raise ValueError("Unsupported update policy semantics")


def run_state(root, name, mode, through):
    collections.identifier(name)
    if mode not in {"incremental", "reconcile"}:
        raise ValueError("Unsupported update mode")
    cutoff = date.fromisoformat(through)
    if cutoff.isoformat() != through or cutoff > date.today():
        raise ValueError("Update cutoff must be an ISO date no later than today")
    root = Path(root)
    config = read(root / "config.json")
    validate_policy(config["policy"])
    folder = root / "runs" / name
    path = folder / "state.json"
    if path.exists():
        state = read(path)
        if (state["config_sha256"] != ingest.identity(config) or state["mode"] != mode
                or state["through"] != through):
            raise ValueError("Resume parameters/policy differ; use a new run ID")
    else:
        history = read(root / "history.json") if (root / "history.json").exists() else {
            "last_successful": None, "last_reconciliation": None}
        if history["last_successful"] and cutoff < date.fromisoformat(history["last_successful"]):
            raise ValueError("Update cutoff precedes the last validated window")
        start = date.fromisoformat(history["last_successful"] or
                                  config["created_at"][:10]) - timedelta(days=config["policy"]["overlap_days"])
        if cutoff < start:
            raise ValueError("Update cutoff precedes the overlap window")
        carried = {}
        if history.get("last_run"):
            prior = read(root / "runs" / history["last_run"] / "state.json")
            saved = checked(prior["discovery_sha256"], root / "runs" / history["last_run"] / "discovery.json")
            carried = saved["records"]
        state = {
            "schema_version": 1, "config_sha256": ingest.identity(config), "name": name,
            "activation_baseline": store_at(config["registry"]).current().release_id,
            "mode": mode, "through": through, "from": start.isoformat(),
            "started_at": literature.utcnow(), "discovery": "pending", "pages": [],
            "queries": {}, "records": carried, "ingestion": {}, "network_bytes": 0,
            "network_attempts": 0, "failures": [], "candidate": None,
            "previous_run": history.get("last_run"), "notice_queries": [],
        }
        write(folder, "state.json", state)
    return config, folder, state


def baseline(config):
    acquisition = Path(config["acquisition_root"])
    graph = checked(config["baseline_graph"], acquisition / "neighborhood.json")
    acquired = checked(config["baseline_acquisition"], acquisition / "acquisition.json")
    manifest = checked(config["baseline_manifest"], Path(config["extraction_root"]) / "pilot.json")
    original = store_at(config["registry"]).load(config["baseline_snapshot"])
    if config["baseline_releases"] != {cid: snap.release_id for cid, snap in original.snapshots.items()}:
        raise ValueError("Original release identities changed")
    return graph, acquired, manifest, original


def id_expression(records):
    terms = set()
    for record in records:
        for identifier in catalog.record_aliases(record)[0]:
            if identifier.startswith("doi:"):
                value = identifier[4:]
                if any(character in value for character in '"\\'):
                    raise ValueError("Unsupported DOI query characters; explicit review required")
                terms.add('DOI:"' + value + '"')
            elif identifier.startswith("pmid:"):
                terms.add("(EXT_ID:" + identifier[5:] + " AND SRC:MED)")
    return sorted(terms)


def query_aliases(query):
    requested = {"doi:" + value.lower() for value in re.findall(r'DOI:"([^"]+)"', query)}
    requested.update("pmid:" + value for value in re.findall(r"EXT_ID:(\d+)", query))
    return requested


def validate_record_scope(query, record, linked_aliases=()):
    requested = query_aliases(query) | set(linked_aliases)
    if not requested.intersection(catalog.record_aliases(record)[0]):
        raise ValueError("Provider returned a record outside the requested first-tier identity/notice scope")


def scope_status(query, record, linked_aliases=()):
    aliases = catalog.record_aliases(record)[0]
    if not any(value.startswith(("doi:", "pmid:")) for value in aliases):
        return "identity_review_required"
    validate_record_scope(query, record, linked_aliases)
    return "matched"


def notice_terms(value):
    return [
        f"(EXT_ID:{link['id']} AND SRC:MED)"
        for link in value.get("commentCorrectionList", {}).get("commentCorrection", [])
        if link.get("source") == "MED" and str(link.get("id", "")).isdigit()
        and re.sub(r"[^a-z]", "", str(link.get("type", "")).lower()) in NOTICE_LINKS]


def additional_notice_queries(terms, queries, batch_size):
    covered = {alias for query in queries if "UPDATE_DATE" not in query for alias in query_aliases(query)}
    pending = sorted(term for term in set(terms) if not query_aliases(term).issubset(covered))
    return ["(" + " OR ".join(pending[start:start + batch_size]) + ")"
            for start in range(0, len(pending), batch_size)]


def validate_pages(folder, pages):
    for page in pages:
        saved = checked(page["sha256"], folder / page["file"])
        if releases.sha256(saved["source"]["raw"].encode()) != saved["source"]["sha256"]:
            raise ValueError("Raw provider response checksum mismatch")
        for item in saved["records"]:
            status = scope_status(saved["query"], item["record"], saved.get("linked_aliases", []))
            if item.get("scope_status", "matched") != status:
                raise ValueError("Provider page identity status differs")


@single_writer
def discover(root, name, *, mode, through, client=None, manifest=None):
    config, folder, state = run_state(root, name, mode, through)
    if state["discovery"] == "complete":
        result = checked(state["discovery_sha256"], folder / "discovery.json")
        validate_pages(folder, result["pages"])
        return result
    validate_pages(folder, state["pages"])
    graph, _, _, _ = baseline(config)
    old_records = {}
    if state["previous_run"]:
        prior_folder = Path(root) / "runs" / state["previous_run"]
        prior = read(prior_folder / "state.json")
        previous_discovery = checked(prior["discovery_sha256"], prior_folder / "discovery.json")
        graph = previous_discovery["graph"]
        old_records = previous_discovery["records"]
    old_graph = deepcopy(graph)
    # Only committed whole pages may advance query progress after an interruption.
    state["records"] = deepcopy(old_records)
    state["queries"] = {}
    for number, descriptor in enumerate(state["pages"]):
        page = checked(descriptor["sha256"], folder / descriptor["file"])
        payload = json.loads(page["source"]["raw"])
        progress = state["queries"].setdefault(ingest.identity(page["query"]), {
            "query": page["query"], "seen": [], "cursor": "*", "count": payload["hitCount"]})
        for item in page["records"]:
            key = catalog.provider_key(item["record"])
            progress["seen"].append(key)
            if item.get("scope_status", "matched") == "matched":
                state["records"][key] = {**item, "page": number, "source": page["source"], "run": name}
        progress.update(complete=len(progress["seen"]) == progress["count"], cursor=payload.get("nextCursorMark"))
    durable_state = deepcopy(state)
    own_client = client is None
    client = client or literature.MetadataClient()
    try:
        if mode == "reconcile":
            if manifest is None:
                raise ValueError("Reconciliation requires the original seed manifest")
            graph_path = folder / "citations" / "neighborhood.json"
            if graph_path.exists():
                graph = read(graph_path)
            else:
                graph = citation.discover(
                    folder / "citations", manifest, citation.CachedAPI(folder / "citations" / "responses", client))
            # A pre-existing indexing gap stays explicit, but failed requests cannot become a complete run.
            if any(provider["status"] == "failed" for seed in graph["seeds"]
                   for provider in seed["providers"].values()):
                raise ValueError("Citation reconciliation contains failed provider requests; use a new run")
        expressions = id_expression(node["record"] for node in graph["nodes"].values())
        batch_size = config["policy"]["metadata_batch_size"]
        queries = ["(" + " OR ".join(expressions[start:start + batch_size]) + ")"
                   for start in range(0, len(expressions), batch_size)]
        if mode == "incremental":
            queries = [query + f" AND UPDATE_DATE:[{state['from']} TO {through}]" for query in queries]
        queries += additional_notice_queries(state["notice_queries"], queries, batch_size)
        pending_notices = []
        index = 0
        while index < len(queries):
            query = queries[index]
            index += 1
            key = ingest.identity(query)
            requested = query_aliases(query)
            linked_aliases = sorted({alias for node in graph["nodes"].values()
                if requested.intersection(catalog.record_aliases(node["record"])[0])
                for alias in catalog.record_aliases(node["record"])[0]})
            progress = state["queries"].setdefault(key, {"query": query, "cursor": "*",
                                                         "seen": [], "count": None, "complete": False})
            if progress["complete"]:
                continue
            say(f"Checking publication metadata and notices: batch {index} of {len(queries)}")
            while True:
                if len(state["pages"]) >= config["policy"]["max_metadata_pages"]:
                    raise ValueError("Metadata page quota exhausted; run remains incomplete")
                payload, provenance = client.get(
                    "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                    {"query": query, "format": "json", "resultType": "core",
                     "pageSize": 100, "cursorMark": progress["cursor"]})
                values = payload.get("resultList", {}).get("result")
                count = payload.get("hitCount")
                if (not isinstance(values, list) or type(count) is not int or count < 0
                        or (progress["count"] is not None and progress["count"] != count)):
                    raise ValueError("Invalid/changing provider result count")
                progress["count"] = count
                page = {"query": query, "source": provenance, "records": [], "linked_aliases": linked_aliases}
                page_number = len(state["pages"])
                for value in values:
                    record = literature.normalize("europepmc", value, "mgus")
                    record["query_topics"] = []  # Identity scope is not a disease classification.
                    status = scope_status(query, record, linked_aliases)
                    record_key = catalog.provider_key(record)
                    semantic = record
                    previous = state["records"].get(record_key)
                    if previous and previous.get("run") == name and previous["record"] != semantic:
                        raise ValueError("Provider changed a record during this run")
                    if record_key in progress["seen"]:
                        raise ValueError("Repeated provider record within a query")
                    progress["seen"].append(record_key)
                    page["records"].append({"record": semantic, "updated": value.get("dateOfRevision"),
                                            "first_indexed": value.get("firstIndexDate"), "scope_status": status})
                    if status == "matched":
                        state["records"][record_key] = {"record": semantic, "page": page_number,
                                                    "updated": value.get("dateOfRevision"),
                                                    "first_indexed": value.get("firstIndexDate"),
                                                    "source": provenance, "run": name}
                    if status == "matched":
                        pending_notices.extend(notice_terms(value))
                if len(progress["seen"]) > count or (not values and len(progress["seen"]) != count):
                    raise ValueError("Nonadvancing/incomplete provider page")
                complete = len(progress["seen"]) == count
                mark = payload.get("nextCursorMark")
                if not complete and (not mark or mark == progress["cursor"]):
                    raise ValueError("Provider cursor did not advance")
                filename = f"pages/{page_number:05}.json"
                write(folder / "pages", f"{page_number:05}.json", page)
                state["pages"].append({"file": filename, "sha256": ingest.identity(page)})
                progress.update(complete=complete, cursor=mark)
                state["notice_queries"] = sorted(set(state["notice_queries"]) | set(pending_notices))
                write(folder, "state.json", state)
                durable_state = deepcopy(state)
                if complete:
                    break
            if index == len(queries) and pending_notices:
                queries.extend(additional_notice_queries(pending_notices, queries, batch_size))
                pending_notices = []
                if len(queries) > config["policy"]["max_metadata_pages"]:
                    raise ValueError("Notice discovery query quota exceeded")
        validate_pages(folder, state["pages"])
        old_nodes = old_graph["nodes"]
        changed = [key for key, node in graph["nodes"].items()
                   if key in old_nodes and node["record"] != old_nodes[key]["record"]]
        missing = sorted(set(old_nodes) - set(graph["nodes"])) if mode == "reconcile" else []
        for key in missing:
            graph["nodes"][key] = {**deepcopy(old_nodes[key]), "upstream_state": "not_returned_in_reconciliation"}
        result = {
            "schema_version": 1, "mode": mode, "from": state["from"], "through": through,
            "completed_at": literature.utcnow(), "graph": graph, "records": state["records"],
            "pages": state["pages"], "query_count": len(state["queries"]),
            "new_provider_records": sorted(set(graph["nodes"]) - set(old_nodes)),
            "changed_provider_records": sorted(changed), "absent_provider_records": missing,
            "changed_metadata_records": sorted(key for key, item in state["records"].items()
                if key in old_records and (item["record"] != old_records[key]["record"]
                                          or item.get("updated") != old_records[key].get("updated"))),
            "quarantined_provider_records": [
                {"provider_record": catalog.provider_key(item["record"]), "page": number}
                for number, page in enumerate(state["pages"])
                for item in read(folder / page["file"])["records"]
                if item.get("scope_status") == "identity_review_required"],
            "absence_meaning": "Not returned in reconciliation; not proof of deletion or retraction.",
            "unsupported_update_identities": sum(not id_expression([node["record"]]) for node in graph["nodes"].values()),
            "limitations": [
                "Incremental UPDATE_DATE searches only tracked strong identities; new citation neighbors require reconciliation.",
                "Provider timestamps/notice links are reported metadata, not independent adjudication.",
                "Unindexed/unresolved references remain visible; no real-time or globally complete coverage claim.",
            ],
        }
        write(folder, "discovery.json", result)
        state.update(discovery="complete", discovery_sha256=ingest.identity(result))
        write(folder, "state.json", state)
        return result
    except (OSError, ValueError, KeyError, httpx.HTTPError) as error:
        state = durable_state
        state["failures"].append({"phase": "discovery", "at": literature.utcnow(),
                                  "type": type(error).__name__, "message": str(error)})
        write(folder, "state.json", state)
        raise
    finally:
        if own_client:
            client.close()


def make_catalog(config, folder, discovery):
    validate_pages(folder, discovery["pages"])
    output = folder / "catalog.sqlite"
    if output.exists():
        return catalog.open_snapshot(output, validate_records=True)
    graph = discovery["graph"]
    graph_id = ingest.identity(graph)
    runs = [{
        "id": graph_id, "provider": "openalex_crossref", "query": graph["scope"],
        "policy_version": config["policy"]["version"], "policy_sha256": ingest.identity(config["policy"]),
        "from_date": "1000-01-01", "through_date": discovery["through"],
        "started_at": graph["acquired_at"], "updated_at": discovery["completed_at"],
        "status": "incomplete" if graph["unresolved"] else "complete", "disease": "citation_neighborhood",
    }]
    epmc_id = ingest.identity({k: v for k, v in discovery.items() if k != "graph"})
    runs.append({**runs[0], "id": epmc_id, "provider": "europepmc",
                 "query": "Tracked identities and linked notices; " + discovery["mode"] + " UPDATE_DATE/reconciliation",
                 "from_date": discovery["from"], "status": "complete"})
    with tempfile.TemporaryDirectory() as temporary:
        with closing(sqlite3.connect(Path(temporary) / "spool.sqlite")) as db:
            spool = catalog.Occurrences(db)
            for node in graph["nodes"].values():
                origins = [{"url": source["url"], "retrieved_at": source["retrieved_at"],
                            "response_sha256": source["sha256"]} for source in node["sources"]]
                for start in range(0, len(origins), 20):
                    spool.add({"record": node["record"], "run_id": graph_id,
                               "page_sha256": ingest.identity(node), "requests": origins[start:start + 20]})
            for item in discovery["records"].values():
                source = item["source"]
                if releases.sha256(source["raw"].encode()) != source["sha256"]:
                    raise ValueError("Carried provider provenance checksum differs")
                spool.add({"record": item["record"], "run_id": epmc_id,
                           "page_sha256": ingest.identity(item), "requests": [{
                               "url": source["url"], "retrieved_at": source["retrieved_at"],
                               "response_sha256": source["sha256"]}]})
            catalog.write_sqlite(
                output, spool, runs, as_of=discovery["through"],
                rationale="First-tier update plus tracked-identity/linked-notice reconciliation; not scientific approval.",
                authorization_basis=config["authorization"],
                istopmm_dois=[seed["seed_doi"] for seed in graph["seeds"]], relationships=())
    return catalog.open_snapshot(output, validate_records=True)


def source_from(entry, record, authorization):
    digest = entry["sha256"]
    return {
        "publication_id": record["id"], "source_id": "literature:" + digest, "sha256": digest,
        "filename": entry["filename"], "bytes": entry["bytes"], "format": "pdf",
        "title": record["title"], "publication_date": record["publication_date"],
        "aliases": record["aliases"], "types": record["publication_types"], "membership": {},
        "curation_disease": None, "acquisition": entry,
        "permissions": {"local_storage": True, "private_passages": True, "private_tables": True,
                        "public_remote_serving": False, "original_serving": False, "basis": authorization},
    }


class TransferBudget:
    """Persist charged bytes, including failed/retried responses, across restarts."""

    def __init__(self, folder, limit):
        self.folder, self.limit, self.lock = folder, limit, threading.Lock()
        path = folder / "transfer.json"
        self.state = read(path) if path.exists() else {"charged_bytes": 0}

    def charge(self, size):
        with self.lock:
            self.state["observed_body_bytes"] = self.state.get("observed_body_bytes", 0) + size
            if self.state["charged_bytes"] + size > self.limit:
                self.state["quota_exhausted"] = True
                write(self.folder, "transfer.json", self.state)
                raise ValueError("Transfer byte quota exhausted")
            self.state["charged_bytes"] += size
            write(self.folder, "transfer.json", self.state)


@single_writer
def process(root, name, *, retry_failed=False):
    config = read(Path(root) / "config.json")
    folder = Path(root) / "runs" / collections.identifier(name)
    state = read(folder / "state.json")
    if state["candidate"]:
        raise ValueError("Candidate is frozen; use a new run rather than modifying staged ingestion")
    if state["config_sha256"] != ingest.identity(config) or state["discovery"] != "complete":
        raise ValueError("Completed discovery with the original configuration is required")
    discovery = checked(state["discovery_sha256"], folder / "discovery.json")
    snapshot = make_catalog(config, folder, discovery)
    graph, prior, manifest, _ = baseline(config)
    sources = {source["publication_id"]: source for source in manifest["sources"]}
    previous_items = {}
    if state["previous_run"]:
        previous_items = read(Path(root) / "runs" / state["previous_run"] / "state.json")["ingestion"]
    papers = Path(config["papers"])
    update_papers = Path(root).absolute() / "papers"
    update_papers.mkdir(exist_ok=True)
    if shutil.disk_usage(update_papers).free < config["policy"]["min_free_bytes"] + config["policy"]["max_bytes"]:
        raise ValueError("Insufficient free disk for the bounded update")
    owners = {}
    for identifier in snapshot.publication_keys():
        for origin in snapshot.get(identifier)["provenance"]:
            owners[origin["provider_record"]] = identifier
    candidates = {}
    for key, node in discovery["graph"]["nodes"].items():
        if node.get("work"):
            candidates.setdefault(owners[key], []).extend(citation.licensed_locations(node["work"]))
    eligible = []
    download_locations = {}
    probe_aliases = {alias for key in discovery.get("changed_metadata_records", [])
                     for alias in catalog.record_aliases(discovery["records"][key]["record"])[0]}
    for identifier in sorted(candidates):
        probe = identifier in probe_aliases
        record = snapshot.get(identifier)
        previous = state["ingestion"].get(identifier)
        if previous and previous["status"] not in {"failed", "unavailable", "quota_pending"}:
            if "source" in previous:
                ingest.source_path(previous.get("papers", papers), previous["source"])
            continue
        if previous and previous["status"] in {"failed", "unavailable"} and not retry_failed:
            continue
        if identifier in previous_items and previous_items[identifier].get("status") in EXTRACTED:
            previous = deepcopy(previous_items[identifier])
            source = previous["source"]
            location = source["acquisition"]["acquisition"]
            if not probe and any(all(location.get(key) == candidate[key] for key in candidate) for candidate in candidates[identifier]):
                ingest.source_path(previous.get("papers", papers), source)
                previous["origin"] = previous.get("origin", str(Path(root) / "runs" / state["previous_run"]))
                checked(previous["sha256"], Path(previous["origin"]) / previous["file"])
                state["ingestion"][identifier] = previous
                continue
        if identifier in sources:
            source = sources[identifier]
            ingest.source_path(papers, source)
            checkpoint = read(Path(config["extraction_root"]) / "extraction.json")["items"][identifier]
            result = ingest.checked_result(Path(config["extraction_root"]), source, checkpoint,
                                           config["baseline_manifest"])
            # Same location version: do not re-download or re-extract unchanged sources.
            prior_locations = prior["items"][identifier]["acquisition"]
            current_locations = candidates[identifier]
            if not probe and any(all(prior_locations.get(key) == location[key] for key in location) for location in current_locations):
                state["ingestion"][identifier] = {"status": "unchanged", "source": source,
                                                  "checkpoint": checkpoint, "origin": config["extraction_root"],
                                                  "first_seen": state["started_at"], "pages": len(result["pages"])}
                continue
        if record["istopmm_overlap"] == "matched_doi":
            state["ingestion"][identifier] = {"status": "existing_istopmm", "first_seen": state["started_at"]}
        elif record["identity_status"] != "resolved" or not identifier.startswith("doi:"):
            state["ingestion"][identifier] = {"status": "identity_review_required", "first_seen": state["started_at"]}
        elif not candidates[identifier]:
            state["ingestion"][identifier] = {"status": "no_licensed_pdf", "first_seen": state["started_at"]}
        elif (identifier in prior["items"] and prior["items"][identifier]["status"] != "downloaded"
              and mode_unchanged_location(graph, discovery["graph"], identifier, owners)):
            state["ingestion"][identifier] = {
                "status": "acquisition_backlog", "first_seen": state["started_at"],
                "prior_status": prior["items"][identifier]["status"],
                "note": "Unchanged prior unavailable/failed location; not automatically retried."}
        else:
            failed_urls = {
                attempt["url"]
                for entry in (prior["items"].get(identifier, {}),
                              state["ingestion"].get(identifier, {}).get("acquisition", {}))
                for attempt in entry.get("attempts", []) if attempt.get("status") == "failed"}
            fresh = [location for location in candidates[identifier] if location["url"] not in failed_urls]
            download_locations[identifier] = fresh or candidates[identifier]
            eligible.append(identifier)
    write(folder, "state.json", state)
    say(f"Downloading new PDFs: {len(eligible)} with a licensed location to try"
        + (" (nothing new to download)" if not eligible else ""))
    done = 0
    pace = citation.RequestPacer()
    budget = TransferBudget(folder, config["policy"]["max_bytes"])
    with httpx.Client(timeout=httpx.Timeout(60), follow_redirects=False) as client:
        with ThreadPoolExecutor(max_workers=config["policy"]["workers"]) as pool:
            for start in range(0, len(eligible), config["policy"]["workers"]):
                identifiers = eligible[start:start + config["policy"]["workers"]]
                jobs = []
                for identifier in identifiers:
                    remaining = config["policy"]["max_bytes"] - budget.state["charged_bytes"]
                    if state["network_attempts"] >= config["policy"]["max_pdfs"] or remaining < citation.MAX_PDF_BYTES:
                        state["ingestion"][identifier] = {"status": "quota_pending", "first_seen": state["started_at"]}
                        continue
                    # Persist an attempt before submission; the shared byte budget caps both workers.
                    state["network_attempts"] += 1
                    write(folder, "state.json", state)
                    jobs.append((identifier, pool.submit(citation.acquire_one, client, snapshot.get(identifier),
                                                         download_locations[identifier][:1], pace, budget.charge)))
                for identifier, job in jobs:
                    entry, data = job.result()
                    item = {"status": "failed", "acquisition": entry, "first_seen": state["started_at"]}
                    if data is None and entry.get("attempts") and all(
                            attempt.get("http_status") is not None or attempt.get("transport_error")
                            for attempt in entry["attempts"]):
                        item["status"] = "unavailable"
                    if data is not None:
                        entry["filename"] = citation.save_pdf(update_papers, data, entry["sha256"], {})
                        source = source_from(entry, snapshot.get(identifier), config["authorization"])
                        old_item = previous_items.get(identifier, {})
                        old_source = old_item.get("source", sources.get(identifier))
                        if old_source and old_source["sha256"] == source["sha256"]:
                            if identifier in sources:
                                checkpoint = read(Path(config["extraction_root"]) / "extraction.json")["items"][identifier]
                                cached = ingest.checked_result(Path(config["extraction_root"]), old_source,
                                                               checkpoint, config["baseline_manifest"])
                                item = {"status": "unchanged", "source": old_source, "checkpoint": checkpoint,
                                        "origin": config["extraction_root"], "pages": len(cached["pages"])}
                            else:
                                item = deepcopy(old_item)
                                item["origin"] = item.get("origin", str(Path(root) / "runs" / state["previous_run"]))
                                checked(item["sha256"], Path(item["origin"]) / item["file"])
                            item.update(first_seen=state["started_at"], latest_acquisition=entry)
                            state["ingestion"][identifier] = item
                            state["network_bytes"] = budget.state["charged_bytes"]
                            write(folder, "state.json", state)
                            continue
                        try:
                            result = ingest.extract_pdf(update_papers / source["filename"], source)
                        except (ValueError, RuntimeError, pymupdf.FileDataError) as error:
                            result = {"status": "failed", "pages": [], "error": str(error)}
                        result.update(publication_id=identifier, source_sha256=source["sha256"],
                                      policy=ingest.POLICY, extracted_at=literature.utcnow())
                        filename = "documents/" + source["sha256"] + ".json"
                        write(folder / "documents", source["sha256"] + ".json", result)
                        item.update(status=result["status"], source=source, file=filename,
                                    papers=str(update_papers),
                                    sha256=ingest.identity(result), pages=len(result["pages"]),
                                    source_revision=identifier in sources and source["sha256"] != sources[identifier]["sha256"])
                        if old_source and old_source["sha256"] != source["sha256"]:
                            if "file" in old_item:
                                old_result = checked(old_item["sha256"],
                                    Path(old_item.get("origin", Path(root) / "runs" / state["previous_run"])) / old_item["file"])
                            else:
                                checkpoint = read(Path(config["extraction_root"]) / "extraction.json")["items"][identifier]
                                old_result = ingest.checked_result(Path(config["extraction_root"]), old_source,
                                                                   checkpoint, config["baseline_manifest"])
                            old_pages = {page["pdf_page"]: page["text_sha256"] for page in old_result["pages"]}
                            new_pages = {page["pdf_page"]: page["text_sha256"] for page in result["pages"]}
                            item.update(source_revision=True, version_diff={
                                "old_sha256": old_source["sha256"], "new_sha256": source["sha256"],
                                "changed_original_pdf_pages": sorted(page for page in old_pages.keys() | new_pages.keys()
                                                                   if old_pages.get(page) != new_pages.get(page)),
                                "meaning": "Native text hash/page differences, not scientific adjudication."})
                    state["ingestion"][identifier] = item
                    state["network_bytes"] = budget.state["charged_bytes"]
                    write(folder, "state.json", state)
                    done += 1
                    pages = f", {item['pages']} pages" if item.get("pages") else ""
                    say(f"  [{done}/{len(eligible)}] {identifier}: {item['status']}{pages}")
    # All discovered catalog entries receive an explicit pipeline state.
    for identifier in snapshot.publication_keys():
        state["ingestion"].setdefault(identifier, {"status": "metadata_only", "first_seen": state["started_at"]})
        if identifier in previous_items:
            state["ingestion"][identifier]["first_seen"] = previous_items[identifier]["first_seen"]
    state["retained_baseline_identity_review"] = sorted(set(sources) - set(state["ingestion"]))
    write(folder, "state.json", state)
    return report(root, name)


def mode_unchanged_location(old_graph, new_graph, identifier, owners):
    def locations(graph):
        return sorted((location for key, node in graph["nodes"].items()
                       if owners.get(key) == identifier and node.get("work")
                       for location in citation.licensed_locations(node["work"])), key=releases.encode)
    return locations(old_graph) == locations(new_graph)


def impact(bundle, discovery, items, metadata=None):
    result = deepcopy(bundle)
    aliases = {}
    for source in result["sources"]:
        # PMID-only sources carry "pmid:..." in the doi field.
        identity = collections.publication(source["doi"])
        aliases[identity] = source["doi"]
        if metadata is not None:
            try:
                record = metadata.get(identity)
            except retrieval.NotFoundError:
                raise
            except retrieval.RetrievalError:
                # Conflicting provider identities (e.g. an erratum indexed under the same DOI) stay
                # unmerged for researcher review; match notices by the DOI alone, not by guessed PMIDs.
                continue
            for identifier in record["aliases"]:
                aliases[identifier] = source["doi"]
    affected = {}
    raw_pages = {}
    for key, item in discovery["records"].items():
        record = item["record"]
        ids = catalog.record_aliases(record)[0]
        doi = next((aliases[value] for value in ids if value in aliases), None)
        if doi and record["publication_state"] in {"retracted", "notice"}:
            affected.setdefault(doi, []).append({"kind": record["publication_state"], "provider_record": key})
        if doi and key in discovery.get("changed_metadata_records", []):
            affected.setdefault(doi, []).append({"kind": "changed_provider_metadata", "provider_record": key})
        if doi and "source" in item:
            source = item["source"]
            if source["sha256"] not in raw_pages:
                raw_pages[source["sha256"]] = {
                    "europepmc:" + value["source"] + ":" + str(value["id"]): value
                    for value in json.loads(source["raw"])["resultList"]["result"]}
            value = raw_pages[source["sha256"]].get(key)
            if value and notice_terms(value):
                affected.setdefault(doi, []).append({"kind": "provider_linked_notice",
                    "provider_record": key, "linked_notice_queries": notice_terms(value)})
        for link in record["relations"]:
            if link["kind"] in NOTICE_KINDS and link["target"] in aliases:
                affected.setdefault(aliases[link["target"]], []).append({"kind": link["kind"], "provider_record": key})
    for identifier, item in items.items():
        if item.get("source_revision") and identifier in aliases:
            affected.setdefault(aliases[identifier], []).append({"kind": "changed_source_bytes", "provider_record": identifier})
    summary = []
    for doi, reasons in sorted(affected.items()):
        unit = next((unit for unit in result["units"] if unit["citation"]["doi"] == doi), None)
        if unit is None:
            raise ValueError("Affected source has no original locator for review flag")
        iid = "update-review:" + ingest.identity({"doi": doi, "reasons": reasons})[:32]
        caveat = "UPDATE REVIEW REQUIRED: " + ", ".join(sorted({r["kind"] for r in reasons})) + (
            ". Historical source values/citations retained; no corrected value or scientific adjudication inferred.")
        record = {
            "id": iid, "citation": deepcopy(unit["citation"]), "verification_status": "machine_extracted",
            "human_verification": "not_reviewed", "caveats": [caveat],
            "status": "unresolved", "category": "ambiguity", "context": "Provider update / source-version impact",
            "explanation": caveat, "evidence": [{"citation": deepcopy(unit["citation"]),
                                               "quote": unit["text"][:256] or "Historical source locator retained."}],
        }
        if not any(issue["record"]["id"] == iid for issue in result["issues"]):
            result["issues"].append({"number": len(result["issues"]) + 1, "title": "Publication update requires review",
                                     "paper_ids": [retrieval.paper_id(doi)], "images": [], "record": record})
        impacted = []
        for section in ("claims", "units", "tables"):
            for value in result[section]:
                if value["citation"]["doi"] == doi:
                    if caveat not in value["caveats"]:
                        value["caveats"].append(caveat)
                    if section in {"claims", "units"} and iid not in value["source_issue_ids"]:
                        value["source_issue_ids"].append(iid)
                    impacted.append(value["id"])
        summary.append({"doi": doi, "reasons": reasons, "affected_records": impacted, "issue_id": iid})
    releases.validate_bundle(result)
    return result, summary


def source_bundle(workspace, source_root, identifier, source, result):
    """Build one exact source version, adding separate local OCR where native text is missing."""
    frozen = {"schema_version": 1, "policy": ingest.POLICY, "sources": [source],
              "pilot_publications": [identifier]}
    write(workspace, "pilot.json", frozen)
    document = {**result, "manifest_sha256": ingest.identity(frozen)}
    write(workspace / "documents", source["sha256"] + ".json", document)
    write(workspace, "extraction.json", {"manifest_sha256": ingest.identity(frozen), "items": {
        identifier: {"file": "documents/" + source["sha256"] + ".json",
                     "sha256": ingest.identity(document), "status": result["status"], "pages": len(result["pages"])}}})
    ocr_root, summary = None, None
    if result["status"] in literature_ocr.OCR_STATUSES:
        ocr_root = workspace / "ocr"
        literature_ocr.complete(workspace, source_root, ocr_root)
        entry = read(ocr_root / "index.json")["items"][identifier]
        summary = {key: entry[key] for key in ("status", "ocr_pages", "unresolved_pages", "low_confidence_pages")}
        ocr_text = any(page["text"].strip() for page in read(ocr_root / entry["file"])["pages"])
        if not ocr_text and not any(page["text"].strip() for page in result["pages"]):
            summary["status"] = "needs_visual_review"
            return {"bundle": None, "grants": {}, "ocr": summary}
    bundle, grants = ingest.make_bundle(workspace, source_root, ocr_root=ocr_root)
    return {"bundle": bundle, "grants": grants, "ocr": summary}


@single_writer
def stage(root, name):
    root = Path(root)
    config = read(root / "config.json")
    folder = root / "runs" / collections.identifier(name)
    state = read(folder / "state.json")
    if (root / "history.json").exists():
        history = read(root / "history.json")
        if (history["last_successful"] and state["through"] < history["last_successful"]
                and not state["candidate"]):
            raise ValueError("Cannot stage an older window after a newer validated run")
    if state["candidate"]:
        if state["candidate"]["ingestion_sha256"] != ingest.identity(state["ingestion"]):
            raise ValueError("Frozen candidate ingestion changed")
        return checked(state["candidate"]["changes_sha256"], folder / "changes.json")
    if state["config_sha256"] != ingest.identity(config) or state["discovery"] != "complete":
        raise ValueError("Completed original discovery required")
    discovery = checked(state["discovery_sha256"], folder / "discovery.json")
    metadata = make_catalog(config, folder, discovery)
    if set(state["ingestion"]) != set(metadata.publication_keys()):
        raise ValueError("Every discovered publication must have an ingestion state")
    blocked = [key for key, item in state["ingestion"].items()
               if item["status"] not in {"unchanged", "extracted", "partial", "ocr_required", "no_native_text",
                                        "metadata_only",
                                        "existing_istopmm", "identity_review_required",
                                        "no_licensed_pdf", "acquisition_backlog", "unavailable",
                                        "quota_pending"}]
    if blocked:
        raise ValueError(f"Candidate is incomplete/invalid; {len(blocked)} blocked ingestion states")
    _, _, manifest, original = baseline(config)
    parent = store_at(config["registry"]).load(state.get("activation_baseline", config["baseline_snapshot"]))
    if parent.snapshots["istopmm"].release_id != config["baseline_releases"]["istopmm"]:
        raise ValueError("Pinned update parent changed the original iStopMM release")
    bundle = deepcopy(parent.snapshots["literature"]._bundle)
    grants = {source["id"]: {"scopes": ["curated", "context"], "basis": config["authorization"]}
              for source in bundle["sources"]}
    existing = {source["sha256"]: source["doi"].lower() for source in bundle["sources"]}
    added = []
    building = [key for key, item in state["ingestion"].items() if item["status"] in EXTRACTED]
    say(f"Building the candidate library: {len(building)} new or changed PDFs"
        + (" (pages without text are OCR'd; this can take minutes per paper)" if building else ""))
    for identifier, item in state["ingestion"].items():
        if item["status"] == "unchanged":
            continue
        if "source" not in item:
            continue
        source = item["source"]
        source_root = Path(item.get("papers", config["papers"]))
        ingest.source_path(source_root, source)
        if source["sha256"] in existing:
            if existing[source["sha256"]] != identifier.removeprefix("doi:"):
                raise ValueError("Source bytes claimed by different publication identities")
            continue
        result = checked(item["sha256"], Path(item.get("origin", folder)) / item["file"])
        replay = ingest.extract_pdf(source_root / source["filename"], source)
        if result["pages"] != replay["pages"] or result["status"] != replay["status"]:
            raise ValueError("New extraction replay differs")
        # Reuse the original importer/schema/rights gates, with the exact new version.
        say(f"  [{building.index(identifier) + 1}/{len(building)}] {identifier}: {len(result['pages'])} pages"
            + (", running OCR" if result["status"] in literature_ocr.OCR_STATUSES else ""))
        built = source_bundle(folder / ("export-" + source["sha256"]), source_root, identifier, source, result)
        if built["ocr"]:
            item["ocr"] = built["ocr"]
        if built["bundle"] is None:
            continue
        for section in releases.SECTIONS:
            bundle[section].extend(built["bundle"][section])
        grants.update(built["grants"])
        existing[source["sha256"]] = identifier.removeprefix("doi:")
        added.append(identifier)
    bundle, impacts = impact(bundle, discovery, state["ingestion"], metadata)
    output = folder / "evidence"
    if output.exists():
        snapshot = releases.validate(output, allowed_profiles={"private_context"})
        if snapshot._bundle != bundle:
            raise ValueError("Existing candidate differs; use a new run")
        evidence_id = snapshot.release_id
    else:
        evidence_id = releases.export(bundle, output, profile="private_context", authorization=grants)
    changes = {
        "baseline_snapshot": parent.release_id, "evidence_release": evidence_id,
        "catalog_release": metadata.release_id, "added_evidence_publications": added,
        "review_impacts": impacts, "absent_provider_records": discovery["absent_provider_records"],
        "source_version_diffs": {key: item["version_diff"] for key, item in state["ingestion"].items()
                                 if item.get("version_diff")},
        "quarantined_provider_records": discovery.get("quarantined_provider_records", []),
        "new_provider_records": discovery["new_provider_records"],
        "changed_provider_records": discovery["changed_provider_records"],
        "changed_metadata_records": discovery.get("changed_metadata_records", []),
        "pending_publications": {key: {"status": item["status"], "acquisition": item.get("acquisition")}
                                 for key, item in state["ingestion"].items()
                                 if item["status"] not in {"unchanged", "existing_istopmm", *EXTRACTED}
                                 or ("source" in item and key not in added and item["status"] in literature_ocr.OCR_STATUSES
                                     and item.get("ocr", {}).get("status") == "needs_visual_review")},
        "scientific_approval": False,
    }
    write(folder, "changes.json", changes)
    state["candidate"] = {
        "evidence_release": evidence_id, "catalog_release": metadata.release_id,
        "changes_sha256": ingest.identity(changes), "ingestion_sha256": ingest.identity(state["ingestion"]),
        "validated_at": literature.utcnow(), "activated": None,
    }
    write(folder, "state.json", state)
    history = read(root / "history.json") if (root / "history.json").exists() else {
        "last_successful": None, "last_reconciliation": None}
    history["last_successful"] = max(history["last_successful"] or state["through"], state["through"])
    if state["mode"] == "reconcile":
        history["last_reconciliation"] = max(history["last_reconciliation"] or state["through"], state["through"])
    history["last_run"] = name
    write(root, "history.json", history)
    report(root, name)
    return changes


@single_writer
def activate(root, name, registry, *, approve=False):
    with writer_lock(registry):
        return activate_locked(root, name, registry, approve=approve)


def activate_locked(root, name, registry, *, approve=False):
    if not approve:
        raise ValueError("Explicit --approve is required; staging never activates")
    config = read(Path(root) / "config.json")
    folder = Path(root) / "runs" / collections.identifier(name)
    state = read(folder / "state.json")
    candidate = state["candidate"]
    if (state["config_sha256"] != ingest.identity(config) or not candidate
            or candidate["ingestion_sha256"] != ingest.identity(state["ingestion"])):
        raise ValueError("Stage and validate unchanged ingestion before activation")
    checked(candidate["changes_sha256"], folder / "changes.json")
    store = store_at(registry)
    current = store.current()
    if candidate["activated"]:
        if current.release_id != candidate["activated"]:
            raise ValueError("Candidate was activated previously; registry has since moved")
        return current.release_id
    if current.release_id == candidate.get("activation_target"):
        if current.snapshots["istopmm"].release_id != config["baseline_releases"]["istopmm"]:
            raise ValueError("iStopMM identity differs after interrupted activation")
        candidate["activated"] = current.release_id
        write(folder, "state.json", state)
        return current.release_id
    if current.release_id != state.get("activation_baseline", config["baseline_snapshot"]):
        raise ValueError("Active registry changed since this run's pinned baseline; create a new run")
    evidence = releases.validate(folder / "evidence", allowed_profiles={"private_context"})
    metadata = catalog.open_snapshot(folder / "catalog.sqlite", validate_records=True)
    if (evidence.release_id != candidate["evidence_release"] or metadata.release_id != candidate["catalog_release"]
            or evidence._manifest["assets"]):
        raise ValueError("Candidate identity or private-rights gate failed")
    document = deepcopy(current.document)
    for entry in document["collections"]:
        if entry["id"] == "literature":
            entry["release_id"] = store.releases.install(folder / "evidence")
        elif entry["id"] == "catalog":
            entry["release_id"] = catalog.install(store.root, folder / "catalog.sqlite")
        else:
            continue
        entry.update(version=state["name"], updated_at=state["through"])
    candidate["activation_target"] = releases.sha256(releases.encode(document))
    write(folder, "state.json", state)
    sid = store.publish(document)
    after = store.current()
    if after.snapshots["istopmm"].release_id != config["baseline_releases"]["istopmm"]:
        raise ValueError("iStopMM changed during activation")
    candidate["activated"] = sid
    write(folder, "state.json", state)
    return sid


@single_writer
def rollback(root, name, registry, *, approve=False):
    with writer_lock(registry):
        return rollback_locked(root, name, registry, approve=approve)


def rollback_locked(root, name, registry, *, approve=False):
    if not approve:
        raise ValueError("Explicit --approve required for rollback")
    config = read(Path(root) / "config.json")
    folder = Path(root) / "runs" / collections.identifier(name)
    state = read(folder / "state.json")
    if not state["candidate"] or not state["candidate"]["activated"]:
        raise ValueError("No activated candidate for this run")
    store = store_at(registry)
    baseline_id = state.get("activation_baseline", config["baseline_snapshot"])
    if store.current().release_id not in {state["candidate"]["activated"], baseline_id}:
        raise ValueError("Registry moved after activation; refusing unrelated rollback")
    restored = store.activate(baseline_id)
    if store.current().snapshots["istopmm"].release_id != config["baseline_releases"]["istopmm"]:
        raise ValueError("iStopMM identity changed")
    state["candidate"].update(rollback_snapshot=restored, rolled_back_at=literature.utcnow())
    write(folder, "state.json", state)
    return restored


@single_writer
def report(root, name):
    config = read(Path(root) / "config.json")
    folder = Path(root) / "runs" / collections.identifier(name)
    state = read(folder / "state.json")
    counts = {}
    for item in state["ingestion"].values():
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    history = read(Path(root) / "history.json") if (Path(root) / "history.json").exists() else {
        "last_successful": None, "last_reconciliation": None}
    ages = [(datetime.now(timezone.utc) - datetime.fromisoformat(item["first_seen"])).total_seconds()
            for item in state["ingestion"].values() if item["status"] not in {"unchanged", *EXTRACTED}]
    result = {
        "application_version": __version__, "run": name, "mode": state["mode"],
        "attempted_window": [state["from"], state["through"]], "discovery": state["discovery"],
        "successful_through": history["last_successful"], "last_reconciliation": history["last_reconciliation"],
        "freshness_basis": "Validated staged candidates, not the serving registry; activation is separate.",
        "counts": {"discovered": len(state["ingestion"]), "acquired": sum("source" in item for item in state["ingestion"].values()),
                   "extracted": sum(item.get("pages", 0) > 0 for item in state["ingestion"].values()),
                   "new_curated": 0, "new_human_reviewed": 0, "states": counts},
        "oldest_unresolved_seconds": max(ages, default=0),
        "retained_baseline_identity_review": state.get("retained_baseline_identity_review", []),
        "charged_transfer_bytes": (read(folder / "transfer.json")["charged_bytes"]
                                   if (folder / "transfer.json").exists() else 0),
        "download_attempts": state["network_attempts"], "policy": config["policy"],
        "candidate": state["candidate"], "failures": state["failures"], "scientific_approval": False,
    }
    write(folder, "report.json", result)
    return result


def backup(registry, destination):
    registry, destination = Path(registry), Path(destination)
    require_separate_paths(registry, destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Backup destination must be new")
    hashes = tree_hashes(registry)
    store_at(registry).current()
    shutil.copytree(registry, destination / "registry")
    if hashes != tree_hashes(destination / "registry") or hashes != tree_hashes(registry):
        raise ValueError("Backup changed during copy; do not use this backup")
    document = {"schema_version": 1, "created_at": literature.utcnow(), "files": hashes}
    write(destination, "backup.json", document)
    return ingest.identity(document)


def restore(backup_root, destination):
    backup_root, destination = Path(backup_root), Path(destination)
    require_separate_paths(backup_root, destination)
    document = read(backup_root / "backup.json")
    if document["files"] != tree_hashes(backup_root / "registry"):
        raise ValueError("Backup checksum mismatch")
    store_at(backup_root / "registry").current()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore requires a new directory; never overwrite a running registry")
    shutil.copytree(backup_root / "registry", destination)
    if document["files"] != tree_hashes(destination):
        raise ValueError("Restore checksum mismatch")
    return store_at(destination).current().release_id


def require_separate_paths(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Backup/restore source and destination must not contain each other")


def latest_run(root):
    path = Path(root) / "latest.json"
    if not path.exists():
        raise ValueError("No source update yet; run make update first")
    return collections.identifier(read(path)["run"])


def initialize_local(root, data_root):
    data_root = Path(data_root)
    acquisition = data_root / "releases/istopmm-first-tier-20261019"
    extraction = data_root / "releases/m9-ingestion-001"
    manifest = read(extraction / "pilot.json")
    bases = sorted({source["permissions"]["basis"] for source in manifest["sources"]})
    if not bases or not all(basis.strip() for basis in bases):
        raise ValueError("Existing private source permissions are required before updating")
    return initialize(root, acquisition, extraction,
                      data_root / "papers/literature/istopmm-first-tier-20261019",
                      data_root / "releases/m9-platform", "\n".join(bases))


def check_local_paths(config, data_root):
    data_root = Path(data_root).resolve()
    expected = {
        "acquisition_root": data_root / "releases/istopmm-first-tier-20261019",
        "extraction_root": data_root / "releases/m9-ingestion-001",
        "papers": data_root / "papers/literature/istopmm-first-tier-20261019",
        "registry": data_root / "releases/m9-platform",
    }
    if any(Path(config[key]).resolve() != path for key, path in expected.items()):
        raise ValueError("Source update configuration points outside this data workspace; "
                         "do not run updates until the workspace has been safely migrated")


@single_writer
def refresh(root, data_root):
    """Refresh when requested; resume unfinished work without operator-managed IDs."""
    root, data_root = Path(root), Path(data_root)
    if not (root / "config.json").exists():
        initialize_local(root, data_root)
    config = read(root / "config.json")
    if root.resolve() == (data_root / "releases/source-updates").resolve():
        check_local_paths(config, data_root)
    latest = root / "latest.json"
    state = None
    if latest.exists():
        name = latest_run(root)
        state = read(root / "runs" / name / "state.json")
        if state["candidate"] and state["candidate"]["activated"]:
            state = None
        elif state.get("activation_baseline") != store_at(config["registry"]).current().release_id:
            # Another validated activation (for example a provided PDF) moved the registry; start over.
            state = None
    if state is None:
        name = "update-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        through = date.today().isoformat()
        run_state(root, name, "reconcile", through)
        write(root, "latest.json", {"run": name})
        say(f"Starting update {name}")
    else:
        through = state["through"]
        say(f"Resuming unfinished update {name}; completed steps are reused")
    say("Step 1/5: finding direct references and citers of the 45 iStopMM papers "
        "(saved provider responses are reused)")
    discover(root, name, mode="reconcile", through=through,
             manifest=data_root / "papers/manifest.csv")
    state = read(root / "runs" / name / "state.json")
    if not state["candidate"]:
        say("Step 2/5: downloading and extracting new PDFs")
        process(root, name)
        say("Step 3/5: building and validating the candidate library")
        stage(root, name)
    say("Step 4/5: activating the new library snapshot")
    activate(root, name, config["registry"], approve=True)
    from . import literature_text, provided_sources
    if provided_sources.pending(data_root):
        say("Adding provided PDFs that were waiting")
    provided = provided_sources.apply(data_root, config["registry"])
    say("Step 5/5: regenerating readable text.md views")
    literature_text.build(data_root, config["registry"])
    say("Done. Summary follows; restart make web / make mcp to read the new snapshot")
    return {**source_summary(root), "provided": provided}


def rebase_pending_run(root, baseline_id, snapshot_id):
    """Carry another validated activation into an unstaged run so it is not discarded later."""
    root = Path(root)
    if not (root / "latest.json").exists():
        return False
    with writer_lock(root):
        folder = root / "runs" / latest_run(root)
        state = read(folder / "state.json")
        if state["candidate"] or state.get("activation_baseline") != baseline_id:
            return False
        state["activation_baseline"] = snapshot_id
        write(folder, "state.json", state)
        return True


def source_summary(root):
    name = latest_run(root)
    result = report(root, name)
    return {"sources": result["counts"]["discovered"], "states": result["counts"]["states"],
            "snapshot": result["candidate"]["activated"] if result["candidate"] else None,
            "details": str(Path(root) / "runs" / name / "report.json")}


def benchmark(registry, output):
    start = time.monotonic()
    store = store_at(registry)
    current = store.current()
    query_start = time.monotonic()
    for _ in range(POLICY["load_budget"]["min_queries"]):
        store.call("search_evidence", {"query": "MGUS", "collections": ["literature"], "limit": 10})
    query_seconds = time.monotonic() - query_start
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result = {
        "registry_snapshot": current.release_id, "host": {"system": platform.system(),
          "python": platform.python_version(), "cpu_count": os.cpu_count(),
          "physical_memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
          "registry_free_disk_bytes": shutil.disk_usage(registry).free},
        "source_count": len(current.snapshots["literature"]._bundle["sources"]),
        "unit_count": len(current.snapshots["literature"]._bundle["units"]),
        "catalog_count": current.snapshots["catalog"].status()["paper_count"],
        "seconds": time.monotonic() - start, "peak_rss_mib": usage.ru_maxrss / (1024 if sys.platform != "darwin" else 1024 ** 2),
        "queries": POLICY["load_budget"]["min_queries"],
        "mean_query_seconds": query_seconds / POLICY["load_budget"]["min_queries"],
        "accepted_budget": POLICY["load_budget"], "scientific_approval": False,
    }
    result["passed"] = (result["seconds"] <= POLICY["load_budget"]["max_seconds"]
                        and result["peak_rss_mib"] <= POLICY["load_budget"]["max_peak_rss_mib"]
                        and result["mean_query_seconds"] <= POLICY["load_budget"]["max_mean_query_seconds"])
    write(Path(output).parent, Path(output).name, result)
    if not result["passed"]:
        raise ValueError("Representative load exceeds documented budget; inspect report")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    data_root = Path(os.environ.get("RESEARCH_LIBRARY_DATA_ROOT", ".")).absolute()
    parser.add_argument("--root", type=Path, default=data_root / "releases/source-updates")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="Update sources now, saving progress and applying valid additions")
    commands.add_parser("status", help="Show source counts and pending downloads from the last update")
    init = commands.add_parser("init")
    for key in ("acquisition", "extraction", "papers", "registry"):
        init.add_argument("--" + key, type=Path, required=True)
    init.add_argument("--authorization", required=True)
    for name in ("discover", "run"):
        command = commands.add_parser(name)
        command.add_argument("--id", required=True)
        command.add_argument("--mode", choices=("incremental", "reconcile"), default="incremental")
        command.add_argument("--through", default=date.today().isoformat())
        command.add_argument("--manifest", type=Path)
    for name in ("ingest", "stage", "report", "activate", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--id", required=True)
        if name in {"activate", "rollback"}:
            command.add_argument("--registry", required=True, type=Path)
            command.add_argument("--approve", action="store_true")
        if name == "ingest":
            command.add_argument("--retry-failed", action="store_true")
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--destination", required=True, type=Path)
    load = commands.add_parser("benchmark")
    load.add_argument("--registry", required=True, type=Path)
    load.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if (args.command in {"discover", "run", "ingest", "stage", "activate", "rollback"}
                and args.root.resolve() == (data_root / "releases/source-updates").resolve()
                and (args.root / "config.json").exists()):
            check_local_paths(read(args.root / "config.json"), data_root)
        if args.command == "refresh":
            result = refresh(args.root, data_root)
        elif args.command == "status":
            result = source_summary(args.root)
        elif args.command == "init":
            result = initialize(args.root, args.acquisition, args.extraction, args.papers,
                                args.registry, args.authorization)
        elif args.command in {"discover", "run"}:
            result = discover(args.root, args.id, mode=args.mode, through=args.through, manifest=args.manifest)
            result = {key: value for key, value in result.items() if key not in {"graph", "records", "pages"}}
            if args.command == "run":
                process(args.root, args.id)
                result = stage(args.root, args.id)
        elif args.command == "ingest":
            result = process(args.root, args.id, retry_failed=args.retry_failed)
        elif args.command == "stage":
            result = stage(args.root, args.id)
        elif args.command == "report":
            result = report(args.root, args.id)
        elif args.command in {"activate", "rollback"}:
            result = {"snapshot_id": (activate if args.command == "activate" else rollback)(
                args.root, args.id, args.registry, approve=args.approve)}
        elif args.command in {"backup", "restore"}:
            result = {"identity": (backup if args.command == "backup" else restore)(args.source, args.destination)}
        else:
            result = benchmark(args.registry, args.output)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, httpx.HTTPError,
            pymupdf.FileDataError, ValidationError) as error:
        print(f"Literature update failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
