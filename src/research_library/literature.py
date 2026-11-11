"""Auditable metadata-only literature discovery; no evidence extraction or scientific approval."""

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from jsonschema import ValidationError

from . import releases
from .collections import atomic_json

POLICY_VERSION = "mgus-smm-mm-journals-2"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DISEASES = {
    "mgus": ('"monoclonal gammopathy of undetermined significance"', '"MGUS"',
             '"monoclonal gammopathy of unknown significance"'),
    "smm": ('"smoldering multiple myeloma"', '"smouldering multiple myeloma"',
            '"smoldering myeloma"', '"smouldering myeloma"', '"asymptomatic multiple myeloma"'),
    "mm": ('"multiple myeloma"',),
}
EXCLUDED_TYPES = {
    "case reports", "case report", "editorial", "letter", "comment", "news",
    "congress", "congresses", "conference abstract", "meeting abstract", "preprint",
    "published erratum", "retraction of publication", "expression of concern",
    "retracted publication", "study protocol", "clinical trial protocol",
    "case-report", "meeting-abstract", "conference-paper", "conference proceedings",
    "abstract", "protocol",
}
INCLUDED_TYPES = {
    "journal article", "research-article", "review", "review-article", "systematic review",
    "meta-analysis", "guideline", "practice guideline", "consensus development conference",
}


class DiscoveryError(ValueError):
    """Discovery failed or provider metadata cannot be interpreted safely."""


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryError(f"Missing or invalid {name}")
    return value


def strings(value, name):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DiscoveryError(f"Invalid {name}")
    return value


def policy():
    return {
        "version": POLICY_VERSION,
        "languages": "all", "earliest_date": "1000-01-01",
        "included_publication_types": sorted(INCLUDED_TYPES),
        "excluded_publication_types": sorted(EXCLUDED_TYPES),
        "unknown_eligibility": "retain_for_review",
        "notices": "Retain corrections, retractions and concerns for audit, not as primary evidence.",
        "reference_classification_checks": [
            {"identifier": "pmid:42571962", "types": ["Journal Article", "Review"],
             "decision": "included", "checked_at": "2026-10-19"},
            {"identifier": "pmid:42431702", "types": ["Journal Article", "Case Reports"],
             "decision": "excluded", "checked_at": "2026-10-19"},
        ],
        "preprints": "excluded", "abstracts": "Not extracted or served as evidence by this command.",
        "disease_terms": DISEASES,
        "limitations": [
            "Discovery topic tags indicate query membership, not validated disease classification.",
            "PubMed supplies controlled MeSH-heading coverage; Europe PMC uses verified TITLE_ABS fields only.",
            "No language restriction; source indexing, translation and terminology gaps remain.",
            "Names are not author identities; no author-name based merge or iStopMM attribution.",
            "Provider publication types do not establish peer review or study quality.",
            "API searches are not transactional snapshots and do not guarantee completeness.",
            "Full-text availability is not a redistribution permission.",
        ],
    }


def query(provider, disease, start, through, focus="all"):
    terms = DISEASES[disease]
    if provider == "pubmed":
        topic = " OR ".join(term + "[Title/Abstract]" for term in terms)
        if disease == "mm":
            topic += ' OR "Multiple Myeloma"[MeSH Terms]'
        elif disease == "mgus":
            topic += ' OR "Monoclonal Gammopathy of Undetermined Significance"[MeSH Terms]'
        expression = f'({topic}) AND ("{start}"[Date - Publication] : "{through}"[Date - Publication])'
        if focus == "reviews":
            expression += ' AND (Review[Publication Type] OR Meta-Analysis[Publication Type] OR Guideline[Publication Type])'
        return expression
    topic = " OR ".join("TITLE_ABS:" + term for term in terms)
    expression = f"({topic}) AND FIRST_PDATE:[{start} TO {through}]"
    if focus == "reviews":
        expression += ' AND (PUB_TYPE:"Review" OR PUB_TYPE:"Meta-Analysis" OR PUB_TYPE:"Guideline")'
    return expression


def eligibility(types):
    lower = {value.lower() for value in types}
    blocked = sorted(lower & EXCLUDED_TYPES)
    if blocked:
        return {"decision": "excluded", "reason": "Excluded publication types: " + ", ".join(blocked)}
    if lower & INCLUDED_TYPES:
        return {"decision": "included", "reason": "Provider type matches the journal/review/guideline policy."}
    return {"decision": "needs_review", "reason": "No unambiguous eligible provider publication type."}


