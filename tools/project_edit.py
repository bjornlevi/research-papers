"""Publish operator corrections as new private evidence snapshots."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit

from research_library import collections, releases
from . import literature_update, project_library, project_text


TABLE_FIELDS = ("title", "headers", "units", "rows", "footnotes")
BIB_FIELDS = ("title", "year", "acquired_from", "acquisition_method", "acquired_at")
CORRECTION_CAVEAT = "Operator-corrected transcription; not independent scientific validation. See correction audit."
DOCUMENT_TEXT_LIMIT = 10000000


def document_units(bundle, source_id):
    return sorted((unit for unit in bundle["units"] if unit["citation"]["source_id"] == source_id),
                  key=lambda unit: (unit["citation"]["locator"].get("pdf_page", 0), unit["id"]))


def page_marker(unit):
    return f'[[PAGE {unit["citation"]["locator"]["pdf_page"]}: {unit["id"]}]]'


def document_draft(units):
    return "\n\n".join(page_marker(unit) + "\n" + unit["text"] for unit in units)


def document_pages(text, units):
    if not isinstance(text, str) or len(text) > DOCUMENT_TEXT_LIMIT:
        raise ValueError("Document text exceeds the 10,000,000 character editing limit")
    text = text.replace("\r\n", "\n")
    markers = list(re.finditer(r"^\[\[PAGE [^\n]*\]\]\n", text, re.MULTILINE))
    if (not units or not markers or markers[0].start() != 0
            or [match.group().removesuffix("\n") for match in markers] != [page_marker(unit) for unit in units]):
        raise ValueError("Keep every page-boundary marker unchanged and in order; your edits have not been saved")
    pages = {}
    for index, (unit, marker) in enumerate(zip(units, markers)):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        page_text = text[marker.end():end]
        if index + 1 < len(markers):
            if not page_text.endswith("\n\n"):
                raise ValueError("Keep two newlines before each page-boundary marker; your edits have not been saved")
            page_text = page_text[:-2]
        if len(page_text) > 1000000:
            raise ValueError("Page text exceeds the 1,000,000 character editing limit")
        pages[unit["id"]] = page_text
    return pages


def paragraph_draft(root, *, snapshot_id, source_id, unit_id):
    import pymupdf
    from . import literature_ingest

    root = Path(root)
    current = project_text.editorial_store(root).current()
    if current.release_id != snapshot_id:
        raise ValueError("Evidence changed while editing; reload the editor before repairing layout")
    bundle = current.snapshots["core"]._bundle
    source = next((item for item in bundle["sources"] if item["id"] == source_id), None)
    unit = next((item for item in bundle["units"] if item["id"] == unit_id
                 and item["citation"]["source_id"] == source_id), None)
    if source is None or unit is None or source["format"] != "pdf" or unit["citation"]["locator"]["kind"] != "pdf":
        raise ValueError("Select an extracted PDF page before repairing layout")
    path = releases.safe_file(root / "papers", source["sha256"] + ".pdf")
    if path.stat().st_size > literature_ingest.POLICY["max_pdf_bytes"]:
        raise ValueError("PDF exceeds extraction size limit")
    data = path.read_bytes()
    if releases.sha256(data) != source["sha256"]:
        raise ValueError("Stored PDF checksum differs; layout has not been repaired")
    with pymupdf.open(stream=data, filetype="pdf") as document:
        number = unit["citation"]["locator"]["pdf_page"]
        if document.is_encrypted or not 1 <= number <= len(document):
            raise ValueError("Original PDF page is unavailable")
        blocks = [block for block in document[number - 1].get_text("blocks", sort=False)
                  if block[6] == 0 and block[4].strip()]
        if len(document) > literature_ingest.POLICY["max_pages"]:
            raise ValueError("PDF exceeds extraction page limit")
        words = set()
        for page in document:
            native = page.get_text("text", sort=False)
            if len(native) > literature_ingest.POLICY["max_page_characters"]:
                raise ValueError("PDF page exceeds extraction text limit")
            words.update(literature_ingest.native_words(native))
    text = literature_ingest.paragraph_text(blocks, words)
    if sum(character.isalnum() for character in text) < literature_ingest.POLICY["sparse_threshold_alphanumeric"]:
        raise ValueError("Too little native PDF text for layout repair; keep the OCR text and correct it manually")
    if len(text) > 1000000:
        raise ValueError("Page text exceeds the 1,000,000 character editing limit")
    return text


def _url(value):
    if value is None:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Source and acquisition URLs must be HTTP(S) without credentials")
    return value


def history(root, source_id):
    root = Path(root)
    events = {}
    paths = [*(root / "runs").glob("correction-*/correction.json"), *(root / "corrections").glob("*.json")]
    for path in sorted(paths):
        event = releases.read_json(releases.safe_file(path.parent, path.name).read_bytes())
        if event["source_id"] == source_id:
            events[releases.sha256(releases.encode(event))] = event
    return sorted(events.values(), key=lambda event: event["at"], reverse=True)


def correct(root, *, snapshot_id, source_id, unit_id, text, bibliography, source_url,
            tables, reviewer, reason, checked, missing_locator=None, whole_document=False,
            track_markdown=True):
    root = Path(root)
    reviewer, reason = reviewer.strip(), reason.strip()
    if not reviewer or len(reviewer) > 200 or not reason or len(reason) > 2000:
        raise ValueError("Provide a reviewer name (up to 200 characters) and correction reason (up to 2000)")
    if whole_document and (not isinstance(text, str) or len(text) > DOCUMENT_TEXT_LIMIT):
        raise ValueError("Document text exceeds the 10,000,000 character editing limit")
    if not whole_document and (not isinstance(text, str) or len(text) > 1000000):
        raise ValueError("Page text exceeds the 1,000,000 character editing limit")
    if whole_document and missing_locator is not None:
        raise ValueError("Add missing pages through the quality review before editing the whole document")
    if (not isinstance(bibliography, dict) or set(bibliography) != set(BIB_FIELDS)
            or any(not isinstance(value, str) for value in bibliography.values())):
        raise ValueError("Invalid paper metadata fields")
    bibliography = {key: value.strip() or None for key, value in bibliography.items()}
    if not bibliography["title"]:
        raise ValueError("A paper title is required")
    if bibliography["year"] and not re.fullmatch(r"\d{4}", bibliography["year"]):
        raise ValueError("Publication year must be four digits or empty")
    _url(bibliography["acquired_from"])
    source_url = _url(source_url.strip() or None)
    if bibliography["acquired_at"]:
        try:
            datetime.fromisoformat(bibliography["acquired_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Acquisition time must be an ISO date/time or empty") from error
    if not isinstance(tables, list) or len(tables) > 100:
        raise ValueError("Tables must be a list of at most 100 tables for this correction")
    with literature_update.writer_lock(root):
        project_text.ensure_editorial(root)
        registry = project_text.editorial_store(root)
        current = registry.current()
        if current.release_id != snapshot_id:
            raise ValueError("Evidence changed while editing; reload the editor before saving")
        original = current.snapshots["core"]
        bundle = deepcopy(original._bundle)
        source = next((item for item in bundle["sources"] if item["id"] == source_id), None)
        unit = next((item for item in bundle["units"] if item["id"] == unit_id
                     and item["citation"]["source_id"] == source_id), None)
        adding = missing_locator is not None
        if adding:
            from research_library import quality
            quality.locator_key(missing_locator)
            if (source is None or source["format"] != "pdf" or missing_locator["kind"] != "pdf"
                    or unit_id or not text.strip()
                    or any(item["citation"]["source_id"] == source_id
                           and item["citation"]["locator"] == missing_locator for item in bundle["units"])):
                raise ValueError("Select a missing PDF page and provide its complete transcription")
            unit_id = source_id + ":quality-" + quality.locator_key(missing_locator)[:16]
            unit = {
                "id": unit_id, "text": "", "truncated": False,
                "citation": {"source_id": source_id, "doi": source["doi"], "source_sha256": source["sha256"],
                             "source_url": source["url"], "provenance": source["provenance"],
                             "availability": "metadata_only", "locator": deepcopy(missing_locator)},
                "verification_status": "machine_extracted", "human_verification": "not_reviewed",
                "caveats": [], "source_issue_ids": [],
            }
            bundle["units"].append(unit)
        if source is None or unit is None:
            raise ValueError("Select an extracted page belonging to this source")
        if source["format"] != "pdf":
            raise ValueError("This editor supports project PDF evidence only")
        locator = unit["citation"]["locator"]
        selected_units = document_units(bundle, source_id) if whole_document else [unit]
        page_texts = document_pages(text, selected_units) if whole_document else {unit_id: text}
        old_tables = {table["id"]: table for table in bundle["tables"]
                      if table["citation"]["source_id"] == source_id
                      and (whole_document or table["citation"]["locator"] == locator)}
        before = {"text": (document_draft(selected_units) if whole_document else None if adding else unit["text"]),
                  "bibliography": deepcopy(source.get("bibliography")),
                  "source_url": source["url"], "tables": list(deepcopy(old_tables).values())}
        new_tables = []
        seen = set()
        for value in tables:
            if not isinstance(value, dict) or set(value) != {"id", *TABLE_FIELDS, *(("unit_id",) if whole_document else ())}:
                raise ValueError("Each table needs id, title, headers, units, rows and footnotes"
                                 + (", plus unit_id for document corrections" if whole_document else ""))
            table_unit = unit
            if whole_document:
                table_unit = next((item for item in selected_units if item["id"] == value["unit_id"]), None)
                if table_unit is None:
                    raise ValueError("Choose a retained page unit_id for every structured table")
            identifier = value["id"]
            if not isinstance(identifier, str):
                raise ValueError("Table ID must be a string; use an empty string for a new table")
            if identifier:
                if identifier not in old_tables or identifier in seen:
                    raise ValueError("Table ID is duplicated or belongs to a different source/page")
                table = deepcopy(old_tables[identifier])
                if whole_document and table["citation"]["locator"] != table_unit["citation"]["locator"]:
                    raise ValueError("Keep existing tables on their original page")
                seen.add(identifier)
            else:
                identifier = table_unit["id"] + ":table:" + releases.sha256(releases.encode(value))[:16]
                table = {key: deepcopy(table_unit[key]) for key in
                         ("citation", "verification_status", "human_verification", "caveats")}
                table.update(id=identifier, truncated=False, next_offset=None)
            changed = not value["id"] or any(table[key] != value[key] for key in TABLE_FIELDS)
            table.update({key: value[key] for key in TABLE_FIELDS})
            if changed:
                if CORRECTION_CAVEAT not in table["caveats"]:
                    table["caveats"].append(CORRECTION_CAVEAT)
                table.update(verification_status="machine_extracted", human_verification="not_reviewed")
            new_tables.append(table)
        source.update(bibliography=bibliography, url=source_url)
        for page_unit in selected_units:
            page_text = page_texts[page_unit["id"]]
            if page_text != page_unit["text"] or page_unit["truncated"]:
                page_unit.update(text=page_text, truncated=False,
                                 verification_status="machine_extracted", human_verification="not_reviewed")
                if CORRECTION_CAVEAT not in page_unit["caveats"]:
                    page_unit["caveats"].append(CORRECTION_CAVEAT)
        bundle["tables"] = [table for table in bundle["tables"] if table["id"] not in old_tables] + new_tables
        # Source URLs describe the current corrected metadata; source IDs, hashes and locators stay immutable.
        def update_citations(value):
            if isinstance(value, dict):
                if "source_id" in value and value["source_id"] == source_id:
                    value["source_url"] = source_url
                for child in value.values():
                    update_citations(child)
            elif isinstance(value, list):
                for child in value:
                    update_citations(child)
        update_citations(bundle)
        if bundle == original._bundle:
            raise ValueError("No data changed")
        audit_note = " Admin-edited draft; original extraction retained in prior snapshots; not published for retrieval."
        if audit_note not in source["provenance"]:
            source["provenance"] += audit_note
        def annotate(value):
            if isinstance(value, dict):
                if value.get("source_id") == source_id and "provenance" in value:
                    if audit_note not in value["provenance"]:
                        value["provenance"] += audit_note
                for child in value.values():
                    annotate(child)
            elif isinstance(value, list):
                for child in value:
                    annotate(child)
        annotate(bundle)
        releases.validate_bundle(bundle)
        now = datetime.now(timezone.utc).isoformat()
        event = {"schema_version": 1, "at": now, "reviewer": reviewer, "reason": reason,
                 "status": "draft",
                 "source_id": source_id, "source_sha256": source["sha256"], "unit_id": unit_id,
                 "parent_snapshot": current.release_id, "parent_release": original.release_id,
                 "before": before,
                 "after": {"text": document_draft(selected_units) if whole_document else unit["text"],
                           "bibliography": bibliography, "source_url": source_url,
                           "tables": new_tables}}
        if whole_document:
            event.update(scope="document", unit_ids=[item["id"] for item in selected_units])
        runs = root / "runs"
        runs.mkdir(exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix="correction-", dir=runs))
        activated = False
        try:
            release_id = releases.export(bundle, run / "evidence", profile="private_context",
                                         authorization=original._manifest["authorization"])
            registry.releases.install(run / "evidence")
            project_library.write_text_views(run / "text", bundle)
            event["release"] = release_id
            collections.atomic_json(run, "correction.json", event)
            document = deepcopy(current.document)
            entry = next(item for item in document["collections"] if item["id"] == "core")
            entry.update(release_id=release_id, version="corrected-" + release_id[:16], updated_at=now[:10])
            # Registry publication is the last operation: failed staging leaves active evidence unchanged.
            snapshot = registry.publish(document)
            activated = True
            if track_markdown:
                project_text.export_git_markdown(root, bundle, source_id=source_id,
                                                 kind="draft", release_id=release_id)
            return {"snapshot": snapshot, "release": release_id, "audit": str(run / "correction.json")}
        finally:
            if not activated:
                shutil.rmtree(run)
