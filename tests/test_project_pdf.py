import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf

from research_library import collections, literature, releases, web_server
from research_library.build import citation_neighborhood, project_library


class ProjectPdfTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.pdf_root = self.root.parent / "pdfs"
        project_library.initialize(self.root, "PDF fixture", "Private authorized research")
        project_library.register(self.root, [{"doi": "10.0000/example", "title": "Synthetic example"}])
        self.work = {"id": "https://openalex.org/W123", "doi": "https://doi.org/10.0000/example",
                     "publication_date": "2020-01-01", "locations": [{
                         "license": "cc-by", "pdf_url": "https://example.org/paper.pdf",
                         "version": "publishedVersion", "landing_page_url": "https://example.org/paper"}]}
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((50, 50), "Synthetic PDF document for testing acquisition without extraction.")
            self.data = pdf.tobytes()

    def test_find_downloads_with_provenance_but_does_not_attach(self):
        with patch.object(literature.MetadataClient, "get", return_value=(self.work, {
                "url": "https://api.openalex.org/works/W123", "retrieved_at": "2026-11-10T00:00:00Z",
                "sha256": "a" * 64})), patch.object(citation_neighborhood, "download", return_value=(
                    self.data, {"final_url": "https://example.org/paper.pdf", "pdf_pages": 1})):
            result = project_library.find_pdf(self.root, "10.0000/example", self.pdf_root)
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["filename"], "synthetic-example.pdf")
        self.assertEqual(result["year"], "2020")
        self.assertEqual((self.pdf_root / result["filename"]).read_bytes(), self.data)
        saved = project_library.downloaded_pdf_metadata(self.pdf_root)
        self.assertEqual(saved[("doi:10.0000/example", releases.sha256(self.data))]["final_url"],
                         "https://example.org/paper.pdf")
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "awaiting_pdf")
        self.assertFalse((self.root / "registry/active.json").exists())

    def test_readable_names_are_safe_repeatable_and_do_not_overwrite_collisions(self):
        self.pdf_root.mkdir()
        digest = releases.sha256(self.data)
        name = project_library.save_named_pdf(
            self.pdf_root, self.data, digest, "../R\u00e9sum\u00e9: A paper / title?")
        self.assertEqual(name, "resume-a-paper-title.pdf")
        self.assertEqual(project_library.save_named_pdf(
            self.pdf_root, self.data, digest, "../R\u00e9sum\u00e9: A paper / title?"), name)
        other = self.data + b"\n"
        other_digest = releases.sha256(other)
        collision = project_library.save_named_pdf(
            self.pdf_root, other, other_digest, "Resume A paper title")
        self.assertEqual(collision, f"resume-a-paper-title-{other_digest[:12]}.pdf")
        self.assertEqual((self.pdf_root / name).read_bytes(), self.data)
        self.assertEqual((self.pdf_root / collision).read_bytes(), other)
        long_name = project_library.save_named_pdf(self.pdf_root, self.data, digest, "a" * 1000)
        self.assertLessEqual(len(long_name), 144)
        (self.pdf_root / "unsafe.pdf").symlink_to(self.pdf_root / name)
        with self.assertRaisesRegex(ValueError, "Symlinked"):
            project_library.save_named_pdf(self.pdf_root, self.data, digest, "unsafe")

    def test_no_license_does_not_attempt_download_and_mismatch_is_rejected(self):
        self.work["locations"][0]["license"] = None
        with patch.object(literature.MetadataClient, "get", return_value=(self.work, {})), \
                patch.object(citation_neighborhood, "download") as download:
            result = project_library.find_pdf(self.root, "10.0000/example", self.pdf_root)
            self.assertEqual(result["status"], "no_explicitly_licensed_pdf")
            download.assert_not_called()
            self.work["doi"] = "https://doi.org/10.0000/different"
            with self.assertRaisesRegex(ValueError, "different DOI"):
                project_library.find_pdf(self.root, "10.0000/example", self.pdf_root)
        self.assertFalse(list(self.pdf_root.glob("*.pdf")))

    def test_web_background_progress_busy_guard_and_error(self):
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdf_root, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        settings = client.get("/settings", base_url=base).get_data(as_text=True)
        self.assertIn('action="/settings/find-pdf"', settings)
        token = re.search(r'name="token" value="([^"]+)"', settings).group(1)
        self.assertEqual(client.post("/settings/find-pdf",
            data={"doi": "10.0000/example"}, base_url=base).status_code, 403)
        started, finish = threading.Event(), threading.Event()

        def find(*args, **kwargs):
            started.set()
            finish.wait(5)
            raise literature.DiscoveryError("Provider quota exhausted; retry later")

        with patch.object(project_library, "find_pdf", side_effect=find):
            response = client.post("/settings/find-pdf",
                data={"doi": "10.0000/example", "token": token}, base_url=base)
            self.assertEqual(response.status_code, 303)
            try:
                self.assertTrue(started.wait(2))
                progress = client.get(response.location, base_url=base)
                self.assertIn(b"<progress", progress.data)
                self.assertEqual(client.post("/settings/reset",
                    data={"confirmation": "RESET PROJECT"}, base_url=base).status_code, 503)
            finally:
                finish.set()
                app.extensions["find_pdf"]["thread"].join(5)
        progress = client.get(response.location, base_url=base)
        self.assertIn(b"Provider quota exhausted", progress.data)
        self.assertNotIn("Refresh", progress.headers)
        self.assertEqual(json.loads((self.root / "pdf-job.json").read_text())["status"], "failed")
        for result in [
            {"status": "downloaded", "filename": "synthetic.pdf", "doi": "10.0000/example",
             "final_url": "https://example.org/paper.pdf", "acquisition": {"license": "cc-by"}},
            {"status": "no_explicitly_licensed_pdf", "attempts": []},
        ]:
            with self.subTest(status=result["status"]), \
                    patch.object(project_library, "find_pdf", return_value=result):
                response = client.post("/settings/find-pdf",
                    data={"doi": "10.0000/example", "token": token}, base_url=base)
                self.assertEqual(response.status_code, 303)
                app.extensions["find_pdf"]["thread"].join(5)
                progress = client.get(response.location, base_url=base)
                if result["status"] == "downloaded":
                    self.assertIn(b"Not attached or extracted", progress.data)
                    self.assertIn(b"synthetic.pdf", progress.data)
                else:
                    self.assertIn(b"No PDF was downloaded", progress.data)

    def discovery(self):
        graph = {
            "schema_version": 1, "acquired_at": "2026-11-10T12:00:00Z",
            "nodes": {"neighbor": {"record": {"title": "Discovered synthetic paper",
                                             "identifiers": {"doi": "10.0000/neighbor"}},
                                   "sources": []},
                      "unresolved": {"record": {"title": "No DOI paper", "identifiers": {}},
                                     "sources": []}},
            "edges": [{"seed_doi": "10.0000/example", "direction": "background",
                       "target_provider_record": key, "sources": []} for key in ["neighbor", "unresolved"]],
            "seeds": [{"seed_doi": "10.0000/example", "title": "Synthetic example", "providers": {}}],
            "unresolved": [], "resolution_issues": [], "limitations": [],
        }
        folder = self.root / "discovery" / ("a" * 32)
        collections.atomic_json(folder, "neighborhood.json", graph)
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "label": "References", "direction": "background",
            "started_at": graph["acquired_at"]})

    def test_library_discovered_attachment_is_searchable_without_becoming_a_core_seed(self):
        self.discovery()
        self.pdf_root.mkdir()
        pdf = self.pdf_root / "discovered-paper.pdf"
        pdf.write_bytes(self.data)
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdf_root, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        listing = client.get("/library", base_url=base)
        self.assertIn(b'name="doi" value="10.0000/neighbor"', listing.data)
        self.assertNotIn(b'name="doi" value=""', listing.data)
        self.assertIn(b'Select PDF and attach/extract', listing.data)
        token = re.search(r'name="token" value="([^"]+)"', listing.get_data(as_text=True)).group(1)
        with patch.object(project_library, "find_pdf", return_value={
            "status": "downloaded", "doi": "10.0000/neighbor", "filename": pdf.name,
            "final_url": "https://example.org/paper.pdf", "acquisition": {"license": "cc-by"},
        }) as find:
            response = client.post("/settings/find-pdf", data={
                "token": token, "doi": "10.0000/neighbor", "title": "Discovered synthetic paper",
                "return_to": "/library"}, base_url=base)
            self.assertEqual(response.status_code, 303)
            app.extensions["find_pdf"]["thread"].join(5)
            find.assert_called_once_with(self.root, "10.0000/neighbor", self.pdf_root,
                                         title="Discovered synthetic paper")
            result = client.get(response.location, base_url=base)
            self.assertIn(b'value="discovered-paper.pdf" selected', result.data)
            self.assertIn(b'name="return_to" value="/library"', result.data)
        form = {"token": token, "doi": "10.0000/neighbor", "pdf": pdf.name,
                "title": "Discovered synthetic paper", "return_to": "/library"}
        self.assertEqual(client.post("/settings/attach-pdf", data=form, base_url=base).status_code, 400)
        form["confirm_identity"] = "yes"
        response = client.post("/settings/attach-pdf", data=form, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/library")
        status = project_library.status(self.root)
        self.assertEqual([paper["doi"] for paper in status["papers"]], ["10.0000/example"])
        self.assertEqual(status["papers"][0]["status"], "awaiting_pdf")
        self.assertEqual(status["discovered"][0]["status"], "extracted")
        listing = client.get("/library?query=Discovered", base_url=base)
        self.assertIn(b"<dd>References</dd>", listing.data)
        self.assertIn(b">Extracted<", listing.data)
        self.assertNotIn(b'action="/settings/find-pdf"', listing.data)
        sources = client.get("/sources", base_url=base)
        self.assertIn(b"Discovered synthetic paper", sources.data)
        search = client.get("/search?query=Synthetic", base_url=base)
        self.assertEqual(search.status_code, 200)
        self.assertIn(b"PDF document for testing acquisition", search.data)
        current = project_library.store(self.root).current()
        unit = current.snapshots["core"]._bundle["units"][0]
        context = current.call("get_source_context", {"unit_id": unit["id"], "collections": ["core"]})
        self.assertIn("Synthetic PDF document", str(context))
        with self.assertRaisesRegex(ValueError, "connected"):
            project_library.attach(self.root, "10.0000/arbitrary", pdf,
                                   identity_confirmed=True, title="Not discovered")
        # A later core import keeps discovered evidence in the shared project snapshot.
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Different core evidence for checking cumulative project extraction.")
            core_pdf = self.pdf_root / "core.pdf"
            document.save(core_pdf)
        project_library.attach(self.root, "10.0000/example", core_pdf, identity_confirmed=True)
        self.assertEqual(len(project_library.store(self.root).current().snapshots["core"]._bundle["sources"]), 2)

    def test_discovered_find_pdf_uses_saved_membership_without_registering_core(self):
        self.discovery()
        self.work["doi"] = "https://doi.org/10.0000/neighbor"
        with patch.object(literature.MetadataClient, "get", return_value=(self.work, {})), \
                patch.object(citation_neighborhood, "download", return_value=(
                    self.data, {"final_url": "https://example.org/paper.pdf", "pdf_pages": 1})):
            result = project_library.find_pdf(self.root, "10.0000/neighbor", self.pdf_root,
                                              title="Discovered synthetic paper")
        self.assertEqual(result["filename"], "discovered-synthetic-paper.pdf")
        self.assertEqual(list(project_library.load(self.root)["papers"]), ["10.0000/example"])
        self.assertFalse((self.root / "registry/active.json").exists())


if __name__ == "__main__":
    unittest.main()