class MetadataClient:
    """Conservative pacing, bounded retries, and optional OpenAlex authentication."""

    def __init__(self, *, sleep=time.sleep, clock=time.monotonic, transport=None, openalex_api_key=None):
        self.sleep, self.clock = sleep, clock
        self.openalex_api_key = openalex_api_key
        self.last_request = None
        self.http = httpx.Client(
            timeout=httpx.Timeout(30), follow_redirects=False, transport=transport,
            headers={"User-Agent": "research-library-literature/0.2 (metadata-discovery)"},
        )

    def close(self):
        self.http.close()

    def get(self, url, params):
        headers = {}
        if self.openalex_api_key and httpx.URL(url).host == "api.openalex.org":
            headers["Authorization"] = "Bearer " + self.openalex_api_key
        for attempt in range(4):
            if self.last_request is not None:
                self.sleep(max(0, 0.5 - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            try:
                with self.http.stream("GET", url, params=params, headers=headers) as response:
                    if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                        retry = response.headers.get("Retry-After")
                        delay = 2 ** attempt
                        if retry:
                            try:
                                delay = float(retry)
                            except ValueError:
                                try:
                                    delay = (parsedate_to_datetime(retry) -
                                             datetime.now(timezone.utc)).total_seconds()
                                except (ValueError, TypeError, OverflowError) as error:
                                    raise DiscoveryError("Invalid provider Retry-After header") from error
                            if not 0 <= delay <= 120:
                                raise DiscoveryError("Provider requires a later retry; resume this run later")
                        if attempt == 3:
                            raise DiscoveryError(f"Provider unavailable after bounded retries (HTTP {response.status_code})")
                        self.sleep(delay)
                        continue
                    if response.status_code != 200:
                        raise DiscoveryError(f"Provider rejected metadata request (HTTP {response.status_code})")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise DiscoveryError("Provider response exceeds metadata byte limit")
                    try:
                        parsed = releases.read_json(bytes(raw))
                    except ValueError as error:
                        raise DiscoveryError("Provider returned invalid JSON metadata") from error
                    if not isinstance(parsed, dict) or parsed.get("error"):
                        raise DiscoveryError("Provider returned an invalid or failed metadata response")
                    return parsed, {
                        "url": str(response.request.url), "retrieved_at": utcnow(),
                        "sha256": releases.sha256(bytes(raw)), "raw": bytes(raw).decode("utf-8"),
                    }
            except httpx.TransportError as error:
                if attempt == 3:
                    raise DiscoveryError("Provider transport failed after bounded retries") from error
                self.sleep(2 ** attempt)
        raise DiscoveryError("Metadata request did not complete")


def normalize(provider, value, disease):
    if not isinstance(value, dict):
        raise DiscoveryError("Invalid provider publication record")
    if provider == "pubmed":
        pid = text(value.get("uid"), "PubMed UID")
        if not pid.isdigit():
            raise DiscoveryError("Invalid PubMed UID")
        ids = {"pmid": pid}
        articleids = value.get("articleids", [])
        if not isinstance(articleids, list):
            raise DiscoveryError("Invalid PubMed identifier list")
        for item in articleids:
            if not isinstance(item, dict):
                raise DiscoveryError("Invalid PubMed identifier")
            kind = item.get("idtype")
            if kind in ("doi", "pmc"):
                normalized_kind = "pmcid" if kind == "pmc" else kind
                identifier = text(item.get("value"), "publication identifier")
                if normalized_kind in ids and ids[normalized_kind] != identifier:
                    raise DiscoveryError("Conflicting identifiers within provider record")
                ids[normalized_kind] = identifier
        types = strings(value.get("pubtype", []), "PubMed publication types")
        languages = strings(value.get("lang", []), "PubMed languages")
        published = value.get("pubdate", "")
        authors = value.get("authors", [])
        if not isinstance(authors, list) or any(not isinstance(a, dict) or not isinstance(a.get("name"), str) for a in authors):
            raise DiscoveryError("Invalid PubMed authors")
        authors = [a["name"] for a in authors]
        source_url = f"https://pubmed.ncbi.nlm.nih.gov/{pid}/"
        abstract_available = "Has Abstract" in strings(value.get("attributes", []), "PubMed attributes")
        full_text_available = "pmcid" in ids
        source = "MED"
    else:
        pid = text(value.get("id"), "Europe PMC ID")
        source = text(value.get("source"), "Europe PMC source")
        if not re.fullmatch(r"[A-Z]+", source) or not re.fullmatch(r"[A-Za-z0-9.-]+", pid):
            raise DiscoveryError("Invalid Europe PMC source identity")
        ids = {}
        for name in ("doi", "pmid", "pmcid"):
            if value.get(name):
                ids[name] = text(value[name], name)
        type_list = value.get("pubTypeList", {})
        if not isinstance(type_list, dict):
            raise DiscoveryError("Invalid Europe PMC publication type container")
        types = strings(type_list.get("pubType", []), "Europe PMC publication types")
        languages = [value["language"]] if value.get("language") else []
        published = value.get("firstPublicationDate", value.get("pubYear", ""))
        authors = [value["authorString"]] if value.get("authorString") else []
        source_url = f"https://europepmc.org/article/{source}/{pid}"
        abstract_available = bool(value.get("abstractText"))
        full_text_available = value.get("isOpenAccess") == "Y" or bool(value.get("pmcid"))
    if (not isinstance(published, str) or any(not isinstance(value, str) for value in languages + authors)):
        raise DiscoveryError("Invalid provider publication metadata")
    if "doi" in ids:
        ids["doi"] = re.sub(r"^(?:https?://doi\.org/|doi:\s*)", "", ids["doi"], flags=re.I).lower()
    journal = value.get("fulljournalname")
    if provider == "europepmc":
        info = value.get("journalInfo", {})
        if not isinstance(info, dict) or not isinstance(info.get("journal", {}), dict):
            raise DiscoveryError("Invalid provider journal metadata")
        journal = info.get("journal", {}).get("title")
    if journal is not None and not isinstance(journal, str):
        raise DiscoveryError("Invalid journal title")
    relations = []
    if provider == "europepmc":
        container = value.get("commentCorrectionList", {})
        if not isinstance(container, dict):
            raise DiscoveryError("Invalid provider relationship container")
        corrections = container.get("commentCorrection", [])
        if not isinstance(corrections, list):
            raise DiscoveryError("Invalid provider notice relationships")
        kinds = {"erratumfor": "corrects", "retractionof": "retracts",
                 "expressionofconcernfor": "concerns"}
        for link in corrections:
            if not isinstance(link, dict):
                raise DiscoveryError("Invalid provider relationship")
            kind = kinds.get(re.sub(r"[^a-z]", "", str(link.get("type", "")).lower()))
            if kind and link.get("source") == "MED" and str(link.get("id", "")).isdigit():
                relations.append({"kind": kind, "target": "pmid:" + str(link["id"]),
                                  "provenance": "Europe PMC commentCorrectionList: " + link["type"]})
    title = value.get("title")
    if title is not None and not isinstance(title, str):
        raise DiscoveryError("Invalid publication title")
    decision = eligibility(types)
    if title is None or not title.strip():
        title = ""
        decision = {"decision": "excluded" if decision["decision"] == "excluded" else "needs_review",
                    "reason": decision["reason"] + " Provider supplied no title; bibliographic review required."}
        print(json.dumps({"event": "metadata_incomplete", "provider": provider,
                          "provider_id": pid, "field": "title"}), file=sys.stderr)
    result = {
        "provider": provider, "provider_source": source, "provider_id": pid,
        "identifiers": ids, "title": title,
        "publication_date": published, "languages": languages, "authors": authors,
        "journal": journal, "relations": relations,
        "publication_types": types, "query_topics": [disease], "eligibility": decision,
        "publication_state": "retracted" if "retracted publication" in {t.lower() for t in types}
                             else "notice" if {t.lower() for t in types} & {
                                 "published erratum", "retraction of publication", "expression of concern"}
                             else "preprint" if source == "PPR" or "preprint" in {t.lower() for t in types}
                             else "unknown",
        "study_design": ["unknown"], "peer_review_status": "not_verified",
        "source_url": source_url,
        "availability": {
            "metadata": True, "abstract_reported": abstract_available,
            "full_text_reported": full_text_available, "full_text_acquired": False,
            "redistribution_authorized": False,
        },
        "extraction_status": "not_extracted", "claim_review_status": "not_reviewed",
    }
    if source == "PPR":
        result["eligibility"] = {"decision": "excluded", "reason": "Preprints excluded by the default policy."}
    return result


def count(value, name):
    if type(value) is not int or value < 0:
        raise DiscoveryError(f"Invalid provider {name}")
    return value


def fetch_page(client, config, cursor):
    provider, disease = config["provider"], config["disease"]
    page_size = config["page_size"]
    provenance = []
    if provider == "europepmc":
        expression = (config["query"] if "query" in config else
                      query(provider, disease, config["from_date"], config["through_date"], config.get("focus", "all")))
        payload, raw = client.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
            "query": expression, "format": "json", "resultType": "core", "pageSize": page_size,
            "cursorMark": cursor["mark"],
        })
        provenance.append(raw)
        total = count(payload.get("hitCount"), "hit count")
        result_list = payload.get("resultList")
        if not isinstance(result_list, dict):
            raise DiscoveryError("Invalid Europe PMC result container")
        values = result_list.get("result")
        if not isinstance(values, list) or len(values) > page_size:
            raise DiscoveryError("Invalid Europe PMC result page")
        if cursor["total"] is not None and cursor["total"] != total:
            raise DiscoveryError("Europe PMC hit count changed; start a fresh discovery snapshot")
        records = [normalize(provider, value, disease) for value in values]
        next_mark = payload.get("nextCursorMark")
        seen = cursor["seen"] + len(records)
        if seen > total or (not records and seen < total):
            raise DiscoveryError("Europe PMC pagination is inconsistent")
        done = seen == total
        if not done and (not isinstance(next_mark, str) or next_mark == cursor["mark"]):
            raise DiscoveryError("Europe PMC cursor did not advance")
        next_cursor = {"mark": next_mark, "seen": seen, "total": total}
        return records, provenance, next_cursor, done

    # PubMed ESearch exposes at most 9,999 IDs per query. Split date windows
    # until each shard is below that ceiling; never silently truncate a search.
    windows = [dict(window) for window in cursor["windows"]]
    while windows:
        window = windows[0]
        expression = query(provider, disease, window["start"], window["end"], config.get("focus", "all"))
        search, raw = client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", {
            "db": "pubmed", "retmode": "json", "term": expression, "sort": "pub_date",
            "retstart": window["offset"], "retmax": page_size, "tool": "research_library",
        })
        provenance.append(raw)
        result = search.get("esearchresult")
        if not isinstance(result, dict):
            raise DiscoveryError("Invalid PubMed result container")
        warnings = result.get("warninglist", {})
        if not isinstance(warnings, dict) or result.get("errorlist") or warnings.get("phrasesnotfound"):
            raise DiscoveryError("PubMed search could not interpret the requested query")
        try:
            total = int(result["count"])
        except (KeyError, ValueError, TypeError) as error:
            raise DiscoveryError("Invalid PubMed hit count") from error
        count(total, "hit count")
        if total > 9999:
            start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
            if start == end:
                raise DiscoveryError("PubMed one-day shard exceeds the ESearch limit; bulk discovery is required")
            middle = start + (end - start) // 2
            windows[:1] = [
                {"start": start.isoformat(), "end": middle.isoformat(), "offset": 0, "total": None},
                {"start": (middle + timedelta(days=1)).isoformat(), "end": end.isoformat(),
                 "offset": 0, "total": None},
            ]
            continue
        if window["total"] is not None and window["total"] != total:
            raise DiscoveryError("PubMed shard hit count changed; start a fresh discovery snapshot")
        ids = strings(result.get("idlist"), "PubMed ID page")
        expected = min(page_size, total - window["offset"])
        if expected < 0 or len(ids) != expected or len(set(ids)) != len(ids) or any(not pid.isdigit() for pid in ids):
            raise DiscoveryError("PubMed pagination is inconsistent")
        records = []
        if ids:
            summary, raw = client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi", {
                "db": "pubmed", "retmode": "json", "id": ",".join(ids), "tool": "research_library",
            })
            provenance.append(raw)
            values = summary.get("result")
            if not isinstance(values, dict) or values.get("uids") != ids:
                raise DiscoveryError("PubMed summary omitted or reordered requested records")
            records = [normalize(provider, values.get(pid), disease) for pid in ids]
            if [record["provider_id"] for record in records] != ids:
                raise DiscoveryError("PubMed summary identity mismatch")
        window.update(offset=window["offset"] + len(records), total=total)
        if window["offset"] == total:
            windows.pop(0)
        return records, provenance, {"windows": windows}, not windows
    raise DiscoveryError("PubMed cursor has no remaining windows")


