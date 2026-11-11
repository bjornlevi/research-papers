#!/usr/bin/env python3
"""Build and query all main papers and acquired supplements with source provenance."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import sys
import unicodedata
import re
import subprocess
from contextlib import closing

import pymupdf

from research_library import retrieval

if __package__:
    from .workspace import workspace_root
else:
    from workspace import workspace_root

ROOT = workspace_root()
EVIDENCE = ROOT / "evidence"
SCHEMA_VERSION = 1


def paper_id(doi):
    return retrieval.paper_id(doi)


def normalize(text):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in records))


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def extract_pages(document, pid, digest, folder, source_path, document_id="main", allow_ocr=False):
    pages = []
    blocks = []
    for page_number, page in enumerate(document, 1):
        text = page.get_text("text", sort=False)
        method = "native_pdf_text"
        if not text.strip() and not allow_ocr:
            raise ValueError(f"{pid}/{document_id} page {page_number} has no text; OCR required, not silently skipped")
        rendered = folder / "pages" / f"page-{page_number:03d}.png"
        rendered.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(rendered)
        if not text.strip():
            image = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False).tobytes("png")
            response = subprocess.run(["tesseract", "stdin", "stdout", "-l", "eng"],
                                      input=image, capture_output=True)
            if response.returncode:
                raise ValueError(f"OCR failed for {pid}/{document_id}/{page_number}: "
                                 + response.stderr.decode("utf-8", errors="replace"))
            text = response.stdout.decode("utf-8")
            method = "tesseract_ocr" if text.strip() else "no_text_detected_after_ocr"
        pages.append({
            "paper_id": pid, "document_id": document_id, "pdf_page": page_number,
            "width_pt": page.rect.width, "height_pt": page.rect.height, "text": text,
            "page_image": rendered.relative_to(folder).as_posix(),
            "repository_page_image": rendered.relative_to(ROOT).as_posix(),
            "source_pdf_sha256": digest, "source_pdf_path": source_path,
            "text_extraction_method": method,
        })
        for block_number, block in enumerate(page.get_text("blocks", sort=False)):
            if block[6] != 0 or not block[4].strip():
                continue
            blocks.append({
                "block_id": f"{pid}:{document_id}:p{page_number}:b{block_number}",
                "paper_id": pid, "document_id": document_id, "pdf_page": page_number,
                "bbox_pt": list(block[:4]), "text": block[4], "reading_order": block_number,
                "verification_status": "machine_extracted", "source_pdf_sha256": digest,
                "extends_beyond_page": not (
                    0 <= block[0] <= block[2] <= page.rect.width + .1
                    and 0 <= block[1] <= block[3] <= page.rect.height + .1),
            })
    write_jsonl(folder / "pages.jsonl", pages)
    write_jsonl(folder / "blocks.jsonl", blocks)
    if __package__:
        from .text_formatting import pdf_text
    else:
        from text_formatting import pdf_text
    pdf_text(folder, pages, blocks)
    return pages, blocks




def build_index(catalog, pages, findings, supplemental_units):
    target = EVIDENCE / "index.sqlite"
    temporary = EVIDENCE / "index.build.sqlite"
    if temporary.exists():
        temporary.unlink()
    with sqlite3.connect(temporary) as db:
        db.executescript("""
            CREATE TABLE papers (paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER, extraction_status TEXT, metadata_json TEXT);
            CREATE TABLE pages (paper_id TEXT, document_id TEXT, pdf_page INTEGER, text TEXT, page_image TEXT, source_pdf_path TEXT, source_pdf_sha256 TEXT, PRIMARY KEY (paper_id,document_id,pdf_page));
            CREATE VIRTUAL TABLE page_search USING fts5(paper_id UNINDEXED,document_id UNINDEXED,pdf_page UNINDEXED,text,tokenize='unicode61 remove_diacritics 2');
            CREATE TABLE results (result_id TEXT PRIMARY KEY, paper_id TEXT, measure TEXT, value REAL, verification_status TEXT, payload_json TEXT);
            CREATE VIRTUAL TABLE result_search USING fts5(result_id UNINDEXED,paper_id UNINDEXED,text,tokenize='unicode61 remove_diacritics 2');
            CREATE TABLE content_units (unit_id TEXT PRIMARY KEY, paper_id TEXT, document_id TEXT, payload_json TEXT);
            CREATE VIRTUAL TABLE content_search USING fts5(unit_id UNINDEXED,paper_id UNINDEXED,text,tokenize='unicode61 remove_diacritics 2');
        """)
        for row in catalog:
            db.execute("INSERT INTO papers VALUES (?,?,?,?,?,?)", (
                row["paper_id"], row["doi"], row["title"], row["year"],
                row["extraction_status"], json.dumps(row, ensure_ascii=False)))
        for page in pages:
            db.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?)", (page["paper_id"], page["document_id"], page["pdf_page"], page["text"], page["repository_page_image"], page["source_pdf_path"], page["source_pdf_sha256"]))
            db.execute("INSERT INTO page_search VALUES (?,?,?,?)", (page["paper_id"], page["document_id"], page["pdf_page"], page["text"]))
        for result in findings:
            db.execute("INSERT INTO results VALUES (?,?,?,?,?,?)", (
                result["result_id"], result["paper_id"], result["measure"], result["value"],
                result["verification_status"], json.dumps(result, ensure_ascii=False)))
            db.execute("INSERT INTO result_search VALUES (?,?,?)", (
                result["result_id"], result["paper_id"],
                " ".join(str(result[k]) for k in ["outcome", "population", "measure", "reported_value", "supporting_quote"])))
        units = [{
            "unit_id": f"{p['paper_id']}:{p['document_id']}:p{p['pdf_page']}",
            "paper_id": p["paper_id"], "document_id": p["document_id"], "pdf_page": p["pdf_page"],
            "text": p["text"], "source_pdf_path": p["source_pdf_path"],
            "source_pdf_sha256": p["source_pdf_sha256"], "source_path": p["source_pdf_path"],
            "source_sha256": p["source_pdf_sha256"], "source_page_image": p["repository_page_image"],
            "source_metadata": f"evidence/papers/{p['paper_id']}/" +
                               (f"supplements/{p['document_id']}/" if p["document_id"] != "main" else "") + "metadata.json",
            "location": {"pdf_page": p["pdf_page"]},
            "verification_status": "machine_extracted", "text_extraction_method": p["text_extraction_method"],
        } for p in pages] + supplemental_units
        for unit in units:
            db.execute("INSERT INTO content_units VALUES (?,?,?,?)", (
                unit["unit_id"], unit["paper_id"], unit["document_id"], json.dumps(unit, ensure_ascii=False)))
            db.execute("INSERT INTO content_search VALUES (?,?,?)", (unit["unit_id"], unit["paper_id"], unit["text"]))
    temporary.replace(target)


def validate_document(folder, pid, document_id, source_path, digest, page_count):
    pages = load_jsonl(folder / "pages.jsonl")
    if len(pages) != page_count:
        raise ValueError(f"Page coverage mismatch: {pid}/{document_id}")
    for number, page in enumerate(pages, 1):
        if (page["paper_id"], page["document_id"], page["pdf_page"],
                page["source_pdf_path"], page["source_pdf_sha256"]) != (
                pid, document_id, number, source_path, digest):
            raise ValueError(f"Page provenance mismatch: {pid}/{document_id}/{number}")
        image = folder / page["page_image"]
        if not image.is_file() or (not page["text"].strip() and page.get("text_extraction_method") != "no_text_detected_after_ocr"):
            raise ValueError(f"Page text or image missing: {pid}/{document_id}/{number}")
        if image.relative_to(ROOT).as_posix() != page["repository_page_image"]:
            raise ValueError("Page image location mismatch")
    for block in load_jsonl(folder / "blocks.jsonl"):
        if (block["paper_id"], block["document_id"], block["source_pdf_sha256"]) != (pid, document_id, digest):
            raise ValueError("Block provenance mismatch")
        if not 1 <= block["pdf_page"] <= len(pages):
            raise ValueError("Block page outside document")
        page = pages[block["pdf_page"]-1]
        x0,y0,x1,y1 = block["bbox_pt"]
        extends = not (0 <= x0 <= x1 <= page["width_pt"]+.1 and 0 <= y0 <= y1 <= page["height_pt"]+.1)
        if extends != block.get("extends_beyond_page", False):
            raise ValueError(f"Out of bounds block: {block['block_id']}")
    return pages




def open_index():
    return retrieval.open_index(ROOT)


def positive_limit(value):
    n = int(value)
    if not 1 <= n <= 100:
        raise argparse.ArgumentTypeError("limit must be 1-100")
    return n


def main():
    parser = argparse.ArgumentParser(description="Query a local evidence index.")
    sub = parser.add_subparsers(dest="command", required=True)
    claims = sub.add_parser("claims")
    claims.add_argument("query", nargs="?")
    for name in ("paper", "topic", "kind", "measure"):
        claims.add_argument("--" + name)
    claims.add_argument("--limit", type=positive_limit, default=100)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--paper")
    search.add_argument("--limit", type=positive_limit, default=10)
    search.add_argument("--offset", type=int, default=0)
    results = sub.add_parser("results")
    results.add_argument("--paper")
    results.add_argument("--measure")
    results.add_argument("--limit", type=positive_limit, default=100)
    results.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    try:
        with closing(open_index()) as db:
            if args.command == "claims":
                payload = retrieval.claims(db, text=args.query, paper=args.paper, topic=args.topic,
                                           kind=args.kind, measure=args.measure, limit=args.limit)
            elif args.command == "search":
                payload = retrieval.search(db, args.query, paper=args.paper,
                                           limit=args.limit, offset=args.offset)
            else:
                payload = retrieval.results(db, paper=args.paper, measure=args.measure,
                                            limit=args.limit, offset=args.offset)
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Evidence error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
