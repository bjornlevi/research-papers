from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf

from research_library import collections, literature, releases, web_server
from research_library.build import project_batch, project_index, project_library


class ProjectBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.pdf_root = self.root.parent / "pdfs"
        self.pdf_root.mkdir()
        project_library.initialize(self.root, "Batch test", "Authorized private synthetic research")
        project_library.register(self.root, [{"doi": "10.0000/core", "title": "Core paper"}])
        folder = self.root / "discovery" / ("a" * 32)
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "label": "Synthetic search", "started_at": "2026-11-10T00:00:00Z"})
        collections.atomic_json(folder, "neighborhood.json", {
            "nodes": {str(n): {"record": {"title": f"Neighbor {n:03}", "identifiers": {"doi": f"10.0000/{n}"}}}
                      for n in range(60)},
            "edges": [{"seed_doi": "10.0000/core", "direction": "background" if n % 2 else "impact",
                       "target_provider_record": str(n)} for n in range(60)]})
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50),
                "Synthetic batch evidence with enough native text for private extraction and retrieval tests.")
            self.pdf = self.pdf_root / "neighbor.pdf"
            document.save(self.pdf)
        self.row = {"doi": "10.0000/1", "title": "Neighbor 001", "status": "Awaiting PDF"}

    def downloaded(self, *args, **kwargs):
        return {"doi": args[1], "status": "downloaded", "filename": self.pdf.name,
                "sha256": releases.sha256(self.pdf.read_bytes()),
                "final_url": "https://example.org/paper.pdf", "license": "cc-by",
                "year": "2020", "version": "publishedVersion", "attempts": []}

    def app(self):
        return web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdf_root, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])

    def test_persistent_download_review_and_confirmed_extraction_without_core_promotion(self):
        project_batch.select(self.root, [self.row], True)
        job = project_batch.prepare_locate(self.root)
        with patch.object(project_library, "find_pdf", side_effect=self.downloaded) as find:
            project_batch.run(self.root, self.pdf_root, job, threading.Event())
            find.assert_called_once()
        self.assertEqual(project_batch.load(self.root)["items"][self.row["doi"]]["status"], "downloaded")
        self.assertFalse((self.root / "registry/active.json").exists())
        with self.assertRaisesRegex(ValueError, "Confirm"):
            project_batch.prepare_extract(self.root, self.pdf_root, [])
        job = project_batch.prepare_extract(self.root, self.pdf_root, [(self.row["doi"], self.pdf.name)])
        project_batch.run(self.root, self.pdf_root, job, threading.Event())
        item = project_batch.load(self.root)["items"][self.row["doi"]]
        self.assertEqual(item["status"], "extracted")
        self.assertFalse(item["selected"])
        self.assertEqual(list(project_library.load(self.root)["papers"]), ["10.0000/core"])
        current = project_library.store(self.root).current()
        hits = current.call("search_evidence", {"query": "batch"})["result"]["items"]
        self.assertEqual(len(hits), 1)
        page = project_index.library_page(self.root, "Neighbor 001", status="extracted")
        self.assertEqual(page["matching"], 1)

    def test_quota_pause_and_resume_reuses_successes_and_stops_before_remaining_requests(self):
        rows = [dict(self.row, doi=f"10.0000/{n}", title=f"Neighbor {n:03}") for n in [1, 2, 3]]
        project_batch.select(self.root, rows, True)
        job = project_batch.prepare_locate(self.root)
        with patch.object(project_library, "find_pdf", side_effect=[
                self.downloaded(self.root, rows[0]["doi"]),
                literature.DiscoveryError("Provider requires a later retry; resume this run later")]) as find:
            project_batch.run(self.root, self.pdf_root, job, threading.Event())
            self.assertEqual(find.call_count, 2)
        self.assertEqual(project_batch.load(self.root)["status"], "paused")
        resumed = project_batch.prepare_locate(self.root)
        self.assertEqual(resumed["tasks"], ["10.0000/2", "10.0000/3"])
        with patch.object(project_library, "find_pdf", side_effect=self.downloaded) as find:
            project_batch.run(self.root, self.pdf_root, resumed, threading.Event())
            self.assertEqual(find.call_count, 2)
        self.assertEqual(project_batch.load(self.root)["status"], "complete")
        self.assertTrue(all(item["status"] == "downloaded"
                            for item in project_batch.load(self.root)["items"].values()))

    def test_local_pdf_confirmation_is_checksum_bound_and_does_not_allow_paths_outside_folder(self):
        project_batch.select(self.root, [self.row], True)
        with self.assertRaisesRegex(ValueError, "data folder"):
            project_batch.prepare_extract(self.root, self.pdf_root, [(self.row["doi"], "../other.pdf")])
        job = project_batch.prepare_extract(self.root, self.pdf_root, [(self.row["doi"], self.pdf.name)])
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\n")
        project_batch.run(self.root, self.pdf_root, job, threading.Event())
        item = project_batch.load(self.root)["items"][self.row["doi"]]
        self.assertEqual(item["status"], "extraction_failed")
        self.assertIn("changed after confirmation", item["error"])
        self.assertFalse((self.root / "registry/active.json").exists())

    def test_pause_between_papers_and_resume_after_interrupted_process(self):
        project_batch.select(self.root, [self.row], True)
        job = project_batch.prepare_locate(self.root)
        pause = threading.Event()
        pause.set()
        with patch.object(project_library, "find_pdf") as find:
            project_batch.run(self.root, self.pdf_root, job, pause)
            find.assert_not_called()
        self.assertEqual(project_batch.load(self.root)["status"], "paused")
        job = project_batch.prepare_locate(self.root)
        job["items"][self.row["doi"]]["status"] = "locating"
        project_batch.save(self.root, job)
        resumed = project_batch.prepare_locate(self.root)
        self.assertEqual(resumed["tasks"], [self.row["doi"]])

    def test_interrupted_extraction_does_not_duplicate_already_activated_evidence(self):
        project_batch.select(self.root, [self.row], True)
        job = project_batch.prepare_extract(self.root, self.pdf_root, [(self.row["doi"], self.pdf.name)])
        # Model a process stopping after registry activation but before the queue save.
        project_library.attach(self.root, self.row["doi"], self.pdf,
                               identity_confirmed=True, title=self.row["title"])
        with patch.object(project_library, "attach", side_effect=AssertionError("Duplicate extraction")):
            project_batch.run(self.root, self.pdf_root, job, threading.Event())
        self.assertEqual(project_batch.load(self.root)["items"][self.row["doi"]]["status"], "extracted")
        self.assertEqual(len(project_library.status(self.root)["discovered"]), 1)

    def test_batch_review_is_paginated_and_errors_remain_visible(self):
        rows = project_index.library_page(self.root, select_all=True)["rows"]
        project_batch.select(self.root, rows, True)
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        first = client.get("/library/batch", base_url=base)
        second = client.get("/library/batch?offset=25", base_url=base)
        self.assertEqual(first.data.count(b'name="confirm"'), 25)
        self.assertEqual(second.data.count(b'name="confirm"'), 25)
        self.assertIn(b">Next</a>", first.data)
        self.assertIn(b">Previous</a>", second.data)
        job = project_batch.load(self.root)
        job.update(status="running", operation="locate", tasks=[self.row["doi"]])
        project_batch.save(self.root, job)
        restarted = self.app().test_client()
        interrupted = restarted.get("/library/batch", base_url=base)
        self.assertIn(b"interrupted", interrupted.data)
        self.assertNotIn("Refresh", interrupted.headers)

    def test_web_toggles_filters_all_matching_selection_review_and_confirmation(self):
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        listing = client.get("/library?relationship=background&status=awaiting", base_url=base)
        self.assertIn(b"30 matching records", listing.data)
        self.assertIn(b'aria-pressed="false">Locate PDF: off', listing.data)
        self.assertIn(b'<script src="/library.js" defer></script>', listing.data)
        script = client.get("/library.js", base_url=base)
        self.assertEqual(script.status_code, 200)
        self.assertIn(b"fetch(form.getAttribute('action')", script.data)
        token = re.search(r'name="token" value="([^"]+)"', listing.get_data(as_text=True)).group(1)
        self.assertEqual(client.post("/library/batch", data={"action": "all"}, base_url=base).status_code, 403)
        form = {"token": token, "action": "toggle", "doi": self.row["doi"], "relationship": "background",
                "status": "awaiting", "offset": "0"}
        response = client.post("/library/batch", data=form, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertIn("#locate-", response.location)
        self.assertTrue(project_batch.load(self.root)["items"][self.row["doi"]]["selected"])
        self.assertIn(b'aria-pressed="true">Locate PDF: on',
                      client.get(response.location.split("#")[0], base_url=base).data)
        client.post("/library/batch", data=form, base_url=base)
        self.assertFalse(project_batch.load(self.root)["items"][self.row["doi"]]["selected"])
        for selected in [True, False]:
            toggled = client.post("/library/batch", data=form, base_url=base,
                                  headers={"Accept": "application/json"})
            self.assertEqual(toggled.status_code, 200)
            self.assertEqual(toggled.get_json(), {"selected": selected, "selected_count": int(selected)})
            self.assertNotIn("Location", toggled.headers)
        client.post("/library/batch", data={"token": token, "action": "all",
                                          "relationship": "impact"}, base_url=base)
        self.assertEqual(sum(item["selected"] for item in project_batch.load(self.root)["items"].values()), 30)
        client.post("/library/batch", data={"token": token, "action": "all"}, base_url=base)
        self.assertEqual(sum(item["selected"] for item in project_batch.load(self.root)["items"].values()), 61)
        client.post("/library/batch", data={"token": token, "action": "clear"}, base_url=base)
        client.post("/library/batch", data=form, base_url=base)
        with patch.object(project_library, "find_pdf", side_effect=self.downloaded):
            response = client.post("/library/batch", data={"token": token, "action": "locate"}, base_url=base)
            self.assertEqual(response.status_code, 303)
            app.extensions["batch_pdf"]["thread"].join(5)
        review = client.get("/library/batch", base_url=base)
        self.assertIn(b"Inspect each PDF", review.data)
        self.assertIn(b'name="confirm"', review.data)
        self.assertIn(b'value="neighbor.pdf" selected', review.data)
        self.assertNotIn(b'type="checkbox" checked', review.data)
        rejected = client.post("/library/batch", data={"token": token, "action": "extract"}, base_url=base)
        self.assertEqual(rejected.status_code, 400)
        self.assertIn(b"Confirm", rejected.data)
        response = client.post("/library/batch", data={"token": token, "action": "extract",
            "confirm": self.row["doi"], "file:" + self.row["doi"]: self.pdf.name}, base_url=base)
        self.assertEqual(response.status_code, 303)
        app.extensions["batch_pdf"]["thread"].join(5)
        self.assertEqual(project_batch.load(self.root)["items"][self.row["doi"]]["status"], "extracted")
        self.assertEqual(list(project_library.load(self.root)["papers"]), ["10.0000/core"])

    def test_running_batch_allows_pause_but_blocks_other_mutations(self):
        project_batch.select(self.root, [self.row], True)
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        token = re.search(r'name="token" value="([^"]+)"',
            client.get("/library", base_url=base).get_data(as_text=True)).group(1)
        started, finish = threading.Event(), threading.Event()

        def locate(*args, **kwargs):
            started.set()
            finish.wait(5)
            return self.downloaded(*args, **kwargs)

        with patch.object(project_library, "find_pdf", side_effect=locate):
            response = client.post("/library/batch", data={"token": token, "action": "locate"}, base_url=base)
            self.assertEqual(response.status_code, 303)
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(client.post("/settings/reset", data={}, base_url=base).status_code, 503)
                self.assertEqual(client.post("/library/batch", data={"token": token, "action": "pause"},
                                             base_url=base).status_code, 303)
                self.assertIn(b"<progress", client.get("/library/batch", base_url=base).data)
            finally:
                finish.set()
                app.extensions["batch_pdf"]["thread"].join(5)


if __name__ == "__main__":
    unittest.main()