def pubmed_shard(requests):
    queries = [parse_qs(urlsplit(request["url"]).query).get("term", [None])[0]
               for request in requests if urlsplit(request["url"]).path.endswith("/esearch.fcgi")]
    return queries[-1] if queries else None


def validate_run(root, *, retain_metadata=True, shards=None):
    if root.is_symlink() or not root.is_dir():
        raise DiscoveryError("Run must be a real directory")
    state_path = releases.safe_file(root, "state.json")
    if state_path.stat().st_size > MAX_RESPONSE_BYTES:
        raise DiscoveryError("Discovery checkpoint exceeds byte limit")
    state = releases.read_json(state_path.read_bytes())
    if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
            or state["schema_version"] != 1 or not isinstance(state.get("pages"), list)
            or not isinstance(state.get("policy"), dict) or not isinstance(state.get("config"), dict)
            or state.get("status") not in ("complete", "incomplete")):
        raise DiscoveryError("Invalid discovery checkpoint")
    if releases.sha256(releases.encode(state["policy"])) != state["config"].get("policy_sha256"):
        raise DiscoveryError("Discovery policy checksum mismatch")
    records = {}
    shards = shards if shards is not None else {}
    duplicate_count = 0
    for index, expected in enumerate(state["pages"]):
        path = releases.safe_file(root, f"page-{index:06}.json")
        if path.stat().st_size > 64 * 1024 * 1024:
            raise DiscoveryError("Discovery page exceeds byte limit")
        data = path.read_bytes()
        if releases.sha256(data) != expected:
            raise DiscoveryError("Discovery page checksum mismatch")
        page = releases.read_json(data)
        if index == len(state["pages"]) - 1 and page["next_cursor"] != state["cursor"]:
            raise DiscoveryError("Checkpoint differs from the last committed cursor")
        for raw in page["requests"]:
            if releases.sha256(raw["raw"].encode()) != raw["sha256"]:
                raise DiscoveryError("Raw metadata response checksum mismatch")
        shard = pubmed_shard(page["requests"]) if state["config"]["provider"] == "pubmed" else None
        for record in page["records"]:
            key = record["provider"] + ":" + record["provider_source"] + ":" + record["provider_id"]
            stored = record if retain_metadata else releases.sha256(releases.encode(record))
            if key in records:
                duplicate_count += 1
                if records[key] != stored:
                    raise DiscoveryError("Publication metadata changed within the same discovery run")
                if shard is None or shard in shards[key]:
                    raise DiscoveryError("Provider repeated a record within a query shard; restart rather than claim complete coverage")
            records[key] = stored
            shards.setdefault(key, set()).add(shard)
    return state, records, duplicate_count


