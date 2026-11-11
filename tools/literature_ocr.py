"""Local OCR of partial native extractions; originals and native text stay unchanged."""

import argparse
import csv
import io
from pathlib import Path
import subprocess
import sys
import tempfile

import pymupdf

from research_library import catalog, releases
from research_library.collections import atomic_json
from . import literature_ingest as ingest

OCR_STATUSES = ingest.OCR_STATUSES


def recognize(page):
    with tempfile.TemporaryDirectory() as temporary:
        image = Path(temporary) / "page.png"
        pixmap = page.get_pixmap(dpi=300, alpha=False)
        blank = min(pixmap.samples) == 255
        pixmap.save(image)
        result = subprocess.run(
            ["tesseract", str(image), "stdout", "-l", "eng", "--psm", "3", "tsv"],
            capture_output=True, text=True, timeout=180, check=True)
    lines = {}
    confidence = []
    for word in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        text = word["text"].strip()
        if not text:
            continue
        key = tuple(word[field] for field in ("block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(text)
        confidence.append(float(word["conf"]))
    text = "\n".join(" ".join(words) for words in lines.values())
    return {
        "text": text, "text_sha256": ingest.identity(text),
        "mean_word_confidence": sum(confidence) / len(confidence) if confidence else None,
        "word_count": len(confidence),
        "status": ("ocr_text" if any(character.isalnum() for character in text)
                   else "blank_page" if blank else "needs_visual_review"),
        "blank_check": "All rendered RGB pixels white at 300 dpi." if blank else None,
    }


def complete(root, papers, output):
    root, papers, output = Path(root), Path(papers), Path(output)
    version = subprocess.run(["tesseract", "--version"], capture_output=True,
                             text=True, timeout=10, check=True).stdout.splitlines()[0]
    policy = {"engine": version, "language": "eng", "dpi": 300, "page_segmentation_mode": 3,
              "version": 2, "blank_page": "All rendered RGB pixels white at 300 dpi.",
              "native_text": "Unchanged; OCR is a separate machine-recognized transcription.",
              "human_verification": "not_reviewed"}
    manifest = ingest.read(root / "pilot.json")
    state = ingest.read(root / "extraction.json")
    if ingest.identity(manifest) != state["manifest_sha256"]:
        raise ValueError("Native extraction manifest mismatch")
    manifest_id = state["manifest_sha256"]
    index = ingest.read(output / "index.json") if (output / "index.json").exists() else {
        "schema_version": 1, "manifest_sha256": manifest_id, "policy": policy, "items": {}}
    if index["policy"] != policy or index["manifest_sha256"] != manifest_id:
        raise ValueError("OCR policy or original manifest changed; select a new output directory")
    for source in manifest["sources"]:
        identifier = source["publication_id"]
        entry = state["items"][identifier]
        if entry["status"] not in OCR_STATUSES:
            continue
        path = ingest.source_path(papers, source)
        native = ingest.checked_result(root, source, entry, manifest_id)
        filename = source["sha256"] + ".json"
        target = output / "documents" / filename
        if target.exists():
            document = ingest.read(target)
            if (document["source_sha256"] != source["sha256"] or document["policy"] != policy
                    or document["native_sha256"] != entry["sha256"]):
                raise ValueError("OCR checkpoint source/policy mismatch")
            if identifier in index["items"] and catalog.file_digest(target) != index["items"][identifier]["sha256"]:
                raise ValueError("OCR checkpoint checksum mismatch")
        else:
            document = {"publication_id": identifier, "source_sha256": source["sha256"],
                        "native_sha256": entry["sha256"], "policy": policy, "pages": []}
        if any(page["text_sha256"] != ingest.identity(page["text"]) for page in document["pages"]):
            raise ValueError("OCR text checksum mismatch")
        done = {page["pdf_page"] for page in document["pages"]}
        with pymupdf.open(path) as pdf:
            for page in native["pages"]:
                if page["status"] == "text" or page["pdf_page"] in done:
                    continue
                result = recognize(pdf[page["pdf_page"] - 1])
                document["pages"].append({
                    **result, "pdf_page": page["pdf_page"], "page_label": page["page_label"],
                    "native_status": page["status"], "native_text_sha256": page["text_sha256"],
                    "caveat": "OCR may misread scientific text, numbers, formulas and tables; not human verified.",
                })
                atomic_json(target.parent, target.name, document)
                print(f"OCR {identifier} page {page['pdf_page']}: {result['status']}", file=sys.stderr, flush=True)
        expected = {page["pdf_page"] for page in native["pages"] if page["status"] != "text"}
        if {page["pdf_page"] for page in document["pages"]} != expected:
            raise ValueError("OCR checkpoint does not cover the original problem pages")
        document["status"] = ("extracted_with_ocr" if all(
            page["status"] in {"ocr_text", "blank_page"} for page in document["pages"]) else "needs_visual_review")
        atomic_json(target.parent, target.name, document)
        index["items"][identifier] = {
            "source_sha256": source["sha256"], "native_sha256": entry["sha256"],
            "file": "documents/" + filename, "sha256": catalog.file_digest(target),
            "status": document["status"], "ocr_pages": len(document["pages"]),
            "unresolved_pages": [page["pdf_page"] for page in document["pages"]
                                 if page["status"] not in {"ocr_text", "blank_page"}],
            "low_confidence_pages": [page["pdf_page"] for page in document["pages"]
                                     if page["mean_word_confidence"] is not None
                                     and page["mean_word_confidence"] < 70],
        }
        atomic_json(output, "index.json", index)
    return {"papers_processed": len(index["items"]),
            "ocr_pages": sum(item["ocr_pages"] for item in index["items"].values()),
            "extracted_with_ocr": sum(item["status"] == "extracted_with_ocr" for item in index["items"].values()),
            "needs_visual_review": sum(item["status"] == "needs_visual_review" for item in index["items"].values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("releases/m9-ingestion-001"))
    parser.add_argument("--papers", type=Path, default=Path("papers/literature/istopmm-first-tier-20261019"))
    parser.add_argument("--output", type=Path, default=Path("releases/literature-ocr-001"))
    args = parser.parse_args()
    try:
        print(complete(args.root, args.papers, args.output))
        return 0
    except (OSError, ValueError, KeyError, releases.ReleaseError,
            subprocess.SubprocessError, pymupdf.FileDataError) as error:
        print(f"Local OCR failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
