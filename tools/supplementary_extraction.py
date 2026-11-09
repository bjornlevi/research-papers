"""Source-linked PDF and OOXML supplement extraction; no inferred clinical results."""

import csv
import hashlib
import html
import json
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

import pymupdf

if __package__:
    from . import evidence
else:
    import evidence

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
ROOT = evidence.ROOT


def read_xml(archive, member):
    return ET.fromstring(archive.read(member))


def word_text(node):
    output = []
    for child in node.iter():
        if child.tag == W + "t":
            output.append(child.text or "")
        elif child.tag == W + "tab":
            output.append("\t")
        elif child.tag in (W + "br", W + "cr"):
            output.append("\n")
        elif child.tag == W + "p":
            output.append("\n")
    return "".join(output).strip()


def relationships(archive, part):
    relpart = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if relpart not in archive.namelist():
        return {}
    return {
        rel.get("Id"): {
            "target": rel.get("Target"), "external": rel.get("TargetMode") == "External",
            "resolved": posixpath.normpath(posixpath.join(posixpath.dirname(part), rel.get("Target"))).lstrip("/")
            if not rel.get("Target", "").startswith("/") else rel.get("Target").lstrip("/"),
        } for rel in read_xml(archive, relpart)
    }


def extract_package_assets(archive, folder):
    assets = []
    for name in archive.namelist():
        if "/media/" not in name and not ("/charts/" in name and name.endswith(".xml")):
            continue
        if name.endswith("/"):
            continue
        target = folder / "assets" / name
        if not target.resolve().is_relative_to((folder / "assets").resolve()):
            raise ValueError("Unsafe embedded asset path")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = archive.read(name)
        target.write_bytes(data)
        assets.append({
            "package_member": name, "path": target.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "kind": "embedded_media" if "/media/" in name else "chart_xml",
            "verification_status": "machine_extracted", "interpretation": "not_curated",
        })
    return assets


