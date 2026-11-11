from contextlib import redirect_stderr
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from research_library import catalog
from research_library.build import literature_ocr as ocr
from research_library.collections import atomic_json
import test_literature_ingest


class OCRTests(unittest.TestCase):
    def setUp(self):
        helper = test_literature_ingest.LiteratureIngestTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.root, self.papers, self.workspace = helper.root, helper.papers, helper.workspace
        self.source = helper.source()
        helper.manifest([self.source])
        helper.extract()
        state = ocr.ingest.read(self.workspace / "extraction.json")
        self.entry = state["items"][self.source["publication_id"]]
        document = ocr.ingest.read(self.workspace / self.entry["file"])
        document["pages"][1]["status"] = "sparse_text"
        document["status"] = "partial"
        atomic_json((self.workspace / self.entry["file"]).parent, Path(self.entry["file"]).name, document)
        self.entry.update(status="partial", sha256=ocr.ingest.identity(document))
        atomic_json(self.workspace, "extraction.json", state)
        self.output = self.root / "ocr"

    def test_ocr_separate_locators_originals_and_resume(self):
        originals = {path: path.read_bytes() for root in (self.workspace, self.papers)
                     for path in root.rglob("*") if path.is_file()}
        result = {"text": "Fictional OCR transcription", "text_sha256": ocr.ingest.identity("Fictional OCR transcription"),
                  "mean_word_confidence": 61.0, "word_count": 3, "status": "ocr_text", "blank_check": None}
        with patch.object(ocr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "tesseract fixture\n")), \
                patch.object(ocr, "recognize", return_value=result) as recognize, redirect_stderr(io.StringIO()):
            report = ocr.complete(self.workspace, self.papers, self.output)
            self.assertEqual(report["extracted_with_ocr"], 1)
            self.assertEqual(report["ocr_pages"], 1)
            self.assertEqual(ocr.complete(self.workspace, self.papers, self.output), report)
            self.assertEqual(recognize.call_count, 1)
        index = ocr.ingest.read(self.output / "index.json")
        entry = index["items"][self.source["publication_id"]]
        document = ocr.ingest.read(self.output / entry["file"])
        self.assertEqual(document["pages"][0]["pdf_page"], 2)
        self.assertEqual(document["pages"][0]["native_text_sha256"],
                         ocr.ingest.read(self.workspace / self.entry["file"])["pages"][1]["text_sha256"])
        self.assertEqual(entry["low_confidence_pages"], [2])
        self.assertEqual(catalog.file_digest(self.output / entry["file"]), entry["sha256"])
        self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
        bundle, grants = ocr.ingest.make_bundle(self.workspace, self.papers, all_sources=True,
                                                ocr_root=self.output)
        page = bundle["units"][1]
        self.assertIn("[Native PDF text]", page["text"])
        self.assertIn("[OCR transcription; not human verified]", page["text"])
        self.assertIn("Fictional OCR transcription", page["text"])
        self.assertTrue(any("LOW CONFIDENCE" in caveat for caveat in page["caveats"]))
        destination = self.root / "ocr-release"
        ocr.releases.export(bundle, destination, profile="private_context", authorization=grants)
        snapshot = ocr.releases.validate(destination, allowed_profiles={"private_context"})
        hit = snapshot.call("search_evidence", {"query": "transcription"})["result"]["items"][0]
        context = snapshot.call("get_source_context", {"unit_id": hit["id"], "max_characters": 2048})["result"]
        self.assertIn("not human verified", context["text"])
        self.assertEqual(context["citation"]["locator"]["pdf_page"], 2)
        (self.output / entry["file"]).write_text("{}")
        with patch.object(ocr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "tesseract fixture\n")):
            with self.assertRaises((ValueError, KeyError)):
                ocr.complete(self.workspace, self.papers, self.output)
        with self.assertRaisesRegex(ValueError, "checksum"):
            ocr.ingest.make_bundle(self.workspace, self.papers, all_sources=True, ocr_root=self.output)

    def test_unreadable_page_not_reported_complete(self):
        result = {"text": "", "text_sha256": ocr.ingest.identity(""), "mean_word_confidence": None,
                  "word_count": 0, "status": "needs_visual_review", "blank_check": None}
        with patch.object(ocr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "tesseract fixture\n")), \
                patch.object(ocr, "recognize", return_value=result), redirect_stderr(io.StringIO()):
            report = ocr.complete(self.workspace, self.papers, self.output)
        self.assertEqual(report["needs_visual_review"], 1)
        self.assertEqual(report["extracted_with_ocr"], 0)

    def test_white_page_distinguished_from_unrecognized_content_and_engine_failure(self):
        with ocr.pymupdf.open() as pdf:
            page = pdf.new_page(width=72, height=72)
            empty = subprocess.CompletedProcess([], 0,
                "block_num\tpar_num\tline_num\tconf\ttext\n")
            with patch.object(ocr.subprocess, "run", return_value=empty):
                self.assertEqual(ocr.recognize(page)["status"], "blank_page")
                page.draw_rect((10, 10, 40, 40), color=(0, 0, 0))
                self.assertEqual(ocr.recognize(page)["status"], "needs_visual_review")
            with patch.object(ocr.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "tesseract")):
                with self.assertRaises(subprocess.CalledProcessError):
                    ocr.recognize(page)
