from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import pymupdf

from research_library.build import evidence, supplementary_extraction as extraction
from research_library.build import text_formatting as formatting
from research_library.build.quality_inventory import expected_locations


class SyntheticExtractionTests(unittest.TestCase):
    def setUp(self):
        formatting.word_evidence.cache_clear()
        self.addCleanup(formatting.word_evidence.cache_clear)

    def test_pdf_source_text_pages_and_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "evidence/papers/fictional"
            with pymupdf.open() as document:
                page = document.new_page()
                page.insert_text((72, 72), "Fictional screening. Not clinical evidence.")
                with patch.object(evidence, "ROOT", root), patch.object(formatting, "ROOT", root):
                    pages, blocks = evidence.extract_pages(
                        document, "fictional", "a" * 64, folder, "papers/fictional.pdf")
                document.save(root / "original.pdf")
            self.assertEqual(expected_locations(root / "original.pdf", "pdf"), [{"kind": "pdf", "pdf_page": 1}])
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0]["pdf_page"], 1)
            self.assertIn("Not clinical evidence", pages[0]["text"])
            self.assertTrue((root / pages[0]["repository_page_image"]).is_file())
            self.assertTrue(blocks[0]["bbox_pt"])
            self.assertTrue((folder / "text.raw.md").is_file())

    def test_word_table_and_worksheet_have_office_not_pdf_locators(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(extraction, "ROOT", root):
                word = root / "fictional.docx"
                with zipfile.ZipFile(word, "w") as archive:
                    archive.writestr("word/document.xml",
                        f'<w:document xmlns:w="{extraction.W[1:-1]}"><w:body>'
                        '<w:p><w:r><w:t>Fictional footnote.</w:t></w:r></w:p>'
                        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>0</w:t></w:r></w:p>'
                        '</w:tc></w:tr></w:tbl></w:body></w:document>')
                with zipfile.ZipFile(word) as archive:
                    units, counts = extraction.extract_docx(archive, root)
                self.assertEqual(counts["word_tables"], 1)
                self.assertEqual(units[1]["location"], {"body_element": 2, "kind": "table"})
                self.assertEqual(expected_locations(word, "docx"), [
                    {"kind": "word_paragraph", "body_element": 1}, {"kind": "word_table", "body_element": 2}])
                sheet = root / "fictional.xlsx"
                with zipfile.ZipFile(sheet, "w") as archive:
                    archive.writestr("xl/workbook.xml",
                        f'<workbook xmlns="{extraction.S[1:-1]}" xmlns:r="{extraction.R[1:-1]}">'
                        '<sheets><sheet name="Fixture" sheetId="1" r:id="r1"/></sheets></workbook>')
                    archive.writestr("xl/_rels/workbook.xml.rels",
                        '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
                    archive.writestr("xl/worksheets/sheet1.xml",
                        f'<worksheet xmlns="{extraction.S[1:-1]}"><sheetData><row r="4">'
                        '<c r="A4"><v>0</v></c></row></sheetData></worksheet>')
                with zipfile.ZipFile(sheet) as archive:
                    units, counts = extraction.extract_xlsx(archive, root)
                self.assertEqual(units[0]["location"]["row"], 4)
                self.assertNotIn("pdf_page", units[0]["location"])
                self.assertEqual(expected_locations(sheet, "xlsx"), [{"kind": "worksheet", "worksheet": "Fixture", "row": 4}])

    def test_formatting_uses_only_synthetic_word_evidence(self):
        with patch.object(formatting, "word_evidence", return_value=({"fictional"}, set())):
            text = ("The investigators performed a detailed fic-\n"
                    "tional examination of synthetic participants in a software fixture.")
            self.assertIn("fictional", formatting.format_block(text))
            self.assertEqual(formatting.repair_line_hyphens("unknown-\nfragment"), "unknown-\nfragment")
