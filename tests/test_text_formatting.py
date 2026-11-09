import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import text_formatting as formatting


class TextFormattingTests(unittest.TestCase):
    def test_prose_with_split_percent_and_superscript_citations_is_joined(self):
        text = (
            "SEMPs typically arise at mucosal sites with high anti-\n"
            "genic load, more common in the head and neck region\n"
            "(85%)\n"
            "26,27,33,34 ; other sites include the GI tract, lung, thyroid,\n"
            "testis, parotid glands, lymph nodes, and CNS.\n"
            "35,36 Symptoms\n"
            "should, by deﬁnition, only be related to local tumor\n"
            "expansion."
        )
        self.assertEqual(formatting.format_block(text), text.replace("anti-\ngenic", "antigenic").replace("\n", " "))
        self.assertNotIn("```", formatting.format_block(text))

    def test_standalone_citation_fragments_in_prose_are_joined(self):
        for citation in ["26,27,33,34", "(26,27)", "26–34"]:
            text = ("The cohort was recruited from the general population.\n"
                    f"{citation}\n"
                    "Participants were followed for clinical outcomes over time.")
            self.assertEqual(formatting.format_block(text), text.replace("\n", " "))

    def test_numeric_and_citation_like_table_rows_remain_literal(self):
        for text in [
            "Clinical category of disease in the study cohort\n(85%)\n(15%)",
            "Disease group\n26,27,33,34\n35,36",
            "The enrolled cohort included participants in these groups\n"
            "19\n72\nThe reported counts are listed in the study table",
        ]:
            self.assertEqual(formatting.format_block(text), formatting.literal(text))

    def test_reported_paragraph_is_not_fenced_in_generated_view(self):
        folder = ROOT / "evidence/papers/10-1200-jco-26-00410"
        view = (folder / "text.md").read_text()
        blocks = re.findall(r"(?ms)^(`{3,})text\n(.*?)^\1$", view)
        self.assertFalse(any("SEMPs typically arise" in text for _, text in blocks))
        self.assertIn("SEMPs typically arise at mucosal sites with high antigenic load", view)

    def test_line_wrap_hyphens_use_word_evidence_and_preserve_compounds(self):
        evidence = ({"antigenic", "classification", "longterm"}, {"population-based", "long-term"})
        for source, expected in [
            ("anti-\ngenic", "antigenic"),
            ("classi-\nfication", "classification"),
            ("population-\nbased", "population-based"),
            ("long-\nterm", "long-term"),
            ("anti\u00ad\ngenic", "antigenic"),
            ("anti\u2010\ngenic", "antigenic"),
            ("unknown-\nfragment", "unknown-\nfragment"),
            ("high- and low-risk", "high- and low-risk"),
            ("anti- genic", "anti- genic"),
            ("10-\n20", "10-\n20"),
        ]:
            self.assertEqual(formatting.repair_line_hyphens(source, evidence), expected)

    def test_hyphen_repair_retains_case_and_original_characters(self):
        evidence = ({"antigenic", "classification"}, set())
        self.assertEqual(formatting.repair_line_hyphens("Anti-\ngenic", evidence), "Antigenic")
        self.assertEqual(formatting.repair_line_hyphens("classi-\nﬁcation", evidence), "classiﬁcation")

    def test_hyphenation_in_tables_and_formulas_is_not_changed(self):
        text = "Group   Label\nanti-\ngenic\n19"
        self.assertEqual(formatting.format_block(text), formatting.literal(text))
        prose = ("The investigators performed a detailed classi-\n"
                 "fication of the study participants at the baseline examination.")
        self.assertIn("classification", formatting.format_block(prose))
        self.assertNotIn("classi- fication", formatting.format_block(prose))

    def test_wrapped_prose_and_trailing_citation_are_joined(self):
        text = ("The overall rate of progression is 0.5% to 1% per year, with clinical\n"
                "guidelines recommending lifelong monitoring for all risk groups\n(13).")
        self.assertEqual(formatting.format_block(text), text.replace("\n", " "))

    def test_heading_and_paragraph_boundaries_are_retained(self):
        self.assertEqual(formatting.format_block("Methods:\n\nStudy design"), "### Methods:\n\n### Study design")

    def test_numbers_tables_and_formulas_keep_line_structure(self):
        for text in ["19\n72\n0.026\n95% CI", "Group   N   Risk\nA   19   0%", "x =\n1.5\n+ 0.2*y"]:
            self.assertEqual(formatting.format_block(text), formatting.literal(text))

    def test_source_characters_and_hyphens_are_not_rewritten(self):
        samples = [
            "The measured prevalence ratio was 1.05 (95% CI, 0.97 to 1.15) for the\npopulation of participants with autoimmune disease.",
            "In a population-\nbased study we found no differences between the selected groups.",
            "Methods:\n\nNo change to ≤10%, 10−8, fi, ﬁ, >15 or [labels] is inferred.",
        ]
        for original in samples:
            view = formatting.format_block(original)
            view = re.sub(r"(?m)^`{3,}(?:text)?\n?", "", view)
            view = re.sub(r"(?m)^### ", "", view)
            view = re.sub(r"\\(.)", r"\1", view)
            self.assertEqual(re.sub(r"\s+", "", view), re.sub(r"\s+", "", original))

    def test_markdown_special_characters_and_fences_are_safe(self):
        self.assertEqual(formatting.escape_markdown("1. *x* [ref] <tag>"), r"1\. \*x\* \[ref\] \<tag\>")
        self.assertTrue(formatting.literal("```not markup```").startswith("````text\n"))

    def test_ocr_and_missing_text_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            pages = [{"pdf_page": 1, "page_image": "pages/page-001.png", "text": "OCR content"},
                     {"pdf_page": 2, "page_image": "pages/page-002.png", "text": ""}]
            formatting.pdf_text(folder, pages, [])
            view = (folder / "text.md").read_text()
            self.assertIn("OCR content", view)
            self.assertIn("No text detected after OCR", view)
            self.assertIn("## PDF page 2", view)

    def test_all_document_views_have_raw_source_and_correct_locators(self):
        base = ROOT / "evidence/papers"
        paths = sorted(base.glob("*/pages.jsonl")) + sorted(base.glob("*/supplements/*/pages.jsonl"))
        offices = sorted(base.glob("*/supplements/*/units.jsonl"))
        self.assertEqual((len(paths), len(offices)), (75, 29))
        for path in paths:
            pages = formatting.load_jsonl(path)
            view = (path.parent / "text.md").read_text()
            raw = (path.parent / "text.raw.md").read_text()
            expected_raw = ("# Extracted source text\n\n"
                            "Machine-extracted text. PDF page numbering is authoritative; use page images for layout, tables and figures.\n\n"
                            + "".join(f"## PDF page {page['pdf_page']}\n\n[Source image]({page['page_image']})\n\n"
                                      + page["text"] + "\n\n" for page in pages))
            self.assertEqual(raw, expected_raw)
            self.assertIn("[Raw extracted text](text.raw.md)", view)
            for page in pages:
                self.assertIn(f"## PDF page {page['pdf_page']}\n", view)
                self.assertIn(f"[Source image]({page['page_image']})", view)
                self.assertIn(page["text"], raw)
        for path in offices:
            units = formatting.load_jsonl(path)
            raw = (path.parent / "text.raw.md").read_text()
            view = (path.parent / "text.md").read_text()
            expected_raw = "# Machine-extracted supplementary content\n\n" + "".join(
                f"## Unit {number}: {json.dumps(unit['location'], ensure_ascii=False)}\n\n{unit['text']}\n\n"
                for number, unit in enumerate(units, 1))
            self.assertEqual(raw, expected_raw)
            self.assertEqual(len(re.findall(r"(?m)^## Unit \d+$", view)), len(units))
            for number, unit in [(1, units[0]), (len(units), units[-1])]:
                self.assertIn(f"## Unit {number}\n", view)
                self.assertIn(json.dumps(unit["location"], ensure_ascii=False), view)

    def test_formatting_is_idempotent_and_does_not_change_raw_records(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            pages = [{"pdf_page": 1, "page_image": "pages/page-001.png", "text": "Methods\nSome source text"}]
            record = folder / "pages.jsonl"
            record.write_text(json.dumps(pages[0]) + "\n")
            before = hashlib.sha256(record.read_bytes()).hexdigest()
            formatting.pdf_text(folder, pages, [])
            first = (folder / "text.md").read_bytes()
            formatting.pdf_text(folder, pages, [])
            self.assertEqual(first, (folder / "text.md").read_bytes())
            self.assertEqual(before, hashlib.sha256(record.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
