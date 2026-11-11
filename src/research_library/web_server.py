"""Local-only information website; separate from the authenticated MCP transport."""

import argparse
from collections import Counter
from contextlib import closing
import csv
from datetime import date
from functools import lru_cache
import hmac
from html import escape
from importlib.metadata import version
import json
import re
import secrets
import shlex
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

from jsonschema import ValidationError
from flask import Flask, Response, has_request_context, jsonify, redirect, request

from . import __version__, catalog, collections, contracts, library_api, literature, quality, releases, retrieval
from .http_auth import resource_url
from .server import DESCRIPTIONS, diagnostic


NAVIGATION = (
    ("Research", (("/", "Overview"), ("/search", "Search evidence"), ("/discovery", "Find related papers"),
                  ("/connect", "Connect your AI"))),
    ("Library", (("/library", "Library"), ("/sources", "Sources available to AI"),
                 ("/publications", "Publication metadata"), ("/coverage", "Literature coverage"))),
    ("Manage", (("/quality", "Data quality"), ("/settings", "Project settings"))),
)

DISCOVERY_LABELS = {"background": "Find references", "impact": "Find citing papers"}


def discovery_label(job):
    return DISCOVERY_LABELS.get(job.get("direction"), job["label"])


def help_note(label, body):
    """Disclosure for trusted explanatory HTML, with a native no-script fallback."""
    return (f'<details class="help"><summary aria-label="Help: {escape(label, quote=True)}">?</summary>'
            f'<div class="help-content"><strong>{escape(label)}</strong>{body}</div></details>')


def page(title, body, *, reader_script=False, settings_script=False, library_script=False):
    current = request.path if has_request_context() else ""
    groups = "".join(
        f'<p class="nav-group">{group}</p>' + "".join(
            f'<a href="{href}"' + (' aria-current="page"' if href == current else "") + f">{label}</a>"
            for href, label in links)
        for group, links in NAVIGATION)
    quick_search = ("" if current == "/search" else
                    '<form class="quick-search" method="get" action="/search" role="search">'
                    '<label class="visually-hidden" for="quick-query">Search evidence</label>'
                    '<input id="quick-query" name="query" type="search" placeholder="Search papers" required>'
                    '<button type="submit" class="secondary">Go</button></form>')
    return Response(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<link rel="stylesheet" href="/style.css">'
        '<script src="/help.js" defer></script>'
        + ('<script src="/reader.js" defer></script>' if reader_script else "")
        + ('<script src="/settings.js" defer></script>' if settings_script else "")
        + ('<script src="/library.js" defer></script>' if library_script else "")
        + f'<title>{escape(title)} | Evidence</title>'
        '<body><a class="skip-link" href="#main">Skip to content</a>'
        '<div class="shell"><header class="rail">'
        '<h1 class="brand"><a href="/">Research Evidence</a></h1>'
        + quick_search + f'<nav class="sidenav" aria-label="Main">{groups}</nav></header>'
        f'<div class="content"><main id="main"><h2>{escape(title)}</h2>{body}</main>'
        '<footer class="site-footer">Private research library '
        + help_note("About this library", "<p>Original files are never served; "
                    "machine extraction is not scientific validation.</p>")
        + '</footer></div></div></body></html>', mimetype="text/html")


def status_tone(label):
    """Visual tone for a status label; the label text itself is always shown."""
    text = label.casefold()
    if any(word in text for word in ("fail", "no licensed", "no native", "unsupported", "could not", "removed")):
        return "bad"
    if any(word in text for word in ("awaiting", "partial", "incomplete", "ocr", "review", "not extracted", "being added",
                                      "waiting", "required")):
        return "warn"
    if any(word in text for word in ("extracted", "downloaded", "provided", "original", "added")):
        return "ok"
    return "neutral"


def badge(label):
    """Status pill around already-escaped label HTML."""
    return f'<span class="badge {status_tone(label)}">{label}</span>'


def metadata_value(value, *, project_scope=False):
    if value is None:
        return '<span class="metadata-empty">Unknown / not recorded</span>'
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        if project_scope and isinstance(value.get("runs"), list):
            reasons = [reason.strip() for reason in value.get("scope", "").split("Incomplete coverage:")[1:]
                       if reason.strip()]
            runs = []
            for run in value["runs"]:
                if run.get("policy_version") == "project-first-tier-1" and run.get("status") == "incomplete":
                    explained = {}
                    for key, item in run.items():
                        explained[key] = item
                        if key == "status":
                            explained["why_incomplete"] = reasons or [
                                "This saved catalog does not include a detailed reason. Inspect the saved discovery searches."]
                            explained["next_steps"] = (
                                "Inspect Find related papers for unresolved identifiers and provider errors. "
                                "A completed search can still have incomplete coverage. Retry affected searches if appropriate, "
                                "then update the catalog; provider records may remain unresolved.")
                    run = explained
                runs.append(run)
            value = {**value, "runs": runs}
        if project_scope and value.get("policy_version") == "project-first-tier-1":
            value = {key: item for key, item in value.items() if key not in {"from_date", "through_date"}}
            value["publication_date_range"] = "Unrestricted (no publication-date filter)"
        if not value:
            return '<span class="metadata-empty">None recorded</span>'
        return '<dl class="metadata-fields">' + "".join(
            f"<dt>{escape(('core_library_overlap' if project_scope and key == 'istopmm_overlap' else key).replace('_', ' ').capitalize())}</dt>"
            f"<dd>{metadata_value(item, project_scope=project_scope)}</dd>"
            for key, item in value.items()
            if not (project_scope and key in {"disease", "diseases"})) + "</dl>"
    if isinstance(value, list):
        if not value:
            return '<span class="metadata-empty">None recorded</span>'
        return '<ul class="metadata-list">' + "".join(
            f"<li>{metadata_value(item, project_scope=project_scope)}</li>" for item in value) + "</ul>"
    text = str(value)
    if project_scope and text.startswith("Inspect Find related papers for unresolved identifiers"):
        return (
            '<ol><li><a href="/discovery?direction=background#coverage-issues">Review unresolved outgoing references</a> '
            'or <a href="/discovery?direction=impact#coverage-issues">review incoming-citation provider errors</a>.</li>'
            '<li>Use the retry button on the affected search if you want to check the provider again. '
            'Unresolved or removed provider records may still be missing after a retry.</li>'
            '<li>After the search finishes, <a href="/publications">update the metadata catalog</a>. '
            'You can also keep using the retrieved results without retrying; core PDFs are unaffected.</li></ol>')
    if not text:
        return '<span class="metadata-empty">Not recorded</span>'
    try:
        parsed = urlsplit(text)
    except ValueError:
        return escape(text)
    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
        return f'<a href="{escape(text, quote=True)}">Open link ({escape(parsed.hostname)})</a>'
    return escape(text)


def discovery_titles(graph):
    from .build.project_index import discovery_titles as titles
    return titles(graph)


def literature_coverage(item, *, project_scope=False):
    def count_table(title, rows):
        return (f'<section><h4>{escape(title)}</h4><table><thead><tr>'
                '<th scope="col">Category</th><th scope="col">Count</th></tr></thead><tbody>'
                + "".join(f'<tr><th scope="row">{escape(label)}</th><td>{value:,}</td></tr>'
                          for label, value in rows)
                + '</tbody></table></section>')

    metrics = (("publication_count", "Publications"), ("observation_count", "Metadata observations"),
               ("duplicates_collapsed", "Duplicate provider records merged"),
               ("full_text_not_reported_count", "Full-text availability unknown"))
    body = ('<article class="literature-coverage"><h3>Discovery catalog</h3>'
            f'<p>As of {escape(item["as_of"])} · '
            + badge("Searches complete" if item["complete_discovery"] else "Incomplete coverage")
            + '</p>'
            + help_note("Search completeness", "<p>A completed search is not exhaustive literature coverage. "
                        "Provider indexing and unresolved records can leave gaps. "
                        "Catalog counts describe metadata, not project-wide PDF availability.</p>")
            + '<dl class="coverage-metrics">'
            + "".join(f'<div><dt>{label}</dt><dd>{item[key]:,}</dd></div>' for key, label in metrics)
            + '</dl><div class="coverage-grid">'
            + count_table("Metadata quality", [
                ("Identity issues", item["identity_issues"]),
                ("Metadata conflicts", item["metadata_conflicts"]),
                ("Unresolved links", item["unresolved_links"]),
                ("Not researcher-reviewed", item["unreviewed_count"])])
            + '</div><details><summary>Publication breakdown</summary><div class="coverage-grid">')
    groups = [("Publication types", "publication_types"), ("Review eligibility", "eligibility"),
              ("Core papers overlap" if project_scope else "iStopMM overlap", "istopmm_overlap")]
    if not project_scope:
        groups.append(("Diseases", "diseases"))
    for title, key in groups:
        rows = [(label.replace("_", " ").capitalize(), value) for label, value in item[key].items()]
        if rows:
            body += count_table(title, rows)
    body += ('</div></details><details><summary>Additional catalog counts</summary>'
             + count_table("Catalog records", [
                 ("Provider records", item["provider_record_count"]),
                 ("Metadata-only publications", item["metadata_only_count"]),
                 ("Full text acquired within this catalog", item["full_text_acquired_count"])])
             + help_note("Catalog acquisition counts", "<p>These counts exclude separately attached Core papers "
                         "and discovered-paper PDFs. See Library for project PDF and extraction statuses.</p>")
             + '</details><details><summary>Search history and provenance</summary>'
             + metadata_value({key: item[key] for key in ("runs", "scope", "catalog_id")},
                              project_scope=project_scope) + '</details></article>')
    return body


def discovery_coverage(graph):
    states = {
        "complete_indexed_neighborhood": "Complete within provider index",
        "complete_deposited_references": "Complete deposited references",
        "partial_deposited_references": "Partial deposited references",
        "failed": "Failed",
    }
    body = '<details><summary>Coverage and limitations</summary><h4>Limitations</h4>'
    limitations = [text.replace("Requested directions: background, impact.", "Searched: References and Citing papers.")
                   .replace("Requested directions: background.", "Searched: References.")
                   .replace("Requested directions: impact.", "Searched: Citing papers.")
                   for text in graph["limitations"]]
    body += metadata_value(limitations)
    body += '<h4>Coverage by core paper</h4>'
    for seed in graph["seeds"]:
        body += '<section><h5>' + escape(seed.get("title") or seed["seed_doi"]) + '</h5>'
        body += f'<p>DOI: {escape(seed["seed_doi"])}</p>'
        for provider, result in seed["providers"].items():
            state = result["status"]
            body += f'<h6>{escape(provider.title())} — {badge(escape(states.get(state, state.replace("_", " ").capitalize())))}</h6>'
            fields = {}
            for kind, label in (("references", "References"), ("citations", "Citing papers")):
                for suffix, term in (("retrieved", "retrieved"), ("reported", "reported by provider")):
                    key = f"{kind}_{suffix}"
                    if key in result:
                        fields[f"{label} {term}"] = result[key]
            if result.get("requested_directions"):
                fields["Searched"] = [{"background": "References", "impact": "Citing papers"}.get(value, value)
                                      for value in result["requested_directions"]]
            if result.get("error"):
                fields["Provider error"] = result["error"]
            body += metadata_value(fields)
            provenance = {key: value for key, value in result.items()
                          if key not in {"status", "requested_directions", "error",
                                         "references_retrieved", "references_reported",
                                         "citations_retrieved", "citations_reported"}}
            if provenance:
                body += ('<details><summary>Provider details</summary>'
                         + metadata_value(provenance) + '</details>')
        body += '</section>'
    return body + '</details>'


def highlight(text, query):
    """Escape text and mark whole query words without touching HTML entities."""
    words = sorted({word for word in query.split() if len(word) > 1}, key=len, reverse=True)
    if not words:
        return escape(text)
    pattern = re.compile("|".join(re.escape(word) for word in words), re.IGNORECASE)
    parts, last = [], 0
    for match in pattern.finditer(text):
        parts.append(escape(text[last:match.start()]) + "<mark>" + escape(match.group()) + "</mark>")
        last = match.end()
    return "".join(parts) + escape(text[last:])


def copy_field(key, label, value, *, rows=5):
    return (f'<div class="copy-field"><div class="copy-head"><label for="{key}">{label}</label>'
            f'<button type="button" class="secondary" data-copy="{key}">Copy {label.lower()}</button></div>'
            f'<textarea id="{key}" readonly rows="{rows}">{escape(value)}</textarea></div>')


def publication_titles(current, identifiers):
    titles = {}
    if isinstance(current, collections.CollectionSnapshot):
        for cid, snapshot in current.snapshots.items():
            if not collections.is_catalog(snapshot):
                continue
            for key in collections.publication_keys(snapshot):
                identifier = current._aliases[cid, key]
                if identifier in identifiers and identifier not in titles:
                    title = snapshot.get(key)["title"]
                    if title and title.strip():
                        titles[identifier] = title
    return titles


def successful_supplement_links(data_root):
    """Use actual acquisitions, not the first discovered candidate URL."""
    if data_root is None:
        return {}
    path = data_root / "papers/supplements/inventory.json"
    if not path.exists():
        return {}
    inventory = releases.read_json(path.read_bytes())
    links = {}
    for publication in inventory:
        for document in publication["files"]:
            url = document.get("downloaded_from")
            if document["status"] != "acquired" or not url:
                continue
            if urlsplit(url).scheme not in {"https", "http"}:
                raise releases.ReleaseError("Invalid successful acquisition URL")
            key = (collections.publication(publication["doi"]), document["sha256"])
            links[key] = url
    return links


def downloaded_pdf_metadata(pdf_data_root):
    from .build import project_library
    return project_library.downloaded_pdf_metadata(pdf_data_root)


QUALITY_LABELS = {
    "coverage_review": "Document completeness review",
    "inventory_missing": "Original locations not inventoried",
    "missing_text": "No text extracted",
    "missing_unit": "Original location omitted",
    "ocr_review": "OCR transcription needs checking",
    "extraction_review": "Extraction warning needs checking",
    "truncated_text": "Extracted text is incomplete",
    "locator_mismatch": "Unexpected or duplicate location",
    "reported_defect": "Operator reported incorrect text",
    "original_included": "Original files included in candidate",
}
PROJECT_AUTHORIZATION_DEFAULT = "I am authorized to extract and retrieve these PDFs privately."
PROJECT_UPLOAD_MAX_BYTES = 4 * 1024 * 1024


def quality_location(locator):
    if locator is None:
        return "Whole document"
    kind = locator["kind"]
    if kind == "pdf":
        return f"PDF page {locator['pdf_page']}"
    if kind in {"word_paragraph", "word_table"}:
        return f"Word body element {locator['body_element']} ({'table' if kind == 'word_table' else 'paragraph/figure'})"
    if kind == "worksheet":
        return f"Worksheet {locator['worksheet']} — row {locator['row']}"
    return "Word package part: " + locator["package_member"]


def quality_order(unit):
    locator = unit["citation"]["locator"]
    return (locator["kind"] not in {"pdf", "word_paragraph", "word_table", "worksheet"},
            locator.get("worksheet", ""), locator.get("pdf_page", locator.get("body_element", locator.get("row", 0))),
            locator.get("package_member", ""), unit["id"])


def quality_document(role):
    if role == "main":
        return "Main paper"
    if role.startswith("supplement-") and role[11:].isdigit():
        return "Supplement " + str(int(role[11:]))
    return "Source document"


def publication_url(identifier):
    identifier = collections.publication(identifier)
    if identifier.startswith("doi:10."):
        return "https://doi.org/" + quote(identifier[4:], safe="/")
    if identifier.startswith("pmid:") and identifier[5:].isascii() and identifier[5:].isdigit():
        return "https://pubmed.ncbi.nlm.nih.gov/" + identifier[5:] + "/"
    return None


def quality_guidance(finding):
    code = finding["code"]
    if code == "coverage_review":
        return ("Required review — not a detected extraction error.",
                "Every source gets this task because matching page counts cannot prove that text, numbers, "
                "tables and reading order were extracted correctly. "
                + ("The document-level comparison is recorded below." if finding["resolved"] else
                   "No document-level comparison has been recorded."),
                "Compare the original with the retained locations below, in order. Check that meaningful text, "
                "figure labels, table values and notes are present. Word body elements count paragraphs AND tables "
                "in the file, not printed pages or just visible paragraphs. Gaps in element numbers can be empty layout elements.")
    if code in {"missing_text", "missing_unit"}:
        return ("Detected gap — the original still needs inspection.",
                ("A retained location exists, but its text is empty." if code == "missing_text" else
                 "The inventory includes this original location, but the release has no text unit for it.")
                + " This does not tell us whether the original is blank, contains a figure, or has text that extraction missed.",
                "Open the original at the location shown. For a Word element, use the neighbouring text below to "
                "find it; it may be a figure anchored between paragraphs. If it has words, labels or numbers, "
                "transcribe/correct those into a new candidate. Only verify blank/non-text when there is genuinely "
                "no transcribable content; do not dismiss an image-only scientific page as blank.")
    if code == "ocr_review":
        return ("Required comparison of machine-recognized text.",
                "This location uses OCR. OCR can confuse digits, letters, decimal points and symbols even with a high confidence score.",
                "Compare the displayed transcription with this original page, especially numbers, units, formulas "
                "and table cells. If correct, record verification. If not, edit the extracted data, then compare again.")
    if code == "inventory_missing":
        return ("Completeness cannot yet be checked.",
                "We have no independent list of the original document's expected locations; retained units alone cannot reveal an omitted final page.",
                "Build the location inventory below from a checksum-matched original. This does not approve its text.")
    return ("Detected problem or extraction warning.",
            finding["message"],
            "Compare this location and its extraction warnings with the original. "
            + ("Record verification only if the extraction is correct; otherwise repair it."
               if code == "extraction_review" else
               "Correct the extracted data; verification cannot waive this problem."))


UPDATE_EXTRACTED = {"extracted", "partial", "ocr_required", "no_native_text"}
PROVIDABLE = {"no_explicitly_licensed_pdf", "licensed_locations_failed"}
PROVIDED_LABELS = {"received": "Provided; waiting to be added", "processing": "Provided; being added (OCR can take minutes)",
                   "failed": "Provided copy could not be added", "rolled_back": "Provided copy was removed (rolled back)"}


def provided_items(data_root):
    """Latest operator-provided PDF/link per publication, read from the local ledger."""
    path = data_root / "releases/provided-sources/ledger.json"
    if not path.exists():
        return {}
    items = {}
    for item in sorted(releases.read_json(path.read_bytes())["items"].values(), key=lambda value: value["received_at"]):
        items[item["publication_id"]] = item
    return items


@lru_cache(maxsize=1)
def library_citations(path, modified_ns, size):
    """Retain only compact metrics from the saved discovery snapshot."""
    snapshot = releases.read_json(path.read_bytes())
    captured = snapshot["acquired_at"][:10]
    metrics = {}
    for node in snapshot["nodes"].values():
        work = node.get("work")
        if not work:
            continue
        count = work.get("cited_by_count")
        if count is not None and (type(count) is not int or count < 0):
            raise releases.ReleaseError("Invalid saved OpenAlex citation count")
        retrieved = max((source["retrieved_at"] for source in node["sources"]), default=captured)[:10]
        published = work.get("publication_date")
        annual = None
        if count is not None and published:
            age = (date.fromisoformat(retrieved) - date.fromisoformat(published)).days / 365.25
            annual = count / max(1, age)
        metric = {"citations": count, "citations_per_year": annual,
                  "citation_date": retrieved, "citation_source": work["id"]}
        for kind, value in node["record"]["identifiers"].items():
            if kind in {"doi", "pmid"}:
                identifier = catalog.alias(kind, value)
                if identifier not in metrics or retrieved > metrics[identifier]["citation_date"]:
                    metrics[identifier] = metric
    return metrics