def report(root):
    state, records, duplicates = validate_run(root)
    decisions = {decision: sum(record["eligibility"]["decision"] == decision for record in records.values())
                 for decision in ("included", "excluded", "needs_review")}
    return {
        "schema_version": 1, "policy_version": state["config"]["policy_version"], "config": state["config"],
        "policy": state["policy"], "status": state["status"], "complete": state["status"] == "complete",
        "started_at": state["started_at"], "updated_at": state["updated_at"],
        "pages": len(state["pages"]), "discovered_unique": len(records), "duplicates": duplicates,
        **decisions, "last_error": state["last_error"],
        "scope": "Provider metadata discovery only; not a catalog release, extracted evidence or scientific validation.",
    }


def bibliography(root):
    state, records, _ = validate_run(root)
    entries = []
    for index in range(len(state["pages"])):
        page_name = f"page-{index:06}.json"
        page = releases.read_json(releases.safe_file(root, page_name).read_bytes())
        origins = [{key: request[key] for key in ("url", "retrieved_at", "sha256")}
                   for request in page["requests"]]
        for record in page["records"]:
            entries.append({
                "record_id": ":".join(record[key] for key in ("provider", "provider_source", "provider_id")),
                **{key: record[key] for key in (
                    "identifiers", "title", "authors", "publication_date", "source_url", "eligibility")},
                "metadata_acquisition": origins, "provenance_page": page_name,
                "full_text_acquired": False, "full_text_acquisition": None,
                "used_as_evidence": False,
            })
    return {
        "schema_version": 1, "status": state["status"], "record_count": len(records),
        "discovery_checkpoint_sha256": releases.sha256(releases.encode(state)),
        "scope": "Discovery citations, including excluded records; not an evidence bibliography. "
                 "No cross-provider deduplication yet. Acquisition refers to metadata, not full text.",
        "items": sorted(entries, key=lambda entry: entry["record_id"]),
    }


