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
import secrets
import shlex
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

from jsonschema import ValidationError
from flask import Flask, Response, jsonify, redirect, request

from . import __version__, catalog, collections, contracts, library_api, quality, releases, retrieval
from .http_auth import resource_url
from .server import DESCRIPTIONS, diagnostic


def page(title, body, *, reader_script=False):
    return Response(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="stylesheet" href="/style.css">'
        + ('<script src="/reader.js" defer></script>' if reader_script else "")
        + f'<title>{escape(title)} | Evidence</title>'
        '<body><header><h1>Research Evidence</h1><nav><a href="/">Overview</a> | '
        '<a href="/search">Search evidence</a> | '
        '<a href="/connect">Connect your AI</a> | '
        '<a href="/library">Library &amp; download status</a> | <a href="/sources">Sources available to AI</a> | <a href="/publications">Publication metadata</a> | '
        '<a href="/coverage">Discovery coverage &amp; gaps</a> | <a href="/quality">Data quality</a></nav></header><main>'
        f'<h2>{escape(title)}</h2>{body}</main></body></html>', mimetype="text/html")


def copy_field(key, label, value, *, rows=5):
    return (f'<label for="{key}">{label}</label><textarea id="{key}" readonly rows="{rows}">{escape(value)}</textarea>'
            f'<button type="button" data-copy="{key}">Copy {label.lower()}</button>')


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
                "and table cells. If correct, record verification. If not, create a corrected text file and candidate, then compare again.")
    if code == "inventory_missing":
        return ("Completeness cannot yet be checked.",
                "We have no independent list of the original document's expected locations; retained units alone cannot reveal an omitted final page.",
                "Run the inventory command below against checksum-matched originals, then reload this report. This does not approve their text.")
    return ("Detected problem or extraction warning.",
            finding["message"],
            "Compare this location and its extraction warnings with the original. "
            + ("Record verification only if the extraction is correct; otherwise repair it."
               if code == "extraction_review" else
               "Repair or rebuild a new candidate using the operator walkthrough; verification cannot waive this problem."))


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


