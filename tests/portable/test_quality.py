from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import httpx

from research_library import collections, quality, releases, web_server
from research_library.quality_cli import repair
from fixtures import snapshots
from test_collections import collection_cases, registry_fixture
from test_http import running_server
from test_releases import fixture_bundle


class QualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.quality = self.root / "quality"
        self.bundle, _ = fixture_bundle(self.root)
        self.release = self.root / "release"
        releases.export(self.bundle, self.release, profile="synthetic")
        self.snapshot = releases.validate(self.release, allowed_profiles={"synthetic"})

    def inventory(self, snapshot=None):
        snapshot = snapshot or self.snapshot
        for source in snapshot._bundle["sources"]:
            locators = [unit["citation"]["locator"] for unit in snapshot._bundle["units"]
                        if unit["citation"]["source_id"] == source["id"]]
            quality.inventory(self.quality, source, locators)

    def verify_reviews(self, snapshot=None):
        snapshot = snapshot or self.snapshot
        for finding in quality.report(snapshot, self.quality)["findings"]:
            if not finding["resolved"] and "verified" in finding["allowed_decisions"]:
                quality.review(snapshot, self.quality, finding["id"], "verified", "Test operator",
                               "Compared all retained text, tables and expected locations with the fictional original.")

    def test_inventory_and_reviews_do_not_modify_evidence(self):
        before = snapshots(self.release)
        first = quality.report(self.snapshot, self.quality)
        self.assertEqual(first["blockers"], 3)
        self.assertEqual({row["code"] for row in first["findings"]}, {"inventory_missing"})
        with self.assertRaisesRegex(releases.ReleaseError, "must be repaired"):
            quality.review(self.snapshot, self.quality, first["findings"][0]["id"], "verified", "A", "Checked")
        self.inventory()
        self.assertEqual(quality.report(self.snapshot, self.quality)["blockers"], 3)
        self.verify_reviews()
        self.assertTrue(quality.require_ready(self.snapshot, self.quality)["ready"])
        self.assertEqual(snapshots(self.release), before)
        record = next((self.quality / "reviews").glob("*.json"))
        row = json.loads(record.read_text())
        self.assertEqual(row["reviewer"], "Test operator")
        self.assertTrue(row["at"])
        record.write_text("{}")
        with self.assertRaisesRegex(releases.ReleaseError, "checksum"):
            quality.report(self.snapshot, self.quality)

    def test_missing_trailing_page_blank_and_stale_review(self):
        self.inventory()
        source = self.snapshot._bundle["sources"][0]
        quality.inventory(self.quality, source, [{"kind": "pdf", "pdf_page": page} for page in (1, 2)])
        self.verify_reviews()
        report = quality.report(self.snapshot, self.quality)
        missing = next(row for row in report["findings"] if row["code"] == "missing_unit")
        self.assertEqual(missing["locator"]["pdf_page"], 2)
        self.assertFalse(report["ready"])
        with self.assertRaises(releases.ReleaseError):
            quality.review(self.snapshot, self.quality, missing["id"], "verified", "A", "Skip it")
        quality.review(self.snapshot, self.quality, missing["id"], "verified_blank", "A",
                       "Viewed original page 2: genuinely blank, no text or scientific content.")
        self.assertTrue(quality.report(self.snapshot, self.quality)["ready"])
        changed = deepcopy(self.bundle)
        changed["units"][0]["text"] = "Changed extraction."
        target = self.root / "changed"
        releases.export(changed, target, profile="synthetic")
        candidate = releases.validate(target, allowed_profiles={"synthetic"})
        self.assertFalse(quality.report(candidate, self.quality)["ready"])
        with self.assertRaisesRegex(releases.ReleaseError, "stale"):
            quality.review(candidate, self.quality, missing["id"], "verified_blank", "A", "Old review")

    def test_legacy_problem_records_remain_readable(self):
        quality.problem(self.snapshot, self.quality, "fictional-unit-1", "A", "Original has a different number.")
        event = quality._events(self.quality, "problems")[0]
        self.assertEqual(event["schema_version"], 2)
        event.update(schema_version=1, extraction_key=quality.unit_key(self.snapshot._bundle["units"][0]))
        legacy_root = self.root / "legacy-quality"
        quality._write(legacy_root, "problems", event)
        self.assertIn("reported_defect",
                      {item["code"] for item in quality.report(self.snapshot, legacy_root)["findings"]})

    def test_ocr_reported_defect_requires_repair_and_keeps_history(self):
        changed = deepcopy(self.bundle)
        changed["units"][0]["caveats"].append("Mean OCR word confidence: 99.0")
        changed["units"][0]["text"] = "OCR wrong 2O"
        bad = self.root / "bad"
        releases.export(changed, bad, profile="synthetic")
        candidate = releases.validate(bad, allowed_profiles={"synthetic"})
        self.inventory(candidate)
        self.verify_reviews(candidate)
        self.assertTrue(quality.report(candidate, self.quality)["ready"])
        quality.problem(candidate, self.quality, "fictional-unit-1", "Operator", "Wrong number: 2O, original says 20.")
        report = quality.report(candidate, self.quality)
        defect = next(row for row in report["findings"] if row["code"] == "reported_defect")
        self.assertFalse(report["ready"])
        with self.assertRaises(releases.ReleaseError):
            quality.review(candidate, self.quality, defect["id"], "verified", "Operator", "Ignore")
        # A repair elsewhere must not clear the known defect.
        other = self.root / "other-repair"
        repair(candidate, other, unit_id="fictional-unit-2", text="Changed Word text.", reviewer="A", note="Compared")
        other_snapshot = releases.validate(other, allowed_profiles={"synthetic"})
        self.assertIn("reported_defect", {row["code"] for row in quality.report(other_snapshot, self.quality)["findings"]})
        fixed = self.root / "fixed"
        old_bytes = snapshots(bad)
        repair(candidate, fixed, unit_id="fictional-unit-1", text="OCR corrected 20",
               reviewer="Operator", note="Compared with the original; retained native/OCR records separately.")
        fixed_snapshot = releases.validate(fixed, allowed_profiles={"synthetic"})
        self.assertFalse(quality.report(fixed_snapshot, self.quality)["ready"])
        self.verify_reviews(fixed_snapshot)
        self.assertTrue(quality.require_ready(fixed_snapshot, self.quality)["ready"])
        self.assertEqual(snapshots(bad), old_bytes)
        self.assertEqual(len(list((self.quality / "problems").glob("*.json"))), 1)
        self.assertEqual(fixed_snapshot._bundle["sources"], candidate._bundle["sources"])
        self.assertIn(candidate.release_id, fixed_snapshot._bundle["units"][0]["caveats"][-1])

    def test_public_store_persists_gate_and_rejection_preserves_active(self):
        public = releases.ReleaseStore(self.root / "public", allowed_profiles={"synthetic"},
                                       public_quality_root=self.quality)
        identifier = public.install(self.release)
        with self.assertRaisesRegex(releases.ReleaseError, "Public activation blocked"):
            public.activate(identifier)
        self.assertFalse((public.root / "active.json").exists())
        self.inventory()
        self.verify_reviews()
        public.activate(identifier)
        pointer = (public.root / "active.json").read_bytes()
        reopened = releases.ReleaseStore(public.root, allowed_profiles={"synthetic"})
        self.assertEqual(reopened.current().release_id, identifier)
        quality.problem(self.snapshot, self.quality, "fictional-unit-1", "A", "Discovered wrong text after publishing.")
        with self.assertRaisesRegex(releases.ReleaseError, "Public activation blocked"):
            reopened.current()
        with self.assertRaisesRegex(releases.ReleaseError, "Public activation blocked"):
            reopened.activate(identifier)
        self.assertEqual((public.root / "active.json").read_bytes(), pointer)
        # Development access is unchanged.
        self.assertTrue(self.snapshot.source_context("fictional-unit-1")["record"]["text"])
        with self.assertRaisesRegex(releases.ReleaseError, "separate public store"):
            self.convert_active()

    def convert_active(self):
        store = releases.ReleaseStore(self.root / "development-active", allowed_profiles={"synthetic"})
        store.activate(store.install(self.release))
        releases.ReleaseStore(store.root, allowed_profiles={"synthetic"}, public_quality_root=self.quality)

    def test_public_collection_gate_checks_even_nonselected_collections(self):
        store, first, second, _ = registry_fixture(self.root)
        document = deepcopy(store.current().document)
        public = collections.CollectionStore(
            self.root / "public-registry", allowed_profiles={"synthetic"},
            allowed_collections={"istopmm"}, public_quality_root=self.quality)
        for path in (first, second):
            public.releases.install(path)
        self.inventory()
        self.verify_reviews()
        with self.assertRaisesRegex(releases.ReleaseError, "Public activation blocked"):
            public.publish(document)
        self.assertFalse((public.root / "active.json").exists())
        second_snapshot = releases.validate(second, allowed_profiles={"synthetic"})
        self.verify_reviews(second_snapshot)
        sid = public.publish(document)
        reopened = collections.CollectionStore(public.root, allowed_profiles={"synthetic"},
                                               allowed_collections={"istopmm"})
        self.assertEqual(reopened.current().release_id, sid)
        quality.problem(second_snapshot, self.quality, "fictional-unit-1", "A", "Wrong text in hidden collection.")
        with self.assertRaises(releases.ReleaseError):
            reopened.load(sid)

    def test_rollback_cannot_bypass_gate(self):
        self.inventory()
        self.verify_reviews()
        public = releases.ReleaseStore(self.root / "public", allowed_profiles={"synthetic"},
                                       public_quality_root=self.quality)
        first = public.install(self.release)
        public.activate(first)
        changed = self.root / "changed"
        repair(self.snapshot, changed, unit_id="fictional-unit-1", text="Corrected fictional text.",
               reviewer="A", note="Compared with the original.")
        candidate = releases.validate(changed, allowed_profiles={"synthetic"})
        second = public.install(changed)
        pointer = (public.root / "active.json").read_bytes()
        with self.assertRaisesRegex(releases.ReleaseError, "Public activation blocked"):
            public.activate(second)
        self.assertEqual((public.root / "active.json").read_bytes(), pointer)
        self.verify_reviews(candidate)
        public.activate(second)
        pointer = (public.root / "active.json").read_bytes()
        quality.problem(self.snapshot, self.quality, "fictional-unit-1", "A", "Known error in older text.")
        with self.assertRaisesRegex(releases.ReleaseError, "Public activation blocked"):
            public.rollback()
        self.assertEqual((public.root / "active.json").read_bytes(), pointer)
        self.assertEqual(public.current().release_id, second)

    def test_empty_truncated_duplicate_and_original_assets_are_blockers(self):
        self.inventory()
        bundle = deepcopy(self.bundle)
        bundle["units"][0].update(text="", truncated=True)
        duplicate = deepcopy(bundle["units"][0])
        duplicate["id"] = "duplicate-location"
        bundle["units"].append(duplicate)
        path = self.root / "defects"
        releases.export(bundle, path, profile="synthetic")
        snapshot = releases.validate(path, allowed_profiles={"synthetic"})
        report = quality.report(snapshot, self.quality)
        codes = {row["code"] for row in report["findings"]}
        self.assertTrue({"missing_text", "truncated_text", "locator_mismatch"} <= codes)
        self.verify_reviews(snapshot)
        with self.assertRaises(releases.ReleaseError):
            quality.require_ready(snapshot, self.quality)
        _, assets = fixture_bundle(self.root)
        path = self.root / "with-originals"
        releases.export(self.bundle, path, profile="synthetic", asset_paths=assets)
        snapshot = releases.validate(path, allowed_profiles={"synthetic"})
        self.assertIn("original_included", {row["code"] for row in quality.report(snapshot, self.quality)["findings"]})
        with self.assertRaises(releases.ReleaseError):
            quality.require_ready(snapshot, self.quality)

    def test_public_registry_browser_json_stdio_and_http_parity(self):
        store, first, second, _ = registry_fixture(self.root)
        document = deepcopy(store.current().document)
        public = collections.CollectionStore(
            self.root / "public-platform", allowed_profiles={"synthetic"},
            allowed_collections={"istopmm", "test-literature"}, defaults=["istopmm"],
            public_quality_root=self.quality)
        for path in (first, second):
            public.releases.install(path)
            snapshot = releases.validate(path, allowed_profiles={"synthetic"})
            self.inventory(snapshot)
            self.verify_reviews(snapshot)
        sid = public.publish(document)
        app = web_server.create_app(registry=public.root, profiles={"synthetic"},
                                    allowed_collections={"istopmm", "test-literature"})
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            response = client.get("/api/text", params={"collection": "istopmm", "snapshot_id": sid,
                                                       "unit_id": "fictional-unit-1"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["result"]["record"], self.snapshot.source_context("fictional-unit-1")["record"])
            self.assertEqual(client.get("/sources").status_code, 200)
            self.assertEqual(client.get("/quality").status_code, 403)
            self.assertEqual(client.post("/quality").status_code, 403)
        expected = self.root / "public-expected.json"
        expected.write_text(json.dumps(collection_cases(public)))
        scripts = Path(__file__).resolve().parents[1]
        result = subprocess.run([
            sys.executable, str(scripts / "mcp_client.py"),
            "--server", str(Path(sys.executable).parent / "research-mcp"),
            "--registry", str(public.root), "--expected", str(expected), "--cwd", str(self.root),
            "--allow-collection", "istopmm", "--allow-collection", "test-literature",
            "--default-collection", "istopmm",
        ], capture_output=True, text=True, cwd=self.root, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with running_server(self.root, None, collection_registry=public.root,
                            allowed_collections=["istopmm", "test-literature"],
                            default_collections=["istopmm"]) as (url, token, registry, _, log):
            result = subprocess.run([
                sys.executable, str(scripts / "http_client.py"), "--url", url,
                "--token-file", str(self.root / "token"), "--expected", str(expected),
            ], capture_output=True, text=True, cwd=self.root, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            quality.problem(self.snapshot, self.quality, "fictional-unit-1", "A", "Newly identified extraction defect.")
            self.assertEqual(httpx.get(url.replace("/mcp", "/readyz")).status_code, 503)
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            self.assertEqual(client.get("/api/collections").status_code, 503)

    def test_web_report_inspection_and_cli_failure(self):
        self.inventory()
        app = web_server.create_app(release=self.release, profiles={"synthetic"}, quality_root=self.quality)
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            response = client.get("/quality")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Public readiness: Not ready", response.text)
            self.assertIn('name="kind"', response.text)
            self.assertIn("Document completeness review: 3 pending", response.text)
            self.assertEqual(client.get("/quality?kind=coverage_review").status_code, 200)
            self.assertEqual(client.get("/quality?kind=invalid").status_code, 400)
            finding = quality.report(self.snapshot, self.quality)["findings"][0]
            response = client.get("/quality", params={"finding": finding["id"], "unit_id": "fictional-unit-1"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Fictional screening context.", response.text)
            self.assertIn("Next text slice", response.text)
            self.assertIn("not a detected extraction error", response.text)
            self.assertIn("Why this is flagged", response.text)
            self.assertIn("What to do", response.text)
            self.assertIn("Take action", response.text)
            self.assertIn("Technical identifiers (for troubleshooting)", response.text)
            self.assertIn('name="decision"', response.text)
            self.assertIn('value="verified"', response.text)
            self.assertIn('value="problem"', response.text)
            self.assertNotIn("quality_cli", response.text)
            self.assertIn(finding["id"], response.text)
            self.assertEqual(client.get("/quality?finding=missing").status_code, 404)
            self.assertEqual(client.get("/quality?offset=-1").status_code, 400)
            self.assertEqual(client.post("/quality").status_code, 403)
            self.assertTrue(client.get("/api/text?unit_id=fictional-unit-1").json()["ok"])
        result = subprocess.run(
            [sys.executable, "-m", "research_library.quality_cli", "--release", str(self.release),
             "--allow-profile", "synthetic", "--quality-root", str(self.quality), "check"],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ready"])

    def test_operator_finding_explanations_locations_and_repair_instructions(self):
        bundle = deepcopy(self.bundle)
        bundle["units"][1]["text"] = ""
        bundle["units"][0]["caveats"].append("Mean OCR word confidence: 61; visual review advised.")
        for number in (11, 3):
            unit = deepcopy(bundle["units"][1])
            unit["id"] = f"word-element-{number}"
            unit["text"] = f"Neighbouring paragraph {number}."
            unit["citation"]["locator"]["body_element"] = number
            bundle["units"].append(unit)
        path = self.root / "operator"
        releases.export(bundle, path, profile="synthetic")
        snapshot = releases.validate(path, allowed_profiles={"synthetic"})
        self.inventory(snapshot)
        findings = quality.report(snapshot, self.quality)["findings"]
        missing = next(row for row in findings if row["code"] == "missing_text")
        coverage = next(row for row in findings if row["source_id"] == missing["source_id"] and row["code"] == "coverage_review")
        ocr = next(row for row in findings if row["code"] == "ocr_review")
        app = web_server.create_app(release=path, profiles={"synthetic"}, quality_root=self.quality)
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            response = client.get("/quality")
            self.assertIn("Word body element 2 (table)", response.text)
            self.assertNotIn("; null;", response.text)
            response = client.get("/quality", params={"finding": missing["id"]})
            self.assertIn("A retained location exists, but its text is empty.", response.text)
            self.assertIn("No text was extracted at this location.", response.text)
            self.assertIn("Neighbouring paragraph 3.", response.text)
            self.assertIn("not printed pages", client.get("/quality", params={"finding": coverage["id"]}).text)
            self.assertIn('name="unit_id" value="fictional-unit-2"', response.text)
            self.assertIn('value="verified_nontext"', response.text)
            self.assertIn("Record comparison", response.text)
            self.assertNotIn("quality_cli", response.text)
            response = client.get("/quality", params={"finding": coverage["id"]})
            self.assertLess(response.text.index("Word body element 2 (table)"),
                            response.text.index("Word body element 3 (table)"))
            self.assertLess(response.text.index("Word body element 3 (table)"),
                            response.text.index("Word body element 11 (table)"))
            self.assertNotIn("No specific retained text unit", response.text)
            response = client.get("/quality", params={"finding": ocr["id"]})
            self.assertIn("Mean OCR word confidence: 61", response.text)
            self.assertIn("decimal points", response.text)

    def test_operator_inventory_and_omitted_page_actions(self):
        app = web_server.create_app(release=self.release, profiles={"synthetic"}, quality_root=self.quality)
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            finding = quality.report(self.snapshot, self.quality)["findings"][0]
            response = client.get("/quality", params={"finding": finding["id"]})
            self.assertIn("Build location inventory", response.text)
            self.assertIn('name="original"', response.text)
            self.assertIn("omitted final page", response.text)
            self.inventory()
            source = self.bundle["sources"][0]
            quality.inventory(self.quality, source, [{"kind": "pdf", "pdf_page": page} for page in (1, 2)])
            finding = next(row for row in quality.report(self.snapshot, self.quality)["findings"] if row["code"] == "missing_unit")
            response = client.get("/quality", params={"finding": finding["id"]})
            self.assertIn("PDF page 2", response.text)
            self.assertIn("release has no text unit", response.text)
            self.assertIn('value="verified_blank"', response.text)
            self.assertIn("Record comparison", response.text)
            self.assertNotIn("quality_cli", response.text)
            self.assertIn("Fictional screening context.", response.text)

    def test_publication_link_is_not_the_supplement_download(self):
        bundle = deepcopy(self.bundle)
        url = ("https://static-content.springer-cdn.com/esm/art%3A10.1038%2Fs41408-023-00951-2/"
               "MediaObjects/41408_2023_951_MOESM1_ESM.docx")
        # Keep citation and source identities consistent in the synthetic release.
        bundle["sources"][0]["url"] = url
        def update(value):
            if isinstance(value, dict):
                if value.get("source_id") == "fictional:main":
                    value["source_url"] = url
                for child in value.values():
                    update(child)
            elif isinstance(value, list):
                for child in value:
                    update(child)
        update(bundle)
        path = self.root / "download-link"
        releases.export(bundle, path, profile="synthetic")
        snapshot = releases.validate(path, allowed_profiles={"synthetic"})
        finding = quality.report(snapshot, self.quality)["findings"][0]
        app = web_server.create_app(release=path, profiles={"synthetic"}, quality_root=self.quality)
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            response = client.get("/quality", params={"finding": finding["id"]})
            self.assertIn('href="https://doi.org/10.0000/fictional" rel="noreferrer">Open publication source', response.text)
            self.assertIn('rel="noreferrer">Try recorded document link', response.text)
            self.assertIn("Supplementary information", response.text)
            self.assertIn("not a verified working link", response.text)
        self.assertEqual(web_server.publication_url("10.1038/s41408-023-00951-2"),
                         "https://doi.org/10.1038/s41408-023-00951-2")
        self.assertEqual(web_server.publication_url("https://doi.org/10.0000/Example"),
                         "https://doi.org/10.0000/example")
        self.assertEqual(web_server.publication_url("pmid:12345"), "https://pubmed.ncbi.nlm.nih.gov/12345/")
        self.assertIsNone(web_server.publication_url("pmid:unknown"))
        self.assertIsNone(web_server.publication_url("unknown"))

    def test_successful_acquisition_replaces_failed_candidate_by_exact_identity(self):
        source = self.bundle["sources"][0]
        acquired = "https://media.springernature.com/actually-downloaded.docx"
        folder = self.root / "papers/supplements"
        folder.mkdir(parents=True)
        path = folder / "inventory.json"
        inventory = [{"doi": source["doi"], "files": [
            {"status": "acquired", "sha256": source["sha256"], "downloaded_from": acquired,
             "urls": ["https://failed.example.org/candidate.docx"]},
            {"status": "failed", "sha256": source["sha256"], "downloaded_from": "https://failed.example.org/other"},
        ]}]
        path.write_text(json.dumps(inventory))
        app = web_server.create_app(release=self.release, profiles={"synthetic"},
                                    quality_root=self.quality, data_root=self.root)
        finding = quality.report(self.snapshot, self.quality)["findings"][0]
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            for url in ("/quality?finding=" + finding["id"], "/sources"):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'href="{acquired}"', response.text)
                self.assertIn("Open successfully acquired document", response.text)
                self.assertNotIn("https://failed.example.org/", response.text)
            inventory[0]["files"][0]["sha256"] = "0" * 64
            path.write_text(json.dumps(inventory))
            self.assertNotIn("Open successfully acquired document", client.get("/quality?finding=" + finding["id"]).text)
            inventory[0]["files"][0]["sha256"] = source["sha256"]
            inventory[0]["doi"] = "10.0000/different-publication"
            path.write_text(json.dumps(inventory))
            self.assertNotIn("Open successfully acquired document", client.get("/sources").text)

    def test_missing_unit_repair_and_invalid_inventory(self):
        self.inventory()
        source = self.bundle["sources"][0]
        with self.assertRaisesRegex(releases.ReleaseError, "every original page"):
            quality.inventory(self.quality, source, [{"kind": "pdf", "pdf_page": 2}])
        quality.inventory(self.quality, source, [{"kind": "pdf", "pdf_page": page} for page in (1, 2)])
        destination = self.root / "added"
        repair(self.snapshot, destination, source_id=source["id"], locator={"kind": "pdf", "pdf_page": 2},
               text="Previously missing page two.", reviewer="A", note="Transcribed against original page two.")
        candidate = releases.validate(destination, allowed_profiles={"synthetic"})
        self.assertNotIn("missing_unit", {row["code"] for row in quality.report(candidate, self.quality)["findings"]})
        self.verify_reviews(candidate)
        self.assertTrue(quality.require_ready(candidate, self.quality)["ready"])


if __name__ == "__main__":
    unittest.main()