def discover(root, *, provider, disease, through, start="1000-01-01", page_size=25, max_pages=1, client=None,
             focus="all", order="provider"):
    if provider not in ("pubmed", "europepmc") or disease not in DISEASES or focus not in ("all", "reviews"):
        raise DiscoveryError("Unsupported provider or disease")
    if order not in ("provider", "publication-date") or (order != "provider" and provider != "europepmc"):
        raise DiscoveryError("Publication-date cursor ordering is supported for Europe PMC only")
    if (type(page_size) is not int or not 1 <= page_size <= 100
            or type(max_pages) is not int or not 1 <= max_pages <= 1000):
        raise DiscoveryError("Use page_size 1-100 and max_pages 1-1000 per invocation")
    if not date(1000, 1, 1) <= date.fromisoformat(start) <= date.fromisoformat(through) <= date.today():
        raise DiscoveryError("Use a valid publication date range, no later than today")
    root = Path(root).absolute()
    if root.is_symlink():
        raise DiscoveryError("Discovery root cannot be a symlink")
    config = {
        "provider": provider, "disease": disease, "from_date": start, "through_date": through,
        "page_size": page_size, "policy_version": POLICY_VERSION,
        "query": query(provider, disease, start, through, focus),
        "policy_sha256": releases.sha256(releases.encode(policy())),
    }
    if focus != "all":
        config["focus"] = focus
    if order != "provider":
        config.update(order=order, query=config["query"] + " sort_date:y")
    known_shards = {}
    if root.exists():
        state, known_records, _ = validate_run(root, shards=known_shards)
        if state["config"] != config:
            raise DiscoveryError("Resume configuration differs; use the original parameters or a new directory")
    else:
        known_records = {}
        state = {
            "schema_version": 1, "config": config, "status": "incomplete", "last_error": None,
            "policy": policy(),
            "started_at": utcnow(), "updated_at": utcnow(), "pages": [],
            "cursor": {"mark": "*", "seen": 0, "total": None} if provider == "europepmc" else
                      {"windows": [{"start": start, "end": through, "offset": 0, "total": None}]},
        }
        root.mkdir(parents=True)
        atomic_json(root, "state.json", state)
    if state["status"] == "complete":
        return report(root)
    owned = client is None
    client = client or MetadataClient()
    try:
        for _ in range(max_pages):
            records, raw, cursor, done = fetch_page(client, config, state["cursor"])
            new_records = {}
            shard = pubmed_shard(raw) if provider == "pubmed" else None
            for record in records:
                key = record["provider"] + ":" + record["provider_source"] + ":" + record["provider_id"]
                if key in new_records:
                    raise DiscoveryError("Provider repeated a record within a page")
                if key in known_records:
                    if known_records[key] != record or shard is None or shard in known_shards[key]:
                        raise DiscoveryError("Provider repeated or changed a record within a query shard; restart the run")
                    print(json.dumps({"event": "discovery_date_overlap", "provider": provider,
                                      "provider_id": record["provider_id"]}), file=sys.stderr)
                new_records[key] = record
            page = {"records": records, "requests": raw, "next_cursor": cursor}
            name = f"page-{len(state['pages']):06}.json"
            # A page left by a crash before checkpoint replacement is deliberately
            # replayed and replaced. Only checksummed checkpoint pages are committed.
            atomic_json(root, name, page)
            state["pages"].append(releases.sha256(releases.encode(page)))
            state.update(cursor=cursor, status="complete" if done else "incomplete",
                         updated_at=utcnow(), last_error=None)
            atomic_json(root, "state.json", state)
            known_records.update(new_records)
            for key in new_records:
                known_shards.setdefault(key, set()).add(shard)
            print(json.dumps({"event": "discovery_page", "provider": provider, "disease": disease,
                              "page": len(state["pages"]), "records": len(records), "complete": done}),
                  file=sys.stderr, flush=True)
            if done:
                break
        return report(root)
    except (DiscoveryError, OSError, ValueError, KeyError, TypeError) as error:
        state.update(status="incomplete", last_error=type(error).__name__, updated_at=utcnow())
        atomic_json(root, "state.json", state)
        raise
    finally:
        if owned:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("policy")
    discovery = commands.add_parser("discover")
    discovery.add_argument("--provider", required=True, choices=("pubmed", "europepmc"))
    discovery.add_argument("--disease", required=True, choices=sorted(DISEASES))
    discovery.add_argument("--through", required=True, help="Explicit publication as-of date YYYY-MM-DD")
    discovery.add_argument("--from-date", default="1000-01-01")
    discovery.add_argument("--page-size", type=int, default=25)
    discovery.add_argument("--max-pages", type=int, default=1, help="Additional pages this invocation; incomplete exits 2")
    discovery.add_argument("--run", required=True, type=Path)
    discovery.add_argument("--focus", choices=("all", "reviews"), default="all",
                           help="Prioritize starter reviews; all retains excluded types for general discovery")
    discovery.add_argument("--order", choices=("provider", "publication-date"), default="provider",
                           help="Europe PMC only: publication-date ordering is recorded in a new run's query")
    inspection = commands.add_parser("report")
    inspection.add_argument("--run", required=True, type=Path)
    citations = commands.add_parser("bibliography", help="Save and print an auditable metadata citation list")
    citations.add_argument("--run", required=True, type=Path)
    catalog = commands.add_parser("catalog", help="Build an immutable metadata catalog from discovery runs")
    catalog.add_argument("--run", required=True, type=Path, action="append")
    catalog.add_argument("--as-of", required=True)
    catalog.add_argument("--rationale", required=True)
    catalog.add_argument("--metadata-authorization-basis", required=True)
    catalog.add_argument("--istopmm-manifest", type=Path)
    catalog.add_argument("--relationships", type=Path,
                         help="Optional explicit typed relationships with source/target IDs and provenance")
    catalog.add_argument("--output", type=Path, required=True)
    inspection = commands.add_parser("catalog-report")
    inspection.add_argument("catalog", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "policy":
            result = policy()
        elif args.command == "report":
            result = report(args.run)
        elif args.command == "bibliography":
            result = bibliography(args.run)
            atomic_json(args.run, "bibliography.json", result)
        elif args.command == "catalog":
            from .catalog import build, coverage
            dois = []
            if args.istopmm_manifest:
                with args.istopmm_manifest.open(newline="") as stream:
                    dois = [row["doi"] for row in csv.DictReader(stream) if row["doi"]]
            result = build(args.run, as_of=args.as_of, rationale=args.rationale,
                             authorization_basis=args.metadata_authorization_basis, istopmm_dois=dois,
                             relationships=releases.read_json(args.relationships.read_bytes())
                             if args.relationships else [],
                             sqlite_output=args.output if args.output.suffix == ".sqlite" else None)
            if args.output.suffix != ".sqlite":
                if args.output.exists() or args.output.is_symlink():
                    raise DiscoveryError("Catalog output already exists; choose a new version path")
                atomic_json(args.output.parent, args.output.name, result)
                result = coverage(result)
        elif args.command == "catalog-report":
            from .catalog import open_snapshot
            result = open_snapshot(args.catalog).coverage()
        else:
            result = discover(args.run, provider=args.provider, disease=args.disease, through=args.through,
                              start=args.from_date, page_size=args.page_size, max_pages=args.max_pages,
                              focus=args.focus, order=args.order)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("complete") is False else 0
    except (OSError, ValueError, KeyError, TypeError, ValidationError, sqlite3.Error) as error:
        print(f"Metadata discovery failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
