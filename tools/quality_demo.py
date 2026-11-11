"""Create a small fictional operator exercise; never use its reviews for real evidence."""

import argparse
from pathlib import Path

import pymupdf

from research_library import quality, releases


def create(root):
    root = Path(root)
    if root.exists():
        raise ValueError("Demo destination already exists; choose a new directory")
    root.mkdir(parents=True)
    original = root / "fictional.pdf"
    with pymupdf.open() as pdf:
        for text in ("Fictional page one.", "Fictional value 20.", "Fictional missing page three.", ""):
            page = pdf.new_page()
            if text:
                page.insert_text((72, 72), text)
        pdf.save(original)
    source = {"id": "fictional:main", "doi": "10.0000/quality-demo",
              "sha256": releases.sha256(original.read_bytes()), "format": "pdf",
              "url": "https://example.org/fictional-publication",
              "provenance": "Authored fictional operator exercise; not clinical evidence.",
              "rights": "synthetic", "omission": "original_not_exported"}
    units = []
    for number, text in ((1, "Fictional page one."), (2, "[OCR transcription]\nFictional value 2O.")):
        units.append({
            "id": f"fictional:main:p{number}", "text": text, "truncated": False,
            "citation": {"source_id": source["id"], "doi": source["doi"], "source_sha256": source["sha256"],
                         "source_url": source["url"], "provenance": source["provenance"],
                         "availability": "metadata_only", "locator": {"kind": "pdf", "pdf_page": number}},
            "verification_status": "machine_extracted", "human_verification": "not_reviewed",
            "caveats": ["OCR not human verified."] if number == 2 else [], "source_issue_ids": [],
        })
    bundle = {"schema_version": 1, "caveats": ["Fictional quality demo, not scientific evidence."],
              **{section: [] for section in releases.SECTIONS}}
    bundle.update(sources=[source], units=units)
    releases.export(bundle, root / "candidate", profile="synthetic")
    quality.inventory(root / "quality", source, [{"kind": "pdf", "pdf_page": number} for number in range(1, 5)])
    (root / "page2.txt").write_text("[OCR transcription; operator corrected]\nFictional value 20.\n")
    (root / "page3.txt").write_text("Fictional missing page three.\n")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(create(args.output))
    except (OSError, ValueError, releases.ReleaseError) as error:
        parser.exit(1, f"Demo creation failed: {error}\n")


if __name__ == "__main__":
    main()
