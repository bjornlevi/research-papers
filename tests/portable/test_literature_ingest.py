from contextlib import redirect_stderr
import io
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from research_library import catalog, releases
from research_library.build import literature_ingest as ingest
from research_library.collections import atomic_json


class LiteratureIngestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.papers = self.root / "papers"
        self.papers.mkdir()
        self.workspace = self.root / "extraction"

    def source(self, name="fictional", *, blank=False, scanned=False):
        path = self.papers / (name + ".pdf")
        with pymupdf.open() as document:
            page = document.new_page()
            if scanned:
                pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
                pixmap.clear_with(255)
                page.insert_image(pymupdf.Rect(50, 50, 150, 150), pixmap=pixmap)
            elif not blank:
                page.insert_text((50, 50), "Fictional screening population: 100 adults; 4 events. Not clinical evidence.")
                page = document.new_page()
                page.insert_text((50, 50), "Fictional footnote: denominator is all adults. Values are not adjusted.")
            document.save(path)
        digest = catalog.file_digest(path)
        return {
            "publication_id": "doi:10.0000/" + name, "source_id": "literature:" + digest,
            "sha256": digest, "filename": path.name, "bytes": path.stat().st_size, "format": "pdf",
            "title": "Authored fictional publication", "publication_date": "2025-01-01",
            "aliases": ["doi:10.0000/" + name], "types": ["article"], "membership": {},
            "curation_disease": None,
            "permissions": {"local_storage": True, "private_passages": True, "private_tables": True,
                            "original_serving": False, "public_remote_serving": False,
                            "basis": "Authored fictional fixture, private software validation."},
            "acquisition": {"acquired_at": "2026-10-19T00:00:00+00:00", "acquisition": {
                "final_url": "https://example.org/fictional.pdf", "url": "https://example.org/fictional.pdf",
                "pdf_pages": 1 if blank or scanned else 2, "license": "cc-by",
            }},
        }

    def manifest(self, sources):
        document = {
            "schema_version": 1, "policy": ingest.POLICY, "sources": sources,
            "pilot_publications": [source["publication_id"] for source in sources],
        }
        atomic_json(self.workspace, "pilot.json", document)
        return document

    def extract(self, **kwargs):
        with redirect_stderr(io.StringIO()):
            return ingest.extract(self.workspace, self.papers, **kwargs)

    def test_pdf_exact_page_locators_resume_and_private_release(self):
        source = self.source()
        self.manifest([source])
        report = self.extract()
        self.assertEqual(report["statuses"], {"extracted": 1})
        self.assertEqual(report["pages"], 2)
        before = (self.workspace / "extraction.json").read_bytes()
        with patch.object(ingest, "extract_pdf", side_effect=AssertionError("Repeated extraction")):
            self.assertEqual(self.extract(), report)
        self.assertEqual(before, (self.workspace / "extraction.json").read_bytes())
        result = ingest.verify(self.workspace, self.papers)
        self.assertTrue(result["exact_native_page_replay"])
        bundle, grants = ingest.make_bundle(self.workspace, self.papers)
        self.assertEqual(bundle["claims"], [])
        self.assertEqual(bundle["tables"], [])
        self.assertEqual(bundle["units"][1]["citation"]["locator"], {"kind": "pdf", "pdf_page": 2})
        self.assertEqual(bundle["units"][0]["citation"]["source_sha256"], source["sha256"])
        self.assertEqual(bundle["units"][0]["verification_status"], "machine_extracted")
        destination = self.root / "release"
        releases.export(bundle, destination, profile="private_context", authorization=grants)
        snapshot = releases.validate(destination, allowed_profiles={"private_context"})
        self.assertEqual(snapshot._manifest["assets"], {})
        hit = snapshot.call("search_evidence", {"query": "screening"})["result"]["items"][0]
        context = snapshot.call("get_source_context", {"unit_id": hit["id"], "max_characters": 20})["result"]
        self.assertEqual(len(context["text"]), 20)
        self.assertTrue(context["truncated"])
        self.assertEqual(hit["citation"]["locator"]["pdf_page"], 1)
        source_row = snapshot.call("list_sources", {})["result"]["items"][0]
        self.assertFalse(source_row["original_served"])
        self.assertEqual(source_row["bibliography"]["title"], source["title"])
        with self.assertRaises(releases.AccessDeniedError):
            releases.validate(destination, allowed_profiles={"curated"})

    def test_column_paragraphs_are_automatic_and_raw_extraction_is_retained(self):
        source = self.source("columns")
        path = self.papers / source["filename"]
        with pymupdf.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 100), "Left first paragraph\ncontinues on its second line.")
            page.insert_text((330, 100), "Right first paragraph\ncontinues in the right column.")
            page.insert_text((40, 160), "Left second paragraph\nends the left column.")
            page.insert_text((330, 160), "Right second paragraph\nends the right column.")
            document.save(path)
        source.update(bytes=path.stat().st_size, sha256=catalog.file_digest(path))
        source["source_id"] = "literature:" + source["sha256"]
        self.manifest([source])
        self.extract()
        state = ingest.read(self.workspace / "extraction.json")
        result = ingest.read(self.workspace / state["items"][source["publication_id"]]["file"])
        extracted = result["pages"][0]
        text = extracted["text"]
        self.assertLess(text.index("Left second"), text.index("Right first"))
        self.assertIn("Left first paragraph continues on its second line.", text)
        self.assertIn("\n\n", text)
        with pymupdf.open(path) as document:
            self.assertEqual(extracted["raw_text"], document[0].get_text("text", sort=True))
        self.assertEqual(extracted["raw_text_sha256"], ingest.identity(extracted["raw_text"]))
        self.assertNotEqual(extracted["raw_text"], text)
        self.assertEqual(ingest.POLICY["version"], "literature-pdf-3")
        self.assertTrue(ingest.verify(self.workspace, self.papers)["exact_native_page_replay"])
        bundle, _ = ingest.make_bundle(self.workspace, self.papers)
        self.assertEqual(bundle["units"][0]["text"], text)
        self.assertEqual(bundle["units"][0]["human_verification"], "not_reviewed")

    def test_line_end_dehyphenation_uses_document_words_and_preserves_compounds(self):
        source = self.source("hyphens")
        path = self.papers / source["filename"]
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((40, 100),
                "A quasi-\nexperimental study found articles cov-\nered by newspapers had higher cita-\ntion rates.")
            page.insert_text((40, 220), "Keep within-line cov- ered unchanged. Unknown zorb-\nified remains uncertain.")
            document.new_page().insert_text((40, 100),
                "These covered articles had higher citation rates in a quasi-experimental study.")
            document.save(path)
        result = ingest.extract_pdf(path, source)
        text = result["pages"][0]["text"]
        self.assertIn("quasi-experimental", text)
        self.assertIn("articles covered by newspapers", text)
        self.assertIn("higher citation rates", text)
        self.assertIn("within-line cov- ered unchanged", text)
        self.assertIn("zorb-ified", text)
        self.assertIn("cov-\nered", result["pages"][0]["raw_text"])
        self.assertEqual(result["pages"][0]["text_sha256"], ingest.identity(text))
        self.assertEqual(ingest._join_block("soft\u00ad\nware", set()), "software")
        self.assertEqual(ingest._join_block("long-\nterm", {"longterm", "long-term"}), "long-term")
        self.assertEqual(ingest.paragraph_text([(0, 0, 50, 20, "cov-"), (100, 0, 150, 20, "ered")],
                                             {"covered"}), "cov-\n\nered")

    def test_no_text_scanned_blank_sparse_and_corrupt_are_not_success(self):
        scanned = self.source("scanned", scanned=True)
        blank = self.source("blank", blank=True)
        self.manifest([scanned, blank])
        report = self.extract()
        self.assertEqual(report["statuses"], {"ocr_required": 1, "no_native_text": 1})
        with self.assertRaisesRegex(ValueError, "no native or OCR text"):
            ingest.make_bundle(self.workspace, self.papers)
        unsupported = {**blank, "format": "legacy"}
        self.assertEqual(ingest.extract_pdf(self.papers / blank["filename"], unsupported)["status"],
                         "unsupported_format")
        bad = self.papers / "bad.pdf"
        bad.write_bytes(b"%PDF-broken")
        broken = {**blank, "filename": "bad.pdf", "bytes": bad.stat().st_size,
                  "sha256": catalog.file_digest(bad), "publication_id": "doi:10.0000/broken"}
        self.manifest([broken])
        (self.workspace / "extraction.json").unlink()
        self.assertEqual(self.extract()["statuses"], {"failed": 1})
        with self.assertRaisesRegex(ValueError, "no native or OCR text"):
            ingest.make_bundle(self.workspace, self.papers)

    def test_source_checkpoint_and_policy_tampering_fail_closed(self):
        source = self.source()
        self.manifest([source])
        self.extract()
        document_path = self.workspace / "documents" / (source["sha256"] + ".json")
        data = document_path.read_bytes()
        document_path.write_bytes(data.replace(b"100 adults", b"900 adults"))
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            self.extract()
        document_path.write_bytes(data)
        path = self.papers / source["filename"]
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "bytes have changed"):
            self.extract()
        document = ingest.read(self.workspace / "pilot.json")
        document["policy"] = {**document["policy"], "version": "different"}
        atomic_json(self.workspace, "pilot.json", document)
        with self.assertRaisesRegex(ValueError, "policy"):
            self.extract()

    def test_sample_does_not_extract_unselected_sources_and_permission_is_enforced(self):
        first, second = self.source("first"), self.source("second")
        manifest = self.manifest([first, second])
        manifest["pilot_publications"] = [first["publication_id"]]
        atomic_json(self.workspace, "pilot.json", manifest)
        self.assertEqual(self.extract(sample=True)["processed"], 1)
        with self.assertRaisesRegex(ValueError, "every frozen source"):
            ingest.verify(self.workspace, self.papers)
        manifest["sources"][0]["permissions"]["private_passages"] = False
        atomic_json(self.workspace, "pilot.json", manifest)
        (self.workspace / "extraction.json").unlink()
        self.extract(sample=True)
        with self.assertRaisesRegex(ValueError, "permissions"):
            ingest.make_bundle(self.workspace, self.papers)

    def test_partial_page_label_rules_do_not_invent_a_label_for_unlabeled_pages(self):
        source = self.source()
        path = self.papers / source["filename"]
        with pymupdf.open(path) as document:
            document.set_page_labels([{"startpage": 1, "prefix": "Appendix-", "style": "D", "firstpagenum": 1}])
            updated = document.tobytes()
        path.write_bytes(updated)
        result = ingest.extract_pdf(path, source)
        self.assertEqual(result["pages"][0]["page_label"], "")
        self.assertEqual(result["pages"][1]["page_label"], "Appendix-1")
        self.assertEqual([page["pdf_page"] for page in result["pages"]], [1, 2])

    def test_full_library_includes_unselected_pmid_without_inventing_doi(self):
        first, second = self.source("first"), self.source("pmid")
        second.update(publication_id="pmid:123456", aliases=["pmid:123456"])
        manifest = self.manifest([first, second])
        manifest["pilot_publications"] = [first["publication_id"]]
        atomic_json(self.workspace, "pilot.json", manifest)
        self.extract()
        pilot, _ = ingest.make_bundle(self.workspace, self.papers)
        self.assertEqual(len(pilot["sources"]), 1)
        bundle, grants = ingest.make_bundle(self.workspace, self.papers, all_sources=True)
        self.assertEqual(len(bundle["sources"]), 2)
        self.assertEqual(bundle["sources"][1]["doi"], "pmid:123456")
        destination = self.root / "full-release"
        releases.export(bundle, destination, profile="private_context", authorization=grants)
        snapshot = releases.validate(destination, allowed_profiles={"private_context"})
        result = snapshot.call("search_evidence", {"query": "screening", "paper": "pmid:123456"})["result"]
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["citation"]["doi"], "pmid:123456")
        self.assertIn("not an invented DOI", result["items"][0]["citation"]["provenance"])

    def curation(self, source):
        anchor = {"doi": source["publication_id"][4:], "pdf_page": 1,
                  "anchor": "Fictional screening population"}
        shared = {**anchor, "caveats": ["Authored fictional records only."]}
        return {
            "schema_version": 1, "source_hashes": {source["publication_id"][4:]: source["sha256"]},
            "claims": [{
                **shared, "id": "fictional:claim", "statement": "A fictional example, not a clinical claim.",
                "claim_kind": "observed_result", "population": "100 fictional adults",
                "study_design": "Authored fixture", "cohort_family": "fictional",
                "comparator": None, "follow_up": None, "adjustment": None,
                "source_issue_ids": ["fictional:issue"],
                "numbers": [{
                    "id": "fictional:number", "label": "Fictional events", "value": 4,
                    "reported_value": "4", "measure": "count", "unit": "events", "numerator": 4,
                    "denominator": 100, "ci_95": None, "reference_interval": None,
                    "context": "Not empirical evidence", "anchor": "100 adults; 4 events",
                }],
            }],
            "issues": [{
                **shared, "id": "fictional:issue", "title": "Fictional ambiguity",
                "status": "unresolved", "category": "ambiguity", "context": "Authored fixture",
                "explanation": "No clinical interpretation", "additional_anchors": [],
            }],
            "tables": [{
                **shared, "id": "fictional:table", "title": "Fictional cells",
                "headers": ["Events", "Unreported"], "units": ["count", None], "rows": [[4, None]],
                "footnotes": ["Fictional missing value stays null, not zero."], "cell_anchors": ["4 events"],
            }],
            "relationships": [],
        }

    def test_curation_requires_frozen_hashes_original_pages_and_unambiguous_anchors(self):
        source = self.source()
        self.manifest([source])
        self.extract()
        specifications = self.curation(source)
        bundle, _ = ingest.make_bundle(self.workspace, self.papers, specifications)
        claim = bundle["claims"][0]
        self.assertEqual(claim["verification_status"], "source_checked_by_ai")
        self.assertEqual(claim["human_verification"], "not_reviewed")
        self.assertIsNone(claim["numbers"][0]["ci_95"])
        self.assertIsNone(bundle["tables"][0]["rows"][0][1])
        self.assertIn("fictional:issue", bundle["units"][0]["source_issue_ids"])
        changed = deepcopy(specifications)
        changed["source_hashes"][source["publication_id"][4:]] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source versions"):
            ingest.make_bundle(self.workspace, self.papers, changed)
        changed = deepcopy(specifications)
        changed["claims"][0]["anchor"] = "Absent source quote"
        with self.assertRaisesRegex(ValueError, "anchor"):
            ingest.make_bundle(self.workspace, self.papers, changed)
        changed = deepcopy(specifications)
        changed["tables"][0]["cell_anchors"] = ["99 invented events"]
        with self.assertRaisesRegex(ValueError, "anchor"):
            ingest.make_bundle(self.workspace, self.papers, changed)

    def test_freeze_verifies_acquisition_and_retains_explicit_source_permissions(self):
        source = self.source()
        acquisition_root = self.root / "acquisition"
        identifier = source["publication_id"]
        entry = {"status": "downloaded", "filename": source["filename"], "sha256": source["sha256"],
                 "bytes": source["bytes"], **source["acquisition"]}
        acquisition = {"catalog_id": "a" * 64, "items": {
            identifier: entry, "doi:10.0000/blocked": {"status": "licensed_locations_failed"},
        }}
        atomic_json(acquisition_root, "acquisition.json", acquisition)
        atomic_json(acquisition_root, "inventory.json", {
            "catalog_id": "a" * 64, "memberships": [{"id": identifier, "background_of": [], "impact_on": []}],
        })
        class Snapshot:
            release_id = "a" * 64
            def get(self, key):
                return {**source, "id": key, "publication_types": ["article"]}
        with patch.object(catalog, "open_snapshot", return_value=Snapshot()):
            with self.assertRaisesRegex(ValueError, "authorization"):
                ingest.freeze(self.workspace, acquisition_root, self.papers, "")
            report = ingest.freeze(self.workspace, acquisition_root, self.papers, "Authored fixture private use.")
            self.assertEqual(report["sources"], 1)
            frozen = ingest.read(self.workspace / "pilot.json")
            self.assertFalse(frozen["sources"][0]["permissions"]["public_remote_serving"])
            self.assertFalse(frozen["sources"][0]["permissions"]["original_serving"])
            self.assertEqual(len(frozen["metadata_only_cases"]), 1)
            with self.assertRaisesRegex(ValueError, "new non-symlink"):
                ingest.freeze(self.workspace, acquisition_root, self.papers, "Authored fixture.")


if __name__ == "__main__":
    unittest.main()
