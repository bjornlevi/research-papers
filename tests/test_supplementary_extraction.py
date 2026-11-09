from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import evidence
import supplementary_extraction as extraction
import supplements


class SupplementExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = evidence.load_jsonl(ROOT / "evidence/supplements.jsonl")

    def test_all_acquired_documents_are_extracted_once(self):
        inventory = json.loads(supplements.INVENTORY.read_text())
        acquired = {f["path"] for e in inventory for f in e["files"] if f["status"] == "acquired"}
        self.assertEqual(len(self.documents), 59)
        self.assertEqual({d["source_path"] for d in self.documents}, acquired)
        self.assertEqual(len({(d["paper_id"], d["document_id"]) for d in self.documents}), 59)
        self.assertEqual(sum(d.get("pdf_pages", 0) for d in self.documents), 366)
        self.assertEqual(sum(d["coverage"].get("word_tables", 0) for d in self.documents), 72)
        self.assertEqual(sum(d["coverage"].get("worksheets", 0) for d in self.documents), 34)
        self.assertEqual(sum(d["coverage"].get("embedded_assets", 0) for d in self.documents), 30)
        self.assertTrue(all(d["structured_findings"] == "not_curated" for d in self.documents))

    def test_index_has_pdf_and_office_coverage_without_duplicating_model(self):
        with closing(evidence.open_index()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM content_units").fetchone()[0], 19280)
            self.assertEqual(db.execute("SELECT count(*) FROM content_search").fetchone()[0], 19280)
            self.assertEqual(db.execute("SELECT count(*) FROM pages WHERE paper_id='10-7326-m23-2540' AND document_id='supplement-01'").fetchone()[0], 14)
            self.assertEqual(db.execute("SELECT count(*) FROM results").fetchone()[0], 16)

    def test_user_supplied_tables_and_booklet_have_searchable_provenance(self):
        auto = next(d for d in self.documents if d["paper_id"] == "10-7326-m23-2867")
        self.assertEqual(auto["download_method"], "manual_user_supplied")
        self.assertEqual(auto["coverage"]["word_tables"], 3)
        folder = ROOT / auto["evidence_path"]
        units = evidence.load_jsonl(folder / "units.jsonl")
        self.assertTrue(any(u["location"]["body_element"] == 7 and "2.13" in u["text"] for u in units))
        self.assertTrue(any("PR: Odds Ratio" in u["text"] for u in units))
        booklet = next(d for d in self.documents if d["paper_id"] == "10-1038-s41408-021-00480-w"
                       and d["document_id"] == "supplement-02")
        self.assertEqual(booklet["pdf_pages"], 11)
        self.assertIn("not asserted", booklet["source_identity_evidence"])
        with closing(evidence.open_index()) as db:
            self.assertTrue(db.execute(
                "SELECT 1 FROM content_search WHERE content_search MATCH ? AND unit_id IN "
                "(SELECT unit_id FROM content_units WHERE paper_id=? AND document_id=?)",
                ("Addison", auto["paper_id"], auto["document_id"])).fetchone())

    def test_ocr_and_no_text_pages_are_explicit(self):
        self.assertEqual(sum(len(d["coverage"].get("ocr_pages", [])) for d in self.documents), 5)
        self.assertEqual(sum(len(d["coverage"].get("no_text_pages", [])) for d in self.documents), 2)
        for document in self.documents:
            if document["format"] != "pdf":
                continue
            folder = ROOT / document["evidence_path"]
            pages = evidence.load_jsonl(folder / "pages.jsonl")
            for number in document["coverage"]["ocr_pages"]:
                self.assertTrue(pages[number - 1]["text"].strip())
                self.assertEqual(pages[number - 1]["text_extraction_method"], "tesseract_ocr")
            for number in document["coverage"]["no_text_pages"]:
                self.assertEqual(pages[number - 1]["text_extraction_method"], "no_text_detected_after_ocr")
                self.assertTrue((folder / pages[number - 1]["page_image"]).is_file())

    def test_original_packages_and_embedded_graphics_preserved(self):
        for document in self.documents:
            if document["format"] == "pdf":
                continue
            folder = ROOT / document["evidence_path"]
            with zipfile.ZipFile(ROOT / document["source_path"]) as archive:
                for member in archive.namelist():
                    if member.endswith((".xml", ".rels")):
                        self.assertEqual((folder / "package" / member).read_bytes(), archive.read(member))
                for asset in json.loads((folder / "assets.json").read_text()):
                    data = (ROOT / asset["path"]).read_bytes()
                    self.assertEqual(data, archive.read(asset["package_member"]))
                    self.assertEqual(hashlib.sha256(data).hexdigest(), asset["sha256"])

    def test_office_search_has_real_coordinates_and_source_links(self):
        cases = [("ICD10", "10.1093/rheumatology/keac253", "body_element"),
                 ("SARS", "10.1038/s43856-023-00326-5", "worksheet")]
        for query, doi, coordinate in cases:
            response = subprocess.run([sys.executable, str(ROOT / "tools/evidence.py"),
                                       "search", query, "--paper", doi], capture_output=True, text=True)
            self.assertEqual(response.returncode, 0, response.stderr)
            hits = json.loads(response.stdout)
            matches = [hit for hit in hits if coordinate in hit["location"]]
            self.assertTrue(matches)
            for hit in matches:
                self.assertIsNone(hit["pdf_page"])
                self.assertIsNone(hit["source_page_image"])
                self.assertTrue((ROOT / hit["source_path"]).is_file())
                self.assertTrue((ROOT / hit["source_metadata"]).is_file())

    def test_extraction_browser_links_resolve(self):
        parser = supplements.Links()
        parser.feed((ROOT / "evidence/supplements.html").read_text())
        for href, _ in parser.links:
            path = href.split("#")[0]
            self.assertTrue((ROOT / "evidence" / path).is_file(), href)

    def test_workbook_values_formulas_merges_and_hidden_state(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            folder = Path(temp)
            source = folder / "fixture.xlsx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("xl/workbook.xml", f'<workbook xmlns="{extraction.S[1:-1]}" xmlns:r="{extraction.R[1:-1]}"><workbookPr date1904="1"/><sheets><sheet name="Hidden data" sheetId="1" state="hidden" r:id="r1"/></sheets></workbook>')
                archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
                archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{extraction.S[1:-1]}"><si><r><t>Risk </t></r><r><t>ratio</t></r></si></sst>')
                archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{extraction.S[1:-1]}"><sheetData><row r="4" hidden="1"><c r="A4" t="s"><v>0</v></c><c r="C4" s="2"><v>45000</v></c><c r="D4"><f>1+1</f><v>2</v></c><c r="E4"><f t="shared" si="0"/></c><c r="F4" t="inlineStr"><is><t>=not-a-formula</t></is></c><c r="G4" t="b"><v>0</v></c></row></sheetData><mergeCells><mergeCell ref="A4:B4"/></mergeCells></worksheet>')
            with zipfile.ZipFile(source) as archive:
                units, coverage = extraction.extract_xlsx(archive, folder)
            self.assertEqual(coverage["date_system"], "1")
            self.assertEqual(units[0]["location"]["row"], 4)
            self.assertEqual(units[0]["location"]["worksheet_state"], "hidden")
            self.assertEqual(units[0]["location"]["row_hidden"], "1")
            sheet = json.loads((folder / "tables/sheet-001.json").read_text())
            cells = {c["ref"]: c for c in sheet["cells"]}
            self.assertEqual(cells["A4"]["value"], "Risk ratio")
            self.assertEqual(cells["C4"]["value"], "45000")
            self.assertEqual(cells["C4"]["style_index"], "2")
            self.assertEqual(cells["D4"]["formula"], "1+1")
            self.assertEqual(cells["D4"]["value"], "2")
            self.assertIsNone(cells["E4"]["value"])
            self.assertEqual(cells["E4"]["formula_attributes"], {"t": "shared", "si": "0"})
            self.assertEqual(cells["G4"]["value"], "0")
            self.assertEqual(sheet["merged_ranges"], ["A4:B4"])
            with (ROOT / sheet["csv_path"]).open(newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(next(r for r in rows if r[0] == "F4")[1], "'=not-a-formula")
            self.assertEqual(cells["F4"]["value"], "=not-a-formula")

    def test_word_tables_retain_merge_markers_and_body_locator(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            folder = Path(temp)
            source = folder / "fixture.docx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("word/document.xml", f'<w:document xmlns:w="{extraction.W[1:-1]}"><w:body><w:p><w:r><w:t>Caption</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>0.95</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr></w:tbl></w:body></w:document>')
            with zipfile.ZipFile(source) as archive:
                units, coverage = extraction.extract_docx(archive, folder)
            self.assertEqual(coverage["word_tables"], 1)
            table = json.loads((folder / "tables.json").read_text())[0]
            self.assertEqual(table["body_element"], 2)
            self.assertEqual(table["cells"][0]["grid_span"], 2)
            self.assertEqual(table["cells"][0]["vertical_merge"], "restart")
            self.assertEqual(table["cells"][1]["vertical_merge"], "continue")
            self.assertEqual(table["cells"][0]["text"], "0.95")
            self.assertEqual(units[1]["location"], {"body_element": 2, "kind": "table"})


if __name__ == "__main__":
    unittest.main()