def create_app(*, release=None, registry=None, profiles, allowed_collections=None,
               default_collections=None, port=8080, mcp_url="http://127.0.0.1:8000/mcp",
               data_root=None, quality_root=None):
    if not 1 <= port <= 65535:
        raise ValueError("Invalid website port")
    mcp_url = resource_url(mcp_url)
    if bool(release) == bool(registry):
        raise ValueError("Select exactly one release or collection registry")
    if not registry and (allowed_collections or default_collections):
        raise ValueError("Collection selection requires a registry")
    store = (collections.CollectionStore(registry, allowed_profiles=profiles,
                                        allowed_collections=allowed_collections or [],
                                        defaults=default_collections) if registry else None)
    pinned = None if store else releases.validate(release, allowed_profiles=profiles)
    if store:
        store.current()
    data_root = Path(data_root) if data_root else (Path(registry).absolute().parent.parent if registry else None)
    quality_root = Path(quality_root) if quality_root else (data_root or Path.cwd()) / "releases/data-quality"

    provide_token = secrets.token_urlsafe(32)
    provider_state = {"thread": None, "again": False}
    provider_lock = threading.Lock()

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
        current = snapshot(request)
        status = current.call("corpus_status", {})["result"]
        counts = "".join(f"<li>{escape(key.replace('_', ' '))}: {status[key]}</li>"
                         for key in ("paper_count", "claim_count", "content_unit_count"))
        tools = "".join(f"<dt><code>{escape(name)}</code></dt><dd>{escape(description)}</dd>"
                        for name, description in DESCRIPTIONS.items())
        visible = current.call("list_collections", {})["result"]["items"]
        memberships = "".join(
            f"<li>{escape(item['title'])}: {escape(item['coverage'])}</li>" for item in visible)
        return page("Overview",
                    f"<p>Application {escape(__version__)}; tool contract {contracts.SCHEMA_VERSION}.</p>"
                    "<h3>Explore the project</h3><ul>"
                    '<li><a href="/search">Search evidence</a>: find passages, read context and copy citations without AI.</li>'
                    '<li><a href="/connect">Connect your AI</a>: MCP setup, verification and credential troubleshooting.</li>'
                    '<li><a href="/library">Library &amp; download status</a>: '
                    "papers found, download/extraction progress and impact rankings.</li>"
                    '<li><a href="/sources">Sources available to AI</a>: '
                    "exact source versions published for MCP search and page-context retrieval.</li>"
                    '<li><a href="/publications">Publication metadata</a>: '
                    "bibliographic records, including sources not downloaded.</li>"
                    '<li><a href="/coverage">Discovery coverage &amp; gaps</a>: '
                    "how the metadata catalog was assembled and its known limitations.</li></ul>"
                    "<p>Found, downloaded, extracted, available to AI and scientifically reviewed "
                    "are different states.</p>"
                    '<p><a href="/api/collections">Read-only library JSON API</a>: list evidence papers, '
                    "documents and page locations, retrieve text, or run keyword search without an AI model "
                    "or MCP process. See docs/library-api.md for examples. Access is local to this machine.</p>"
                    "<h3>MCP connection</h3>"
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
                    "Override <code>CLIENT_PROJECT</code> for another project.</p>"
                    "<h3>Verify the running MCP server</h3>"
                    "<ol><li>Start <code>make mcp</code> with the intended release/collection configuration.</li>"
                    "<li>In another terminal check liveness and readiness:<pre>"
                    f"{escape('curl --fail-with-body ' + mcp_url.removesuffix('/mcp') + '/healthz')}\n"
                    f"{escape('curl --fail-with-body ' + mcp_url.removesuffix('/mcp') + '/readyz')}"
                    "</pre>Expected: alive and ready. These checks do not validate your client token.</li>"
                    "<li>Connect an MCP client using your current bearer token; list tools "
                    f"({len(DESCRIPTIONS)} in version {escape(__version__)}), call <code>list_collections</code>, then pin its snapshot "
                    "for evidence and catalog calls. Test <code>search_evidence</code> in iStopMM "
                    "and <code>list_publications</code> in a metadata catalog collection.</li>"
                    "<li>See <code>docs/verify-mcp.md</code> in the project for a copy-and-run SDK "
                    "check of the live endpoint. It needs no model provider and never prints the token.</li></ol>"
                    "<p>Opening /mcp in a browser is not a protocol test. "
                    "<code>make test-catalog</code> and <code>make test-http</code> launch separate "
                    "temporary servers; they do not check this running endpoint. "
                    "401 usually means an expired/invalid token; connection refused means no listener.</p>"
                    f"<h3>Configured evidence</h3><ul>{counts}</ul>"
                    f"<p>Snapshot: <code>{escape(current.release_id)}</code></p>"
                    f"<p>{escape(status['coverage'])}</p><ul>{memberships}</ul>"
                    "<p>Local authorized evidence only, not all published knowledge. "
                    "Metadata discovery does not imply full-text acquisition. "
                    "Machine extraction is not scientific or human validation.</p>"
                    f"<h3>Read-only tools</h3><dl>{tools}</dl>")

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
        options = '<option value="">Endpoint default</option>' + "".join(
            f'<option value="{escape(item["id"], quote=True)}"'
            f'{" selected" if item["id"] == cid else ""}>{escape(item["title"])}</option>'
            for item in visible)
        form = ('<form method="get"><label>DOI / publication ID '
                f'<input name="paper" value="{escape(paper, quote=True)}"></label> '
                + (f'<label>Collection <select name="collection">{options}</select></label> ' if store else "")
                + '<button type="submit">Filter</button></form>')
        entries = []
        acquired_links = successful_supplement_links(data_root)
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
                document_label = "Source " + source["source_id"]
            details["Document"] = document_label
            fields = "".join(f"<dt>{escape(key)}</dt><dd>{escape(str(value)) if value is not None else 'Unknown / not recorded'}</dd>"
                             for key, value in details.items())
            entries.append(
                '<article class="source-entry"><details>'
                f"<summary>{escape(title or source['doi'])} &mdash; {escape(document_label)}</summary>"
                f"<dl>{fields}</dl>"
                + (f'<p><a href="{escape(acquired_url, quote=True)}" rel="noreferrer">Open successfully acquired document</a>'
                   " — Worked at acquisition; publisher access may change.</p>" if acquired_url else "")
                + "</details></article>")
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
        return page("Sources available to AI", "<p>Exact source versions in the selected active MCP evidence "
                    'collections, not the full discovered bibliography. <a href="/search">Search evidence</a> '
                    "or use Read retained text on a document below. This page is an inventory, not a chat. "
                    "Availability does not mean scientific review.</p>" + form + "<p>Citation links are not necessarily download origins. "
                    "Unknown acquisition details are not inferred. Original files and extracted text "
                    "are not displayed on this inventory page. Authorized extracted text is available through "
                    "the local JSON API or MCP; original files are not served. This is corpus inventory, "
                    "not the sources used in a particular answer.</p>"
                    f"<p>Snapshot: <code>{escape(current.release_id)}</code></p>"
                    + ("".join(entries) or "<p>No matching sources.</p>")
                    + "<nav>" + " | ".join(navigation) + "</nav>")

    def stylesheet():
        return Response(
            "body{font:16px/1.6 system-ui,sans-serif;max-width:72rem;margin:2rem auto;"
            "padding:0 1.5rem;color:#243447;background:#f7f9fc}a{color:#135fa3}"
            "header{border-bottom:2px solid #d5dfeb;padding-bottom:1rem}"
            "article{background:white;border:1px solid #d5dfeb;border-radius:.5rem;"
            "padding:1rem 1.5rem;margin:1rem 0}dt{font-weight:600}dd{margin:0 0 .75rem;"
            "overflow-wrap:anywhere}code{overflow-wrap:anywhere}"
            ".source-entry{padding:0;margin:.4rem 0}"
            ".source-entry summary{cursor:pointer;padding:.6rem 1rem;font-weight:600;"
            "overflow-wrap:anywhere}"
            ".source-entry summary:focus-visible{outline:2px solid #135fa3;outline-offset:2px}"
            ".source-entry dl{margin:0;padding:.5rem 1rem 1rem}"
            "input,select,button{font:inherit;max-width:100%;padding:.35rem;margin:.3rem}"
            "nav{padding:.5rem 0}table{border-collapse:collapse;width:100%;background:white}"
            "th,td{text-align:left;vertical-align:top;border-bottom:1px solid #d5dfeb;padding:.6rem}"
            ".table-scroll{overflow-x:auto}small{display:block;overflow-wrap:anywhere}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere}"
            "textarea{display:block;box-sizing:border-box;width:100%;font:inherit;margin:.5rem 0}"
            "blockquote{white-space:pre-wrap;overflow-wrap:anywhere}",
            mimetype="text/css")

    def library():
        if set(request.args) - {"query", "download", "extraction", "offset", "sort"}:
            raise ValueError("Unknown library parameter")
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
                rows.append(f"<tr><td>{escape(item['title'])}</td><td>{escape(doi)}</td><td>{state}</td></tr>")
            return page("Core paper library",
                        "<p>Paper identities are supplied by the operator and are not independently verified. "
                        "Manage papers with the project command-line tool.</p>"
                        "<table><thead><tr><th>Title</th><th>DOI</th><th>Status</th></tr></thead><tbody>"
                        + "".join(rows) + "</tbody></table>")
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
        body = (f"<p>{len(rows)} listed sources; {downloaded} downloaded; "
                f"{counts['extracted']} extracted; {counts['partial']} partially extracted.</p>"
                + (f"<p>{counts['extracted_with_ocr']} extracted with OCR; "
                   f"{counts['needs_visual_review']} need visual review. OCR text may contain errors; "
                   "original native text is retained unchanged.</p>"
                   if counts["extracted_with_ocr"] or counts["needs_visual_review"] else "") +
                f"<p>{len(versions)} extracted PDF versions; "
                f"{sum(version['pages'] for version in versions.values())} pages across retained versions.</p>"
                "<p>The downloaded count covers additional-library downloads. Extraction and PDF-version "
                "totals also include listed papers already in the original iStopMM collection; their "
                "main-paper pages are counted, not supplements.</p>"
                "<p>Provider-indexed direct references and citers, not guaranteed complete worldwide. "
                "Extraction does not mean active MCP retrieval or scientific review. "
                "This page reads the local inventory and extraction records on each request; original files are not served.</p>"
                + ("<p>Have a copy of a paper that could not be downloaded? Use <strong>Provide PDF or link</strong> "
                   "on its row. The PDF is checked, extracted (with OCR for pages without text) and added to the "
                   "library as a new snapshot; the previous snapshot is kept for rollback. Provided copies are "
                   "private, never served, and not confirmed as the publisher version.</p>" if providing_enabled else "") +
                '<p><a href="/library?download=awaiting&amp;sort=citations">Prioritize papers awaiting download</a></p>'
                "<p>Impact uses saved OpenAlex citation counts, not live counts. Unknown counts sort last, "
                "not as zero. Citations/year divides by elapsed years at capture, with a minimum age of one year. "
                "iStopMM connections counts distinct seed papers in either citation direction. "
                "These are prioritization aids, not measures of scientific quality or field-normalized impact.</p>"
                '<form method="get"><label>Search title, author or DOI '
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
        body += f"<p>{len(matches)} matching sources.</p>"
        body += (f"<p>Citation counts available for {sum(row['citations'] is not None for row in matches)} "
                 "matching sources; others are unknown.</p>")
        entries = []
        for row in matches[offset:offset + 50]:
            title = escape(row["title"].strip() or "[Unresolved reference]")
            url = row["citation_url"]
            if urlsplit(url).scheme in {"https", "http"}:
                title = f'<a href="{escape(url, quote=True)}">{title}</a>'
            direction = ("Referenced by iStopMM" if row["background_of"] else "")
            if row["impact_on"]:
                direction += ("; " if direction else "") + "Cites iStopMM"
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
                           f"<td>{escape(download_labels.get(row['acquisition_status'], row['acquisition_status']))}"
                           f"{provide_cell(row, providing_enabled)}</td>"
                           f"<td>{escape(extraction_labels.get(row['extraction'], row['extraction']))}"
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
        return page("Library & download status",
                    "<p>The working inventory of discovered related sources: what has been downloaded "
                    "and extracted, and what is still pending. Extraction alone does not publish a source "
                    'to MCP; see <a href="/sources">Sources available to AI</a> for the serving inventory.</p>'
                    + body + "<nav>" + " | ".join(links) + "</nav>")

    def can_provide():
        if not (store and data_root) or "literature" not in store.current().snapshots:
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
        current = snapshot(request)
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
        else:
            result = current.call(tool, args)["result"]
        options = '<option value="">All metadata catalogs</option>' + "".join(
            f'<option value="{escape(item["id"], quote=True)}"'
            f'{" selected" if item["id"] == cid else ""}>{escape(item["title"])}</option>'
            for item in visible)
        form = ('<form method="get">' +
                (f'<label>Collection <select name="collection">{options}</select></label>' if store else "") +
                (f'<label>Words <input name="query" value="{escape(request.args.get("query", ""), quote=True)}"></label>'
                 if tool == "list_publications" else "") + '<button>Filter</button></form>')
        description = (
            "Audit view of metadata discovery: providers, search dates and queries, counts, unresolved "
            "identities and incomplete searches. This is not download/extraction progress or scientific review."
            if tool == "catalog_status" else
            "Bibliographic records for discovered publications, including papers not downloaded. "
            "Use this page for authors, identifiers and metadata provenance, not paper-text search.")
        body = "<p>" + description + "</p>" + form + "<p>Provisional metadata only. Not researcher-approved, not full-text evidence. " \
                      "A reported abstract or full-text location is not acquisition or permission.</p>"
        if evidence_metadata:
            body += (f"<p>{len(ordered)} matching bibliographic records from authorized metadata catalogs "
                     "for papers in the selected evidence collection. Matches use publication identifiers, "
                     "not title guesses; sources without matching catalog metadata are not listed.</p>")
        if tool == "catalog_status":
            import json
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
            body += "".join("<article><pre>" + escape(json.dumps(item, indent=2, ensure_ascii=False)) +
                            "</pre></article>" for item in result["items"])
        else:
            for record in result["items"]:
                body += (f"<article><h3>{escape(record['title'] or '[Title unavailable in provider metadata]')}</h3><dl>" + "".join(
                    f"<dt>{escape(key.replace('_', ' '))}</dt><dd>{escape(str(record[key]))}</dd>"
                    for key in ("id", "authors", "journal", "publication_date", "citation_url", "aliases",
                                "diseases", "publication_types", "eligibility", "publication_state",
                                "availability", "researcher_review", "istopmm_overlap", "selection_rationale",
                                "relations", "provenance")) + "</dl></article>")
            links = []
            for offset, label in ((max(0, args["offset"] - 5), "Previous"), (result["next_offset"], "Next")):
                if offset is None or label == "Previous" and args["offset"] == 0:
                    continue
                params = dict(request.args)
                params["offset"] = offset
                if store:
                    params["snapshot_id"] = current.release_id
                links.append(f'<a href="/publications?{escape(urlencode(params), quote=True)}">{label}</a>')
            body += "<nav>" + " | ".join(links) + "</nav>"
        if evidence_coverage and not result["items"]:
            body += "<p>No authorized discovery catalog has identifier-matched records for this collection.</p>"
        elif not result["items"]:
            body += "<p>No metadata catalog records in this selection. "
            if catalog_collections:
                body += "Choose All metadata catalogs or a metadata catalog in the Collection filter. "
            else:
                body += "No metadata catalog is configured among the authorized collections. "
            body += 'This does not mean the <a href="/library">source library</a> is empty.</p>'
        return page("Discovery coverage & gaps" if tool == "catalog_status" else "Publication metadata", body)

    def api(endpoint):
        return library_api.respond(snapshot(request), request)

    def connect_page():
        if request.args:
            raise ValueError("Connection page does not accept parameters or credentials")
        web_url = request.url_root.rstrip("/")
        generic_project = bool(store and set(store.current().snapshots) == {"core"})
        sample_query = "YOUR_SEARCH_TERM" if generic_project else "myeloma"
        sample_collection = "core" if generic_project else "istopmm"
        sdk_command = shlex.join([
            "istopmm-mcp-check", "--url", mcp_url, "--web-url", web_url,
            "--token-file", "releases/http-local/research-papers-claude-token", "--query", sample_query,
            *(["--collection", sample_collection] if generic_project else []),
        ])
        def command(action):
            return shlex.join(["make", action, *(["HTTP_URL=" + mcp_url]
                              if mcp_url != "http://127.0.0.1:8000/mcp" else [])])

        connect_command = command("connect")
        prompt = (
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
            "<p>Run these commands from the research-papers project folder. "
            "They manage the Claude Code HTTP connection named <code>istopmm-evidence-http</code> "
            "in this project by default.</p>"
            f"<p>MCP endpoint: <code>{escape(mcp_url)}</code>.</p>"
            "<h3>1. Set up and start</h3>"
            "<pre>make setup\nmake mcp</pre>"
            "<p>Leave MCP running. Use another terminal for the commands below.</p>"
            "<h3>2. Connect</h3>"
            + copy_field("claude-setup", "Claude setup command", connect_command, rows=2)
            + "<p>Creates the local registration and a private token (valid for 24 hours). "
            "Restart Claude Code in the target project and run <code>/mcp</code>; "
            "expect <code>istopmm-evidence-http</code> to connect.</p>"
            "<h3>3. Reset and reconnect</h3>"
            + copy_field("reset", "Reset command", command("reconnect"), rows=2)
            + "<p>Removes the old registration, revokes its token and creates a fresh connection. "
            "Restart Claude Code afterward.</p>"
            "<h3>4. Remove the connection / start empty</h3>"
            + copy_field("remove", "Remove command", command("disconnect"), rows=2)
            + "<p>Removes this named local registration, revokes its token and deletes the managed token file. "
            "You now have no managed connection. Run Connect again when ready.</p>"
            "<p>This does <strong>not</strong> delete papers, evidence, other client connections or the token registry. "
            "Revoked-token records remain so old credentials stay invalid. It does not stop MCP or the website.</p>"
            "<details><summary>Different project or connection</summary>"
            "<p>Append <code>CLIENT_PROJECT=/absolute/path/to/your/project</code> to any command above. "
            "Use the same project, endpoint and token settings for connect, reset and removal.</p>"
            "<pre>make connect CLIENT_PROJECT=/absolute/path/to/your/project\n"
            "make reconnect CLIENT_PROJECT=/absolute/path/to/your/project\n"
            "make disconnect CLIENT_PROJECT=/absolute/path/to/your/project</pre>"
            "<p>Use distinct <code>CLIENT_NAME</code> and <code>CLIENT_TOKEN_FILE</code> values for independent "
            "connections. These commands do not remove a separate stdio registration such as "
            "<code>istopmm-evidence</code>. Disable/remove that entry in your client's MCP configuration separately; "
            "do not delete an entire configuration file containing other servers.</p></details>"
            "<details><summary>Verify retrieval without an AI model</summary>"
            + copy_field("sdk-check", "SDK check command", sdk_command, rows=3)
            + ("<p>Replace YOUR_SEARCH_TERM with a distinctive word found in one of your papers.</p>"
               if generic_project else "")
            + "<p>Expected: <code>passed: true</code> and <code>search_context_reader_agree: true</code>. "
            "Open the reported <code>reader_url</code> to inspect the cited passage. "
            "This page does not probe MCP or read credentials.</p></details>"
            "<details><summary>Integrate an application without MCP</summary>"
            "<p>The same evidence is available through the local read-only JSON API. "
            "Copy <code>examples/library_json.py</code> into your application's directory and run:</p>"
            "<pre>python3 library_json.py --base-url " + escape(web_url)
            + " --collection " + sample_collection + " --query " + sample_query + "</pre>"
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
                         "Find retained evidence about MGUS prevalence and progression risk. Explain how the "
                         "populations, denominators and follow-up differ. Cite the supporting source passages; "
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
        current, pin = browser_snapshot({"query", "offset"})
        offset = int(request.args.get("offset", "0"))
        query = request.args.get("query", "")
        cid = request.args.get("collection", "all" if store else "")
        scope, choices = {}, ""
        if store:
            evidence_ids = [key for key, value in current.snapshots.items() if not collections.is_catalog(value)]
            if cid != "all" and cid not in evidence_ids:
                raise releases.AccessDeniedError("Choose an authorized evidence collection, not a metadata catalog")
            scope = {"snapshot_id": current.release_id, "collections": evidence_ids if cid == "all" else [cid]}
            if not scope["collections"]:
                raise ValueError("No evidence collections available")
            choices = '<label>Collection <select name="collection"><option value="all">All evidence collections</option>'
            choices += "".join(f'<option value="{escape(key, quote=True)}"{" selected" if cid == key else ""}>'
                               f'{escape(current.entries[key]["title"])}</option>' for key in evidence_ids) + "</select></label>"
        if offset < 0 or offset > retrieval.MAX_OFFSET or (offset and not query):
            raise ValueError("Invalid search offset")
        if offset and not request.args.get("snapshot_id" if store else "release_id"):
            raise ValueError("Pagination requires a pinned snapshot")
        body = ('<form method="get"><label>Words or phrase <input name="query" required '
                f'value="{escape(query, quote=True)}"></label> {choices}<button>Search</button></form>'
                "<p>Keyword search of retained extracted text, not an AI answer or semantic search. "
                "Metadata-only publications are not searched as full text. No match does not prove there is no evidence.</p>"
                f"<p>Evidence snapshot: <code>{current.release_id}</code>. "
                "OCR and extraction warnings remain attached to results.</p>")
        if "query" not in request.args:
            return page("Search evidence", body + "<p>Enter words from a topic or passage to begin.</p>")
        response = current.call("search_evidence", {**scope, "query": query, "offset": offset, "limit": 5})
        refs = response.get("collection_context", {}).get("evidence_refs", [])
        for index, result in enumerate(response["result"]["items"]):
            matches = [ref for ref in refs if ref["result_index"] == index] if store else [None]
            representative = matches[0]
            result_cid = representative["collection_id"] if store else ""
            evidence = current.snapshots[result_cid] if store else current
            source = next(source for source in evidence._bundle["sources"]
                          if source["id"] == result["citation"]["source_id"])
            body += ('<article><h3>' + escape(browser_title(current, result_cid, source)) + "</h3>"
                     f"<p>{escape(quality_document(source['id'].rsplit(':', 1)[-1]))} — "
                     f"{escape(quality_location(result['citation']['locator']))}</p>"
                     f"<blockquote>{escape(result['snippet'])}</blockquote>"
                     + source_warnings(result) + "<p>")
            for ref in matches:
                member_cid = ref["collection_id"] if store else ""
                unit = current.snapshots[member_cid]._lookup("units", ref["record_id"]) if store else result
                body += (f'<a href="{escape(reader_url(pin, member_cid, unit), quote=True)}">Read source context'
                         + (f" ({escape(current.entries[member_cid]['title'])})" if store else "") + "</a> ")
            body += "</p></article>"
        if not response["result"]["items"]:
            body += "<p>No matching retained passages. Try fewer or different words, or another collection. This is not proof of no evidence.</p>"
        params = {**pin, "query": query, **({"collection": cid} if store else {})}
        body += "<nav>"
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
        body = (f"<h3>{escape(title)}</h3><p>{escape(role)} — {escape(location)}</p>"
                f"<p>Collection: {escape(cid or 'Single release')}. Evidence release: <code>{evidence.release_id}</code></p>"
                "<p>This is retained extracted text, not an AI interpretation. Original documents are accessed externally. "
                "OCR labels, warnings and empty locations are preserved; retained text may not capture every feature of the original.</p>"
                + source_warnings(unit)
                + "<h3>Retained text</h3>"
                + (f"<pre>{escape(unit['text'])}</pre>" if unit["text"] else
                   "<p>No text retained at this location. Inspect the original; this does not prove it is blank.</p>")
                + f"<p>Showing {len(unit['text'])} of {context['total_characters']} retained characters, starting at offset {offset}.</p>")
        if evidence._lookup("units", unit_id)["truncated"]:
            body += "<p><strong>The stored extraction itself is marked incomplete.</strong></p>"
        body += "<nav>"
        for position, label in ((max(0, offset - 2048) if offset else None, "Previous text slice"),
                                (context["next_offset"], "Next text slice")):
            if position is not None:
                body += f'<a href="{escape(reader_url(pin, cid, unit, offset=position), quote=True)}">{label}</a> '
        units = sorted(evidence.source_units(source_id), key=quality_order)
        position = next(index for index, item in enumerate(units) if item["id"] == unit_id)
        for adjacent, label in ((position - 1, "Previous retained location"), (position + 1, "Next retained location")):
            if 0 <= adjacent < len(units):
                body += f'<a href="{escape(reader_url(pin, cid, units[adjacent]), quote=True)}">{label}</a> '
        body += "</nav><p>Adjacent links follow retained locations in document order; gaps are not invented pages.</p>"
        body += "<h3>Copy and share</h3><p>These fields are selectable even if clipboard access is unavailable. "
        body += "The excerpt includes only this text slice; follow Next text slice to read more.</p>"
        for key, label, value in (("citation", "Citation and stable link", citation), ("excerpt", "Cited excerpt", excerpt)):
            body += copy_field(key, label, value)
        body += '<p id="copy-status" role="status" aria-live="polite"></p>'
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
        return Response(
            'for (const button of document.querySelectorAll("[data-copy]")) {'
            'button.addEventListener("click", async () => {'
            'const field = document.getElementById(button.dataset.copy);'
            'const status = document.getElementById("copy-status");'
            'try { await navigator.clipboard.writeText(field.value); status.textContent = "Copied."; }'
            'catch (error) { field.focus(); field.select();'
            'status.textContent = "Clipboard unavailable. Text selected: use Ctrl+C or Command+C to copy."; }'
            '});}', mimetype="text/javascript")

    def quality_page():
        if store and store.public_quality_root is not None:
            raise releases.AccessDeniedError("Operator quality pages are development-only")
        if set(request.args) - {"collection", "snapshot_id", "finding", "offset", "text_offset", "show", "kind", "unit_id", "unit_offset"}:
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
        form = ('<form method="get">' + form
                + f'<label>Finding <select name="kind">{kind_options}</select></label> '
                + '<button>Show</button></form>')

        def link(extra, label):
            return f'<a href="/quality?{escape(urlencode({**params, **extra}), quote=True)}">{label}</a>'

        body = (form + "<p>Development operator view. All existing evidence remains available here; "
                "this report does not publish or hide documents. Metadata-only/unacquired publications are not "
                "missing-text defects in these acquired source documents.</p>"
                f"<p><strong>{'Ready for operator approval' if report['ready'] else 'Public readiness: Not ready'}</strong>"
                f" &mdash; {report['blockers']} unresolved findings; {report['sources']} source documents; "
                f"{report['units']} retained text units.</p>"
                + "<p>Pending tasks include routine comparisons, not only detected errors. "
                "A completeness review is assigned to every document; it does not mean that document is broken.</p>"
                + "<ul>" + "".join(f"<li>{escape(QUALITY_LABELS[code])}: {count} pending</li>" for code, count in sorted(counts.items()))
                + "</ul>"
                f"<p>Evidence release: <code>{escape(evidence.release_id)}</code></p>"
                f"<p>{escape(report['limitation'])}</p>"
                "<p>Run <code>.venv/bin/python -m istopmm_evidence.quality_cli --help</code> for local "
                "inventory, review, problem and repair commands. Follow <code>docs/data-quality.md</code>. "
                "Reviews require comparison with the original; known errors must be repaired, not waived. "
                "Original files are accessed at their publication source, never served here.</p>"
                + link({"show": "all"}, "All findings (including reviewed)") + " | "
                + link({}, "Pending findings"))
        selected = request.args.get("finding")
        if selected:
            finding = next((item for item in report["findings"] if item["id"] == selected), None)
            if finding is None:
                raise retrieval.NotFoundError("Unknown or stale quality finding")
            body += f"<h3>{escape(finding['title'] or finding['publication_id'])}</h3>"
            status, reason, instructions = quality_guidance(finding)
            body += (f"<p><strong>{escape(QUALITY_LABELS[finding['code']])}</strong> — "
                     f"{escape(quality_document(finding['document_role']))}; "
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
            candidate_path = Path(registry) / "releases" / evidence.release_id if store else Path(release)
            base_command = [".venv/bin/python", "-m", "istopmm_evidence.quality_cli",
                            "--release", str(candidate_path), "--allow-profile", evidence.profile,
                            "--quality-root", str(quality_root)]
            body += "<h3>Record the result</h3><p>This page is read-only. Run these commands from the repository folder. "
            body += "Replace YOUR_NAME and the comparison note; never record a comparison you have not performed.</p>"
            if finding["code"] == "inventory_missing":
                body += "<pre>" + escape(shlex.join(base_command + ["inventory", "--papers", "papers"])) + "</pre>"
            for decision in finding["allowed_decisions"]:
                condition = {"verified": "Only after comparison confirms the text/coverage is correct",
                             "verified_blank": "Only if the original PDF page is genuinely blank",
                             "verified_nontext": "Only if the original location genuinely contains no transcribable text"}[decision]
                command = base_command + ["review", selected, "--decision", decision, "--reviewer", "YOUR_NAME",
                                          "--note", "DESCRIBE_WHAT_YOU_COMPARED_WITH_THE_ORIGINAL"]
                body += f"<p>{escape(condition)}:</p><pre>{escape(shlex.join(command))}</pre>"
            if unit_id:
                command = base_command + ["problem", "--unit-id", unit_id, "--reviewer", "YOUR_NAME",
                                          "--note", "DESCRIBE_THE_ERROR_AND_WHAT_THE_ORIGINAL_SHOWS"]
                body += ("<p>If you find an error in the displayed text, record it first so it remains a blocker until corrected:</p>"
                         f"<pre>{escape(shlex.join(command))}</pre>")
            if unit_id or finding["code"] == "missing_unit":
                repair_target = (["--unit-id", unit_id] if unit_id else
                                 ["--source-id", finding["source_id"], "--locator", json.dumps(finding["locator"])])
                command = base_command + ["repair", "releases/corrected-candidate-NEW", *repair_target,
                                          "--text-file", "releases/corrected-text.txt", "--reviewer", "YOUR_NAME",
                                          "--note", "DESCRIBE_THE_CORRECTION_AND_ORIGINAL_COMPARISON"]
                body += ("<p>If text is missing or incorrect: first save the correct transcription in "
                         "<code>releases/corrected-text.txt</code>. Preserve the full unit text, not just the changed word. "
                         "Choose a new, unused output folder, then run:</p><pre>" + escape(shlex.join(command)) + "</pre>"
                         "<p>This creates a new candidate; it does not update the active library. Check/review the new candidate "
                         "before explicitly activating it. See <code>docs/data-quality.md</code> for the complete sequence.</p>")
            body += "<details><summary>Technical identifiers (for troubleshooting)</summary><dl>" + "".join(
                f"<dt>{escape(key)}</dt><dd>{escape(json.dumps(value, ensure_ascii=False))}</dd>"
                for key, value in finding.items()) + "</dl></details>"
        else:
            findings = [item for item in report["findings"]
                        if (show == "all" or not item["resolved"]) and (not kind or item["code"] == kind)]
            for finding in findings[offset:offset + 25]:
                body += ('<article class="source-entry"><details><summary>'
                         + link({"finding": finding["id"]}, escape(finding["title"] or finding["publication_id"]))
                         + f" &mdash; {escape(quality_document(finding['document_role']))}; "
                         + escape(quality_location(finding["locator"])) + f"; {escape(QUALITY_LABELS[finding['code']])}"
                         + (" (reviewed)" if finding["resolved"] else "") + "</summary>"
                         + f"<p>{escape(quality_guidance(finding)[0])} {escape(quality_guidance(finding)[1])}</p>"
                         + f"<p>{escape(finding['next_action'])}</p>"
                         + f"<p>Finding: <code>{finding['id']}</code></p></details></article>")
            if not findings:
                body += "<p>No pending findings.</p>"
            if offset:
                body += link({"offset": max(0, offset - 25), "show": show, "kind": kind}, "Previous")
            if offset + 25 < len(findings):
                body += " | " + link({"offset": offset + 25, "show": show, "kind": kind}, "Next")
        return page("Data quality", body)

    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    for path, endpoint, view in (
            ("/api/<endpoint>", "api", api), ("/", "overview", overview),
            ("/library", "library", library), ("/sources", "sources", sources),
            ("/publications", "publications", catalog_page), ("/coverage", "coverage", catalog_page),
            ("/quality", "quality", quality_page),
            ("/search", "search", search_page), ("/read", "read", read_page),
            ("/reader.js", "reader_script", reader_script),
            ("/connect", "connect", connect_page),
            ("/style.css", "stylesheet", stylesheet)):
        app.add_url_rule(path, endpoint, view, methods=["GET"])
    app.add_url_rule("/library/provide", "provide", provide, methods=["POST"])
    app.config["MAX_CONTENT_LENGTH"] = 65 * 1024 * 1024
    if can_provide() and (data_root / "releases/provided-sources/ledger.json").exists():
        from .build import provided_sources
        if provided_sources.pending(data_root):
            start_provider()

    @app.before_request
    def local_boundary():
        host = request.headers.get("host")
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if host not in allowed:
            return Response("Untrusted host", status=421, mimetype="text/plain")
        if request.headers.get("origin") not in (None, f"http://{host}"):
            return Response("Untrusted origin", status=403, mimetype="text/plain")

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
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Referrer-Policy": "no-referrer",
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
    args = parser.parse_args()
    try:
        app = create_app(release=args.release, registry=args.registry, profiles=set(args.allow_profile),
                         allowed_collections=args.allow_collection, default_collections=args.default_collection,
                         port=args.port, mcp_url=args.mcp_url, data_root=args.data_root, quality_root=args.quality_root)
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        diagnostic("web_startup_failed", exception_type=type(error).__name__)
        return 1
    diagnostic("web_starting", url=f"http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
