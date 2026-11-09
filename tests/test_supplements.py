import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import supplements


class SupplementCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = json.loads(supplements.INVENTORY.read_text())

    def test_all_catalog_sources_are_accounted_for(self):
        self.assertEqual(len(self.entries), 45)
        self.assertEqual(len({e["paper_id"] for e in self.entries}), 45)
        self.assertEqual(sum(e["discovery_status"] == "identified" for e in self.entries), 32)
        self.assertEqual(sum(e["discovery_status"] == "none_identified" for e in self.entries), 13)
        self.assertEqual(sum(len(e["files"]) for e in self.entries), 59)

    def test_all_supplements_acquired_with_manual_import_provenance(self):
        files = [f for e in self.entries for f in e["files"]]
        self.assertEqual(sum(f["status"] == "acquired" for f in files), 59)
        missing = [(e["doi"], f) for e in self.entries for f in e["files"] if f["status"] != "acquired"]
        self.assertEqual(missing, [])
        auto = next(e for e in self.entries if e["inventory_number"] == 25)["files"][0]
        self.assertEqual(auto["format"], "docx")
        self.assertEqual(auto["download_method"], "manual_user_supplied")
        self.assertTrue(any("403" in a["error"] for a in auto["attempts"]))
        self.assertIn("equivalence", auto["source_identity_evidence"])
        for kind, expected in [("pdf", 30), ("docx", 19), ("xlsx", 10)]:
            self.assertEqual(sum(f.get("format") == kind for f in files), expected)

    def test_offline_integrity_and_parent_validation(self):
        supplements.validate(self.entries)

    def test_main_sources_remain_unchanged(self):
        catalog = [json.loads(line) for line in (ROOT / "evidence/catalog.jsonl").read_text().splitlines()]
        for row in catalog:
            self.assertEqual(hashlib.sha256((ROOT / row["pdf_path"]).read_bytes()).hexdigest(), row["pdf_sha256"])
            entry = next(e for e in self.entries if e["paper_id"] == row["paper_id"])
            self.assertEqual(row["supplement_files_identified"], len(entry["files"]))
            self.assertEqual(row["supplement_files_acquired"], sum(f["status"] == "acquired" for f in entry["files"]))
            self.assertTrue((ROOT / row["supplement_inventory_path"]).is_file())

    def test_generated_inventory_links_and_csv_rows(self):
        parser = supplements.Links()
        parser.feed((supplements.FOLDER / "inventory.html").read_text())
        local = [href for href, _ in parser.links if not href.startswith(("http:", "https:"))]
        self.assertEqual(sum(Path(href).suffix in [".pdf", ".docx", ".xlsx"] for href in local), 59)
        for href in local:
            self.assertTrue((supplements.FOLDER / href).is_file(), href)
        with (supplements.FOLDER / "manifest.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 59 + 13)
        self.assertEqual(sum(row["status"] == "acquired" for row in rows), 59)

    def test_duplicate_merging_preserves_acquired_provenance(self):
        first = supplements.new_file("Supplement", "supp.pdf", ["https://example.org/one"], "https://example.org/article")
        second = supplements.new_file("Supplement", "supp.pdf", ["https://example.org/two"], "https://example.org/article")
        second.update(status="acquired", sha256="recorded-hash", path="preserved-path")
        merged = supplements.merge_files([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["urls"], ["https://example.org/one", "https://example.org/two"])
        self.assertEqual(merged[0]["sha256"], "recorded-hash")
        self.assertEqual(merged[0]["path"], "preserved-path")

    def test_additional_sources_are_idempotent(self):
        first = supplements.apply_sources(json.loads(json.dumps(self.entries)))
        second = supplements.apply_sources(json.loads(json.dumps(first)))
        self.assertEqual(first, second)

    def test_html_and_wrong_office_package_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTML response"):
            supplements.validate_bytes(b"<!doctype html><html>Challenge</html>", ".pdf")
        with self.assertRaisesRegex(ValueError, "not PDF"):
            supplements.validate_bytes(b"not a pdf", ".pdf")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/not-a-workbook.xml", "<test/>")
        with self.assertRaisesRegex(ValueError, "valid .xlsx"):
            supplements.validate_bytes(buffer.getvalue(), ".xlsx")

    def test_acquired_files_are_never_refetched(self):
        model = next(e for e in self.entries if e["inventory_number"] == 23)
        with patch.object(supplements, "fetch", side_effect=AssertionError("Network should not be used")):
            self.assertEqual(supplements.acquire(model), model)

    def test_supplement_citations_not_misclassified(self):
        alopecia = next(e for e in self.entries if e["inventory_number"] == 21)
        self.assertEqual(alopecia["discovery_status"], "none_identified")
        self.assertIn("conference-abstract", alopecia["additional_evidence"])


if __name__ == "__main__":
    unittest.main()