def csv_value(value):
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def write_table(folder, name, rows):
    target = folder / "tables" / (name + ".csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Neutralize spreadsheet formulas in this convenience view; JSON preserves exact text.
    with target.open("w", newline="") as handle:
        csv.writer(handle).writerows([
            [csv_value(value) for value in row]
            for row in rows])
    return target.relative_to(ROOT).as_posix()


def extract_docx(archive, folder):
    body = read_xml(archive, "word/document.xml").find(W + "body")
    if body is None:
        raise ValueError("Word document body missing")
    rels = relationships(archive, "word/document.xml")
    units, tables = [], []
    for number, node in enumerate(body, 1):
        text = word_text(node)
        table = None
        if node.tag == W + "tbl":
            cells = []
            rows = []
            for row_number, row in enumerate(node.findall(W + "tr"), 1):
                row_text = []
                for cell_number, cell in enumerate(row.findall(W + "tc"), 1):
                    value = word_text(cell)
                    span = cell.find(W + "tcPr/" + W + "gridSpan")
                    merge = cell.find(W + "tcPr/" + W + "vMerge")
                    cells.append({"row": row_number, "cell": cell_number, "text": value,
                                  "grid_span": int(span.get(W + "val", "1")) if span is not None else 1,
                                  "vertical_merge": merge.get(W + "val", "continue") if merge is not None else None})
                    row_text.append(value)
                rows.append(row_text)
            table = {"id": f"table-{number:04d}", "body_element": number, "cells": cells,
                     "csv_path": write_table(folder, f"table-{number:04d}", rows),
                     "coverage": "Stored cells, not expanded into a rectangular grid; merged cells and nested tables require source XML.",
                     "source_xml": "word/document.xml"}
            tables.append(table)
        refs = []
        for blip in node.iter(A + "blip"):
            rid = blip.get(R + "embed") or blip.get(R + "link")
            if rid and rid in rels:
                ref = dict(rels[rid])
                if not ref["external"]:
                    target = folder / "assets" / ref["resolved"]
                    if target.is_file():
                        ref["path"] = target.relative_to(ROOT).as_posix()
                refs.append(ref)
        if text or refs:
            units.append({"location": {"body_element": number, "kind": "table" if table else "paragraph"},
                          "text": text, "embedded_graphics": refs,
                          "table_id": table["id"] if table else None})
    # Headers, footers, notes and comments are retained separately from body order.
    for member in archive.namelist():
        if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", member):
            text = word_text(read_xml(archive, member))
            if text:
                units.append({"location": {"package_member": member}, "text": text})
    evidence.write_json(folder / "tables.json", tables)
    return units, {"word_tables": len(tables)}


def string_text(node):
    return "".join(t.text or "" for t in node.iter(S + "t"))


def extract_xlsx(archive, folder):
    shared = [string_text(node) for node in read_xml(archive, "xl/sharedStrings.xml")] \
        if "xl/sharedStrings.xml" in archive.namelist() else []
    workbook = read_xml(archive, "xl/workbook.xml")
    rels = relationships(archive, "xl/workbook.xml")
    units, sheets = [], []
    for sheet in workbook.find(S + "sheets"):
        name = sheet.get("name")
        member = rels[sheet.get(R + "id")]["resolved"]
        xml = read_xml(archive, member)
        sheet_number = len(sheets) + 1
        cells, rows = [], []
        for row in xml.findall(S + "sheetData/" + S + "row"):
            row_cells = []
            for cell in row.findall(S + "c"):
                kind = cell.get("t", "n")
                raw = cell.findtext(S + "v")
                formula = cell.find(S + "f")
                if kind == "s":
                    value = shared[int(raw)] if raw is not None else None
                elif kind == "inlineStr":
                    inline = cell.find(S + "is")
                    value = string_text(inline) if inline is not None else None
                else:
                    value = raw
                record = {"ref": cell.get("r"), "type": kind, "style_index": cell.get("s"),
                          "value": value, "raw_value": raw,
                          "formula": formula.text if formula is not None else None,
                          "formula_attributes": dict(formula.attrib) if formula is not None else None}
                cells.append(record)
                row_cells.append(record)
            text = "\n".join(f"{c['ref']}: {c['value'] if c['value'] is not None else '[no cached value]'}"
                             + (f" [formula: {c['formula']}]" if c["formula"] else "") for c in row_cells)
            if row_cells:
                units.append({"location": {"worksheet": name, "worksheet_state": sheet.get("state", "visible"),
                                           "row": int(row.get("r")), "row_hidden": row.get("hidden", "0"),
                                           "package_member": member},
                              "text": name + "\n" + text})
                rows.extend([[c["ref"], c["value"] if c["value"] is not None else "",
                              c["formula"] or "", c["type"], c["style_index"] or ""] for c in row_cells])
        data = {"name": name, "state": sheet.get("state", "visible"), "package_member": member,
                "cells": cells, "merged_ranges": [m.get("ref") for m in xml.findall(S + "mergeCells/" + S + "mergeCell")],
                "csv_path": write_table(folder, f"sheet-{sheet_number:03d}",
                                       [["cell", "stored_value", "formula", "type", "style_index"]] + rows)}
        evidence.write_json(folder / "tables" / f"sheet-{sheet_number:03d}.json", data)
        sheets.append({k: v for k, v in data.items() if k != "cells"})
    evidence.write_json(folder / "worksheets.json", sheets)
    return units, {"worksheets": len(sheets), "date_system": workbook.find(S + "workbookPr").get("date1904", "0")
                  if workbook.find(S + "workbookPr") is not None else "0"}


def extract_office(source, folder, metadata):
    with zipfile.ZipFile(source) as archive:
        # Preserve exact XML, relationships and layout/styles to verify flattened views.
        for member in archive.namelist():
            if not member.endswith((".xml", ".rels")):
                continue
            target = folder / "package" / member
            if not target.resolve().is_relative_to((folder / "package").resolve()):
                raise ValueError("Unsafe package member path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
        assets = extract_package_assets(archive, folder)
        if source.suffix == ".docx":
            units, coverage = extract_docx(archive, folder)
        else:
            units, coverage = extract_xlsx(archive, folder)
    provenance = {k: metadata[k] for k in ("paper_id", "doi", "document_id", "source_path", "source_sha256")}
    if source.suffix == ".docx":
        tables = json.loads((folder / "tables.json").read_text())
        for table in tables:
            table.update(provenance, verification_status="machine_extracted", human_verification="not_reviewed")
            evidence.write_json(folder / "tables" / (table["id"] + ".json"), table)
        evidence.write_json(folder / "tables.json", tables)
    else:
        for sidecar in (folder / "tables").glob("sheet-*.json"):
            table = json.loads(sidecar.read_text())
            table.update(provenance, verification_status="machine_extracted", human_verification="not_reviewed")
            evidence.write_json(sidecar, table)
    for asset in assets:
        asset.update(provenance)
    evidence.write_json(folder / "assets.json", assets)
    for number, unit in enumerate(units, 1):
        unit.update(
            unit_id=f"{metadata['paper_id']}:{metadata['document_id']}:u{number}",
            paper_id=metadata["paper_id"], document_id=metadata["document_id"],
            source_path=metadata["source_path"], source_sha256=metadata["source_sha256"],
            source_metadata=(folder / "metadata.json").relative_to(ROOT).as_posix(),
            pdf_page=None, source_page_image=None, verification_status="machine_extracted",
        )
    evidence.write_jsonl(folder / "units.jsonl", units)
    if __package__:
        from .text_formatting import office_text
    else:
        from text_formatting import office_text
    office_text(folder, units)
    return units, {**coverage, "search_units": len(units), "embedded_assets": len(assets),
                   "pagination": "Not assigned; cite body element or worksheet/cell, not invented PDF pages",
                   "tables": "Machine-extracted, not source-checked numerical findings",
                   "csv_policy": "Formula-leading text prefixed with apostrophe for spreadsheet safety; JSON/XML retain original values",
                   "workbook_values": "Stored values and cached formula results; no recalculation or date conversion. Styles and formulas retained in package XML."}


def build_supplements(inventory, catalog):
    pages, units, documents = [], [], []
    for parent in catalog:
        acquired = [f for f in inventory.get(parent["paper_id"], {}).get("files", []) if f["status"] == "acquired"]
        parent["supplement_files_extracted"] = len(acquired)
        for file in acquired:
            source = ROOT / file["path"]
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if digest != file["sha256"]:
                raise ValueError(f"Supplement source changed: {source}")
            doc_id = re.search(r"(supplement-\d+)\.", source.name).group(1)
            folder = evidence.EVIDENCE / "papers" / parent["paper_id"] / "supplements" / doc_id
            folder.mkdir(parents=True, exist_ok=True)
            metadata = {
                "schema_version": 1, "paper_id": parent["paper_id"], "doi": parent["doi"],
                "document_id": doc_id, "label": file["label"], "original_name": file["original_name"],
                "source_path": file["path"], "source_sha256": digest, "format": file["format"],
                "discovery_url": file["discovery_url"], "source_urls": file["urls"],
                "verification_status": "machine_extracted", "human_verification": "not_reviewed",
                "structured_findings": "not_curated", "rights": "No new reuse permission inferred; consult original source license.",
            }
            for key in ("download_method", "imported_from", "imported_at",
                        "source_identity_evidence", "identified_original_name"):
                if key in file:
                    metadata[key] = file[key]
            if file["format"] == "pdf":
                # The pilot model supplement has already been extracted in this build.
                if parent["inventory_number"] == 23 and doc_id == "supplement-01":
                    extracted = evidence.load_jsonl(folder / "pages.jsonl")
                else:
                    with pymupdf.open(source) as document:
                        extracted, _ = evidence.extract_pages(document, parent["paper_id"], digest, folder,
                                                              file["path"], doc_id, allow_ocr=True)
                    pages.extend(extracted)
                metadata.update(pdf_pages=len(extracted), coverage={
                    "pages": "All pages rendered, including figures/tables; no curve digitization",
                    "text": "Native PDF text; English OCR on pages with no native text only",
                    "ocr_pages": [p["pdf_page"] for p in extracted if p["text_extraction_method"] == "tesseract_ocr"],
                    "no_text_pages": [p["pdf_page"] for p in extracted if p["text_extraction_method"] == "no_text_detected_after_ocr"],
                    "blocks": "Native text blocks only; OCR text does not have block geometry",
                    "geometry": "Original boxes retained; extends_beyond_page flags source text spilling beyond page bounds. Use rendered page as authority.",
                })
            elif file["format"] in ("docx", "xlsx"):
                extracted, coverage = extract_office(source, folder, metadata)
                units.extend(extracted)
                metadata["coverage"] = coverage
            else:
                raise ValueError(f"Unsupported supplemental extraction format: {file['format']}")
            evidence.write_json(folder / "metadata.json", metadata)
            documents.append({**metadata, "evidence_path": folder.relative_to(ROOT).as_posix()})
    evidence.write_jsonl(evidence.EVIDENCE / "supplements.jsonl", documents)
    links = ["<!doctype html><html lang='en'><meta charset='utf-8'>"
             "<title>Extracted supplements</title><h1>Extracted supplements</h1>"
             "<p>Machine-extracted content, not curated findings. "
             "<a href='README.md#supplement-extraction'>Guide</a></p><ul>"]
    for record in documents:
        base = record["evidence_path"].removeprefix("evidence/")
        links.append(f"<li>{html.escape(record['doi'])} / {record['document_id']}: "
                     f"{html.escape(record['label'])} "
                     f"<a href='{base}/text.md'>Text</a> | <a href='{base}/metadata.json'>Metadata</a> | "
                     f"<a href='../{html.escape(record['source_path'], quote=True)}'>Original</a></li>")
    (evidence.EVIDENCE / "supplements.html").write_text("\n".join(links) + "</ul></html>\n")
    return pages, units


def validate_supplements(catalog):
    inventory = json.loads((ROOT / "papers/supplements/inventory.json").read_text())
    expected = {f["path"]: (parent["paper_id"], f) for parent in inventory
                for f in parent["files"] if f["status"] == "acquired"}
    records = evidence.load_jsonl(evidence.EVIDENCE / "supplements.jsonl")
    if len(records) != len(expected) or {r["source_path"] for r in records} != set(expected):
        raise ValueError("Supplement extraction coverage differs from acquired inventory")
    pages, units = [], []
    for record in records:
        pid, file = expected[record["source_path"]]
        if record["paper_id"] != pid or record["source_sha256"] != file["sha256"]:
            raise ValueError("Supplement extraction provenance mismatch")
        folder = ROOT / record["evidence_path"]
        if json.loads((folder / "metadata.json").read_text()) != {k: v for k, v in record.items() if k != "evidence_path"}:
            raise ValueError("Supplement metadata differs from extraction manifest")
        source = ROOT / record["source_path"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != record["source_sha256"]:
            raise ValueError("Supplement PDF/Office source changed")
        if record["format"] == "pdf":
            extracted = evidence.validate_document(folder, pid, record["document_id"], record["source_path"],
                                                  record["source_sha256"], record["pdf_pages"])
            pages.extend(extracted)
        else:
            extracted = evidence.load_jsonl(folder / "units.jsonl")
            if len(extracted) != record["coverage"]["search_units"]:
                raise ValueError("Office extraction unit coverage mismatch")
            for unit in extracted:
                if (unit["paper_id"], unit["document_id"], unit["source_sha256"], unit["source_path"]) != (
                        pid, record["document_id"], record["source_sha256"], record["source_path"]):
                    raise ValueError("Office unit provenance mismatch")
            units.extend(extracted)
            if record["format"] == "docx":
                tables = json.loads((folder / "tables.json").read_text())
                if len(tables) != record["coverage"]["word_tables"]:
                    raise ValueError("Word table coverage mismatch")
            else:
                tables = [json.loads(p.read_text()) for p in sorted((folder / "tables").glob("sheet-*.json"))]
                if len(tables) != record["coverage"]["worksheets"]:
                    raise ValueError("Worksheet coverage mismatch")
            for table in tables:
                if (table["source_path"], table["source_sha256"], table["paper_id"], table["document_id"]) != (
                        record["source_path"], record["source_sha256"], pid, record["document_id"]):
                    raise ValueError("Office table provenance mismatch")
                if record["format"] == "docx":
                    rows = {}
                    for cell in table["cells"]:
                        rows.setdefault(cell["row"], []).append(cell["text"])
                    expected_rows = list(rows.values())
                else:
                    expected_rows = [["cell", "stored_value", "formula", "type", "style_index"]] + [
                        [c["ref"], c["value"] if c["value"] is not None else "",
                         c["formula"] or "", c["type"], c["style_index"] or ""] for c in table["cells"]]
                with (ROOT / table["csv_path"]).open(newline="") as handle:
                    if list(csv.reader(handle)) != [[csv_value(v) for v in row] for row in expected_rows]:
                        raise ValueError("Office CSV differs from structured cells")
            with zipfile.ZipFile(source) as archive:
                for member in archive.namelist():
                    if member.endswith((".xml", ".rels")) and (folder / "package" / member).read_bytes() != archive.read(member):
                        raise ValueError("Preserved OOXML differs from source")
                for asset in json.loads((folder / "assets.json").read_text()):
                    data = (ROOT / asset["path"]).read_bytes()
                    if data != archive.read(asset["package_member"]) or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                        raise ValueError("Embedded graphic differs from original package")
    for parent in catalog:
        if parent["supplement_files_extracted"] != sum(r["paper_id"] == parent["paper_id"] for r in records):
            raise ValueError("Catalog supplement extraction count mismatch")
    return pages, units