def library_rows(data_root):
    inventory = data_root / "releases/istopmm-first-tier-20261019/bibliography.csv"
    with inventory.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"id", "title", "acquisition_status", "pdf_sha256", "background_of", "impact_on", "citation_url"}
        if not required.issubset(reader.fieldnames or []):
            raise releases.ReleaseError("Invalid source inventory columns")
        rows = {}
        for row in reader:
            if not row["id"] or row["id"] in rows:
                raise releases.ReleaseError("Missing or duplicate source inventory identity")
            rows[row["id"]] = {**row, "extraction": "not_extracted", "pages": 0, "versions": {}}
    original_rows = [row for row in rows.values()
                     if row["acquisition_status"] == "existing_seed_collection"]
    if original_rows:
        index = data_root / "evidence/index.sqlite"
        with closing(sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            originals = {}
            for doi, digest, pages, nonempty in connection.execute(
                    "SELECT p.doi, g.source_pdf_sha256, COUNT(*), "
                    "SUM(CASE WHEN LENGTH(TRIM(g.text)) > 0 THEN 1 ELSE 0 END) "
                    "FROM papers p JOIN pages g ON p.paper_id = g.paper_id "
                    "WHERE g.document_id = 'main' GROUP BY p.doi, g.source_pdf_sha256"):
                identifier = catalog.alias("doi", doi)
                originals.setdefault(identifier, {})[digest] = {
                    "status": "extracted" if pages == nonempty else "partial", "pages": pages}
        for row in original_rows:
            versions = originals.get(row["id"])
            if not versions:
                raise releases.ReleaseError("Original iStopMM source has no main-paper extraction record")
            row["versions"].update(versions)
            row.update(extraction="extracted" if all(
                version["status"] == "extracted" for version in versions.values()) else "partial",
                       pages=sum(version["pages"] for version in versions.values()))
    extraction_root = data_root / "releases/m9-ingestion-001"
    extraction = releases.read_json((extraction_root / "extraction.json").read_bytes())
    manifest = releases.read_json((extraction_root / "pilot.json").read_bytes())
    source_hashes = {source["publication_id"]: source["sha256"] for source in manifest["sources"]}
    for identifier, item in extraction["items"].items():
        if identifier in rows and rows[identifier]["pdf_sha256"] == source_hashes.get(identifier):
            rows[identifier].update(extraction=item["status"], pages=item["pages"])
            rows[identifier]["versions"][source_hashes[identifier]] = {
                "status": item["status"], "pages": item["pages"]}
    ocr_root = data_root / "releases/literature-ocr-001"
    if (ocr_root / "index.json").exists():
        ocr = releases.read_json((ocr_root / "index.json").read_bytes())
        if ocr["manifest_sha256"] != extraction["manifest_sha256"]:
            raise releases.ReleaseError("OCR source manifest differs")
        for identifier, item in ocr["items"].items():
            if identifier not in rows or item["source_sha256"] != rows[identifier]["pdf_sha256"]:
                raise releases.ReleaseError("OCR source version differs from inventory")
            if item["native_sha256"] != extraction["items"][identifier]["sha256"]:
                raise releases.ReleaseError("OCR native extraction identity differs")
            path = releases.safe_file(ocr_root, item["file"])
            if catalog.file_digest(path) != item["sha256"]:
                raise releases.ReleaseError("OCR checkpoint checksum differs")
            rows[identifier].update(extraction=item["status"],
                                    ocr_review_pages=item.get("low_confidence_pages", []))
            rows[identifier]["versions"][item["source_sha256"]]["status"] = item["status"]
    updates = []
    for name in ("m10-updates-001", "source-updates"):
        for path in (data_root / "releases" / name / "runs").glob("*/state.json"):
            state = releases.read_json(path.read_bytes())
            updates.append(state)
    for state in sorted(updates, key=lambda value: (value["started_at"], value["name"])):
        for identifier, item in state["ingestion"].items():
            if item["status"] not in UPDATE_EXTRACTED:
                continue
            source = item["source"]
            row = rows.setdefault(identifier, {
                "id": identifier, "title": source["title"], "authors": "", "publication_date": source["publication_date"],
                "journal": "", "citation_url": "", "background_of": "", "impact_on": "",
                "versions": {},
            })
            previous = row["versions"].get(source["sha256"], {})
            status = (previous["status"] if previous.get("status") in {
                "extracted_with_ocr", "needs_visual_review"} else item.get("ocr", {}).get("status", item["status"]))
            if row.get("pdf_sha256") != source["sha256"]:
                row.pop("ocr_review_pages", None)
            row.update(acquisition_status="downloaded", pdf_sha256=source["sha256"],
                       extraction=status, pages=item["pages"])
            row["versions"][source["sha256"]] = {"status": status, "pages": item["pages"]}
    for item in provided_items(data_root).values():
        row = rows.get(item["publication_id"])
        if row is None:
            continue
        row["provided"] = item
        if item["status"] == "added":
            row["versions"][item["sha256"]] = {"status": item["extraction"], "pages": item["pages"]}
            if row["acquisition_status"] in PROVIDABLE:
                row.update(acquisition_status="provided", pdf_sha256=item["sha256"],
                           extraction=item["extraction"], pages=item["pages"])
                if item.get("ocr"):
                    row["ocr_review_pages"] = item["ocr"]["low_confidence_pages"]
    neighborhood = data_root / "releases/istopmm-first-tier-20261019/neighborhood.json"
    metrics = {}
    if neighborhood.exists():
        stat = neighborhood.stat()
        metrics = library_citations(neighborhood, stat.st_mtime_ns, stat.st_size)
    for row in rows.values():
        row.update(metrics.get(row["id"], {
            "citations": None, "citations_per_year": None, "citation_date": "", "citation_source": ""}))
        row["seed_connections"] = len({
            seed.strip() for field in ("background_of", "impact_on")
            for seed in row[field].split(";") if seed.strip()})
    return sorted(rows.values(), key=lambda row: (
        not row["title"].strip(), row["title"].strip().casefold(), row["id"]))


STYLESHEET = """
:root{
  --paper:#fff;--rail:#eef1f5;--raised:#f6f8fa;--ink:#1c232b;--muted:#596472;--line:#dce1e7;--line-strong:#b9c1cb;
  --accent:#2350a0;--accent-hover:#183c7d;--accent-ink:#fff;--accent-soft:#e7edf8;
  --ok:#2e6b36;--ok-soft:#e6f1e6;--warn:#8a5600;--warn-soft:#fbf0d6;--bad:#a8291c;--bad-soft:#fbe6e3;
  --mark:#fde68a;--mark-ink:#1c232b;--code:#1e2630;--code-ink:#e6ebf0;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --serif:Charter,"Bitstream Charter","Sitka Text",Cambria,Georgia,serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#16191d;--rail:#1d2126;--raised:#1f2328;--ink:#e3e7eb;--muted:#9aa5b1;--line:#2e343b;--line-strong:#46505b;
  --accent:#8fb2f0;--accent-hover:#b4ccf6;--accent-ink:#0f1a2e;--accent-soft:#1f2b40;
  --ok:#8ccf94;--ok-soft:#1c2e1f;--warn:#e9bf62;--warn-soft:#33290f;--bad:#f2968a;--bad-soft:#3a1d19;
  --mark:#6b5a12;--mark-ink:#fff7d6;--code:#0f1215;--code-ink:#dfe5ea;
}}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font:15.5px/1.6 var(--sans);color:var(--ink);background:var(--paper);overflow-wrap:break-word}
a{color:var(--accent);text-underline-offset:.15em;text-decoration-thickness:1px}
a:hover{color:var(--accent-hover);text-decoration-thickness:2px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.skip-link{position:absolute;left:1rem;top:-3rem;z-index:10;background:var(--accent);color:var(--accent-ink);padding:.5rem .9rem}
.skip-link:focus{top:.75rem}

/* Layout: a fixed side rail with the name, search and navigation; the page itself is plain paper */
.shell{display:grid;grid-template-columns:15.5rem minmax(0,1fr);min-height:100vh}
.rail{background:var(--rail);border-right:1px solid var(--line);padding:1.25rem 0 1.5rem;
  position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;gap:1rem}
.brand{margin:0;padding:0 1.25rem;font-size:1rem;font-weight:700;letter-spacing:-.005em}
.brand a{color:var(--ink);text-decoration:none}
.quick-search{display:flex;gap:.3rem;margin:0;padding:0 1rem}
.quick-search input{flex:1;min-width:0;margin:0;padding:.35rem .55rem;font-size:.92rem}
.quick-search button{margin:0;padding:.3rem .6rem;font-size:.88rem}
.sidenav{display:flex;flex-direction:column}
.nav-group{margin:1rem 1.25rem .25rem;font-size:.8rem;font-weight:600;color:var(--muted)}
.nav-group:first-child{margin-top:0}
.sidenav a{display:block;padding:.3rem 1.25rem;color:var(--ink);text-decoration:none;font-size:.93rem;
  border-left:3px solid transparent}
.sidenav a:hover{background:color-mix(in srgb,var(--ink) 5%,transparent);color:var(--ink)}
.sidenav a[aria-current="page"]{background:var(--paper);border-left-color:var(--accent);font-weight:600}
.content{min-width:0;display:flex;flex-direction:column}
main{flex:1;min-width:0;width:100%;max-width:68rem;padding:2rem 3rem 3rem}
main>h2:first-child{margin:0 0 1.25rem;font-size:1.65rem;line-height:1.2;font-weight:700;letter-spacing:-.015em}
main h3{font-size:1.12rem;line-height:1.35;font-weight:650;margin:2rem 0 .5rem}
main h4{font-size:1rem;margin:1.25rem 0 .4rem}
main>p,main>ul,main>ol,.panel>p,.panel>ul,main>dl{max-width:75ch}
.site-footer{padding:1rem 3rem 1.5rem;color:var(--muted);font-size:.83rem;max-width:68rem}

/* Sections: separated by a rule and space rather than boxed */
article,.panel{margin:0;padding:1.25rem 0;border-top:1px solid var(--line)}
article h3,.panel h3{margin-top:0}
.panel+.panel,article+article{margin-top:0}
.panel.danger{border:1px solid color-mix(in srgb,var(--bad) 40%,var(--line));border-left:4px solid var(--bad);
  padding:1rem 1.25rem;margin-top:2rem;border-radius:4px}
.panel.danger h3{color:var(--bad)}
.two-col{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,22rem),1fr));gap:0 2.5rem}
.callout,.alert,.note{padding:.7rem 1rem;max-width:75ch;border-radius:3px}
.callout{background:var(--accent-soft);border-left:3px solid var(--accent)}
.alert{background:var(--bad-soft);border-left:3px solid var(--bad)}
.note{background:var(--raised);color:var(--muted);font-size:.92rem}
.meta{color:var(--muted);font-size:.9rem;margin:.25rem 0}
.lead{font-size:1.08rem;font-weight:600;margin:.2rem 0}
.summary{margin-bottom:.5rem}
.empty-state{padding:1.5rem 0;color:var(--muted);max-width:60ch}
.result-count{color:var(--muted);font-size:.92rem}
.result-count strong{color:var(--ink)}
small{display:block;color:var(--muted);font-size:.82rem;overflow-wrap:anywhere}

/* Overview */
.hero-search{display:block;margin:0 0 1rem}
.hero-search>label{display:block;font-weight:600;margin-bottom:.35rem}
.input-row{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.input-row input[type="search"]{flex:1 1 18rem;font-size:1.05rem;padding:.55rem .75rem;margin:0;border-color:var(--line-strong)}
.input-row label{display:inline-flex;align-items:center;gap:.4rem;color:var(--muted);font-size:.9rem}
.input-row select,.input-row button{margin:0}
.input-row button{padding:.55rem 1rem}
.tally{font-size:1.05rem;margin:1.5rem 0 .2rem}
.tally strong{font-variant-numeric:tabular-nums}
.destinations{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.45rem 1.5rem;margin:0;max-width:62rem}
dl.destinations dt{font-weight:500}
.destinations dd{margin:0;color:var(--muted)}
.steps{padding-left:1.25rem}.steps li{margin:.4rem 0}
.tool-list{display:grid;grid-template-columns:minmax(11rem,max-content) 1fr;gap:.45rem 1.25rem;margin:0}
.tool-list dd{margin:0}

/* Progress: a real sequence, so it keeps its numbers */
.progress-steps{list-style:none;padding:0;margin:.75rem 0 1.25rem;display:flex;flex-wrap:wrap;gap:.35rem 1.5rem;counter-reset:step}
.progress-steps li{counter-increment:step;display:flex;align-items:center;gap:.45rem;color:var(--muted);font-size:.92rem}
.progress-steps li::before{content:counter(step);display:grid;place-items:center;width:1.4rem;height:1.4rem;border-radius:50%;
  border:1px solid var(--line-strong);font-size:.78rem;font-weight:600}
.progress-steps li.active{color:var(--ink);font-weight:600}
.progress-steps li.active::before{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.progress-steps li.done::before{content:"\\2713";color:var(--ok);border-color:var(--ok)}

/* Forms */
input,select,textarea,button{font:inherit;color:inherit;max-width:100%}
input,select,textarea{background:var(--paper);border:1px solid var(--line-strong);border-radius:4px;padding:.4rem .6rem;margin:.3rem 0}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:var(--accent)}
input[type="file"]{padding:.3rem;border-style:dashed}
button,.button{display:inline-flex;align-items:center;gap:.4rem;background:var(--accent);color:var(--accent-ink);border:1px solid var(--accent);
  border-radius:4px;padding:.4rem .9rem;margin:.3rem 0;font-weight:600;cursor:pointer;text-decoration:none;line-height:1.4}
button:hover,.button:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:var(--accent-ink);text-decoration:none}
button.secondary,.button.secondary{background:var(--paper);color:var(--ink);border-color:var(--line-strong);font-weight:500}
button.secondary:hover,.button.secondary:hover{background:var(--raised);border-color:var(--muted);color:var(--ink)}
button.locate-toggle[aria-pressed="true"]{background:var(--ok-soft);color:var(--ok);border-color:var(--ok)}
button.danger{background:var(--bad);border-color:var(--bad);color:#fff}
button.danger:hover{filter:brightness(1.1)}
button.remove-core{background:transparent;color:var(--bad);padding:.15rem .5rem;font-size:.8rem;font-weight:500}
button.remove-core:hover{background:var(--bad-soft);color:var(--bad)}
dialog{background:var(--paper);color:var(--ink);border:1px solid var(--line-strong);border-radius:6px;
  padding:1.25rem;width:min(42rem,calc(100% - 2rem));max-height:85vh;overflow-y:auto;box-sizing:border-box}
dialog::backdrop{background:rgb(0 0 0 / .6)}
form{margin:1rem 0}
form[method="post"]{display:flex;flex-direction:column;align-items:flex-start;gap:.15rem;max-width:40rem}
form.batch-actions{flex-direction:row;flex-wrap:wrap;gap:.5rem;max-width:none}
form[method="post"] label{font-weight:600;margin-top:.6rem}
form[method="post"] input:not([type="file"]),form[method="post"] textarea{width:100%}
.filters{display:flex;flex-wrap:wrap;align-items:flex-end;gap:.6rem 1rem;padding:0 0 1rem;border-bottom:1px solid var(--line)}
.filters label{display:flex;flex-direction:column;font-size:.85rem;font-weight:600;color:var(--muted);gap:.1rem}
.filters input,.filters select{margin:0;min-width:12rem;font-size:.95rem;color:var(--ink);font-weight:400}
.filters button{margin:0}
textarea{display:block;width:100%;font:.88rem/1.5 var(--mono);resize:vertical}
.copy-field{margin:1rem 0;max-width:62rem}
.copy-head{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}
.copy-head label{font-weight:600}
.copy-head button{padding:.2rem .65rem;font-size:.85rem}
button.copied{background:var(--ok-soft);color:var(--ok);border-color:var(--ok)}
#copy-status:empty{display:none}
#copy-status{color:var(--muted);font-size:.9rem}

/* Tables */
.table-scroll{overflow-x:auto;margin:1rem 0}
table{border-collapse:collapse;width:100%;font-size:.93rem}
th,td{text-align:left;vertical-align:top;padding:.6rem .75rem;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{padding-left:0}
th{font-size:.85rem;font-weight:600;color:var(--muted);white-space:nowrap;border-bottom:1px solid var(--line-strong)}
tbody tr:hover td{background:var(--raised)}
td>a:first-child{font-weight:600}
.library-records{list-style:none;padding:0;margin:1rem 0;max-width:none;border-top:1px solid var(--line-strong)}
.library-record{padding:1rem 0;min-width:0;border-bottom:1px solid var(--line)}
.library-record h3,.library-record h4{margin:0 0 .6rem;font-size:1.02rem;overflow-wrap:anywhere}
.library-record dl{display:flex;flex-wrap:wrap;gap:.6rem 2rem;margin:0}
.library-record dl>div{min-width:0;max-width:100%}
.library-record dt{font-size:.82rem;color:var(--muted);font-weight:600}
.library-record dd{margin:.15rem 0 0}
.library-record code{overflow-wrap:anywhere;word-break:normal}
.library-record .badge{white-space:normal}
.library-record details{margin:.75rem 0 0;overflow-wrap:anywhere}

/* Status labels */
.badge{display:inline-block;padding:.05rem .45rem;border-radius:3px;font-size:.82rem;font-weight:600;line-height:1.55;
  white-space:nowrap;background:var(--raised);color:var(--muted);border:1px solid var(--line)}
.badge.ok{background:var(--ok-soft);color:var(--ok);border-color:color-mix(in srgb,var(--ok) 25%,transparent)}
.badge.warn{background:var(--warn-soft);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 25%,transparent)}
.badge.bad{background:var(--bad-soft);color:var(--bad);border-color:color-mix(in srgb,var(--bad) 25%,transparent)}
.chip{display:inline-block;color:var(--muted);font-size:.85rem;font-weight:600}

/* Disclosure */
details{margin:.5rem 0}
summary{cursor:pointer;font-weight:600;list-style:none;display:flex;gap:.5rem;align-items:baseline}
summary::-webkit-details-marker{display:none}
summary::before{content:"";flex:none;width:.4rem;height:.4rem;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
  transform:rotate(-45deg) translateY(-.05rem);transition:transform .12s}
details[open]>summary::before{transform:rotate(45deg) translateY(-.15rem)}
summary:hover{color:var(--accent)}
main>details,.notes{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:.65rem 0;margin:1.25rem 0}
main>details+details{border-top:0;margin-top:-1.25rem}
.notes{color:var(--muted);font-size:.93rem}
.notes summary{color:var(--ink)}
.notes h3{color:var(--ink)}
.source-entry{padding:0;margin:0;border-top:0;border-bottom:1px solid var(--line)}
.source-entry summary{padding:.7rem 0;overflow-wrap:anywhere}
.source-entry details{margin:0}
.source-entry dl{padding:.25rem 0 .75rem 1rem}
td>details{margin:0}
td details.provide{margin-top:0}
td details.provide summary{font-size:.85rem;color:var(--accent)}
td details.provide form{display:flex;flex-direction:column;gap:.25rem;margin:.5rem 0;padding:.75rem;background:var(--raised);
  border:1px solid var(--line);border-radius:4px;min-width:16rem}
td details.provide label{display:flex;flex-direction:column;font-size:.82rem;font-weight:600;margin:0}

/* Definition lists */
article dl{display:grid;grid-template-columns:minmax(9rem,max-content) minmax(0,1fr);gap:.3rem 1.25rem;margin:.5rem 0}
article dt{color:var(--muted);font-size:.88rem;font-weight:600}
article dd{margin:0;overflow-wrap:anywhere}
.metadata-list{margin:0;padding-left:1.25rem}
.metadata-list>li+li{margin-top:.65rem}
article dl.metadata-fields{display:block;margin:0;padding:.65rem .85rem;
  border-left:2px solid var(--line);background:var(--surface-2);border-radius:4px}
.metadata-fields>dt:not(:first-child){margin-top:.5rem}
.coverage-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,13rem),1fr));gap:1rem}
.coverage-metrics>div{border:1px solid var(--line);border-radius:6px;padding:1rem}
.coverage-metrics dt{font-size:.9rem}
.coverage-metrics dd{margin:.35rem 0 0;font-size:1.75rem;font-weight:650}
.coverage-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,18rem),1fr));gap:1.5rem}
.coverage-grid section{min-width:0}
.literature-coverage th{overflow-wrap:anywhere}
.literature-coverage td{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.metadata-empty{color:var(--muted)}
main>details dl dt,dl:not(.tool-list) dt{font-weight:600}

/* Text from the papers is set in a reading serif; everything else stays in the interface face */
code{font:.85em/1.4 var(--mono);background:var(--raised);border:1px solid var(--line);border-radius:3px;padding:.05em .3em;overflow-wrap:anywhere}
pre{font:.85rem/1.55 var(--mono);background:var(--code);color:var(--code-ink);border-radius:4px;padding:.85rem 1rem;
  white-space:pre-wrap;overflow-wrap:anywhere;max-width:62rem}
pre.reading{font:1.06rem/1.75 var(--serif);background:none;color:var(--ink);border-left:3px solid var(--line-strong);
  border-radius:0;padding:.25rem 0 .25rem 1.5rem;max-width:70ch}
blockquote{margin:.6rem 0;padding:.1rem 0 .1rem 1rem;border-left:3px solid var(--line-strong);
  white-space:pre-wrap;overflow-wrap:anywhere;font:1.02rem/1.65 var(--serif);max-width:none}
mark{background:var(--mark);color:var(--mark-ink);padding:0 .08em;border-radius:1px}
.hit-title{font-size:1.05rem;margin-bottom:.15rem}
.hit-title+.meta code{margin-left:.5rem}
.hit-title+.meta .chip{margin-right:.4rem}
.reader-head{margin-bottom:.75rem}
.reader-head h3{margin:0 0 .25rem;font-size:1.35rem}
.actions{display:flex;flex-wrap:wrap;gap:.5rem;margin:.6rem 0 0}
.actions .button{margin:0}

/* Pagination */
.pager,main>nav{display:flex;flex-wrap:wrap;gap:.5rem;margin:1.25rem 0}
.pager:empty{display:none}
.pager a,main>nav a{display:inline-block;padding:.35rem .85rem;border:1px solid var(--line-strong);border-radius:4px;
  text-decoration:none;font-weight:500;font-size:.92rem;color:var(--ink)}
.pager a:hover,main>nav a:hover{background:var(--raised);border-color:var(--muted);color:var(--ink)}
.pager a[aria-current="page"]{background:var(--raised);border-color:var(--accent);font-weight:700}

.help{display:inline-block;vertical-align:middle;margin:.2rem .35rem;position:relative;font-size:.9rem;font-weight:400;
  padding:0;border:0}
.help>summary{display:inline-flex;align-items:center;justify-content:center;width:1.55rem;height:1.55rem;
  padding:0;border:1px solid var(--line-strong);border-radius:50%;cursor:pointer;color:var(--muted);list-style:none}
.help>summary::-webkit-details-marker{display:none}
.help>summary::before{display:none}
.help>summary:hover,.help>summary:focus-visible{color:var(--ink);border-color:var(--accent)}
.help-content{background:var(--paper);color:var(--ink);border:1px solid var(--line-strong);border-radius:6px;
  padding:1rem;box-shadow:0 6px 24px #0003;width:min(30rem,calc(100vw - 3rem));max-height:70vh;overflow:auto;
  text-align:left;line-height:1.5;overflow-wrap:anywhere}
.help[open]>.help-content{position:absolute;z-index:100;left:0;top:100%}
.help-content p:last-child{margin-bottom:0}
.help-content>strong{display:block}

/* Data quality */
.quality-status{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem .75rem;margin:0 0 1.25rem;color:var(--muted)}
.tabs{display:flex;gap:1.5rem;margin:0 0 1rem;border-bottom:1px solid var(--line);flex-wrap:wrap}
.tabs a{padding:.4rem 0;margin-bottom:-1px;border-bottom:2px solid transparent;text-decoration:none;color:var(--muted);font-weight:600}
.tabs a:hover{color:var(--ink)}
.tabs a[aria-current="page"]{color:var(--ink);border-bottom-color:var(--accent)}
.kind-chips{list-style:none;padding:0;margin:0 0 .5rem;display:flex;flex-wrap:wrap;gap:.4rem;max-width:none}
.kind-chip{display:inline-block;padding:.15rem .6rem;border:1px solid var(--line-strong);border-radius:4px;
  color:var(--ink);text-decoration:none;font-size:.88rem}
.kind-chip:hover{background:var(--raised);color:var(--ink)}
.kind-chip.active{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.finding-group{margin:2rem 0}
.group-head{display:flex;align-items:baseline;gap:.75rem;flex-wrap:wrap}
.group-head h3{margin:0}
.finding-group>.meta{margin:.3rem 0 .6rem;max-width:75ch}
.finding-list{list-style:none;padding:0;margin:0;border-top:1px solid var(--line-strong);max-width:none}
.finding-list li{border-bottom:1px solid var(--line)}
.finding-row{display:flex;align-items:center;gap:1rem;padding:.6rem .25rem;text-decoration:none;color:var(--ink)}
.finding-row::after{content:"";flex:none;width:.4rem;height:.4rem;border-right:2px solid var(--muted);
  border-top:2px solid var(--muted);transform:rotate(45deg);margin-right:.25rem}
.finding-row:hover{background:var(--raised);color:var(--ink)}
.finding-row:hover strong{color:var(--accent);text-decoration:underline}
.finding-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:.1rem}
.finding-main strong{font-weight:600;overflow-wrap:anywhere}
.finding-main .meta{margin:0}
.back-link{margin:0 0 .5rem}
.back-link a{text-decoration:none;font-weight:600}

@media (max-width:900px){
  .shell{display:block}
  .rail{position:static;height:auto;padding:.85rem 0 .5rem;gap:.6rem;border-right:0;border-bottom:1px solid var(--line)}
  .brand{padding:0 1rem}
  .sidenav{flex-direction:row;overflow-x:auto;padding:0 1rem .25rem;gap:.25rem;scrollbar-width:thin}
  .nav-group{display:none}
  .sidenav a{white-space:nowrap;border:1px solid var(--line);border-radius:4px;background:var(--paper);font-size:.88rem;padding:.25rem .6rem}
  .sidenav a[aria-current="page"]{border-color:var(--accent);color:var(--accent)}
  main{padding:1.25rem 1rem 2rem}
  .site-footer{padding:1rem}
  main>h2:first-child{font-size:1.4rem}
  .panel.danger{padding:1rem}
  article dl,.tool-list,.destinations{grid-template-columns:1fr;gap:.1rem}
  article dd,.tool-list dd,.destinations dd{margin-bottom:.55rem}
  .filters input,.filters select{min-width:0;width:100%}
  .filters label{flex:1 1 100%}
  pre.reading{padding-left:1rem}
  th:first-child,td:first-child{padding-left:.5rem}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media print{
  .rail,.site-footer,.pager,button{display:none!important}
  .shell{display:block}body{background:#fff}
}
"""

HELP_SCRIPT = """
document.querySelectorAll('details.help').forEach(help => {
  const summary = help.querySelector('summary');
  const panel = help.querySelector('.help-content');
  let pinned = false;
  function position() {
    if (!help.open) return;
    panel.style.position = 'fixed';
    const rect = summary.getBoundingClientRect();
    panel.style.left = Math.max(12, Math.min(rect.left, innerWidth - panel.offsetWidth - 12)) + 'px';
    const below = innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    const placeBelow = below >= Math.min(panel.scrollHeight, innerHeight * .7) || below >= above;
    panel.style.maxHeight = Math.min(innerHeight * .7, Math.max(0, placeBelow ? below : above)) + 'px';
    panel.style.top = Math.max(12, placeBelow ? rect.bottom : rect.top - panel.offsetHeight) + 'px';
  }
  function close() { pinned = false; help.open = false; }
  summary.addEventListener('pointerenter', event => {
    if (event.pointerType === 'mouse') { help.open = true; position(); }
  });
  help.addEventListener('pointerleave', () => {
    if (!pinned && !help.contains(document.activeElement)) help.open = false;
  });
  summary.addEventListener('click', event => {
    event.preventDefault();
    pinned = !pinned;
    help.open = pinned;
    position();
  });
  help.addEventListener('focusout', event => {
    if (!pinned && !help.contains(event.relatedTarget)) help.open = false;
  });
  document.addEventListener('pointerdown', event => { if (!help.contains(event.target)) close(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && help.open) {
      const focused = help.contains(document.activeElement);
      close();
      if (focused) summary.focus();
    }
  });
  window.addEventListener('resize', position);
  window.addEventListener('scroll', position, true);
});
"""


LIBRARY_SCRIPT = """
document.querySelectorAll('button.locate-toggle').forEach(button => {
  const form = button.form;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    const data = new FormData(form);
    data.set('action', 'toggle');
    const status = form.querySelector('[data-toggle-status]');
    status.textContent = '';
    button.disabled = true;
    try {
      const response = await fetch(form.getAttribute('action'), {
        method: 'POST', body: data, headers: {'Accept': 'application/json'}
      });
      if (!response.ok) {
        const text = await response.text();
        const parsed = new DOMParser().parseFromString(text, 'text/html');
        throw new Error(parsed.querySelector('[role="alert"]')?.textContent || parsed.body.textContent);
      }
      const result = await response.json();
      button.setAttribute('aria-pressed', String(result.selected));
      button.textContent = 'Locate PDF: ' + (result.selected ? 'on' : 'off');
      document.querySelector('#batch-selected-count').textContent = String(result.selected_count);
    } catch (error) {
      status.textContent = 'Could not change selection: ' + error.message;
    } finally {
      button.disabled = false;
    }
  });
});
"""

SETTINGS_SCRIPT = """
const purgeDialog = document.querySelector('#purge-dialog');
if (purgeDialog && typeof purgeDialog.showModal === 'function') {
  let activeRequest;
  purgeDialog.addEventListener('close', () => activeRequest?.abort());
  purgeDialog.querySelector('[data-close-dialog]').addEventListener('click', () => purgeDialog.close());
  document.querySelectorAll('form[action="/settings/purge-preview"]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button');
      const content = purgeDialog.querySelector('[data-dialog-content]');
      const controller = new AbortController();
      activeRequest = controller;
      content.textContent = 'Preparing deletion preview. No data has been deleted.';
      content.setAttribute('role', 'status');
      purgeDialog.showModal();
      button.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: 'POST', body: new FormData(form), signal: controller.signal
        });
        const preview = new DOMParser().parseFromString(await response.text(), 'text/html');
        if (!response.ok) {
          throw new Error(preview.querySelector('#main')?.textContent || preview.body.textContent);
        }
        const main = preview.querySelector('#main');
        if (!main || !main.querySelector('form[action="/settings/purge-paper"]')) {
          throw new Error('Deletion preview unavailable. Reload Project settings and try again.');
        }
        content.replaceChildren(...Array.from(main.childNodes));
        content.querySelector('input[name="confirmation"]')?.focus();
      } catch (error) {
        if (error.name === 'AbortError') return;
        const message = document.createElement('p');
        message.setAttribute('role', 'alert');
        message.textContent = 'Could not load deletion preview: ' + error.message;
        content.replaceChildren(message);
      } finally {
        button.disabled = false;
      }
    });
  });
}
"""

READER_SCRIPT = """
for (const button of document.querySelectorAll("[data-copy]")) {
  const label = button.textContent;
  button.addEventListener("click", async () => {
    const field = document.getElementById(button.dataset.copy);
    const status = document.getElementById("copy-status");
    try {
      await navigator.clipboard.writeText(field.value);
      status.textContent = "Copied.";
      button.textContent = "Copied";
      button.classList.add("copied");
      setTimeout(() => { button.textContent = label; button.classList.remove("copied"); }, 1800);
    } catch (error) {
      field.focus(); field.select();
      status.textContent = "Clipboard unavailable. Text selected: use Ctrl+C or Command+C to copy.";
    }
  });
}
"""


def create_app(*, release=None, registry=None, profiles, allowed_collections=None,
               default_collections=None, port=8080, mcp_url="http://127.0.0.1:8000/mcp",
               data_root=None, quality_root=None, project_root=None, pdf_data_root=None):
    if not 1 <= port <= 65535:
        raise ValueError("Invalid website port")
    mcp_url = resource_url(mcp_url)
    if bool(release) == bool(registry):
        raise ValueError("Select exactly one release or collection registry")
    if project_root and not registry:
        raise ValueError("Project settings require a collection registry")
    if not registry and (allowed_collections or default_collections):
        raise ValueError("Collection selection requires a registry")
    project_root = Path(project_root).absolute() if project_root else None
    pdf_data_root = Path(pdf_data_root or "releases/pdf-data").absolute()
    if project_root and project_root.resolve() != project_root:
        raise ValueError("Project root cannot contain a symlinked path")
    if project_root and Path(registry).absolute() != project_root / "registry":
        raise ValueError("Project settings must use that project's registry")
    store = (collections.CollectionStore(registry, allowed_profiles=profiles,
                                        allowed_collections=allowed_collections or [],
                                        defaults=default_collections) if registry else None)
    pinned = None if store else releases.validate(release, allowed_profiles=profiles)
    if store and not project_root:
        store.current()
    data_root = (Path(data_root) if data_root else
                 project_root or (Path(registry).absolute().parent.parent if registry else None))
    quality_root = Path(quality_root) if quality_root else (data_root or Path.cwd()) / "releases/data-quality"

    provide_token = secrets.token_urlsafe(32)
    provider_state = {"thread": None, "again": False}
    provider_lock = threading.Lock()
    discovery_lock = threading.Lock()
    discovery_state = {"thread": None, "run": None}
    metadata_state = {"thread": None}
    purge_state = {"thread": None}
    pdf_state = {"thread": None}
    batch_state = {"thread": None, "pause": threading.Event()}

    def batch_running():
        return batch_state["thread"] is not None and batch_state["thread"].is_alive()

    def purge_running():
        return purge_state["thread"] is not None and purge_state["thread"].is_alive()

    def require_idle_project():
        if (purge_running() or metadata_running() or pdf_running()
                or discovery_state["thread"] and discovery_state["thread"].is_alive()):
            raise ValueError("Wait for the active discovery, metadata update, PDF search, or purge to finish before changing core papers")

    def pdf_running():
        return batch_running() or pdf_state["thread"] is not None and pdf_state["thread"].is_alive()

    def metadata_running():
        return metadata_state["thread"] is not None and metadata_state["thread"].is_alive()

    def discovery_root():
        root = project_root / "discovery"
        if root.resolve() != root:
            raise ValueError("Discovery directory cannot contain a symlinked path")
        return root

    def discovery_page():
        if not project_root or not (project_root / "project.json").is_file():
            return page("Find related papers",
                        '<p><a href="/settings">Initialize a project</a> and register core papers first.</p>')
        if set(request.args) - {"run", "offset", "direction"}:
            raise ValueError("Unknown discovery parameter")
        body = (
            help_note("Finding related papers", "<p>Find References (papers cited by Core papers) or Citing papers "
            "(papers that cite Core papers). "
            "References use OpenAlex and Crossref; citing papers use OpenAlex. These are provider-indexed "
            "relationships, not an exhaustive bibliography or proof of scientific support. "
            "No PDFs are downloaded and your Core papers are not changed.</p>")
            + '<form method="post" action="/discovery">'
            f'<input type="hidden" name="token" value="{provide_token}">'
            '<button name="direction" value="background">Find references</button>'
            '<button name="direction" value="impact">Find citing papers</button>'
            '</form>')
        root = discovery_root()
        jobs = {p.name: releases.read_json(releases.safe_file(p, "job.json").read_bytes())
                for p in root.iterdir()
                if p.is_dir() and not p.is_symlink() and len(p.name) == 32
                and all(c in "0123456789abcdef" for c in p.name) and (p / "job.json").is_file()} if root.exists() else {}
        runs = sorted(jobs, key=lambda name: (jobs[name]["started_at"], name), reverse=True)
        direction = request.args.get("direction")
        if direction and direction not in {"background", "impact"}:
            raise ValueError("Invalid discovery direction")
        selected_runs = [name for name in runs if not direction or jobs[name]["direction"] == direction]
        run = request.args.get("run", selected_runs[0] if selected_runs else "")
        if run:
            if len(run) != 32 or any(c not in "0123456789abcdef" for c in run):
                raise ValueError("Invalid discovery run")
            folder = root / run
            job = releases.read_json(releases.safe_file(folder, "job.json").read_bytes())
            body += (f"<h3>{escape(discovery_label(job))}</h3>"
                     f"<p>Started: {escape(job['started_at'])}. Status: <strong>{escape(job['status'])}</strong>.</p>")
            if job["status"] == "running":
                thread = discovery_state["thread"]
                if discovery_state["run"] != run or thread is None or not thread.is_alive():
                    body += "<p>The process was interrupted. Start a new search; partial provider responses remain on disk.</p>"
                else:
                    body += '<p>Discovery is running. <a href="/discovery?run=' + run + '">Refresh progress</a>.</p>'
                progress = folder / "discovery-progress.json"
                if progress.is_file():
                    body += '<details><summary>Detailed progress</summary><pre>' + escape(json.dumps(releases.read_json(
                        releases.safe_file(folder, progress.name).read_bytes()), indent=2)) + "</pre></details>"
            elif job["status"] == "failed":
                body += f'<p role="alert">{escape(job["error"])}</p>'
            else:
                report = job["report"]
                body += (f"<p>{report['edges']} provider relationships; "
                         f"{report['unresolved']} unresolved references; "
                         f"{report['seeds_with_provider_gaps']} core papers with provider gaps. "
                         "</p>" + help_note("Relationship counts",
                         "<p>Results are provider occurrences, not deduplicated publications.</p>"))
                graph = releases.read_json(releases.safe_file(folder, "neighborhood.json").read_bytes())
                titles = discovery_titles(graph)
                unresolved = graph.get("unresolved", [])
                failures = [(seed["seed_doi"], provider, result)
                            for seed in graph["seeds"] for provider, result in seed["providers"].items()
                            if result["status"] not in {"complete_indexed_neighborhood", "complete_deposited_references"}]
                body += '<section class="panel" id="coverage-issues"><h4>What needs attention</h4>'
                if unresolved:
                    from .build import project_library
                    core = project_library.load(project_root)["papers"]
                    body += (help_note("Unresolved references", "<p>OpenAlex returned these reference IDs in core-paper bibliographies, but did not "
                             "return a matching work record. They may be removed, merged, or temporarily missing. "
                             "No title or DOI can be established from the ID alone.</p>") + "<ul>")
                    for item in unresolved:
                        identifier = item["openalex_id"]
                        doi = item["seed_doi"]
                        title = core.get(doi, {}).get("title", doi)
                        body += (f'<li>Unresolved reference in '
                                 f'<a href="https://doi.org/{quote(doi, safe="/")}">{escape(title)}</a>. '
                                 f'<a href="https://openalex.org/{quote(identifier, safe="")}">'
                                 'View unresolved reference in OpenAlex</a>, or inspect the core paper bibliography locally. '
                                 f'<small>OpenAlex record ID: <code>{escape(identifier)}</code>.</small></li>')
                    body += "</ul>"
                for doi, provider, result in failures:
                    body += f'<p>{escape(doi)} ({escape(provider)}): {escape(result.get("error", result["status"]))}</p>'
                if unresolved or failures:
                    body += (
                        '<form method="post" action="/discovery">'
                        f'<input type="hidden" name="token" value="{provide_token}">'
                        f'<button name="direction" value="{escape(job["direction"], quote=True)}">'
                        f'Retry {escape(discovery_label(job).lower())}</button></form>'
                        + help_note("Retrying discovery", '<p>This starts a fresh search in this direction, not just the missing records. '
                        'After it finishes, <a href="/publications">update the metadata catalog</a>. '
                        'Retrying does not guarantee providers can resolve these records.</p>'))
                else:
                    body += "<p>No unresolved reference identifiers or provider failures in this search.</p>"
                body += "</section>"
                body += discovery_coverage(graph)
                offset = int(request.args.get("offset", "0"))
                if not 0 <= offset <= 1000000:
                    raise ValueError("Invalid discovery offset")
                edges = [edge for edge in graph["edges"] if edge["direction"] == job["direction"]]
                body += '<div class="table-scroll"><table><thead><tr><th>Paper</th><th>DOI</th><th>Core paper DOI</th></tr></thead><tbody>'
                for edge in edges[offset:offset + 50]:
                    node = graph["nodes"][edge["target_provider_record"]]
                    record = node["record"]
                    title = titles[edge["target_provider_record"]] or "[Unresolved title]"
                    doi = record["identifiers"].get("doi", "")
                    body += (f"<tr><td>{escape(title)}</td><td>{escape(doi)}</td>"
                             f"<td>{escape(edge['seed_doi'])}</td></tr>")
                body += "</tbody></table></div>"
                if not edges:
                    body += "<p>No indexed relationships returned. Check provider gaps; this is not proof that none exist.</p>"
                if offset:
                    body += f'<a href="/discovery?run={run}&amp;offset={max(0, offset - 50)}">Previous</a> '
                if offset + 50 < len(edges):
                    body += f'<a href="/discovery?run={run}&amp;offset={offset + 50}">Next</a>'
            body += help_note("Saved search", f"<p>Saved locally: <code>{escape(str(folder))}</code>. "
                              "Resetting the project deletes these results.</p>")
        if runs:
            body += "<h3>Saved searches</h3><ul>"
            for name in runs[:20]:
                job = jobs[name]
                body += (f'<li><a href="/discovery?run={name}">{escape(job["started_at"])}: '
                         f'{escape(discovery_label(job))}</a> ({escape(job["status"])})</li>')
            body += "</ul>"
        return page("Find related papers", body)

    def start_discovery():
        if not project_root:
            raise ValueError("Project discovery is not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Discovery form expired; reload the page.", status=403, mimetype="text/plain")
        direction = request.form.get("direction")
        labels = DISCOVERY_LABELS
        if direction not in labels:
            raise ValueError("Invalid discovery direction")
        from .build import project_library
        with discovery_lock:
            if pdf_running():
                return Response("Wait for PDF processing to finish before starting discovery.",
                                status=409, mimetype="text/plain")
            if metadata_running():
                return Response("Wait for the metadata catalog update to finish.", status=409, mimetype="text/plain")
            if not project_library.load(project_root)["papers"]:
                raise ValueError("Register at least one core paper before discovery")
            if discovery_state["thread"] and discovery_state["thread"].is_alive():
                return Response("Discovery is already running; wait for it to finish.", status=409, mimetype="text/plain")
            run = secrets.token_hex(16)
            output = discovery_root() / run
            job = {"direction": direction, "label": labels[direction], "status": "running",
                   "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            collections.atomic_json(output, "job.json", job)

            def work():
                try:
                    report = project_library.discover(project_root, output, directions=[direction])
                    job.update(status="complete", report=report)
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                        releases.ReleaseError, ValidationError, literature.DiscoveryError) as error:
                    diagnostic("web_discovery_failed", exception_type=type(error).__name__)
                    job.update(status="failed", error=str(error))
                collections.atomic_json(output, "job.json", job)

            thread = threading.Thread(target=work, daemon=True)
            discovery_state["thread"] = thread
            discovery_state["run"] = run
            thread.start()
        return redirect("/discovery?run=" + run, code=303)

    def snapshot(request):
        if store:
            sid = request.args.get("snapshot_id")
            if sid:
                contracts.request("list_sources", {"snapshot_id": sid})
                return store.load(sid)
            return store.current()
        if "snapshot_id" in request.args:
            raise ValueError("Snapshot selection requires a registry")
        return pinned

    def overview():
        if project_root and not (project_root / "registry/active.json").is_file():
            return redirect("/settings")
        current = snapshot(request)
        status = current.call("corpus_status", {})["result"]
        def amount(key, one, many):
            return f"<strong>{status[key]:,}</strong> {one if status[key] == 1 else many}"
        counts = (f"{amount('paper_count', 'paper', 'papers')}, "
                  f"{amount('content_unit_count', 'searchable passage', 'searchable passages')}"
                  + (f" and {amount('claim_count', 'structured claim', 'structured claims')}" if status["claim_count"] else ""))
        tools = "".join(f"<dt><code>{escape(name)}</code></dt><dd>{escape(description)}</dd>"
                        for name, description in DESCRIPTIONS.items())
        visible = current.call("list_collections", {})["result"]["items"]
        memberships = "".join(
            f"<li><strong>{escape(item['title'])}</strong>: {escape(item['coverage'])}</li>" for item in visible)
        destinations = (
            ("/search", "Search evidence", "Find passages, read context and copy citations without AI."),
            ("/connect", "Connect your AI", "MCP setup, verification and credential troubleshooting."),
            ("/library", "Library",
             "Papers found, download/extraction progress and impact rankings."),
            ("/sources", "Sources available to AI",
             "Exact source versions published for MCP search and page-context retrieval."),
            ("/publications", "Publication metadata", "Bibliographic records, including sources not downloaded."),
            ("/coverage", "Literature coverage",
             "How the metadata catalog was assembled and its known limitations."),
        )
        cards = "".join(f'<dt><a href="{href}">{label}</a></dt><dd>{help_note(label, "<p>" + text + "</p>")}</dd>'
                        for href, label, text in destinations)
        health = mcp_url.removesuffix("/mcp")
        return page("Overview",
                    '<form class="hero-search" method="get" action="/search" role="search">'
                    '<label for="overview-query">Search the retained text of your papers</label>'
                    '<div class="input-row"><input id="overview-query" name="query" type="search" required '
                    'placeholder="Words or phrase"><button type="submit">Search evidence</button></div></form>'
                    f'<p class="tally">The library holds {counts}.</p>'
                    + help_note("Application", f"<p>Application {escape(__version__)}; tool contract {contracts.SCHEMA_VERSION}.</p>")
                    + f'<h3>Pages</h3><dl class="destinations">{cards}</dl>'
                    + help_note("Evidence states", '<p>Found, downloaded, extracted, available to AI and scientifically reviewed '
                    "are different states.</p>")
                    + '<details class="notes"><summary>Configured evidence and API</summary>'
                    f'<p>{escape(status["coverage"])}</p>'
                    f"<ul>{memberships}</ul>"
                    "<p>Local authorized evidence only, not all published knowledge. "
                    "Metadata discovery does not imply full-text acquisition. "
                    "Machine extraction is not scientific or human validation.</p>"
                    '<p><a href="/api/collections">Read-only library JSON API</a>: list evidence papers, '
                    "documents and page locations, retrieve text, or run keyword search without an AI model "
                    "or MCP process. See docs/library-api.md for examples. Access is local to this machine.</p></details>"
                    '<details class="notes"><summary>Connecting an AI client (MCP)</summary>'
                    '<div class="two-col"><section><h3>MCP connection</h3>'
                    f"<p>Configured endpoint: <code>{escape(mcp_url)}</code></p>"
                    "<p>Streamable HTTP; bearer token required. Start separately with "
                    "<code>make mcp</code>. This website does not start or probe the MCP server "
                    "and never reads credentials.</p>"
                    "<p>For Claude Code in this project, run "
                    "<code>make connect</code> in the research-papers terminal, then restart Claude Code "
                    "in this project and use <code>/mcp</code>. "
                    "<code>make reconnect</code> replaces this local connection with a fresh token; "
                    "<code>make disconnect</code> removes it and revokes its token. "
                    "These commands do not change papers, evidence or other MCP servers. "
                    "Override <code>CLIENT_PROJECT</code> for another project.</p></section>"
                    '<section><h3>Verify the running MCP server</h3>'
                    '<ol class="steps"><li>Start <code>make mcp</code> with the intended release/collection configuration.</li>'
                    "<li>In another terminal check liveness and readiness:<pre>"
                    f"{escape('curl --fail-with-body ' + health + '/healthz')}\n"
                    f"{escape('curl --fail-with-body ' + health + '/readyz')}"
                    "</pre>Expected: alive and ready. These checks do not validate your client token.</li>"
                    "<li>Connect an MCP client using your current bearer token; list tools "
                    f"({len(DESCRIPTIONS)} in version {escape(__version__)}), call <code>list_collections</code>, then pin its snapshot "
                    "for evidence and catalog calls. Test <code>search_evidence</code> in iStopMM "
                    "and <code>list_publications</code> in a metadata catalog collection.</li>"
                    "<li>See <code>docs/verify-mcp.md</code> in the project for a copy-and-run SDK "
                    "check of the live endpoint. It needs no model provider and never prints the token.</li></ol>"
                    '<p class="note">Opening /mcp in a browser is not a protocol test. '
                    "<code>make test-catalog</code> and <code>make test-http</code> launch separate "
                    "temporary servers; they do not check this running endpoint. "
                    "401 usually means an expired/invalid token; connection refused means no listener.</p></section></div>"
                    f'<h3>Read-only tools</h3><dl class="tool-list">{tools}</dl></details>')

    def available_project_pdfs():
        if pdf_data_root.resolve() != pdf_data_root:
            raise ValueError("PDF data folder cannot contain a symlinked path")
        if not pdf_data_root.exists():
            return {}
        return {path.name: path for path in sorted(pdf_data_root.iterdir())
                if path.suffix.casefold() == ".pdf" and not path.is_symlink() and path.is_file()}

    def pdf_controls(doi, title, *, prefix, pdfs, return_to="/settings", selected="", summary=None):
        hidden = (f'<input type="hidden" name="token" value="{provide_token}">'
                  f'<input type="hidden" name="doi" value="{escape(doi, quote=True)}">'
                  f'<input type="hidden" name="title" value="{escape(title, quote=True)}">'
                  f'<input type="hidden" name="return_to" value="{return_to}">')
        body = ""
        if pdfs:
            body += ('<details><summary>' + (summary or 'Select PDF and attach/extract') + '</summary>'
                     '<form method="post" action="/settings/attach-pdf">' + hidden
                     + f'<label for="{prefix}-pdf">PDF from data folder</label>'
                     f'<select id="{prefix}-pdf" name="pdf" required><option value="">Select a PDF</option>'
                     + "".join(f'<option value="{escape(name, quote=True)}"'
                               + (' selected' if name == selected else '') + f'>{escape(name)}</option>' for name in pdfs)
                     + '</select>'
                     f'<label for="{prefix}-license">License or permission note (optional)</label>'
                     f'<input id="{prefix}-license" name="license">'
                     '<label><input type="checkbox" name="confirm_identity" value="yes" required>'
                     ' I checked that this PDF matches this paper&#39;s title and DOI and am authorized '
                     'to extract it privately.</label><button>Attach and extract PDF</button></form></details>')
        else:
            body += (summary or '') + '<p>No local PDFs yet. Use Find PDF or place an authorized copy in the PDF data folder.</p>'
        body += ('<form method="post" action="/settings/find-pdf">' + hidden
                 + '<button class="secondary">Find PDF</button></form>')
        return body

    def project_settings(error=None, status_code=200):
        if not project_root:
            response = page("Project settings", "<p>Project settings are not enabled for this website.</p>")
            response.status_code = 404
            return response
        from .build import project_library
        initialized = (project_root / "project.json").is_file()
        body = help_note("Project storage", "<p>Manage the independent private project configured for this website. "
                         f"Project root: <code>{escape(str(project_root))}</code>.</p>")
        if error:
            body += f'<p role="alert" class="alert"><strong>Could not complete the request:</strong> {escape(error)}</p>'
        if initialized:
            status = project_library.status(project_root)
            body += (f'<section class="panel"><h3>{escape(status["name"])}</h3>'
                     f"<p>{len(status['papers'])} core papers registered.</p>")
            awaiting = [item for item in status["papers"] if item["status"] == "awaiting_pdf"]
            searchable = status["snapshot"] is not None
            body += ('<ol class="progress-steps"><li class="done">Register core papers</li>'
                     f'<li class="{"done" if not awaiting else "active"}">Attach PDFs</li>'
                     f'<li class="{"active" if searchable and not awaiting else ""}">Search and connect AI</li></ol>')
            pdfs = {}
            if awaiting:
                body += ('<p class="callout"><strong>Next step:</strong> '
                         f"{len(awaiting)} of {len(status['papers'])} papers are awaiting a PDF. "
                         "Select a matching PDF from the awaiting_pdf status in each paper's entry.</p>")
                pdfs = available_project_pdfs()
                body += ('<h4>Attach PDFs</h4>'
                         f'<p>PDF data folder: <code>{escape(str(pdf_data_root))}</code>. '
                         "Files in this shared folder are preserved when the project is reset. "
                         "Inspect the PDF locally before confirming its identity.</p>")
                if not pdfs:
                    body += '<p>No PDFs found. Place .pdf files in the PDF data folder, then reload this page.</p>'
            if status["papers"]:
                rows = []
                for index, item in enumerate(status["papers"]):
                    state = badge(escape(item["status"]))
                    if item["status"] == "awaiting_pdf":
                        state = pdf_controls(item["doi"], item["title"], prefix=f"attach-{index}",
                                             pdfs=pdfs, summary=state)
                    rows.append('<li class="library-record">'
                                f"<h4>{escape(item['title'])}</h4><dl>"
                                f"<div><dt>DOI</dt><dd><code>{escape(item['doi'])}</code></dd></div>"
                                f"<div><dt>Status</dt><dd>{state}</dd></div></dl>"
                                '<form method="post" action="/settings/purge-preview">'
                                f'<input type="hidden" name="token" value="{provide_token}">'
                                f'<input type="hidden" name="doi" value="{escape(item["doi"], quote=True)}">'
                                f'<button class="danger remove-core" aria-label="Remove {escape(item["title"], quote=True)}">'
                                'Remove…</button></form></li>')
                body += ('<ul class="library-records">'
                         + "".join(rows)
                         + "</ul>")
            body += "</section>"
            body += help_note("Find PDF", '<p><strong>Find PDF</strong> looks for an explicitly licensed open-access copy using OpenAlex. '
                     'Downloads go to the PDF data folder; inspect the file and confirm identity and permission '
                     'before attaching. A missing result is not proof that no copy exists.</p>')
            if (project_root / "pdf-job.json").is_file():
                body += '<p><a href="/settings/pdf-progress">Latest Find PDF result/progress</a></p>'
            body += (
                '<section class="panel"><h3>Add core papers</h3>'
                + help_note("Adding core papers", '<p>New papers start awaiting PDF. Adding a paper does not download it. '
                'Run discovery again to include new core papers in reference/citation searches.</p>')
                + '<form method="post" action="/settings/add-papers">'
                f'<input type="hidden" name="token" value="{provide_token}">'
                '<label for="add-doi">DOI</label><input id="add-doi" name="doi" required>'
                '<label for="add-title">Title</label><input id="add-title" name="title" maxlength="1024" required>'
                '<button>Add core paper</button></form>'
                '<h4>Import additional core papers</h4>'
                + help_note("Import format", '<p>CSV needs doi,title columns; JSON uses the same manifest format as initialization. '
                'Duplicate DOIs are rejected; an invalid import adds nothing.</p>')
                + '<form method="post" action="/settings/import-papers" enctype="multipart/form-data">'
                f'<input type="hidden" name="token" value="{provide_token}">'
                '<label for="import-papers">CSV or JSON file</label>'
                '<input id="import-papers" name="papers" type="file" accept=".csv,.json" required>'
                '<button>Import core papers</button></form></section>')
            body += ('<section class="panel"><h3>Discover related papers</h3>'
                     '<p><a href="/discovery">Find references or citing papers</a>.</p>'
                     + help_note("Discovery and metadata", '<p>Metadata-only discovery, with saved provider coverage and gaps; no PDFs are downloaded.</p>'
                     '<p>After searches finish, <a href="/publications">configure/update the publication metadata catalog</a> '
                     'to make saved bibliography available on Publication metadata and Literature coverage.</p>') + '</section>')
            body += ('<section class="panel danger"><h3>Reset to factory settings</h3>'
                     "<p>This permanently deletes everything inside this project's root, including its PDFs, "
                     "extracted evidence, snapshots, discovery output and settings. It cannot be undone. "
                     "Files outside the project root are not affected.</p>"
                     '<form method="post" action="/settings/reset">'
                     '<label for="reset-confirm">Type RESET PROJECT to confirm</label> '
                     '<input id="reset-confirm" name="confirmation" required autocomplete="off">'
                     '<button type="submit" class="danger">Reset to factory settings</button></form></section>')
        else:
            body += ('<section class="panel"><h3>Initialize project</h3>'
                     "<ol class=\"progress-steps\"><li class=\"active\">Register core papers</li>"
                     "<li>Attach PDFs</li><li>Search and connect AI</li></ol>"
                     "<p>Select a core-paper CSV (doi and title columns) or JSON manifest "
                     "(schema_version 1 with a papers list of doi/title records). The file is validated "
                     "before project data is created. Original PDFs are not uploaded or acquired here.</p>"
                     '<form method="post" action="/settings/initialize" enctype="multipart/form-data">'
                     '<label for="project-name">Project name</label> '
                     '<input id="project-name" name="name" maxlength="256" value="My research project" required>'
                     '<label for="project-auth">Private-use authorization basis</label>'
                     f'<textarea id="project-auth" name="authorization" required rows="3">'
                     f'{escape(PROJECT_AUTHORIZATION_DEFAULT)}</textarea>'
                     '<label for="core-papers">Core papers file (.csv or .json)</label> '
                     '<input id="core-papers" name="papers" type="file" accept=".csv,.json" required>'
                     '<button type="submit">Initialize project</button></form>'
                     "<p>The authorization note records your stated basis; it does not verify rights. "
                     "Registered papers start as awaiting PDF.</p></section>")
        key_configured = project_library.openalex_key_path(project_root).is_file()
        body += (
            '<section class="panel"><h3>OpenAlex API key</h3>'
            f'<p>Status: <strong>{"Configured" if key_configured else "Not configured"}</strong>. '
            '</p>'
            + help_note("OpenAlex API key", '<p>A key enables cursor pagination beyond 10,000 incoming citations.</p>'
            '<p><a href="https://openalex.org/signup">Create a free OpenAlex account</a>, then '
            '<a href="https://openalex.org/settings/api-key">copy your API key from OpenAlex settings</a>. '
            '<a href="https://help.openalex.org/api/authentication/">API key instructions</a>.</p>')
            + '<form method="post" action="/settings/openalex-key">'
            f'<input type="hidden" name="token" value="{provide_token}">'
            '<label for="openalex-key">New OpenAlex API key</label>'
            '<input id="openalex-key" name="api_key" type="password" maxlength="512" autocomplete="new-password" required>'
            '<button>Save API key</button></form>'
            + help_note("Key storage and use", '<p>The saved key is never displayed. It is stored privately for this installation, '
            'survives project reset, and is sent only to OpenAlex. New searches use it automatically; '
            'existing incomplete results are unchanged. Start a new incoming-citation search and then '
            'update the metadata catalog.</p>')
            + (f'<form method="post" action="/settings/openalex-key">'
               f'<input type="hidden" name="token" value="{provide_token}">'
               '<button name="action" value="remove">Remove saved API key</button></form>'
               if key_configured else "") + '</section>')
        body += ('<dialog id="purge-dialog" aria-label="Confirm permanent core-paper removal">'
                 '<button type="button" class="secondary" data-close-dialog>Cancel</button>'
                 '<div data-dialog-content></div></dialog>')
        response = page("Project settings", body, settings_script=True)
        response.status_code = status_code
        return response

    def add_project_papers():
        if not project_root:
            raise ValueError("Project settings are not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Settings form expired; reload the page.", status=403, mimetype="text/plain")
        from .build import project_library
        try:
            with discovery_lock:
                require_idle_project()
                if request.path == "/settings/import-papers":
                    upload = request.files.get("papers")
                    if upload is None or not upload.filename:
                        raise ValueError("Choose a CSV or JSON core-paper file")
                    data = upload.stream.read(PROJECT_UPLOAD_MAX_BYTES + 1)
                    if len(data) > PROJECT_UPLOAD_MAX_BYTES:
                        raise ValueError("The core-papers file exceeds the 4 MiB limit")
                    suffix = Path(upload.filename).suffix.casefold()
                    if suffix not in {".csv", ".json"}:
                        raise ValueError("Choose a .csv or .json core-paper file")
                    rows = (project_library.read_csv_bytes(data) if suffix == ".csv"
                            else project_library.read_manifest_bytes(data))
                else:
                    rows = [{"doi": request.form.get("doi", ""), "title": request.form.get("title", "")}]
                project_library.register(project_root, rows)
        except ValueError as error:
            return project_settings(str(error), 400)
        return redirect("/settings", code=303)

    def perform_core_purge():
        if not project_root:
            raise ValueError("Project settings are not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Settings form expired; reload the page.", status=403, mimetype="text/plain")
        from .build import project_purge
        with discovery_lock:
            require_idle_project()
            doi = request.form.get("doi", "")
            data = project_purge.plan(project_root, doi, pdf_data_root, prepare=False)
            report = project_purge.summary(data)
            if request.path == "/settings/purge-preview":
                body = (
                    f'<h3>{escape(report["title"])}</h3><p>DOI: <code>{escape(report["doi"])}</code>.</p>'
                    '<p>This permanently removes the paper from core and AI evidence, its project PDFs/text, '
                    'and discovered records connected only to it. Shared connections to remaining core papers stay.</p>'
                    f'<p>{report["related_removed"]} provider relationship records lose their last core connection.</p>'
                    '<p>Connections are determined from saved provider results, which may be incomplete. '
                    'Review the preview carefully; this is not an exhaustive check of every bibliography.</p>'
                    '<p>Affected historical evidence snapshots and metadata catalogs are deleted. Discovery response caches '
                    'are deleted and saved search results are filtered. The current metadata catalog is rebuilt if configured. '
                    'Old snapshot links may stop working. This is not reversible.</p>'
                    '<p>Stop any MCP servers before purging and restart them afterward to discard cached old evidence. '
                    'This removes application-managed copies, not external backups or copies elsewhere on your computer. '
                    'Text in retained core PDFs is not rewritten if it cites the removed paper.</p>'
                    '<h4>Matching shared PDFs to delete</h4><ul>'
                    + "".join(f'<li>{escape(path)}</li>' for path in report["pdf_copies"])
                    + ('<li>No matching shared PDF copies.</li>' if not report["pdf_copies"] else "")
                    + '</ul><form method="post" action="/settings/purge-paper">'
                    f'<input type="hidden" name="token" value="{provide_token}">'
                    f'<input type="hidden" name="doi" value="{escape(report["doi"], quote=True)}">'
                    f'<input type="hidden" name="plan_id" value="{project_purge.fingerprint(data)}">'
                    '<label>Type the full DOI to confirm permanent deletion'
                    '<input name="confirmation" required autocomplete="off"></label>'
                    '<button class="danger">Permanently purge paper and exclusive related material</button></form>'
                    '<p><a href="/settings">Cancel and return to Project settings</a></p>')
                return page("Confirm permanent core-paper removal", body)
            if request.form.get("confirmation") != report["doi"]:
                raise ValueError("Type the full DOI exactly to confirm permanent deletion")
            plan_id = request.form.get("plan_id", "")
            if not hmac.compare_digest(plan_id, project_purge.fingerprint(data)):
                raise ValueError("Project data changed or the confirmation expired; review a new purge preview")
            job = {"status": "running", "message": "Preparing permanent paper removal"}
            purge_state["job"] = job
            collections.atomic_json(project_root, "purge-job.json", job)

            def progress(message):
                job["message"] = message
                collections.atomic_json(project_root, "purge-job.json", job)

            def work():
                try:
                    result = project_purge.purge(project_root, doi, pdf_data_root, progress=progress,
                                                 expected_plan=plan_id)
                    job.update(status="complete", message="Paper and exclusive related material permanently removed",
                               report=result)
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ValidationError) as error:
                    diagnostic("web_core_purge_failed", exception_type=type(error).__name__)
                    job.update(status="failed", message=str(error))
                store.clear_cache()
                collections.atomic_json(project_root, "purge-job.json", job)

            thread = threading.Thread(target=work, daemon=True)
            purge_state["thread"] = thread
            thread.start()
        return redirect("/settings/purge-progress", code=303)

    def purge_core_paper():
        try:
            return perform_core_purge()
        except ValueError as error:
            diagnostic("web_core_purge_rejected", exception_type=type(error).__name__)
            return project_settings(str(error), 400)

    def purge_progress():
        if not project_root:
            raise ValueError("Project settings are not enabled")
        path = project_root / "purge-job.json"
        if purge_running():
            job = dict(purge_state["job"])
        elif path.is_file():
            job = releases.read_json(releases.safe_file(project_root, path.name).read_bytes())
        else:
            return redirect("/settings")
        running = job["status"] == "running" and purge_running()
        message = job["message"]
        if job["status"] == "running" and not running:
            message = "Paper purge was interrupted. Inspect project data before retrying; deletion may be incomplete."
        body = f'<p role="status">{escape(message)}</p>'
        if running:
            body += '<progress aria-label="Permanently removing paper"></progress><p>Do not stop the app during deletion.</p>'
        else:
            if job["status"] == "complete":
                report = job["report"]
                body += (f'<p>Removed: {escape(report["title"])} ({escape(report["doi"])}). '
                         f'{len(report["pdf_copies"])} shared PDF copies deleted.</p>')
            body += ('<p><a href="/settings">Project settings</a> | <a href="/library">Library</a></p>'
                     '<p>Restart any running MCP server to discard cached old snapshots.</p>')
        response = page("Permanent paper removal", body)
        response.headers["Cache-Control"] = "no-store"
        if running:
            response.headers["Refresh"] = "2"
        return response

    def save_project_openalex_key():
        if not project_root:
            raise ValueError("Project settings are not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Settings form expired; reload the page.", status=403, mimetype="text/plain")
        from .build import project_library
        if request.form.get("action") == "remove":
            path = project_library.openalex_key_path(project_root)
            if path.is_symlink():
                raise ValueError("OpenAlex API key file cannot be a symlink")
            path.unlink(missing_ok=True)
        else:
            project_library.save_openalex_key(project_root, request.form.get("api_key", ""))
        return redirect("/settings", code=303)

    def initialize_project():
        if not project_root:
            return project_settings("Project settings are not enabled for this website.", 404)
        if (project_root / "project.json").exists():
            return project_settings("This project is already initialized.", 409)
        upload = request.files.get("papers")
        if upload is None or not upload.filename:
            return project_settings("Choose a CSV or JSON core-papers file.", 400)
        data = upload.stream.read(PROJECT_UPLOAD_MAX_BYTES + 1)
        if len(data) > PROJECT_UPLOAD_MAX_BYTES:
            return project_settings("The core-papers file exceeds the 4 MiB limit.", 400)
        suffix = Path(upload.filename).suffix.casefold()
        from .build import project_library
        try:
            if suffix == ".csv":
                papers = project_library.read_csv_bytes(data)
            elif suffix == ".json":
                papers = project_library.read_manifest_bytes(data)
            else:
                return project_settings("Choose a .csv or .json core-papers file.", 400)
            project_library.initialize(project_root, request.form.get("name", ""),
                                       request.form.get("authorization", PROJECT_AUTHORIZATION_DEFAULT), papers)
        except ValueError as error:
            return project_settings(str(error), 400)
        return redirect("/settings", code=303)

    def reset_project():
        if not project_root:
            return project_settings("Project settings are not enabled for this website.", 404)
        if request.form.get("confirmation") != "RESET PROJECT":
            return project_settings("Type RESET PROJECT exactly to confirm deletion.", 400)
        from .build import project_library
        with discovery_lock:
            if pdf_running():
                return project_settings("Wait for PDF processing to finish before resetting.", 409)
            if metadata_running():
                return project_settings("Wait for the metadata catalog update to finish before resetting.", 409)
            if discovery_state["thread"] and discovery_state["thread"].is_alive():
                return project_settings("Wait for citation discovery to finish before resetting the project.", 409)
            try:
                project_library.reset(project_root)
            except ValueError as error:
                return project_settings(str(error), 400)
        if store:
            store.clear_cache()
        return redirect("/settings", code=303)

    def find_project_pdf():
        if not project_root:
            raise ValueError("Project settings are not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return project_settings("This form expired. Reload settings and try again.", 403)
        from .build import project_library
        import httpx
        with discovery_lock:
            require_idle_project()
            doi = request.form.get("doi", "")
            paper = project_library.library_paper(project_root, doi, request.form.get("title"))
            return_to = "/library" if request.form.get("return_to") == "/library" else "/settings"
            job = {"status": "running", "title": paper["title"], "return_to": return_to,
                   "message": "Looking for an explicitly licensed open-access PDF"}
            collections.atomic_json(project_root, "pdf-job.json", job)

            def work():
                try:
                    result = project_library.find_pdf(project_root, doi, pdf_data_root, title=paper["title"])
                    job.update(status="complete", result=result)
                except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError,
                        releases.ReleaseError, literature.DiscoveryError) as error:
                    diagnostic("web_find_pdf_failed", exception_type=type(error).__name__)
                    job.update(status="failed", message=str(error))
                collections.atomic_json(project_root, "pdf-job.json", job)

            thread = threading.Thread(target=work, daemon=True)
            pdf_state["thread"] = thread
            thread.start()
        return redirect("/settings/pdf-progress", code=303)

    def pdf_progress():
        if not project_root:
            raise ValueError("Project settings are not enabled")
        path = project_root / "pdf-job.json"
        if not path.exists():
            return redirect("/settings")
        job = releases.read_json(releases.safe_file(project_root, path.name).read_bytes())
        body = f'<h3>{escape(job["title"])}</h3>'
        running = job["status"] == "running" and pdf_running()
        if running:
            body += ('<p role="status">Finding and downloading an explicitly licensed PDF. '
                     'This uses OpenAlex and the hosting site; no access controls are bypassed.</p>'
                     '<progress aria-label="Finding PDF"></progress>')
        elif job["status"] == "complete":
            result = job["result"]
            if result["status"] == "downloaded":
                body += (f'<p>Downloaded <code>{escape(result["filename"])}</code> to the PDF data folder.</p>'
                         f'<p>Acquired from: {metadata_value(result["final_url"])}. '
                         f'License: {escape(result["acquisition"]["license"])}.</p>'
                         '<p>Not attached or extracted. Inspect the PDF locally, then select it in Project settings '
                         'and confirm its identity and private-use permission.</p>')
                body += pdf_controls(result["doi"], job["title"], prefix="found",
                                     pdfs=available_project_pdfs(), return_to=job.get("return_to", "/settings"),
                                     selected=result["filename"])
            else:
                body += ('<p>No PDF was downloaded. This does not mean no accessible copy exists.</p>'
                         f'<p>Result: {escape(result["status"])}. You can supply an authorized copy yourself.</p>')
                for attempt in result["attempts"]:
                    body += f'<p>{metadata_value(attempt["url"])}: {escape(attempt["error"])}</p>'
        else:
            message = job["message"] if job["status"] == "failed" else "PDF search was interrupted; retry from Project settings."
            body += f'<p role="alert">{escape(message)}</p>'
        body += '<p><a href="/settings">Return to Project settings</a></p>'
        body += '<p><a href="/library">Return to Library</a></p>'
        response = page("Find PDF", body)
        response.headers["Cache-Control"] = "no-store"
        if running:
            response.headers["Refresh"] = "2"
        return response

    def attach_project_pdf():
        if not project_root:
            return project_settings("Project settings are not enabled for this website.", 404)
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return project_settings("This form expired. Reload settings and try again.", 403)
        from .build import project_library
        try:
            if request.form.get("confirm_identity") != "yes":
                raise ValueError("Confirm that the PDF matches the selected title and DOI and private use is authorized.")
            selected = available_project_pdfs().get(request.form.get("pdf", ""))
            if selected is None:
                raise ValueError("Select an available PDF from the PDF data folder.")
            with discovery_lock:
                require_idle_project()
                project_library.attach(project_root, request.form.get("doi", ""), selected,
                                       identity_confirmed=True, license=request.form.get("license"),
                                       title=request.form.get("title"))
        except ValueError as error:
            return project_settings(str(error), 400)
        store.clear_cache()
        return redirect("/library" if request.form.get("return_to") == "/library" else "/settings", code=303)

    def sources():
        if set(request.args) - {"offset", "paper", "collection", "snapshot_id"}:
            raise ValueError("Unknown query parameter")
        current = snapshot(request)
        args = {"limit": 20, "offset": int(request.args.get("offset", "0"))}
        paper = request.args.get("paper", "")
        cid = request.args.get("collection", "")
        if paper:
            args["paper"] = paper
        if store:
            args["snapshot_id"] = current.release_id
            if cid:
                args["collections"] = [cid]
        elif cid:
            raise ValueError("Collection selection requires a registry")
        result = current.call("list_sources", args)
        visible = current.call("list_collections", {})["result"]["items"]
        options = '<option value="">Default collections</option>' + "".join(
            f'<option value="{escape(item["id"], quote=True)}"'
            f'{" selected" if item["id"] == cid else ""}>{escape(item["title"])}</option>'
            for item in visible)
        form = ('<form method="get" class="filters"><label>DOI / publication ID '
                f'<input name="paper" value="{escape(paper, quote=True)}"></label> '
                + (f'<label>Collection <select name="collection">{options}</select></label> '
                   if store and len(visible) > 1 else "")
                + '<button type="submit">Filter</button></form>')
        collection_note = (f"<p>Showing AI evidence from <strong>{escape(visible[0]['title'])}</strong>.</p>"
                           if store and len(visible) == 1 else "")
        entries = []
        acquired_links = successful_supplement_links(data_root)
        downloads = downloaded_pdf_metadata(pdf_data_root) if project_root else {}
        metadata_snapshots = []
        if project_root:
            from .build import project_library
            ids = project_library.metadata_collections(project_root)
            if ids:
                metadata_store = collections.CollectionStore(
                    registry, allowed_profiles=profiles, allowed_collections=["core", *ids], defaults=["core"])
                metadata_current = metadata_store.current()
                metadata_snapshots = [metadata_current.snapshots[key] for key in ids]
        refs = result.get("collection_context", {}).get("evidence_refs", [])
        missing_titles = {source["publication_id"] for source in result["result"]["items"]
                          if not source["bibliography"]["title"]}
        catalog_titles = publication_titles(current, missing_titles)
        for index, source in enumerate(result["result"]["items"]):
            bib = source["bibliography"]
            title = bib["title"] or catalog_titles.get(source["publication_id"])
            details = {
                "Publication": source["publication_id"], "Year": bib["year"],
                "Source ID": source["source_id"], "Format": source["format"],
                "SHA-256": source["source_sha256"], "Citation/source URL": source["source_url"],
                "Acquired from": bib["acquired_from"], "Acquisition method": bib["acquisition_method"],
                "Acquired at": bib["acquired_at"], "Provenance": source["provenance"],
                "Used in": ", ".join(source["used_in"]) or "No evidence references",
            }
            download = downloads.get((collections.publication(source["doi"]), source["source_sha256"]))
            if download and not bib["acquired_from"]:
                details["Acquired from"] = download["final_url"] or download["source_url"]
                details["Acquisition method"] = "Downloaded PDF, then attached locally (SHA-256 and DOI matched)"
                details["Acquired at"] = download["retrieved_at"]
                details["Download provenance"] = "Saved local download record; retained evidence snapshot unchanged"
                if download.get("version_note"):
                    details["PDF version note"] = download["version_note"]
                if download.get("license"):
                    details["Download rights note"] = download["license"]
                if not bib["year"] and download.get("year"):
                    details["Year"] = download["year"]
                    details["Year metadata source"] = "DOI-matched PDF lookup metadata"
            if not bib["year"]:
                for metadata in metadata_snapshots:
                    try:
                        record = metadata.get(catalog.alias("doi", source["doi"].removeprefix("doi:")))
                    except retrieval.NotFoundError:
                        continue
                    if record["publication_date"]:
                        details["Year"] = record["publication_date"][:4]
                        details["Year metadata source"] = "DOI-matched publication catalog; not inferred from DOI or download date"
                        break
            acquired_url = acquired_links.get((collections.publication(source["doi"]), source["source_sha256"]))
            if acquired_url:
                details["Successful acquisition URL (matching source checksum)"] = acquired_url
            if refs:
                details["Collection"] = refs[index]["collection_id"]
                details["Evidence release"] = refs[index]["release_id"]
            if not bib["title"] and title:
                details["Display title from"] = "Identifier-matched metadata catalog; evidence source record unchanged"
            document = source["source_id"].rsplit(":", 1)[-1]
            if document == "main":
                document_label = "Main paper"
            elif document.startswith("supplement-") and document.removeprefix("supplement-").isdigit():
                document_label = "Supplement " + str(int(document.removeprefix("supplement-")))
            else:
                document_label = "PDF" if source["format"] == "pdf" else "Source document"
            details["Document"] = document_label
            fields = "".join(f"<dt>{escape(key)}</dt><dd>{metadata_value(value)}</dd>"
                             for key, value in details.items())
            entries.append(
                '<article class="source-entry"><details>'
                f"<summary>{escape(title or source['doi'])} &mdash; {escape(document_label)}</summary>"
                f"<dl>{fields}</dl>"
                + (f'<p><a href="{escape(acquired_url, quote=True)}" rel="noreferrer">Open successfully acquired document</a>'
                   " — Worked at acquisition; publisher access may change.</p>" if acquired_url else "")
                + "</details></article>")
            if project_root and refs and refs[index]["collection_id"] == "core":
                entries[-1] += (f'<p><a href="/library/edit?{escape(urlencode({"doi": source["doi"]}), quote=True)}">'
                                'View and correct extracted data</a></p>')
            member_cid = refs[index]["collection_id"] if store else ""
            evidence = current.snapshots[member_cid] if store else current
            units = sorted(evidence.source_units(source["source_id"]), key=quality_order)
            if units and evidence.profile != "curated":
                pin = {"snapshot_id": current.release_id} if store else {"release_id": current.release_id}
                reader_link = f'<p><a href="{escape(reader_url(pin, member_cid, units[0]), quote=True)}">Read retained text</a></p>'
                entries[-1] = entries[-1].replace("</details></article>", reader_link + "</details></article>")
        def link(offset, label):
            params = {"offset": offset, "paper": paper}
            if store:
                params.update(snapshot_id=current.release_id, collection=cid)
            return f'<a href="/sources?{escape(urlencode(params), quote=True)}">{label}</a>'
        navigation = []
        if args["offset"]:
            navigation.append(link(max(0, args["offset"] - 20), "Previous"))
        continuation = result["result"]["next_offset"]
        if continuation is not None:
            navigation.append(link(continuation, "Next"))
        return page("Sources available to AI", help_note("Sources available to AI", "<p>Exact source versions in the selected active MCP evidence "
                    'collections, not the full discovered bibliography. <a href="/search">Search evidence</a> '
                    "or use Read retained text on a document below. This page is an inventory, not a chat. "
                    "Availability does not mean scientific review.</p><p>Citation links are not necessarily download origins. "
                    "Unknown acquisition details are not inferred. Original files and extracted text "
                    "are not displayed on this inventory page. Authorized extracted text is available through "
                    "the local JSON API or MCP; original files are not served. This is corpus inventory, "
                    "not the sources used in a particular answer.</p>"
                    ) + collection_note + form
                    + ("".join(entries) or "<p>No matching sources.</p>")
                    + '<nav class="pager">' + "".join(navigation) + "</nav>")

    def stylesheet():
        return Response(STYLESHEET, mimetype="text/css")

    def edit_paper(error=None, status_code=200, *, draft_text=None):
        if not project_root:
            raise ValueError("Editing is enabled only for private project libraries")
        if request.method == "GET" and set(request.args) - {"doi", "unit_id"}:
            raise ValueError("Unknown editor parameter")
        from .build import project_edit, project_library
        doi = request.values.get("doi", "")
        current = project_library.store(project_root).current()
        evidence = current.snapshots["core"]
        source = next((source for source in evidence._bundle["sources"]
                       if collections.publication(source["doi"]) == collections.publication(doi)), None)
        if not source:
            raise ValueError("Attach and extract this paper before editing")
        units = sorted([unit for unit in evidence._bundle["units"]
                        if unit["citation"]["source_id"] == source["id"]], key=quality_order)
        unit_id = request.values.get("unit_id", units[0]["id"] if units else "")
        unit = next((unit for unit in units if unit["id"] == unit_id), None)
        if unit is None:
            raise ValueError("Select an extracted page belonging to this paper")
        tables = [table for table in evidence._bundle["tables"]
                  if table["citation"]["source_id"] == source["id"]
                  and table["citation"]["locator"] == unit["citation"]["locator"]]
        bibliography = source.get("bibliography") or {key: None for key in project_edit.BIB_FIELDS}
        submitted = request.form if request.method == "POST" else {}
        def value(name, default=""):
            if name == "text" and draft_text is not None:
                return escape(draft_text, quote=True)
            return escape(submitted.get(name, default) or "", quote=True)
        body = (f'<h3>{escape(bibliography["title"] or source["doi"])}</h3>'
                + help_note("Editing extracted data", '<p>Edit retained page text/OCR, paper metadata and structured tables. '
                'Compare against the original PDF locally; original files are not served here. '
                'Saving publishes a new evidence version used by search and AI. Original versions, '
                'source checksums, page locators and old snapshot links are preserved. '
                'Corrections are operator assertions, not independent scientific validation.</p>')
                + f'<p>DOI: <code>{escape(source["doi"])}</code>.</p>')
        if error:
            body += f'<p role="alert">{escape(error)}</p>'
        if draft_text is not None:
            body += ('<p role="status">Paragraph-layout draft prepared; nothing has been saved. '
                     'Text blocks are ordered by column and lines joined within each block. '
                     'Check reading order, paragraph boundaries, hyphenated words and tables against the original PDF '
                     'before saving. This replaces the text draft, not the retained evidence.</p>')
        body += ('<nav class="pager" aria-label="Retained pages">'
                 + "".join('<a href="/library/edit?'
                           + escape(urlencode({"doi": doi, "unit_id": item["id"]}), quote=True) + '#page-editor"'
                           + (' aria-current="page"' if item["id"] == unit_id else '')
                           + f'>{escape(quality_location(item["citation"]["locator"]))}</a> ' for item in units)
                 + '</nav>')
        body += ('<form method="post" action="/library/edit">'
                 f'<input type="hidden" name="token" value="{provide_token}">'
                 f'<input type="hidden" name="doi" value="{escape(doi, quote=True)}">'
                 f'<input type="hidden" name="unit_id" value="{escape(unit_id, quote=True)}">'
                 f'<input type="hidden" name="snapshot_id" value="{value("snapshot_id", current.release_id)}">'
                 f'<input type="hidden" name="source_id" value="{value("source_id", source["id"])}">'
                 f'<h4 id="page-editor">{escape(quality_location(unit["citation"]["locator"]))} — Page text / OCR</h4>'
                 '<p class="note">Save changes before switching pages.</p>'
                 f'<label for="edit-text">Full retained text: {escape(quality_location(unit["citation"]["locator"]))}</label>'
                 f'<textarea id="edit-text" name="text" rows="18" maxlength="1000000">{value("text", unit["text"])}</textarea>'
                 '<button type="submit" name="action" value="repair-layout" formnovalidate class="secondary">'
                 'Repair paragraph layout</button>'
                 '<small>Replaces unsaved text edits.</small>'
                 + help_note("Repair paragraph layout", '<p>Rebuild an unsaved text draft from the original PDF text blocks, separating columns and paragraphs '
                 'instead of mixing lines across columns. Replaces any text edits in this form; review before saving. '
                 'For scanned/OCR-only pages, edit manually. No original extraction is changed by this button.</p>')
                 + '<details><summary>Paper metadata</summary>'
                 + help_note("Paper metadata", '<p>These fields apply to the entire paper, not only this page. '
                 'DOI/source identity cannot be changed here. Saved provider/download provenance remains unchanged.</p>'))
        labels = {"title": "Title", "year": "Publication year (YYYY)",
                  "acquired_from": "Acquired from URL", "acquisition_method": "Acquisition method",
                  "acquired_at": "Acquisition date/time (ISO format)"}
        for key in project_edit.BIB_FIELDS:
            body += (f'<label for="edit-{key}">{labels[key]}</label>'
                     f'<input id="edit-{key}" name="{key}" value="{value(key, bibliography.get(key))}">'
                     )
        body += ('<label for="edit-source-url">Citation/source URL</label>'
                 f'<input id="edit-source-url" name="source_url" value="{value("source_url", source["url"])}">'
                 f'</details><details><summary>Structured tables ({len(tables)})</summary>'
                 + help_note("Editing tables", '<p>The PDF extractor may retain tables only as page text. You can add a structured transcription here. '
                 'Edit the JSON list below: each table has id, title, headers, units, rows and footnotes. '
                 'Keep existing IDs; use an empty id for a new table. Removing a table from the list removes it '
                 'from the new version only. Rows and column units must match the header width. '
                 'Maximum 100 tables, 100 columns and 100 rows per table.</p>'))
        for table in tables:
            body += f'<h5>{escape(table["title"])}</h5><div class="table-scroll"><table><thead><tr>'
            body += "".join(f'<th>{escape(header)}</th>' for header in table["headers"]) + '</tr></thead><tbody>'
            for row in table["rows"]:
                body += '<tr>' + "".join(f'<td>{escape(str(cell)) if cell is not None else ""}</td>' for cell in row) + '</tr>'
            body += '</tbody></table></div>'
        editable = [{"id": table["id"], **{key: table[key] for key in project_edit.TABLE_FIELDS}} for table in tables]
        body += ('<details><summary>New table example</summary><pre>'
                 + escape(json.dumps([{"id": "", "title": "Table 1", "headers": ["Measure", "Value"],
                                       "units": [None, None], "rows": [["Example", 1]], "footnotes": []}], indent=2))
                 + '</pre></details><label for="edit-tables">Tables (JSON)</label>'
                 f'<textarea id="edit-tables" name="tables" rows="12">{value("tables", json.dumps(editable, ensure_ascii=False, indent=2))}</textarea>'
                 '</details><h4>Correction audit</h4><label for="edit-reviewer">Your name</label>'
                 f'<input id="edit-reviewer" name="reviewer" maxlength="200" required value="{value("reviewer")}">'
                 '<label for="edit-reason">What changed and why</label>'
                 f'<textarea id="edit-reason" name="reason" maxlength="2000" rows="3" required>{value("reason")}</textarea>'
                 '<label><input type="checkbox" name="checked" value="yes" required>'
                 ' I checked these corrections against the original PDF. This is not scientific approval of the paper.</label>'
                 '<button>Save corrected version</button></form>')
        history = project_edit.history(project_root, source["id"])
        body += f'<details><summary>Correction history ({len(history)})</summary>'
        for event in history[:20]:
            body += ('<details><summary>' + escape(event["at"] + " — " + event["reviewer"])
                     + '</summary>' + metadata_value({
                         "Reason": event["reason"], "Original PDF checksum": event["source_sha256"],
                         "Parent snapshot": event["parent_snapshot"], "New evidence release": event["release"],
                         "Before": event["before"], "After": event["after"]}) + '</details>')
        body += '</details><p><a href="/library">Library</a> | <a href="/sources">Sources available to AI</a></p>'
        response = page("View and correct extracted data", body)
        response.status_code = status_code
        response.headers["Cache-Control"] = "no-store"
        return response

    def save_paper_correction():
        if not project_root:
            raise ValueError("Project settings are not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Editor form expired; reload the page.", status=403, mimetype="text/plain")
        from .build import project_edit
        try:
            with discovery_lock:
                require_idle_project()
                if request.form.get("action") == "repair-layout":
                    draft = project_edit.paragraph_draft(project_root,
                        snapshot_id=request.form.get("snapshot_id", ""), source_id=request.form.get("source_id", ""),
                        unit_id=request.form.get("unit_id", ""))
                    return edit_paper(draft_text=draft)
                try:
                    tables = json.loads(request.form.get("tables", "[]"))
                except ValueError as error:
                    raise ValueError("Tables must contain valid JSON; your edits have not been saved") from error
                project_edit.correct(project_root, snapshot_id=request.form.get("snapshot_id", ""),
                    source_id=request.form.get("source_id", ""), unit_id=request.form.get("unit_id", ""),
                    text=request.form.get("text", ""),
                    bibliography={key: request.form.get(key, "") for key in project_edit.BIB_FIELDS},
                    source_url=request.form.get("source_url", ""), tables=tables,
                    reviewer=request.form.get("reviewer", ""), reason=request.form.get("reason", ""),
                    checked=request.form.get("checked") == "yes")
        except (ValueError, ValidationError) as error:
            diagnostic("web_correction_rejected", exception_type=type(error).__name__)
            return edit_paper(str(error), 400)
        store.clear_cache()
        return redirect("/library/edit?" + urlencode({
            "doi": request.form.get("doi", ""), "unit_id": request.form.get("unit_id", "")}), code=303)

    def batch_action():
        from .build import project_batch, project_index
        if not project_root:
            raise ValueError("Project settings are not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Batch form expired; reload Library.", status=403, mimetype="text/plain")
        try:
            with discovery_lock:
                action = request.form.get("action", "")
                if action == "pause":
                    if not batch_running():
                        raise ValueError("No active batch to pause")
                    batch_state["pause"].set()
                    return redirect("/library/batch", code=303)
                require_idle_project()
                if action in {"toggle", "page", "all", "clear"}:
                    job = project_batch.load(project_root)
                    if action == "clear":
                        for item in job["items"].values():
                            item["selected"] = False
                        project_batch.save(project_root, job)
                    else:
                        query = request.form.get("query", "")
                        relationship = request.form.get("relationship", "")
                        status = request.form.get("status", "")
                        offset = int(request.form.get("offset", "0"))
                        if action == "toggle":
                            doi = request.form.get("doi", "")
                            result = project_index.library_page(project_root, doi, select_all=True)
                            rows = [row for row in result["rows"] if row["doi"] == doi]
                            if not rows:
                                raise ValueError("Paper no longer exists in Library")
                            selected = not job["items"].get(doi, {}).get("selected", False)
                        else:
                            result = project_index.library_page(project_root, query, offset,
                                relationship=relationship, status=status, select_all=action == "all")
                            rows, selected = result["rows"], True
                        job = project_batch.select(project_root, rows, selected)
                        if action == "toggle" and request.headers.get("Accept") == "application/json":
                            item = job["items"].get(doi)
                            if item is None:
                                raise ValueError("Paper is no longer eligible for PDF location")
                            return jsonify(selected=item["selected"],
                                           selected_count=sum(item["selected"] for item in job["items"].values()))
                    params = {name: request.form.get(name, "") for name in
                              ("query", "relationship", "status", "offset")}
                    params["offset"] = params["offset"] or "0"
                    fragment = ("#locate-" + releases.sha256(request.form["doi"].encode())[:16]
                                if action == "toggle" else "")
                    return redirect("/library?" + urlencode(params) + fragment, code=303)
                if action == "locate":
                    job = project_batch.prepare_locate(project_root)
                elif action == "extract":
                    choices = [(doi, request.form.get("file:" + doi, ""))
                               for doi in request.form.getlist("confirm")]
                    job = project_batch.prepare_extract(project_root, pdf_data_root, choices)
                else:
                    raise ValueError("Unknown batch action")
                batch_state["pause"].clear()

                def work():
                    project_batch.run(project_root, pdf_data_root, job, batch_state["pause"],
                        on_error=lambda error: diagnostic("web_batch_pdf_failed", exception_type=type(error).__name__))
                    store.clear_cache()

                thread = threading.Thread(target=work, daemon=True)
                batch_state["thread"] = thread
                thread.start()
            return redirect("/library/batch", code=303)
        except ValueError as error:
            diagnostic("web_batch_pdf_rejected", exception_type=type(error).__name__)
            response = page("Batch PDF request rejected",
                f'<p role="alert">{escape(str(error))}</p>'
                '<p><a href="/library/batch">Review queue</a> | <a href="/library">Library</a></p>')
            response.status_code = 400
            return response

    def batch_page():
        from .build import project_batch
        if not project_root:
            raise ValueError("Project settings are not enabled")
        if set(request.args) - {"offset"}:
            raise ValueError("Unknown batch parameter")
        offset = int(request.args.get("offset", "0"))
        if not 0 <= offset <= 1000000:
            raise ValueError("Invalid batch page")
        job = project_batch.load(project_root)
        running = batch_running()
        counts = Counter(item["status"] for item in job["items"].values())
        state = "interrupted" if job["status"] == "running" and not running else job["status"]
        hidden = f'<input type="hidden" name="token" value="{provide_token}">'
        body = (f'<p role="status">Batch status: {escape(state)}. '
                f'{job.get("processed", 0)} of {len(job["tasks"])} tasks processed in this pass.</p>'
                + help_note("Batch extraction", '<p>Downloads are not identity verification. Inspect each PDF locally and confirm its '
                'title/DOI and private-use permission before extraction. Discovered papers stay outside core.</p>'
                f'<p>Local PDF data folder: <code>{escape(str(pdf_data_root))}</code>. Original files are not served.</p>')
                + metadata_value(dict(counts)))
        if job.get("message"):
            body += f'<p role="alert">{escape(job["message"])}</p>'
        if running:
            body += ('<progress aria-label="Batch PDF progress"></progress>'
                     '<form method="post" action="/library/batch">' + hidden
                     + '<button name="action" value="pause">Pause after current paper</button></form>')
        else:
            body += (help_note("Resuming a batch", '<p>Selection and per-paper results are saved across pages and app restarts. '
                     'Resume/retry reuses completed downloads; provider errors pause the queue. '
                     'Extraction retries require fresh confirmation below.</p>')
                     + '<form method="post" action="/library/batch">' + hidden
                     + '<button name="action" value="locate">Locate selected / resume / retry missing PDFs</button></form>'
                     '<form id="batch-extract" method="post" action="/library/batch">' + hidden
                     + '<input type="hidden" name="action" value="extract">'
                     '<button>Attach and extract confirmed PDFs on this page</button></form>')
        items = list(job["items"].values())
        pdfs = available_project_pdfs()
        for index, item in enumerate(items[offset:offset + 25], offset):
            body += (f'<article class="library-record"><h3>{escape(item["title"])}</h3>'
                     f'<p><code>{escape(item["doi"])}</code> — {escape(item["status"])}. '
                     f'Locate PDF: {"on" if item["selected"] else "off"}.</p>')
            if item.get("error"):
                body += f'<p role="alert">{escape(item["error"])}</p>'
            result = item.get("result", {})
            if result.get("final_url"):
                body += '<p>Acquired from: ' + metadata_value(result["final_url"]) + '</p>'
                body += metadata_value({key: result.get(key) for key in ("filename", "year", "license", "version")})
            for attempt in result.get("attempts", []):
                if attempt.get("error"):
                    body += '<p>' + metadata_value(attempt["url"]) + ': ' + escape(attempt["error"]) + '</p>'
            if not running and item["status"] != "extracted":
                body += (
                    f'<label for="review-file-{index}">PDF from data folder</label>'
                    f'<select id="review-file-{index}" name="file:{escape(item["doi"], quote=True)}" form="batch-extract">'
                    '<option value="">Select a PDF</option>'
                    + "".join(f'<option value="{escape(name, quote=True)}"'
                              + (' selected' if name == item.get("filename") else '')
                              + f'>{escape(name)}</option>' for name in pdfs)
                    + '</select>'
                    f'<label for="review-confirm-{index}"><input type="checkbox" id="review-confirm-{index}" '
                    f'name="confirm" value="{escape(item["doi"], quote=True)}" form="batch-extract">'
                    ' I inspected this PDF, confirmed its title and DOI, and am authorized to extract it privately.</label>')
            body += '</article>'
        for start, label in ((max(0, offset - 25), "Previous"), (offset + 25, "Next")):
            if label == "Previous" and offset or label == "Next" and start < len(items):
                body += f'<a href="/library/batch?offset={start}">{label}</a> '
        body += '<p><a href="/library">Return to Library and change selection</a></p>'
        response = page("Batch PDFs: review and extract", body)
        response.headers["Cache-Control"] = "no-store"
        if running:
            response.headers["Refresh"] = "2"
        return response

    def project_library_page():
        from .build import project_batch, project_index
        if not (project_root / "project.json").is_file():
            return redirect("/settings")
        if set(request.args) - {"query", "offset", "relationship", "status"}:
            raise ValueError("Unsupported project library filter")
        offset = int(request.args.get("offset", "0"))
        if not 0 <= offset <= 1000000:
            raise ValueError("Invalid library offset")
        query = request.args.get("query", "")
        relationship, status = request.args.get("relationship", ""), request.args.get("status", "")
        result = project_index.library_page(project_root, query, offset, relationship=relationship, status=status)
        job = project_batch.load(project_root)
        selected_count = sum(item["selected"] for item in job["items"].values())
        batch_hidden = (f'<input type="hidden" name="token" value="{provide_token}">'
                        + "".join(f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'
                                  for name, value in (("query", query), ("offset", offset),
                                                      ("relationship", relationship), ("status", status))))
        def filter_select(name, label, options, selected):
            return (f'<label>{label}<select name="{name}">' + "".join(
                f'<option value="{value}"' + (' selected' if value == selected else '')
                + f'>{text}</option>' for value, text in options) + '</select></label>')
        searches = result["searches"]
        core_count = result["core_count"]
        papers = result["core"]
        body = (
            f"<p>{core_count} core papers; {result['total'] - core_count} additional discovered records.</p>"
            + help_note("Library records and PDF status", "<p>Records with a DOI are combined across searches; records without a DOI retain provider identities.</p>"
            "<p>References without a supplied title are listed last. Their provider metadata may contain "
            "only a DOI or partial citation; this does not mean the PDF is unavailable.</p>"
            "<p>Discovered papers are metadata only until a PDF is supplied and extracted. "
            "Awaiting PDF means no PDF is attached, not that a download has been attempted. "
            "Discovery alone does not add papers to your Core papers or make them available to AI. "
            "Find PDF and attach/extract below make text available to AI without changing core discovery seeds. "
            "A resolved DOI and title are required; inspect each PDF before confirming.</p>")
            + '<p><a href="/discovery">Find related papers</a> · '
            '<a href="/settings">Manage core papers</a></p>'
            '<form method="get" class="filters"><label>Search title or DOI '
            f'<input name="query" value="{escape(query, quote=True)}"></label>'
            + filter_select("relationship", "Relationship", [
                ("", "All papers"), ("core", "Core papers"), ("background", "References"),
                ("impact", "Citing papers")], relationship)
            + filter_select("status", "PDF / extraction status", [
                ("", "All statuses"), ("awaiting", "Awaiting PDF"), ("extracted", "Extracted"),
                ("review", "Needs visual review"), ("unresolved", "DOI unresolved")], status)
            + '<button>Search</button></form>'
            f'<section class="panel"><h3>Batch PDFs</h3><p><span id="batch-selected-count">{selected_count}</span> papers switched on for Locate PDF. '
            '</p>'
            + help_note("Batch PDF selection", '<p>Only papers with a DOI/title awaiting PDF can be selected. '
                        'Download first; review and confirm before extraction.</p>')
            + '<form method="post" class="batch-actions" action="/library/batch">' + batch_hidden
            + '<button name="action" value="page">Turn on this page</button>'
            f'<button name="action" value="all">Turn on all matching papers ({result["matching"]} records; eligible only)</button>'
            '<button name="action" value="clear" class="secondary">Turn all off</button>'
            '<button name="action" value="locate">Locate selected PDFs</button></form>'
            '<p><a href="/library/batch">Review batch results and confirm extraction</a></p></section>'
            f"<p>{result['matching']} matching records.</p>"
            '<ul class="library-records" aria-label="Library papers">')

        def paper_link(title, doi):
            if not doi:
                return escape(title)
            url = "https://doi.org/" + quote(doi, safe="/")
            return f'<a href="{escape(url, quote=True)}">{escape(title)}</a>'

        search_jobs = dict(searches)
        pdfs = available_project_pdfs()
        for index, row in enumerate(result["rows"]):
            directions = {direction for direction, _ in row["relations"]}
            category = "Core papers" if row["core"] else "; ".join(
                label for direction, label in (("background", "References"), ("impact", "Citing papers"))
                if direction in directions)
            connections = ""
            for direction, doi in sorted(row["relations"]):
                related = papers.get(doi.removeprefix("doi:"))
                title = related["title"] if related and related["title"] else doi
                linked = paper_link(title, doi)
                connections += (
                    f"<li>Core paper {linked} cites this paper.</li>" if direction == "background"
                    else f"<li>This paper cites core paper {linked}.</li>")
            discovery_links = "".join(
                f'<li><a href="/discovery?run={run}">{escape(discovery_label(search_jobs[run]))}'
                f' — {escape(search_jobs[run]["started_at"])}</a></li>'
                for run in sorted(row["runs"]))
            body += (
                f'<li class="library-record" id="locate-{releases.sha256((row["doi"] or row["id"]).encode())[:16]}">'
                f'<h3>{paper_link(row["title"] or "[Unresolved title]", row["doi"])}</h3>'
                f'<dl><div><dt>DOI</dt><dd><code>{paper_link(row["doi"] or "Unresolved", row["doi"])}</code></dd></div>'
                f'<div><dt>Library</dt><dd>{category}</dd></div>'
                f'<div><dt>Status</dt><dd>{badge(row["status"])}</dd></div></dl>'
                + ('<form method="post" action="/library/batch">' + batch_hidden
                   + f'<input type="hidden" name="doi" value="{escape(row["doi"], quote=True)}">'
                   f'<button name="action" value="toggle" class="secondary locate-toggle" '
                   f'aria-label="Locate PDF for {escape(row["title"], quote=True)}" aria-pressed="'
                   + ("true" if job["items"].get(row["doi"], {}).get("selected") else "false")
                   + '">Locate PDF: '
                   + ("on" if job["items"].get(row["doi"], {}).get("selected") else "off")
                   + '</button><span data-toggle-status role="alert"></span></form>'
                   if row["doi"] and row["title"] and row["status"] == "Awaiting PDF" else "")
                + (pdf_controls(row["doi"], row["title"], prefix=f"library-{index}", pdfs=pdfs,
                                return_to="/library")
                   if row["doi"] and row["title"] and row["status"] == "Awaiting PDF" else "")
                + (f'<p><a href="/library/edit?{escape(urlencode({"doi": row["doi"]}), quote=True)}">'
                   'View and correct extracted data</a></p>'
                   if row["doi"] and row["status"] not in {"Awaiting PDF", "Metadata only — DOI unresolved"} else "")
                + (f"<details><summary>Connections to your core papers</summary><ul>{connections}</ul>"
                   f"<p>Found in these discovery searches (including provider coverage and gaps):</p>"
                   f"<ul>{discovery_links}</ul></details>" if connections else "") + "</li>")
        body += "</ul>"
        for start, label in ((max(0, offset - 50), "Previous"), (offset + 50, "Next")):
            if (label == "Previous" and offset) or (label == "Next" and start < result["matching"]):
                body += f'<a href="/library?{escape(urlencode({"query": query, "offset": start, "relationship": relationship, "status": status}), quote=True)}">{label}</a> '
        if searches:
            body += "<h3>Discovery coverage</h3><ul>"
            for run, job in searches:
                body += (f'<li><a href="/discovery?run={run}">{escape(discovery_label(job))}</a>: '
                         f'{escape(job["status"])}. Inspect the search for provider gaps and unresolved references.</li>')
            body += "</ul>"
        return page("Library", body, library_script=True)

    def library():
        if set(request.args) - {"query", "download", "extraction", "offset", "sort", "relationship", "status"}:
            raise ValueError("Unknown library parameter")
        if project_root:
            return project_library_page()
        project_path = Path(registry).absolute().parent / "project.json" if registry else None
        if project_path and project_path.is_file() and set(store.current().snapshots) == {"core"}:
            if request.args:
                raise ValueError("Core project library filters are not yet supported")
            project = releases.read_json(project_path.read_bytes())
            bundle = store.current().snapshots["core"]._bundle
            sources = {collections.publication(source["doi"]): source["id"] for source in bundle["sources"]}
            rows = []
            for doi, item in sorted(project["papers"].items()):
                source_id = sources.get(collections.publication(doi))
                caveats = [caveat for unit in bundle["units"] if unit["citation"]["source_id"] == source_id
                           for caveat in unit["caveats"]]
                state = ("Awaiting PDF" if source_id is None else
                         "Needs visual review" if any("needs_visual_review" in caveat for caveat in caveats) else
                         "Extracted with OCR (not human verified)" if any(
                             caveat.startswith("Separate local OCR") for caveat in caveats) else "Extracted")
                rows.append(f"<tr><td>{escape(item['title'])}</td><td><code>{escape(doi)}</code></td>"
                            f"<td>{badge(state)}</td></tr>")
            return page("Core paper library",
                        "<p>Paper identities are supplied by the operator and are not independently verified. "
                        "Manage papers with the project command-line tool.</p>"
                        '<div class="table-scroll"><table><thead><tr><th>Title</th><th>DOI</th><th>Status</th></tr></thead><tbody>'
                        + "".join(rows) + "</tbody></table></div>")
        offset = int(request.args.get("offset", "0"))
        if not 0 <= offset <= 1000000:
            raise ValueError("Invalid library offset")
        if data_root is None:
            return page("Source library", "<p>No local source inventory configured for this release.</p>")
        rows = library_rows(data_root)
        providing_enabled = can_provide()
        download_labels = {"awaiting": "Awaiting download (excludes original collection)",
                           "downloaded": "Downloaded", "no_explicitly_licensed_pdf": "No licensed PDF",
                           "licensed_locations_failed": "Download failed", "existing_seed_collection": "Original iStopMM collection",
                           "provided": "Provided PDF/link"}
        extraction_labels = {"extracted": "Extracted", "partial": "Partially extracted",
                             "extracted_with_ocr": "Extracted with OCR (not human verified)",
                             "needs_visual_review": "Needs visual review",
                             "not_extracted": "Not extracted", "failed": "Extraction failed",
                             "ocr_required": "OCR required", "no_native_text": "No native text",
                             "unsupported_format": "Unsupported format"}
        query = request.args.get("query", "")
        download = request.args.get("download", "")
        extracted = request.args.get("extraction", "")
        sort = request.args.get("sort", "citations" if download == "awaiting" else "title")
        sort_labels = {"citations": "Most cited", "annual": "Citations per year",
                       "connections": "Most iStopMM connections", "title": "Title"}
        if sort not in sort_labels:
            raise ValueError("Invalid library sort")
        if download and download not in download_labels or extracted and extracted not in extraction_labels:
            raise ValueError("Invalid library filter")
        def select(name, label, labels, selected, *, include_all=True):
            return (f'<label>{label} <select name="{name}">' +
                    ('<option value="">All</option>' if include_all else "") +
                    "".join(f'<option value="{escape(key, quote=True)}"'
                            f'{" selected" if key == selected else ""}>{escape(value)}</option>'
                            for key, value in labels.items()) + "</select></label> ")
        counts = Counter(row["extraction"] for row in rows)
        downloaded = sum(row["acquisition_status"] in {"downloaded", "provided"} for row in rows)
        versions = {digest: version for row in rows for digest, version in row["versions"].items()}
        body = (f'<div class="summary"><p class="lead">{len(rows)} listed sources; {downloaded} downloaded; '
                f"{counts['extracted']} extracted; {counts['partial']} partially extracted.</p>"
                + (f"<p>{counts['extracted_with_ocr']} extracted with OCR; "
                   f"{counts['needs_visual_review']} need visual review. OCR text may contain errors; "
                   "original native text is retained unchanged.</p>"
                   if counts["extracted_with_ocr"] or counts["needs_visual_review"] else "") +
                f"<p>{len(versions)} extracted PDF versions; "
                f"{sum(version['pages'] for version in versions.values())} pages across retained versions.</p></div>"
                '<details class="notes"><summary>About these numbers</summary>'
                "<p>The downloaded count covers additional-library downloads. Extraction and PDF-version "
                "totals also include listed papers already in the original iStopMM collection; their "
                "main-paper pages are counted, not supplements.</p>"
                "<p>Provider-indexed direct references and citers, not guaranteed complete worldwide. "
                "Extraction does not mean active MCP retrieval or scientific review. "
                "This page reads the local inventory and extraction records on each request; original files are not served.</p>"
                "<p>Impact uses saved OpenAlex citation counts, not live counts. Unknown counts sort last, "
                "not as zero. Citations/year divides by elapsed years at capture, with a minimum age of one year. "
                "iStopMM connections counts distinct seed papers in either citation direction. "
                "These are prioritization aids, not measures of scientific quality or field-normalized impact.</p></details>"
                + ('<p class="callout">Have a copy of a paper that could not be downloaded? Use <strong>Provide PDF or link</strong> '
                   "on its row. The PDF is checked, extracted (with OCR for pages without text) and added to the "
                   "library as a new snapshot; the previous snapshot is kept for rollback. Provided copies are "
                   "private, never served, and not confirmed as the publisher version.</p>" if providing_enabled else "") +
                '<p><a class="button secondary" href="/library?download=awaiting&amp;sort=citations">Prioritize papers awaiting download</a></p>'
                '<form method="get" class="filters"><label>Search title, author or DOI '
                f'<input name="query" value="{escape(query, quote=True)}"></label> ' +
                select("download", "Download", download_labels, download) +
                select("extraction", "Extraction", extraction_labels, extracted) +
                select("sort", "Sort", sort_labels, sort, include_all=False) +
                '<button type="submit">Filter</button></form>')
        matches = [row for row in rows if all(word in " ".join(
            row.get(key, "") for key in ("id", "title", "authors", "doi")).casefold() for word in query.casefold().split())
            and (not download or (row["acquisition_status"] not in {"downloaded", "existing_seed_collection", "provided"}
                                 if download == "awaiting" else row["acquisition_status"] == download))
            and (not extracted or row["extraction"] == extracted)]
        if sort != "title":
            metric = {"citations": "citations", "annual": "citations_per_year",
                      "connections": "seed_connections"}[sort]
            matches.sort(key=lambda row: (not row["title"].strip(), row[metric] is None, -(row[metric] or 0),
                                         -(row["citations"] or 0), row["title"].casefold(), row["id"]))
        body += f'<p class="result-count"><strong>{len(matches)} matching sources.</strong> '
        body += (f"Citation counts available for {sum(row['citations'] is not None for row in matches)} "
                 "matching sources; others are unknown.</p>")
        entries = []
        for row in matches[offset:offset + 50]:
            title = escape(row["title"].strip() or "[Unresolved reference]")
            url = row["citation_url"]
            if urlsplit(url).scheme in {"https", "http"}:
                title = f'<a href="{escape(url, quote=True)}">{title}</a>'
            direction = ("References" if row["background_of"] else "")
            if row["impact_on"]:
                direction += ("; " if direction else "") + "Citing papers"
            details = "".join(f"<small>{escape(text)}</small>" for text in
                              (row["id"], row.get("authors", ""), row.get("publication_date", ""),
                               row.get("journal", "")) if text)
            impact = ("Unknown citations" if row["citations"] is None else
                      f'{row["citations"]:,} citations')
            if row["citation_source"]:
                source_url = row["citation_source"]
                if urlsplit(source_url).scheme in {"https", "http"}:
                    impact = f'<a href="{escape(source_url, quote=True)}">{impact}</a>'
                impact += f'<small>OpenAlex; captured {escape(row["citation_date"])}</small>'
            if row["citations_per_year"] is not None:
                impact += f'<small>{row["citations_per_year"]:.1f} citations/year</small>'
            impact += f'<small>{row["seed_connections"]} iStopMM connections</small>'
            entries.append(f"<tr><td>{title}{details}</td><td>{escape(direction or 'New update source')}</td>"
                           f"<td>{impact}</td>"
                           f"<td>{badge(escape(download_labels.get(row['acquisition_status'], row['acquisition_status'])))}"
                           f"{provide_cell(row, providing_enabled)}</td>"
                           f"<td>{badge(escape(extraction_labels.get(row['extraction'], row['extraction'])))}"
                           f"<small>{row['pages']} pages; {len(row['versions'])} retained PDF versions</small></td></tr>")
            if row.get("ocr_review_pages"):
                entries[-1] = entries[-1].replace("</td></tr>",
                    "<small>Low OCR confidence on PDF pages: " +
                    escape(", ".join(str(number) for number in row["ocr_review_pages"])) + "</small></td></tr>")
        if entries:
            body += ('<div class="table-scroll"><table><thead><tr><th>Paper</th><th>Relation</th>'
                     '<th>Impact</th><th>Download</th><th>Extraction</th></tr></thead><tbody>' +
                     "".join(entries) + "</tbody></table></div>")
        else:
            body += "<p>No matching sources.</p>"
        links = []
        for position, label in ((max(0, offset - 50), "Previous"), (offset + 50, "Next")):
            if label == "Previous" and offset == 0 or label == "Next" and position >= len(matches):
                continue
            params = {**dict(request.args), "offset": position}
            links.append(f'<a href="/library?{escape(urlencode(params), quote=True)}">{label}</a>')
        return page("Library",
                    "<p>The working inventory of discovered related sources: what has been downloaded "
                    "and extracted, and what is still pending. Extraction alone does not publish a source "
                    'to MCP; see <a href="/sources">Sources available to AI</a> for the serving inventory.</p>'
                    + body + '<nav class="pager">' + "".join(links) + "</nav>")

    def can_provide():
        if (not (store and data_root)
                or project_root and not (project_root / "registry/active.json").is_file()
                or "literature" not in store.current().snapshots):
            return False
        try:
            from .build import provided_sources  # noqa: F401  (requires the optional build extra)
        except ImportError:
            return False
        return True

    def provide_cell(row, enabled):
        item = row.get("provided")
        html = ""
        if item and item["status"] != "added":
            html += f"<small>{escape(PROVIDED_LABELS.get(item['status'], item['status']))}</small>"
            if item["status"] == "failed" and item.get("error"):
                html += f"<small>{escape(item['error'])}</small>"
        if not enabled or row["acquisition_status"] not in PROVIDABLE:
            return html
        if item and item["status"] in {"received", "processing"}:
            return html
        if not row["id"].startswith("doi:"):
            return html + "<small>Providing a copy requires a DOI.</small>"
        identifier = escape(row["id"], quote=True)
        return html + (
            '<details class="provide"><summary>Provide PDF or link</summary>'
            '<form method="post" action="/library/provide" enctype="multipart/form-data">'
            f'<input type="hidden" name="publication_id" value="{identifier}">'
            f'<input type="hidden" name="token" value="{provide_token}">'
            '<label>PDF file <input type="file" name="pdf" accept="application/pdf,.pdf"></label>'
            '<label>or direct PDF link <input type="url" name="url" placeholder="https://"></label>'
            '<label>Licence (optional) <input type="text" name="license" maxlength="200" '
            'placeholder="looked up by DOI if left empty"></label>'
            '<button type="submit">Add to library</button>'
            "<small>Only for copies you are permitted to use privately. Links must return the PDF itself; "
            "for login-protected publishers, download in your browser and upload the file.</small>"
            "</form></details>")

    def provide():
        if not can_provide():
            return Response("Providing PDFs is not available for this website configuration.", status=404,
                            mimetype="text/plain")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Expired or invalid form; reload the library page.", status=403, mimetype="text/plain")
        from .build import provided_sources
        identifier = request.form.get("publication_id", "")
        upload = request.files.get("pdf")
        link = request.form.get("url", "").strip()
        license = request.form.get("license", "")
        back = '<p><a href="/library?' + escape(urlencode({"query": identifier}), quote=True) + '">Back to the paper</a></p>'
        try:
            if bool(upload and upload.filename) == bool(link):
                raise ValueError("Choose a PDF file or paste a link (one of them).")
            if link:
                data, final_url = provided_sources.fetch(link)
                item = provided_sources.receive(data_root, identifier, data, method="link", url=link,
                                                final_url=final_url, registry=registry, license=license)
            else:
                item = provided_sources.receive(data_root, identifier, upload.read(provided_sources.MAX_BYTES + 1),
                                                method="upload", filename=upload.filename, registry=registry,
                                                license=license)
        except ValueError as error:
            response = page("PDF not added", f"<p>{escape(str(error))}</p>{back}")
            response.status_code = 400
            return response
        start_provider()
        return page("PDF received",
                    f"<p>Received a {item['bytes']:,}-byte PDF for <strong>{escape(item['title'] or identifier)}</strong>. "
                    "It is being extracted and added to the library in the background; pages without text are "
                    "OCR'd, which can take a few minutes. Reload the paper's row to see its status.</p>" + back)

    def provider_loop():
        from .build import literature_text, provided_sources
        while True:
            waiting = 0
            try:
                result = provided_sources.apply(data_root, registry)
                waiting = result["waiting"]
                if result["snapshot"]:
                    literature_text.build(data_root, registry)
                diagnostic("provided_applied", added=len(result["added"]), failed=len(result["failed"]),
                           waiting=waiting)
            except Exception as error:  # Keep the website alive; record the failure on the queued rows.
                diagnostic("provided_failed", exception_type=type(error).__name__)
                provided_sources.fail_pending(data_root, "Adding failed (" + type(error).__name__
                                              + "); see the website log, then provide the file again.")
            if waiting:
                time.sleep(30)
                continue
            with provider_lock:
                if not provider_state["again"]:
                    provider_state["thread"] = None
                    return
                provider_state["again"] = False

    def start_provider():
        with provider_lock:
            if provider_state["thread"] is not None:
                provider_state["again"] = True
                return
            provider_state["thread"] = threading.Thread(target=provider_loop, name="provided-pdfs", daemon=True)
            provider_state["thread"].start()

    def catalog_page():
        allowed = {"query", "disease", "eligibility", "collection", "snapshot_id", "offset"}
        if set(request.args) - allowed:
            raise ValueError("Unknown query parameter")
        if project_root and metadata_running():
            return redirect("/settings/metadata-progress")
        if project_root and not (project_root / "registry/active.json").exists():
            return page("Publication metadata" if request.path == "/publications" else "Literature coverage",
                        '<section class="panel"><h3>Set up your metadata catalog</h3>'
                        '<ol><li><a href="/settings">Initialize the project</a> and attach at least one PDF '
                        'to create its registry.</li><li>Use <a href="/discovery">Find related papers</a> '
                        'and wait for searches to complete.</li><li>Return here and click '
                        'Configure/update metadata catalog to publish the saved bibliography.</li></ol>'
                        '<p>Discovery alone saves search results; it does not configure a metadata catalog. '
                        'Catalog setup does not download PDFs or add full-text evidence.</p></section>')
        current = snapshot(request)
        if project_root:
            from .build import project_library
            metadata_ids = project_library.metadata_collections(project_root)
            if metadata_ids:
                metadata_store = collections.CollectionStore(
                    registry, allowed_profiles=profiles,
                    allowed_collections=["core", *metadata_ids], defaults=["core"])
                current = (metadata_store.load(request.args["snapshot_id"])
                           if request.args.get("snapshot_id") else metadata_store.current())
        args = {}
        cid = request.args.get("collection")
        visible = current.call("list_collections", {})["result"]["items"]
        catalog_collections = [item for item in visible if item["kind"] == "catalog"]
        if store:
            args["snapshot_id"] = current.release_id
            if cid:
                args["collections"] = [cid]
            else:
                args["collections"] = [item["id"] for item in catalog_collections]
        elif cid:
            raise ValueError("Collection selection requires a registry")
        tool = "catalog_status" if request.path == "/coverage" else "list_publications"
        if tool == "list_publications":
            args.update(offset=int(request.args.get("offset", "0")), limit=5)
            args.update({key: request.args[key] for key in ("query", "disease", "eligibility")
                         if request.args.get(key)})
        evidence_selection = (store and cid in current.snapshots
                              and not collections.is_catalog(current.snapshots[cid]))
        evidence_metadata = evidence_selection and tool == "list_publications"
        evidence_coverage = evidence_selection and tool == "catalog_status"
        matched_catalogs = []
        if evidence_selection:
            selected_snapshot = current.snapshots[cid]
            identities = {current._aliases[cid, key] for key in collections.publication_keys(selected_snapshot)}
        if evidence_metadata:
            contracts.request("list_publications", args)
            records = {}
            for item in catalog_collections:
                catalog_id = item["id"]
                metadata = current.snapshots[catalog_id]
                for identifier in metadata.matching_ids(args):
                    if current._aliases[catalog_id, identifier] in identities:
                        records.setdefault(identifier, metadata.get(identifier))
            ordered = [records[key] for key in sorted(records)]
            selected_records, continuation = releases.Snapshot._page(ordered, args)
            result = {"items": selected_records, "next_offset": continuation}
        elif evidence_coverage:
            matched = set()
            for item in catalog_collections:
                catalog_id = item["id"]
                catalog_identities = {
                    current._aliases[catalog_id, key]
                    for key in collections.publication_keys(current.snapshots[catalog_id])}
                overlap = identities & catalog_identities
                if overlap:
                    matched.update(overlap)
                    matched_catalogs.append(catalog_id)
            result = (current.call(tool, {**args, "collections": matched_catalogs})["result"]
                      if matched_catalogs else {"items": []})
        elif store and not cid and not catalog_collections:
            result = {"items": [], "next_offset": None}  # No catalog configured: show the empty state.
        else:
            result = current.call(tool, args)["result"]
        options = '<option value="">All metadata catalogs</option>' + "".join(
            f'<option value="{escape(item["id"], quote=True)}"'
            f'{" selected" if item["id"] == cid else ""}>{escape(item["title"])}</option>'
            for item in visible)
        show_collection_filter = store and len(visible) > 1 and not project_root
        form = ('<form method="get" class="filters">' +
                (f'<label>Collection <select name="collection">{options}</select></label>'
                 if show_collection_filter else "") +
                (f'<label>Words <input name="query" value="{escape(request.args.get("query", ""), quote=True)}"></label>'
                 if tool == "list_publications" else "") + '<button>Filter</button></form>'
                ) if show_collection_filter or tool == "list_publications" else ""
        description = (
            "Audit view of metadata discovery: providers, search dates and queries, counts, unresolved "
            "identities and incomplete searches. This is not download/extraction progress or scientific review."
            if tool == "catalog_status" else
            "Bibliographic records for discovered publications, including papers not downloaded. "
            "Use this page for authors, identifiers and metadata provenance, not paper-text search.")
        body = help_note("About this page", "<p>" + description + "</p><p>Provisional metadata only. "
                         "Not researcher-approved, not full-text evidence. "
                         "A reported abstract or full-text location is not acquisition or permission.</p>") + form
        if project_root and tool == "list_publications":
            body += (
                '<section class="panel"><h3>Configure publication metadata</h3>'
                + help_note("Updating the catalog", '<ol><li>Use <a href="/discovery">Find related papers</a> to search references, citing papers, or both. '
                'Wait for each search to complete.</li>'
                '<li>Click Configure/update metadata catalog below to publish metadata from all completed searches.</li>'
                '<li>Run this again after new searches to update Publication metadata and Literature coverage.</li></ol>'
                '<p>This uses saved local metadata; it makes no network requests and downloads no PDFs. '
                'Incomplete provider coverage and unresolved references remain labelled. '
                'Attach at least one core or discovered-paper PDF first to create the project registry. '
                'The catalog is preserved when you attach more PDFs; factory reset deletes it.</p>')
                + '<form method="post" action="/settings/configure-metadata">'
                f'<input type="hidden" name="token" value="{provide_token}">'
                '<button>Configure/update metadata catalog</button></form>'
                + help_note("Metadata over MCP", '<p>To expose this metadata catalog to AI over MCP too, restart your MCP server with '
                '<code>COLLECTIONS="core metadata"</code> and keep <code>DEFAULT_COLLECTIONS=core</code>. '
                'The catalog contains bibliography, not full-text evidence.</p>') + '</section>')
        elif project_root:
            body += ('<p>To configure or update this catalog, '
                     '<a href="/publications">open Publication metadata</a>.</p>')
        if store and len(visible) == 1:
            body += f"<p>Project: <strong>{escape(visible[0]['title'])}</strong>.</p>"
        if store and not catalog_collections:
            body += ("<p>No metadata catalog is configured. "
                     '<a href="/library">Library</a> shows your core papers and saved discoveries; '
                     '<a href="/discovery">Find related papers</a> shows saved searches and their provider coverage.</p>')
        if evidence_metadata:
            body += (f"<p>{len(ordered)} matching bibliographic records from authorized metadata catalogs "
                     "for papers in the selected evidence collection. Matches use publication identifiers, "
                     "not title guesses; sources without matching catalog metadata are not listed.</p>")
        if tool == "catalog_status":
            import json
            if project_root:
                from .build import project_library
                core_papers = project_library.status(project_root)["papers"]
                available = sum(paper["status"] != "awaiting_pdf" for paper in core_papers)
                body += (
                    '<section class="panel"><h3>Core full-text evidence</h3>'
                    f'<p>{available} of {len(core_papers)} core papers have attached PDFs and retained extracted text. '
                    'Extraction may include OCR or require visual review; '
                    '<a href="/sources">Sources available to AI</a> lists the exact evidence versions.</p>'
                    + help_note("Evidence and metadata counts", '<p>The discovery catalog below stores bibliography only. Its acquisition counts do not '
                    'include separately attached core PDFs or discovered-paper PDFs and are not project-wide full-text totals. '
                    '<a href="/library">Library</a> shows individual PDF/extraction statuses.</p>')
                    + '</section>')
            if evidence_coverage:
                info = current.info(cid)
                body += (
                    f"<article><h3>{escape(info['title'])}: evidence coverage</h3>"
                    f"<p>{info['paper_count']} papers; {info['content_unit_count']} searchable content units; "
                    f"{info['claim_count']} structured claims.</p>"
                    f"<p>{escape(info['coverage'])}</p>"
                    f"<p>{len(matched)} of {len(identities)} evidence publications have identifier-matched "
                    f"metadata in authorized discovery catalogs; {len(identities - matched)} have no matching "
                    "catalog record. This checks metadata linkage, not completeness of published research.</p>"
                    "</article><h3>Related discovery catalogs</h3>"
                    "<p>The statistics below cover each entire related catalog, not just the selected "
                    "evidence collection. They do not measure missing full text or scientific validity.</p>")
            body += (
                help_note("Full-text availability counts", "<p><strong>Full-text availability unknown from saved metadata</strong> counts records "
                "where providers supplied no full-text availability indication. This is not a count of "
                "unavailable PDFs or failed downloads; availability has not been independently checked, "
                "and separately attached project PDFs are not reflected in this metadata flag.</p>"))
            for item in result["items"]:
                body += literature_coverage(item, project_scope=bool(project_root))
        else:
            for record in result["items"]:
                body += (f"<article><h3>{escape(record['title'] or '[Title unavailable in provider metadata]')}</h3><dl>" + "".join(
                    f"<dt>{escape(('core_library_overlap' if project_root and key == 'istopmm_overlap' else key).replace('_', ' ').capitalize())}</dt>"
                    f"<dd>{metadata_value(record[key], project_scope=bool(project_root))}</dd>"
                    for key in ("authors", "journal", "publication_date", "citation_url"))
                    + '</dl><details><summary>More metadata and provenance</summary><dl>' + "".join(
                    f"<dt>{escape(('core_library_overlap' if project_root and key == 'istopmm_overlap' else key).replace('_', ' ').capitalize())}</dt>"
                    f"<dd>{metadata_value(record[key], project_scope=bool(project_root))}</dd>"
                    for key in ("id", "aliases",
                                "diseases", "publication_types", "eligibility", "publication_state",
                                "availability", "researcher_review", "istopmm_overlap", "selection_rationale",
                                "relations", "provenance")
                    if not (project_root and key == "diseases")) + "</dl></details></article>")
            links = []
            for offset, label in ((max(0, args["offset"] - 5), "Previous"), (result["next_offset"], "Next")):
                if offset is None or label == "Previous" and args["offset"] == 0:
                    continue
                params = dict(request.args)
                params["offset"] = offset
                if store:
                    params["snapshot_id"] = current.release_id
                links.append(f'<a href="/publications?{escape(urlencode(params), quote=True)}">{label}</a>')
            body += '<nav class="pager">' + "".join(links) + "</nav>"
        if evidence_coverage and not result["items"]:
            body += "<p>No authorized discovery catalog has identifier-matched records for this collection.</p>"
        elif not result["items"]:
            body += "<p class=\"empty-state\">No metadata catalog records in this selection. "
            if catalog_collections:
                body += "Choose All metadata catalogs or a metadata catalog in the Collection filter. "
            else:
                body += "No metadata catalog is configured among the authorized collections. "
            body += 'This does not mean the <a href="/library">source library</a> is empty.</p>'
        return page("Literature coverage" if tool == "catalog_status" else "Publication metadata", body)

    def configure_project_metadata():
        if not project_root:
            raise ValueError("Project metadata setup is not enabled")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Metadata form expired; reload the page.", status=403, mimetype="text/plain")
        with discovery_lock:
            if pdf_running():
                return Response("Wait for PDF processing to finish before configuring metadata.",
                                status=409, mimetype="text/plain")
            if metadata_running():
                return redirect("/settings/metadata-progress", code=303)
            if discovery_state["thread"] and discovery_state["thread"].is_alive():
                return Response("Wait for citation discovery to finish before configuring metadata.",
                                status=409, mimetype="text/plain")
            from .build import project_library
            job = {"status": "running", "message": "Preparing metadata catalog update"}
            collections.atomic_json(project_root, "metadata-job.json", job)

            def progress(message):
                job["message"] = message
                collections.atomic_json(project_root, "metadata-job.json", job)

            def work():
                try:
                    report = project_library.configure_metadata(project_root, progress=progress)
                    job.update(status="complete", message="Metadata catalog updated", report=report)
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                        releases.ReleaseError, ValidationError, literature.DiscoveryError) as error:
                    diagnostic("web_metadata_failed", exception_type=type(error).__name__)
                    job.update(status="failed", message=str(error))
                collections.atomic_json(project_root, "metadata-job.json", job)

            thread = threading.Thread(target=work, daemon=True)
            metadata_state["thread"] = thread
            thread.start()
        return redirect("/settings/metadata-progress", code=303)

    def metadata_progress():
        if not project_root:
            raise ValueError("Project metadata setup is not enabled")
        path = project_root / "metadata-job.json"
        if not path.is_file():
            return redirect("/publications")
        job = releases.read_json(releases.safe_file(project_root, path.name).read_bytes())
        running = job["status"] == "running" and metadata_running()
        message = job["message"]
        if job["status"] == "running" and not running:
            message = "Catalog update was interrupted. Return to Publication metadata and retry."
        body = f'<p role="status" aria-live="polite">{escape(message)}</p>'
        if running:
            body += ('<progress aria-label="Updating metadata catalog"></progress>'
                     '<p>This may take a while for large libraries. This page refreshes every two seconds; '
                     'you can leave it and return later.</p>'
                     '<p><a href="/settings/metadata-progress">Refresh status</a></p>')
        elif job["status"] == "complete":
            body += f'<p>Published metadata from {job["report"]["searches"]} saved searches.</p>'
        body += '<p><a href="/publications">Publication metadata</a></p>'
        response = page("Metadata catalog update", body)
        response.headers["Cache-Control"] = "no-store"
        if running:
            response.headers["Refresh"] = "2"
        return response

    def api(endpoint):
        return library_api.respond(snapshot(request), request)

    def connect_page():
        if request.args:
            raise ValueError("Connection page does not accept parameters or credentials")
        web_url = request.url_root.rstrip("/")
        sample_query = "YOUR_SEARCH_TERM"
        evidence_ids = []
        if store:
            if project_root and not (project_root / "registry/active.json").is_file():
                evidence_ids = list(store.defaults)
            else:
                current = store.current()
                evidence_ids = [cid for cid in store.defaults if not collections.is_catalog(current.snapshots[cid])]
                evidence_ids += [cid for cid in sorted(current.snapshots)
                                 if cid not in evidence_ids and not collections.is_catalog(current.snapshots[cid])]
        sample_collection = evidence_ids[0] if evidence_ids else None
        sdk_command = shlex.join([
            "research-mcp-check", "--url", mcp_url, "--web-url", web_url,
            "--token-file", "releases/http-local/research-papers-claude-token", "--query", sample_query,
            *(["--collection", sample_collection] if sample_collection else []),
        ])
        def command(action):
            return shlex.join(["make", action, *(["HTTP_URL=" + mcp_url]
                              if mcp_url != "http://127.0.0.1:8000/mcp" else [])])

        connect_command = command("connect")
        mcp_arguments = ["make", "mcp"]
        if project_root:
            mcp_arguments.append("PROJECT_ROOT=" + str(project_root))
        if store:
            mcp_arguments.extend(["COLLECTION_REGISTRY=" + str(store.root),
                                  "COLLECTIONS=" + " ".join(sorted(store.allowed)),
                                  "DEFAULT_COLLECTIONS=" + " ".join(store.defaults)])
        else:
            mcp_arguments.extend(["COLLECTION_REGISTRY=", "RELEASE=" + str(Path(release).absolute())])
        mcp_arguments.append("PROFILE=" + " ".join(sorted(profiles)))
        if mcp_url != "http://127.0.0.1:8000/mcp":
            mcp_arguments.append("HTTP_URL=" + mcp_url)
            endpoint = urlsplit(mcp_url)
            if endpoint.hostname in {"localhost", "127.0.0.1"}:
                mcp_arguments.append("HTTP_PORT=" + str(endpoint.port or (443 if endpoint.scheme == "https" else 80)))
        mcp_command = shlex.join(mcp_arguments)
        prompt = (
            "Use the project terms Core papers (operator-selected discovery seeds), References "
            "(papers cited by Core papers), Citing papers (papers that cite Core papers), and All papers "
            "(the combined library). Groups may overlap. The internal core evidence collection also "
            "contains extracted References and Citing papers; it does not define Core papers membership. "
            "Use the research evidence MCP server, not filesystem searches. Call list_collections. If it returns "
            "collections, choose an authorized evidence collection (not a metadata catalog) and pin "
            "collection_context.snapshot_id for every follow-up call. If it returns an empty list with the "
            "legacy single-release caveat, call corpus_status, omit collections and snapshot_id from tool calls, "
            "and preserve the returned release_id; do not invent a registry snapshot or stop retrieval. "
            "Search for a topic in the core papers, retrieve source context for a matching passage, and explain what that "
            "passage supports. Cite the publication DOI/PMID, source/unit IDs, checksum, release/snapshot and "
            "original page/Office locator. Preserve denominators, uncertainty, cohort overlap and extraction/OCR "
            "warnings. For bibliography lookups, use get_publication in a kind='catalog' collection from "
            "list_collections, keeping the same snapshot_id, even if the publication_id came from evidence. "
            "No matching passage is not proof of no evidence. Do not invent references or file links."
        )
        body = (
            help_note("Connecting your AI", "<p>Run these commands from the research-papers project folder. "
            "They manage the Claude Code HTTP connection named <code>research-evidence-http</code> "
            "in this project by default. The server serves the project and collections configured below.</p>")
            + f"<p>MCP endpoint: <code>{escape(mcp_url)}</code>.</p>"
            "<h3>1. Set up and start</h3>"
            "<pre>make setup\n" + escape(mcp_command) + "</pre>"
            + help_note("Starting MCP", "<p>Use this start command to serve the same evidence configuration as this website. "
            "Initialize the project and attach at least one PDF before starting MCP. "
            "<code>PROJECT_ROOT</code> selects research data; <code>CLIENT_PROJECT</code> selects "
            "the directory where Claude Code's connection is registered.</p>")
            + "<p>Leave MCP running. Use another terminal for the commands below.</p>"
            "<h3>2. Connect</h3>"
            + copy_field("claude-setup", "Claude setup command", connect_command, rows=2)
            + "<p>Creates the local registration and a private token (valid for 24 hours). "
            "Restart Claude Code in the target project and run <code>/mcp</code>; "
            "expect <code>research-evidence-http</code> to connect.</p>"
            "<details><summary>Reconnect or remove a connection</summary>"
            "<h3>Reset and reconnect</h3>"
            + copy_field("reset", "Reset command", command("reconnect"), rows=2)
            + "<p>Removes the old registration, revokes its token and creates a fresh connection. "
            "Restart Claude Code afterward.</p>"
            "<h3>Remove the connection / start empty</h3>"
            + copy_field("remove", "Remove command", command("disconnect"), rows=2)
            + "<p>Removes this named local registration, revokes its token and deletes the managed token file. "
            "You now have no managed connection. Run Connect again when ready.</p>"
            "<p>This does <strong>not</strong> delete papers, evidence, other client connections or the token registry. "
            "Revoked-token records remain so old credentials stay invalid. It does not stop MCP or the website.</p></details>"
            "<details><summary>Different project or connection</summary>"
            "<p>Append <code>CLIENT_PROJECT=/absolute/path/to/your/project</code> to any command above. "
            "Use the same project, endpoint and token settings for connect, reset and removal.</p>"
            "<pre>make connect CLIENT_PROJECT=/absolute/path/to/your/project\n"
            "make reconnect CLIENT_PROJECT=/absolute/path/to/your/project\n"
            "make disconnect CLIENT_PROJECT=/absolute/path/to/your/project</pre>"
            "<p>Use distinct <code>CLIENT_NAME</code> and <code>CLIENT_TOKEN_FILE</code> values for independent "
            "connections. These commands do not remove a separate stdio registration. "
            "Disable/remove that entry in your client's MCP configuration separately; "
            "do not delete an entire configuration file containing other servers.</p></details>"
            "<details><summary>Verify retrieval without an AI model</summary>"
            + copy_field("sdk-check", "SDK check command", sdk_command, rows=3)
            + "<p>Replace YOUR_SEARCH_TERM with a distinctive word found in one of your papers.</p>"
            + "<p>Expected: <code>passed: true</code> and <code>search_context_reader_agree: true</code>. "
            "Open the reported <code>reader_url</code> to inspect the cited passage. "
            "This page does not probe MCP or read credentials.</p></details>"
            "<details><summary>Integrate an application without MCP</summary>"
            "<p>The same evidence is available through the local read-only JSON API. "
            "Copy <code>examples/library_json.py</code> into your application's directory and run:</p>"
            "<pre>python3 library_json.py --base-url " + escape(web_url)
            + ((" --collection " + escape(shlex.quote(sample_collection))) if sample_collection else "")
            + " --query " + sample_query + "</pre>"
            "<p>Expected: exact retained text slices, citation, snapshot/release and a reader_url. "
            "Omit --collection on a single-release service. Only Python's standard library is needed; "
            "no MCP client, model or API credential. Your host application makes the requests and controls "
            "answer generation. See <code>docs/library-api.md</code> for pagination and error checks. "
            "This is local development access, not a public endpoint.</p></details>"
            "<h3>Try it: ask for cited evidence</h3>"
            "<p>After connecting, paste this question into Claude Code. Expect it to use MCP search and "
            "source-context tools, then answer with publication and original-location citations.</p>"
            + copy_field("prompt", "Example question", prompt, rows=6)
            + "<h3>More things to try</h3>"
            "<p>Use the same MCP server and citation requirements for these follow-up questions:</p>"
            + copy_field("example-compare", "Compare findings",
                         "Compare findings from two papers in this project's evidence library on a shared topic. "
                         "Explain how their methods, populations, measurements and limitations differ. Cite the supporting source passages; "
                         "do not treat shared cohorts as independent replication.", rows=4)
            + copy_field("example-inspect", "Inspect a source",
                         "For one source you just cited, retrieve its original source context and report the "
                         "publication, source/unit IDs, checksum, release/snapshot and page or Office locator. "
                         "Show its extraction/OCR warnings and distinguish retained text from your interpretation.", rows=4)
            + copy_field("example-coverage", "Check available evidence",
                         "List the evidence collections and describe their reported coverage and limitations. "
                         "Distinguish metadata-only publications from retained full text. If this is a legacy "
                         "single-release endpoint, use corpus_status instead. Do not equate no search match "
                         "with no scientific evidence.", rows=4)
            + "<h3>Check the answer against the source</h3>"
            "<ol><li>Look for actual MCP tool calls and cited source/unit IDs, not just a plausible answer.</li>"
            '<li>Use <a href="/search">Search evidence</a> to find the passage and open '
            "<strong>Read source context</strong>. Compare publication, original locator, source/unit IDs and "
            "release/snapshot with the answer. The SDK check above also returns an exact pinned reader_url.</li>"
            "<li>Check the retained text and warnings. If there is no supporting passage, the answer should "
            "say so rather than invent a reference. Original documents open at their publication source.</li></ol>"
            "<p>These are usage examples, not claims about what the evidence proves. "
            "AI interpretation still needs review; only use a provider approved to receive the evidence.</p>"
            "<details><summary>Troubleshooting and compatibility</summary><dl>"
            "<dt>Connection refused</dt><dd>Start <code>make mcp</code>; the website runs separately.</dd>"
            "<dt>401 / expired token</dt><dd>Run Connect again, or Reset for a fresh token, then restart the client.</dd>"
            "<dt>403 / 503</dt><dd>Ask the operator to check permissions, evidence readiness and token configuration.</dd>"
            "<dt>Empty list_collections with legacy caveat</dt><dd>You reached a single-release registration. "
            "Use release_id without collections/snapshot_id, or select the intended registry connection.</dd></dl>"
            "<p>Tested locally: Python MCP SDK " + escape(version("mcp")) + " retrieval; "
            "Claude Code 2.1.311 connection/reset/removal. Other applications and versions are untested. "
            "Pre-issued bearer headers only; no interactive OAuth. No model is hosted here.</p>"
            "<p>Public HTTPS deployment is deferred. This setup is local to this computer. "
            "Never paste tokens into questions or tracked configuration; only use approved clients/providers.</p>"
            "<p>Full operator instructions: <code>docs/connect-ai.md</code>.</p></details>"
            '<p id="copy-status" role="status" aria-live="polite"></p>')
        return page("Connect your AI", body, reader_script=True)

    def browser_snapshot(allowed):
        if (set(request.args) - allowed - {"snapshot_id", "release_id", "collection"}
                or len(list(request.args.items(multi=True))) != len(request.args)):
            raise ValueError("Unknown or repeated browser parameter")
        current = snapshot(request)
        if store:
            if "release_id" in request.args:
                raise ValueError("Use snapshot_id for a collection registry")
            return current, {"snapshot_id": current.release_id}
        pinned.verify_backing(release)
        selected = request.args.get("release_id")
        if selected is not None and selected != pinned.release_id:
            raise collections.SnapshotUnavailable("Requested single release is not available on this endpoint")
        if request.args.get("collection"):
            raise ValueError("Single release has no named collections")
        return current, {"release_id": current.release_id}

    def browser_title(current, cid, source):
        title = source.get("bibliography", {}).get("title")
        if not title and store:
            identifier = current._aliases[cid, source["doi"]]
            title = publication_titles(current, {identifier}).get(identifier)
        return title or source["doi"]

    def reader_url(pin, cid, unit, **extra):
        params = {**pin, **({"collection": cid} if store else {}),
                  "source_id": unit["citation"]["source_id"], "unit_id": unit["id"], **extra}
        return "/read?" + urlencode(params)

    def source_warnings(record):
        ocr = "ocr" in " ".join(record["caveats"]).lower()
        return (("<p><strong>OCR transcription — compare with original; recognition can contain errors.</strong></p>" if ocr else "")
                + "<details><summary>Extraction warnings and provenance</summary><ul>"
                + "".join(f"<li>{escape(value)}</li>" for value in record["caveats"])
                + "</ul><p>Human verification: " + escape(record["human_verification"])
                + ". Machine text is not scientific approval.</p></details>")

    def search_page():
        current, pin = browser_snapshot({"query", "offset", "relationship"})
        offset = int(request.args.get("offset", "0"))
        query = request.args.get("query", "")
        cid = request.args.get("collection", "all" if store else "")
        relationship = request.args.get("relationship", "")
        relationships = (("", "All papers"), ("core", "Core papers"),
                         ("background", "References"), ("impact", "Citing papers"))
        if relationship not in dict(relationships) or (relationship and not project_root):
            raise ValueError("Invalid project search scope")
        scope, choices = {}, ""
        if store:
            evidence_ids = [key for key, value in current.snapshots.items() if not collections.is_catalog(value)]
            if cid != "all" and cid not in evidence_ids:
                raise releases.AccessDeniedError("Choose an authorized evidence collection, not a metadata catalog")
            scope = {"snapshot_id": current.release_id, "collections": evidence_ids if cid == "all" else [cid]}
            if not scope["collections"]:
                raise ValueError("No evidence collections available")
            if project_root:
                if "core" not in evidence_ids:
                    raise releases.AccessDeniedError("Project evidence collection is not authorized")
                scope["collections"] = ["core"]
                choices = '<label>Search in <select name="relationship">' + "".join(
                    f'<option value="{key}"{" selected" if relationship == key else ""}>{label}</option>'
                    for key, label in relationships) + "</select></label>"
            else:
                choices = '<label>Collection <select name="collection"><option value="all">All evidence collections</option>'
                choices += "".join(f'<option value="{escape(key, quote=True)}"{" selected" if cid == key else ""}>'
                                   f'{escape(current.entries[key]["title"])}</option>' for key in evidence_ids) + "</select></label>"
        if offset < 0 or offset > retrieval.MAX_OFFSET or (offset and not query):
            raise ValueError("Invalid search offset")
        if offset and not request.args.get("snapshot_id" if store else "release_id"):
            raise ValueError("Pagination requires a pinned snapshot")
        body = ('<form method="get" class="hero-search" role="search"><label for="search-query">Words or phrase</label>'
                '<div class="input-row"><input id="search-query" name="query" type="search" required autofocus '
                f'value="{escape(query, quote=True)}"> {choices}<button>Search</button></div></form>'
                + help_note("Searching evidence", '<p>Keyword search of retained extracted text, not an AI answer or semantic search. '
                "Metadata-only publications are not searched as full text. No match does not prove there is no evidence.</p>"
                "<p>OCR and extraction warnings remain attached to results.</p>"
                + ("<p>Scopes use current core membership and saved discovery relationships. "
                   "References are papers cited by Core papers; Citing papers are papers that cite Core papers. "
                   "A paper can match both directions, including core papers linked to other core papers. "
                   "Only attached, extracted text is searched.</p>" if project_root else "")))
        if "query" not in request.args:
            return page("Search evidence", body + '<p class="empty-state">Enter words from a topic or passage to begin.</p>')
        arguments = {**scope, "query": query, "offset": offset, "limit": 5}
        if project_root and relationship:
            from .build import project_index
            papers = {collections.publication(row["doi"]) for row in
                      project_index.library_page(project_root, relationship=relationship, select_all=True)["rows"]
                      if row["doi"]}
            selected = contracts.request("search_evidence", arguments)
            result, references = current._search(scope["collections"], selected, papers=papers)
            response = {"result": result, "collection_context": {"evidence_refs": references}}
        else:
            response = current.call("search_evidence", arguments)
        refs = response.get("collection_context", {}).get("evidence_refs", [])
        for index, result in enumerate(response["result"]["items"]):
            matches = [ref for ref in refs if ref["result_index"] == index] if store else [None]
            representative = matches[0]
            result_cid = representative["collection_id"] if store else ""
            evidence = current.snapshots[result_cid] if store else current
            source = next(source for source in evidence._bundle["sources"]
                          if source["id"] == result["citation"]["source_id"])
            body += ('<article><h3 class="hit-title">' + escape(browser_title(current, result_cid, source)) + "</h3>"
                     f'<p class="meta"><span class="chip">{escape(quality_document(source["id"].rsplit(":", 1)[-1]))}</span> '
                     f"{escape(quality_location(result['citation']['locator']))} <code>{escape(source['doi'])}</code></p>"
                     f"<blockquote>{highlight(result['snippet'], query)}</blockquote>"
                     + source_warnings(result) + '<p class="actions">')
            for ref in matches:
                member_cid = ref["collection_id"] if store else ""
                unit = current.snapshots[member_cid]._lookup("units", ref["record_id"]) if store else result
                body += (f'<a class="button secondary" href="{escape(reader_url(pin, member_cid, unit), quote=True)}">Read source context'
                         + (f" ({escape(current.entries[member_cid]['title'])})" if store and not project_root else "") + "</a> ")
            body += "</p></article>"
        if not response["result"]["items"]:
            body += ('<p class="empty-state">No matching retained passages. Try fewer or different words, or another '
                     + ("search scope" if project_root else "collection") + '. This is not proof of no evidence.</p>')
        params = {**pin, "query": query, **({"relationship": relationship} if project_root else
                                        {"collection": cid} if store else {})}
        body += '<nav class="pager">'
        for position, label in ((max(0, offset - 5) if offset else None, "Previous results"),
                                (response["result"]["next_offset"], "Next results")):
            if position is not None:
                body += f'<a href="/search?{escape(urlencode({**params, "offset": position}), quote=True)}">{label}</a> '
        return page("Search evidence", body + "</nav>")

    def read_page():
        current, pin = browser_snapshot({"unit_id", "source_id", "offset"})
        cid = request.args.get("collection", "")
        if store:
            if cid not in current.snapshots:
                raise releases.AccessDeniedError("Select one authorized evidence collection")
            evidence = current.snapshots[cid]
            if collections.is_catalog(evidence):
                raise ValueError("Metadata catalogs do not contain source text")
        else:
            evidence = current
        unit_id = request.args.get("unit_id", "")
        contracts.request("get_source_context", {"unit_id": unit_id})
        offset = int(request.args.get("offset", "0"))
        context = evidence.source_context(unit_id, offset=offset)
        unit = context["record"]
        source_id = unit["citation"]["source_id"]
        if request.args.get("source_id", source_id) != source_id:
            raise retrieval.NotFoundError("Source and unit do not match")
        canonical = reader_url(pin, cid, unit, offset=offset)
        if not request.args.get("snapshot_id" if store else "release_id") or "source_id" not in request.args:
            return redirect(canonical)
        source = next(source for source in evidence._bundle["sources"] if source["id"] == source_id)
        title = browser_title(current, cid, source)
        location = quality_location(unit["citation"]["locator"])
        role = quality_document(source_id.rsplit(":", 1)[-1])
        absolute_url = request.url_root.rstrip("/") + canonical
        citation = (f"{title}. {role}; {location}. Publication: {source['doi']}. "
                    f"Source: {source_id}; unit: {unit_id}; SHA-256: {source['sha256']}. "
                    + (f"Collection: {cid}; registry snapshot: {current.release_id}; " if store else "")
                    + f"Evidence release: {evidence.release_id}. Retained text characters "
                    f"{offset + 1 if context['total_characters'] else 0}–{offset + len(unit['text'])} "
                    f"of {context['total_characters']}. {absolute_url}")
        excerpt = unit["text"] + "\n\n" + citation
        body = (f'<div class="reader-head"><h3>{escape(title)}</h3>'
                f'<p class="meta"><span class="chip">{escape(role)}</span> {escape(location)}</p>'
                '</div>'
                + help_note("Reading extracted text", '<p>This is retained extracted text, not an AI interpretation. Original documents are accessed externally. '
                "OCR labels, warnings and empty locations are preserved; retained text may not capture every feature of the original.</p>")
                + source_warnings(unit)
                + "<h3>Retained text</h3>"
                + (f'<pre class="reading">{escape(unit["text"])}</pre>' if unit["text"] else
                   "<p>No text retained at this location. Inspect the original; this does not prove it is blank.</p>")
                + f'<p class="meta">Showing {len(unit["text"])} of {context["total_characters"]} retained characters, starting at offset {offset}.</p>')
        if evidence._lookup("units", unit_id)["truncated"]:
            body += "<p><strong>The stored extraction itself is marked incomplete.</strong></p>"
        body += '<nav class="pager">'
        for position, label in ((max(0, offset - 2048) if offset else None, "Previous text slice"),
                                (context["next_offset"], "Next text slice")):
            if position is not None:
                body += f'<a href="{escape(reader_url(pin, cid, unit, offset=position), quote=True)}">{label}</a> '
        units = sorted(evidence.source_units(source_id), key=quality_order)
        position = next(index for index, item in enumerate(units) if item["id"] == unit_id)
        for adjacent, label in ((position - 1, "Previous retained location"), (position + 1, "Next retained location")):
            if 0 <= adjacent < len(units):
                body += f'<a href="{escape(reader_url(pin, cid, units[adjacent]), quote=True)}">{label}</a> '
        body += '</nav>'
        if project_root and cid == "core":
            body += (f'<p><a href="/library/edit?{escape(urlencode({"doi": source["doi"], "unit_id": unit_id}), quote=True)}">'
                     'View and correct this page in the active evidence version</a></p>')
        body += '<details><summary>Copy citation and excerpt</summary>'
        body += help_note("Copying citations", "<p>These fields are selectable even if clipboard access is unavailable. "
                              "The excerpt includes only this text slice; follow Next text slice to read more.</p>")
        for key, label, value in (("citation", "Citation and stable link", citation), ("excerpt", "Cited excerpt", excerpt)):
            body += copy_field(key, label, value)
        body += '<p id="copy-status" role="status" aria-live="polite"></p></details>'
        article_url = publication_url(source["doi"])
        if article_url:
            body += f'<p><a href="{escape(article_url, quote=True)}" rel="noreferrer">Open publication source</a> — Find supplements in the article supporting information.</p>'
        else:
            body += "<p>No DOI/PMID article link recorded; locate the publication by title.</p>"
        acquired_url = successful_supplement_links(data_root).get((collections.publication(source["doi"]), source["sha256"]))
        document_url = acquired_url or source.get("bibliography", {}).get("acquired_from") or source["url"]
        if document_url and urlsplit(document_url).scheme in {"http", "https"}:
            body += (f'<p><a href="{escape(document_url, quote=True)}" rel="noreferrer">'
                     + ("Open successfully acquired document" if acquired_url else "Try recorded document link")
                     + "</a> — External publisher/repository link; access may change. Not revalidated by opening this reader.</p>")
        else:
            body += "<p>No external document link recorded. Original files are not hosted here.</p>"
        return page("Read source context", body, reader_script=True)

    def reader_script():
        return Response(READER_SCRIPT, mimetype="text/javascript")

    def quality_page(error=None, status_code=200):
        if store and store.public_quality_root is not None:
            raise releases.AccessDeniedError("Operator quality pages are development-only")
        if set(request.args) - {"collection", "snapshot_id", "finding", "offset", "text_offset", "show", "kind", "unit_id", "unit_offset", "notice"}:
            raise ValueError("Unknown quality parameter")
        current = snapshot(request)
        cid = request.args.get("collection", "")
        offset = int(request.args.get("offset", "0"))
        text_offset = int(request.args.get("text_offset", "0"))
        if offset < 0 or text_offset < 0 or offset > retrieval.MAX_OFFSET:
            raise ValueError("Invalid quality pagination")
        show = request.args.get("show", "pending")
        if show not in {"pending", "all"}:
            raise ValueError("Invalid quality filter")
        if store:
            visible = {key: value for key, value in current.snapshots.items() if not collections.is_catalog(value)}
            if not cid:
                cid = next(iter(visible), "")
            if cid not in visible:
                raise releases.AccessDeniedError("Select an authorized evidence collection")
            evidence = visible[cid]
            options = "".join(f'<option value="{escape(key, quote=True)}"'
                              f'{" selected" if key == cid else ""}>{escape(key)}</option>'
                              for key in visible)
            form = f'<label>Collection <select name="collection">{options}</select></label> '
        else:
            if cid:
                raise ValueError("Single release has no named collections")
            evidence, form = current, ""
        report = quality.report(evidence, quality_root)
        if store:
            titles = publication_titles(current, {
                current._aliases[cid, finding["publication_id"]] for finding in report["findings"] if not finding["title"]})
            for finding in report["findings"]:
                if not finding["title"]:
                    finding["title"] = titles.get(current._aliases[cid, finding["publication_id"]])
        params = {"snapshot_id": current.release_id, "collection": cid} if store else {}
        counts = Counter(item["code"] for item in report["findings"] if not item["resolved"])
        kind = request.args.get("kind", "")
        kinds = sorted({item["code"] for item in report["findings"]})
        if kind and kind not in kinds:
            raise ValueError("Unknown quality finding kind")
        kind_options = '<option value="">All kinds</option>' + "".join(
            f'<option value="{escape(code, quote=True)}"{" selected" if kind == code else ""}>'
            f'{escape(QUALITY_LABELS[code])} ({counts[code]} pending)</option>' for code in kinds)
        form = ('<form method="get" class="filters">' + form
                + f'<label>Finding <select name="kind">{kind_options}</select></label> '
                + ('<input type="hidden" name="show" value="all">' if show == "all" else "")
                + '<button>Show</button></form>')

        def href(extra):
            return "/quality?" + escape(urlencode({**params, **extra}), quote=True)

        def link(extra, label):
            return f'<a href="{href(extra)}">{label}</a>'

        readiness = "Ready for operator approval" if report["ready"] else "Public readiness: Not ready"
        status_line = (f'<p class="quality-status"><span class="badge {"ok" if report["ready"] else "warn"}">'
                       f"{readiness}</span> {report['blockers']} unresolved findings; {report['sources']} source "
                       f"documents; {report['units']} retained text units.</p>")
        selected = request.args.get("finding")
        if error and not any(item["id"] == selected for item in report["findings"]):
            selected = None
        if selected:
            back = {key: value for key, value in (("show", show), ("kind", kind)) if value and value != "pending"}
            body = (f'<p class="back-link"><a href="{href(back)}">&larr; Back to findings</a></p>' + status_line)
        else:
            tab = lambda value, label: (f'<a href="{href({"show": value} if value == "all" else {})}"'
                                        + (' aria-current="page"' if show == value else "") + f">{label}</a>")
            chips = "".join(
                f'<li><a class="kind-chip{" active" if kind == code else ""}" '
                f'href="{href({"kind": code, **({"show": "all"} if show == "all" else {})})}">'
                f"{escape(QUALITY_LABELS[code])}: {count} pending</a></li>"
                for code, count in sorted(counts.items()))
            if chips and kind:
                chips = (f'<li><a class="kind-chip" href="{href({"show": "all"} if show == "all" else {})}">'
                         "&times; All kinds</a></li>" + chips)
            body = (status_line
                    + '<div class="tabs" role="group" aria-label="Findings shown">'
                    + tab("pending", "Pending findings") + tab("all", "All findings (including reviewed)") + "</div>"
                    + (f'<ul class="kind-chips" aria-label="Pending by kind">{chips}</ul>' if chips else "")
                    + form
                    + '<details class="notes"><summary>How this report works</summary>'
                    "<p>Development operator view. All existing evidence remains available here; "
                    "this report does not publish or hide documents. Metadata-only/unacquired publications are not "
                    "missing-text defects in these acquired source documents.</p>"
                    "<p>Pending tasks include routine comparisons, not only detected errors. "
                    "A completeness review is assigned to every document; it does not mean that document is broken.</p>"
                    f"<p>{escape(report['limitation'])}</p>"
                    "<p>Open a finding to build its location inventory, record a comparison, report an error, "
                    "or edit extracted data. "
                    "Reviews require comparison with the original; known errors must be repaired, not waived. "
                    "Original files are accessed at their publication source, never served here.</p></details>")
        if selected:
            finding = next((item for item in report["findings"] if item["id"] == selected), None)
            if finding is None:
                raise retrieval.NotFoundError("Unknown or stale quality finding")
            body += f"<h3>{escape(finding['title'] or finding['publication_id'])}</h3>"
            status, reason, instructions = quality_guidance(finding)
            body += (f'<p class="meta"><span class="badge {"ok" if finding["resolved"] else "warn"}">'
                     f"{escape(QUALITY_LABELS[finding['code']])}</span> "
                     f'<span class="chip">{escape(quality_document(finding["document_role"]))}</span> '
                     f"{escape(quality_location(finding['locator']))}</p>"
                     f"<h3>Why this is flagged</h3><p><strong>{escape(status)}</strong> {escape(reason)}</p>"
                     f"<h3>What to do</h3><p>{escape(instructions)}</p>")
            if finding["resolved"]:
                review = finding["review"]
                body += (f"<p><strong>Review recorded:</strong> {escape(review['reviewer'])}, "
                         f"{escape(review['at'])}. {escape(review['note'])}</p>")
            article_url = publication_url(finding["publication_id"])
            file_url = finding["source_url"]
            acquired_url = successful_supplement_links(data_root).get(
                (collections.publication(finding["publication_id"]), finding["source_sha256"]))
            if article_url:
                body += (f'<p><a href="{escape(article_url, quote=True)}" rel="noreferrer">Open publication source</a>'
                         " — Article record. For a supplement, use the article's Supplementary information "
                         "or Supporting information section.</p>")
            else:
                body += "<p>No DOI or PMID publication link is available; locate the publication by title.</p>"
            if acquired_url:
                body += (f'<p><a href="{escape(acquired_url, quote=True)}" rel="noreferrer">'
                         "Open successfully acquired document</a> — This URL was recorded as the successful download "
                         "for this exact source checksum, not just a discovered candidate. "
                         "It worked at acquisition; publisher access may change. If it fails now, use the article page above.</p>")
            elif file_url and urlsplit(file_url).scheme in {"http", "https"}:
                body += (f'<p><a href="{escape(file_url, quote=True)}" rel="noreferrer">Try recorded document link</a>'
                         " — This is the retained acquisition/source URL, not a verified working link. "
                         "Publisher file links can expire or be blocked. If it fails, use the publication page above "
                         "and locate the main paper or supplement there.</p>")
            units = sorted(evidence.source_units(finding["source_id"]), key=quality_order)
            expected = quality._inventory(quality_root, next(
                source for source in evidence._bundle["sources"] if source["id"] == finding["source_id"]))
            body += f"<p>This source has {len(units)} retained locations"
            body += (f"; {len(expected['locators'])} expected original locations.</p>" if expected else
                     "; expected original locations are not yet inventoried.</p>")
            if finding["unit_id"]:
                unit_id = finding["unit_id"]
            else:
                unit_id = request.args.get("unit_id")
                unit_offset = int(request.args.get("unit_offset", "0"))
                if not 0 <= unit_offset <= retrieval.MAX_OFFSET:
                    raise ValueError("Invalid unit pagination")
                if unit_id and not any(unit["id"] == unit_id for unit in units):
                    raise retrieval.NotFoundError("Unit does not belong to this source")
                body += "<h3>Inspect retained source locations</h3><p>In document order. "
                body += "Select a location to read its text; previews help locate it in the original.</p><ul>"
                for unit in units[unit_offset:unit_offset + 25]:
                    preview = evidence.source_context(unit["id"], limit=160)["record"]["text"]
                    body += "<li>" + link({"finding": selected, "unit_id": unit["id"]},
                                         escape(quality_location(unit["citation"]["locator"]))) + " — " + escape(
                                             preview.strip() or "No text extracted; inspect original for figures or missing content.") + "</li>"
                body += "</ul>"
                if unit_offset:
                    body += link({"finding": selected, "unit_offset": max(0, unit_offset - 25)}, "Previous locations")
                if unit_offset + 25 < len(units):
                    body += " | " + link({"finding": selected, "unit_offset": unit_offset + 25}, "Next locations")
            if unit_id:
                context = evidence.source_context(unit_id, offset=text_offset)
                body += "<h3>Retained text (compare with the original)</h3>"
                body += (f"<pre>{escape(context['record']['text'])}</pre>" if context["total_characters"] else
                         "<p><strong>No text was extracted at this location.</strong> There is nothing to compare here "
                         "until you open the original. Use the neighbouring text below to locate it.</p>")
                if context["record"]["caveats"]:
                    body += "<h4>Recorded extraction warnings and provenance</h4><ul>" + "".join(
                        f"<li>{escape(warning)}</li>" for warning in context["record"]["caveats"]) + "</ul>"
                if context["next_offset"] is not None:
                    body += link({"finding": selected, "unit_id": unit_id, "text_offset": context["next_offset"]}, "Next text slice")
                if text_offset:
                    body += " | " + link({"finding": selected, "unit_id": unit_id}, "Start of text")
            else:
                body += ("<p>This is a whole-document task: inspect the locations above.</p>" if finding["locator"] is None else
                         "<p>This original location has no retained unit. Inspect it at the publication source.</p>")
            if finding["locator"] is not None:
                target_order = quality_order({"id": "", "citation": {"locator": finding["locator"]}})
                neighbours = [unit for unit in units if unit["id"] != finding["unit_id"]]
                before = [unit for unit in neighbours if quality_order(unit) < target_order]
                after = [unit for unit in neighbours if quality_order(unit) > target_order]
                nearby = before[-1:] + after[:1]
                if nearby:
                    body += "<h3>Neighbouring text — find this location in the original</h3>"
                    for unit in nearby:
                        preview = evidence.source_context(unit["id"], limit=400)["record"]["text"]
                        body += (f"<p><strong>{escape(quality_location(unit['citation']['locator']))}</strong></p>"
                                 f"<blockquote>{escape(preview.strip() or 'No text extracted at this neighbouring location.')}</blockquote>")
            body += "<h3>Take action</h3>"

            def action_form(action, content, button, *, upload=False):
                fields = {"token": provide_token, "snapshot_id": current.release_id,
                          "finding_id": selected, "unit_id": unit_id or "", "action": action}
                return (f'<form method="post" action="{href({"finding": selected, **({"unit_id": unit_id} if unit_id else {})})}"'
                        + (' enctype="multipart/form-data"' if upload else "") + '>'
                        + "".join(f'<input type="hidden" name="{key}" value="{escape(value, quote=True)}">'
                                  for key, value in fields.items())
                        + content + f'<button>{escape(button)}</button></form>')

            if finding["code"] == "inventory_missing":
                body += ('<section class="panel"><h4>Build location inventory</h4>'
                         '<p>Use the stored original, or select your original file. Its checksum must match '
                         'this source. Inventory counts locations; it does not verify extracted text.</p>'
                         + action_form("inventory",
                             '<label>Original file (optional if stored locally) '
                             '<input type="file" name="original" accept=".pdf,.docx,.xlsx"></label>',
                             "Build location inventory", upload=True) + '</section>')
            operator_fields = (
                '<label>Reviewer <input name="reviewer" required maxlength="200" value="'
                + escape(request.form.get("reviewer", ""), quote=True) + '"></label>'
                '<label>Comparison or problem note <textarea name="note" required maxlength="2000">'
                + escape(request.form.get("note", "")) + '</textarea></label>')
            if finding["allowed_decisions"] and not finding["resolved"]:
                decisions = {"verified": "Text or document coverage is correct",
                             "verified_blank": "Original PDF page is genuinely blank",
                             "verified_nontext": "Original location contains no transcribable text"}
                body += ('<section class="panel"><h4>Record comparison</h4>'
                         + action_form("review", operator_fields
                             + '<label>Result <select name="decision">'
                             + "".join(f'<option value="{decision}">{decisions[decision]}</option>'
                                       for decision in finding["allowed_decisions"]) + '</select></label>'
                             + '<label><input type="checkbox" name="checked" value="yes" required> '
                             'I compared this location or whole document with the original. '
                             'I am not waiving a known error.</label>', "Record comparison") + '</section>')
            if unit_id:
                body += ('<details><summary>Report an extraction error</summary>'
                         '<p>The reported error remains a blocker until corrected in a new evidence version.</p>'
                         + action_form("problem", operator_fields, "Report error") + '</details>')
            if project_root and cid == "core":
                if unit_id:
                    body += ('<p><a class="button" href="/library/edit?'
                             + escape(urlencode({"doi": finding["publication_id"], "unit_id": unit_id}), quote=True)
                             + '#page-editor">Edit extracted data</a></p>')
                elif finding["code"] == "missing_unit":
                    body += ('<details><summary>Add missing page text</summary>'
                             '<p>Enter the complete transcription after comparing with the original PDF. '
                             'Saving creates and activates a new version, preserving previous evidence.</p>'
                             + action_form("repair", operator_fields
                                 + '<label>Complete page text <textarea name="text" required maxlength="1000000">'
                                 + escape(request.form.get("text", "")) + '</textarea></label>'
                                 '<label><input type="checkbox" name="checked" value="yes" required> '
                                 'I checked this transcription against the original PDF and want to save a new version.</label>',
                                 "Save missing page") + '</details>')
            elif unit_id or finding["code"] == "missing_unit":
                body += '<p>Text editing is available in private project libraries.</p>'
            body += "<details><summary>Technical identifiers (for troubleshooting)</summary><dl>" + "".join(
                f"<dt>{escape(key)}</dt><dd>{escape(json.dumps(value, ensure_ascii=False))}</dd>"
                for key, value in finding.items()) + "</dl></details>"
        else:
            rank = {code: position for position, code in enumerate(QUALITY_LABELS)}
            findings = sorted((item for item in report["findings"]
                               if (show == "all" or not item["resolved"]) and (not kind or item["code"] == kind)),
                              key=lambda item: (rank.get(item["code"], len(rank)), item["resolved"],
                                                (item["title"] or item["publication_id"]).casefold()))
            generic = {"coverage_review", "missing_text", "missing_unit", "ocr_review", "inventory_missing"}
            group = None
            for finding in findings[offset:offset + 25]:
                if finding["code"] != group:
                    if group is not None:
                        body += "</ul></section>"
                    group = finding["code"]
                    members = [item for item in findings if item["code"] == group]
                    pending = sum(not item["resolved"] for item in members)
                    tally = f"{pending} pending" + (f", {len(members) - pending} reviewed" if show == "all" and len(members) > pending else "")
                    next_action = next((item["next_action"] for item in members if not item["resolved"]), "")
                    body += ('<section class="finding-group"><div class="group-head">'
                             f"<h3>{escape(QUALITY_LABELS[group])}</h3>"
                             f'<span class="badge {"warn" if pending else "ok"}">{tally}</span></div>'
                             f'<p class="meta"><strong>{escape(quality_guidance(finding)[0])}</strong> '
                             f"{escape(next_action)}</p>"
                             '<ul class="finding-list">')
                state = ""
                if show == "all":
                    state = ('<span class="badge ok">Reviewed</span>' if finding["resolved"]
                             else '<span class="badge warn">Pending</span>')
                detail = ("" if finding["code"] in generic else f"<small>{escape(finding['message'])}</small>")
                body += (f'<li><a class="finding-row" href="{href({"finding": finding["id"], **({"show": "all"} if show == "all" else {}), **({"kind": kind} if kind else {})})}">'
                         f'<span class="finding-main"><strong>{escape(finding["title"] or finding["publication_id"])}</strong>'
                         f'<span class="meta"><span class="chip">{escape(quality_document(finding["document_role"]))}</span> '
                         f"{escape(quality_location(finding['locator']))}</span>{detail}</span>{state}</a></li>")
            if group is not None:
                body += "</ul></section>"
            if not findings:
                body += ('<p class="empty-state">' + ("No pending findings." if show == "pending" else
                                                      "No findings in this selection.") + "</p>")
            elif len(findings) > 25:
                body += (f'<p class="meta">Showing {offset + 1}–{min(offset + 25, len(findings))} '
                         f"of {len(findings)} findings.</p>")
            body += '<nav class="pager">'
            if offset:
                body += link({"offset": max(0, offset - 25), "show": show, "kind": kind}, "Previous")
            if offset + 25 < len(findings):
                body += link({"offset": offset + 25, "show": show, "kind": kind}, "Next")
            body += "</nav>"
        notices = {
            "review": "Comparison recorded. This does not imply scientific validation.",
            "problem": "Extraction error recorded; it remains a blocker until corrected.",
            "inventory": "Location inventory saved. Text still needs comparison with the original.",
            "repair": "Missing page saved in a new evidence version. Previous evidence is preserved.",
        }
        notice = request.args.get("notice", "")
        if notice and notice not in notices:
            raise ValueError("Unknown quality notice")
        if notice:
            body = f'<p role="status">{notices[notice]}</p>' + body
        if error:
            body = f'<p role="alert">{escape(error)}</p>' + body
        response = page("Data quality", body)
        response.status_code = status_code
        return response

    def quality_action():
        if store and store.public_quality_root is not None:
            raise releases.AccessDeniedError("Operator quality actions are development-only")
        if not hmac.compare_digest(request.form.get("token", ""), provide_token):
            return Response("Quality form expired; reload the page.", status=403, mimetype="text/plain")
        try:
            with discovery_lock:
                require_idle_project()
                current = store.current() if store else pinned
                if request.form.get("snapshot_id") != current.release_id:
                    raise ValueError("Evidence changed; reload Data quality before recording a result")
                cid = request.args.get("collection", "")
                if store:
                    if cid not in current.snapshots or collections.is_catalog(current.snapshots[cid]):
                        raise releases.AccessDeniedError("Select an authorized evidence collection")
                    evidence = current.snapshots[cid]
                else:
                    if cid:
                        raise ValueError("Single release has no named collections")
                    evidence = current
                finding = next((item for item in quality.report(evidence, quality_root)["findings"]
                                if item["id"] == request.form.get("finding_id")), None)
                if finding is None:
                    raise ValueError("Finding changed; reload Data quality")
                unit_id = request.form.get("unit_id", "")
                if unit_id and (not any(unit["id"] == unit_id for unit in evidence.source_units(finding["source_id"]))
                                or finding["unit_id"] and finding["unit_id"] != unit_id):
                    raise ValueError("Select a retained location belonging to this finding")
                action = request.form.get("action")
                reviewer, note = request.form.get("reviewer", "").strip(), request.form.get("note", "").strip()
                if action in {"review", "problem", "repair"}:
                    if len(reviewer) > 200 or len(note) > 2000:
                        raise ValueError("Reviewer and note exceed their length limits")
                    quality._operator(reviewer, note)
                if action == "review":
                    if request.form.get("checked") != "yes":
                        raise ValueError("Confirm that you compared with the original before recording a review")
                    quality.review(evidence, quality_root, finding["id"], request.form.get("decision", ""),
                                   reviewer, note)
                elif action == "problem":
                    if not unit_id:
                        raise ValueError("Select a retained location before reporting an error")
                    quality.problem(evidence, quality_root, unit_id, reviewer, note)
                elif action == "inventory":
                    if finding["code"] != "inventory_missing":
                        raise ValueError("This finding does not need a location inventory")
                    from .build.quality_inventory import expected_locations
                    from tempfile import TemporaryDirectory
                    source = next(item for item in evidence._bundle["sources"] if item["id"] == finding["source_id"])
                    with TemporaryDirectory(prefix="quality-original-") as folder:
                        upload = request.files.get("original")
                        if upload and upload.filename:
                            path = Path(folder) / ("original." + source["format"])
                            upload.save(path)
                        elif project_root:
                            name = source["sha256"] + "." + source["format"]
                            if not (project_root / "papers" / name).is_file():
                                raise ValueError("Stored original is unavailable; select your original file")
                            path = releases.safe_file(project_root / "papers", name)
                        else:
                            raise ValueError("Select the original file to build its inventory")
                        if catalog.file_digest(path) != source["sha256"]:
                            raise ValueError("Original checksum does not match this source; inventory was not saved")
                        locators = expected_locations(path, source["format"])
                        if catalog.file_digest(path) != source["sha256"]:
                            raise ValueError("Original changed during inventory; inventory was not saved")
                        quality.inventory(quality_root, source, locators)
                elif action == "repair":
                    if not project_root or cid != "core" or finding["code"] != "missing_unit":
                        raise ValueError("Select a missing page in a private project library")
                    from .build import project_edit
                    source = next(item for item in evidence._bundle["sources"] if item["id"] == finding["source_id"])
                    bibliography = source.get("bibliography") or {}
                    project_edit.correct(project_root, snapshot_id=current.release_id, source_id=source["id"],
                        unit_id=None, missing_locator=finding["locator"], text=request.form.get("text", ""),
                        bibliography={key: bibliography.get(key) or "" for key in project_edit.BIB_FIELDS},
                        source_url=source["url"] or "", tables=[], reviewer=reviewer, reason=note,
                        checked=request.form.get("checked") == "yes")
                    store.clear_cache()
                else:
                    raise ValueError("Unknown quality action")
        except releases.AccessDeniedError:
            raise
        except (ValueError, releases.ReleaseError, ValidationError) as error:
            diagnostic("web_quality_rejected", exception_type=type(error).__name__)
            return quality_page(str(error), 400)
        return redirect("/quality?" + urlencode({"collection": cid, "notice": action}), code=303)

    app = Flask(__name__, static_folder=None)
    app.extensions["citation_discovery"] = discovery_state
    app.extensions["metadata_update"] = metadata_state
    app.extensions["core_purge"] = purge_state
    app.extensions["find_pdf"] = pdf_state
    app.extensions["batch_pdf"] = batch_state
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    for path, endpoint, view in (
            ("/api/<endpoint>", "api", api), ("/", "overview", overview),
            ("/settings", "project_settings", project_settings),
            ("/discovery", "discovery", discovery_page),
            ("/library", "library", library), ("/sources", "sources", sources),
            ("/publications", "publications", catalog_page), ("/coverage", "coverage", catalog_page),
            ("/quality", "quality", quality_page),
            ("/search", "search", search_page), ("/read", "read", read_page),
            ("/reader.js", "reader_script", reader_script),
            ("/help.js", "help_script", lambda: Response(HELP_SCRIPT, mimetype="text/javascript")),
            ("/settings.js", "settings_script", lambda: Response(SETTINGS_SCRIPT, mimetype="text/javascript")),
            ("/library.js", "library_script", lambda: Response(LIBRARY_SCRIPT, mimetype="text/javascript")),
            ("/connect", "connect", connect_page),
            ("/style.css", "stylesheet", stylesheet)):
        app.add_url_rule(path, endpoint, view, methods=["GET"])
    app.add_url_rule("/settings/initialize", "initialize_project", initialize_project, methods=["POST"])
    app.add_url_rule("/settings/reset", "reset_project", reset_project, methods=["POST"])
    app.add_url_rule("/settings/attach-pdf", "attach_project_pdf", attach_project_pdf, methods=["POST"])
    app.add_url_rule("/settings/find-pdf", "find_project_pdf", find_project_pdf, methods=["POST"])
    app.add_url_rule("/settings/pdf-progress", "pdf_progress", pdf_progress, methods=["GET"])
    app.add_url_rule("/library/batch", "batch_page", batch_page, methods=["GET"])
    app.add_url_rule("/library/batch", "batch_action", batch_action, methods=["POST"])
    app.add_url_rule("/library/edit", "edit_paper", edit_paper, methods=["GET"])
    app.add_url_rule("/library/edit", "save_paper_correction", save_paper_correction, methods=["POST"])
    app.add_url_rule("/quality", "quality_action", quality_action, methods=["POST"])
    app.add_url_rule("/discovery", "start_discovery", start_discovery, methods=["POST"])
    app.add_url_rule("/settings/configure-metadata", "configure_project_metadata",
                     configure_project_metadata, methods=["POST"])
    app.add_url_rule("/settings/metadata-progress", "metadata_progress", metadata_progress, methods=["GET"])
    app.add_url_rule("/settings/openalex-key", "save_project_openalex_key",
                     save_project_openalex_key, methods=["POST"])
    app.add_url_rule("/settings/add-papers", "add_project_papers", add_project_papers, methods=["POST"])
    app.add_url_rule("/settings/import-papers", "import_project_papers", add_project_papers, methods=["POST"])
    app.add_url_rule("/settings/purge-preview", "purge_preview", purge_core_paper, methods=["POST"])
    app.add_url_rule("/settings/purge-paper", "purge_core_paper", purge_core_paper, methods=["POST"])
    app.add_url_rule("/settings/purge-progress", "purge_progress", purge_progress, methods=["GET"])
    app.add_url_rule("/library/provide", "provide", provide, methods=["POST"])
    app.config["MAX_CONTENT_LENGTH"] = 65 * 1024 * 1024
    if can_provide() and (data_root / "releases/provided-sources/ledger.json").exists():
        from .build import provided_sources
        if provided_sources.pending(data_root):
            start_provider()

    @app.before_request
    def local_boundary():
        host = request.headers.get("host")
        local_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in local_hosts:
            return Response("Untrusted host", status=421, mimetype="text/plain")
        local_origins = {f"http://{local_host}" for local_host in local_hosts}
        origin = request.headers.get("origin")
        if origin is not None and origin not in local_origins:
            return Response("Untrusted origin", status=403, mimetype="text/plain")
        pause_batch = (batch_running() and request.path == "/library/batch"
                       and request.method == "POST" and request.form.get("action") == "pause")
        if pdf_running() and request.method != "GET" and not pause_batch:
            return error_response("PDF search is running; retry after it finishes.", "project_busy", 503)
        if purge_running() and request.path not in {"/settings/purge-progress", "/style.css"}:
            if request.method == "GET" and not request.path.startswith("/api/"):
                return redirect("/settings/purge-progress")
            return error_response("Permanent core-paper removal is running; retry after it finishes.",
                                  "project_busy", 503)

    def error_response(message, code, status):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": {"code": code, "message": message}}), status
        if request.path in {"/search", "/read"}:
            response = page("Evidence request could not be completed",
                            f"<p>{escape(message)}</p><p>Check the query, collection and source link. "
                            "Unavailable snapshots are not silently replaced with current evidence. "
                            '<a href="/search">Start a new search</a> against the active snapshot, '
                            "or ask the operator to restore the requested snapshot. Search needs at least one word or number.</p>")
            response.status_code = status
            return response
        return Response(message, status=status, mimetype="text/plain")

    @app.errorhandler(collections.SnapshotUnavailable)
    def snapshot_unavailable(error):
        return error_response("Snapshot no longer available; start a new request.", "snapshot_unavailable", 410)

    @app.errorhandler(releases.AccessDeniedError)
    def access_denied(error):
        diagnostic("web_rejected", exception_type=type(error).__name__)
        return error_response("Unknown or unauthorized evidence access.", "permission_denied", 403)

    @app.errorhandler(retrieval.NotFoundError)
    def not_found(error):
        diagnostic("web_rejected", exception_type=type(error).__name__)
        return error_response("Requested evidence record was not found.", "not_found", 404)

    @app.errorhandler(releases.ReleaseError)
    def release_unavailable(error):
        diagnostic("web_unavailable", exception_type=type(error).__name__)
        return error_response("Evidence unavailable; check operator logs.", "unavailable", 503)

    def invalid_input(error):
        diagnostic("web_rejected", exception_type=type(error).__name__)
        return error_response("Invalid request or unavailable collection.", "invalid_input", 400)

    def unavailable(error):
        diagnostic("web_unavailable", exception_type=type(error).__name__)
        return error_response("Evidence unavailable; check operator logs.", "unavailable", 503)

    for exception in (retrieval.RetrievalError, ValidationError, ValueError):
        app.register_error_handler(exception, invalid_input)
    for exception in (OSError, sqlite3.Error):
        app.register_error_handler(exception, unavailable)

    @app.after_request
    def response_headers(response):
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Referrer-Policy": "same-origin",
        })
        return response

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    roots = parser.add_mutually_exclusive_group(required=True)
    roots.add_argument("--release", type=Path)
    roots.add_argument("--registry", type=Path)
    parser.add_argument("--allow-profile", required=True, action="append", choices=sorted(releases.PROFILES))
    parser.add_argument("--allow-collection", action="append")
    parser.add_argument("--default-collection", action="append")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--data-root", type=Path, help="Local inventory workspace (inferred from the registry by default)")
    parser.add_argument("--quality-root", type=Path, help="Operator quality records (default: releases/data-quality)")
    parser.add_argument("--project-root", type=Path, help="Enable project initialization and reset settings")
    parser.add_argument("--pdf-data-root", type=Path, default=Path("releases/pdf-data"),
                        help="Shared local PDF folder offered in project settings")
    args = parser.parse_args()
    try:
        app = create_app(release=args.release, registry=args.registry, profiles=set(args.allow_profile),
                         allowed_collections=args.allow_collection, default_collections=args.default_collection,
                         port=args.port, mcp_url=args.mcp_url, data_root=args.data_root, quality_root=args.quality_root,
                         project_root=args.project_root, pdf_data_root=args.pdf_data_root)
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        diagnostic("web_startup_failed", exception_type=type(error).__name__)
        return 1
    diagnostic("web_starting", url=f"http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
