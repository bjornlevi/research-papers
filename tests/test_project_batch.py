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
        self.assertEqual(len(hits), 0)
        self.assertEqual(project_library.status(self.root)["discovered"][0]["status"], "draft")
        page = project_index.library_page(self.root, "Neighbor 001", status="extracted")
        self.assertEqual(page["matching"], 1)

    def test_locating_local_pdf_reuses_nested_file_and_preserves_confirmation(self):
        folder = self.pdf_root / "preserved"
        folder.mkdir()
        path = folder / "neighbor.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50),
                "Neighbor 001 DOI: 10.0000/1. Synthetic evidence for testing local lookup.")
            document.save(path)
        project_batch.select(self.root, [self.row], True)
        job = project_batch.prepare_locate(self.root)
        with patch.object(literature.MetadataClient, "get", side_effect=AssertionError("Online lookup")):
            project_batch.run(self.root, self.pdf_root, job, threading.Event())
        item = project_batch.load(self.root)["items"][self.row["doi"]]
        self.assertEqual(item["status"], "local_found")
        self.assertEqual(item["filename"], "preserved/neighbor.pdf")
        self.assertFalse((self.root / "registry/active.json").exists())
        with self.assertRaisesRegex(ValueError, "without a downloaded PDF"):
            project_batch.prepare_locate(self.root)
        page = self.app().test_client().get("/library/batch", base_url="http://localhost:8080")
        self.assertIn(b"Found local PDF", page.data)
        self.assertIn(b'preserved/neighbor.pdf" selected', page.data)
        job = project_batch.prepare_extract(self.root, self.pdf_root, [
            (self.row["doi"], "preserved/neighbor.pdf")])
        project_batch.run(self.root, self.pdf_root, job, threading.Event())
        self.assertEqual(project_batch.load(self.root)["items"][self.row["doi"]]["status"], "extracted")
        self.assertEqual(list(project_library.load(self.root)["papers"]), ["10.0000/core"])

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
        self.assertEqual(rejected.status_code, 303)
        confirmation = client.get(rejected.location, base_url=base)
        self.assertIn(b"Confirm suggested papers and PDFs", confirmation.data)
        self.assertIn(b"No papers were confirmed", confirmation.data)
        self.assertIn(b'class="two-col"', confirmation.data)
        self.assertIn(b'value="neighbor.pdf" selected', confirmation.data)
        self.assertNotIn(b'type="checkbox" checked', confirmation.data)
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

    def test_settings_find_all_only_locates_awaiting_core_papers_and_reuses_results(self):
        project_library.register(self.root, [{"doi": "10.0000/second", "title": "Second Core paper"}])
        project_batch.select(self.root, [self.row], True)
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        settings = client.get("/settings", base_url=base).get_data(as_text=True)
        self.assertIn('value="find-all">Find all</button>', settings)
        self.assertIn("Check local PDFs and subfolders first", settings)
        token = re.search(r'name="token" value="([^"]+)"', settings).group(1)
        self.assertEqual(client.post("/library/batch", data={"action": "find-all"}, base_url=base).status_code, 403)
        with patch.object(project_library, "find_pdf", side_effect=lambda *args, **kwargs: {
                "doi": args[1], "status": "local_found", "filename": self.pdf.name,
                "match_basis": "DOI in first page or PDF metadata"}) as find:
            response = client.post("/library/batch", data={
                "token": token, "action": "find-all"}, base_url=base)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.location, "/library/batch")
            app.extensions["batch_pdf"]["thread"].join(5)
            self.assertEqual({call.args[1] for call in find.call_args_list},
                             {"10.0000/core", "10.0000/second"})
            repeated = client.post("/library/batch", data={
                "token": token, "action": "find-all"}, base_url=base)
            self.assertEqual(repeated.status_code, 303)
            self.assertEqual(find.call_count, 2)
        job = project_batch.load(self.root)
        self.assertEqual(job["items"][self.row["doi"]]["status"], "queued")
        self.assertFalse((self.root / "registry/active.json").exists())
        review = client.get("/library/batch", base_url=base)
        self.assertIn(b"Found local PDF", review.data)
        self.assertNotIn(b'type="checkbox" checked', review.data)

    def test_confirmation_pairs_are_paginated_and_only_checked_papers_are_extracted(self):
        rows = [dict(self.row, doi=f"10.0000/{n}", title=f"Neighbor {n:03}") for n in range(30)]
        job = project_batch.select(self.root, rows, True)
        for item in job["items"].values():
            item.update(status="local_found", filename=self.pdf.name,
                        result={"match_basis": "title filename"})
        project_batch.save(self.root, job)
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        first = client.get("/library/batch/confirm", base_url=base)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data.count(b'class="two-col"'), 25)
        self.assertEqual(first.data.count(b'value="accept">Accept</button>'), 25)
        self.assertNotIn(b'type="checkbox"', first.data)
        self.assertIn(b"Neighbor 000", first.data)
        self.assertIn(b"Suggested PDF", first.data)
        self.assertNotIn(b'type="checkbox" checked', first.data)
        second = client.get("/library/batch/confirm?offset=25", base_url=base)
        self.assertEqual(second.data.count(b'value="accept">Accept</button>'), 5)
        token = re.search(r'name="token" value="([^"]+)"', second.get_data(as_text=True)).group(1)
        self.assertEqual(client.post("/library/batch", data={
            "action": "extract", "confirm": "10.0000/25", "file:10.0000/25": self.pdf.name},
            base_url=base).status_code, 403)
        empty = client.post("/library/batch", data={
            "token": token, "action": "extract", "offset": "25"}, base_url=base)
        self.assertEqual(empty.status_code, 303)
        self.assertIn("offset=25", empty.location)
        self.assertFalse((self.root / "registry/active.json").exists())
        accepted = client.post("/library/batch", data={
            "token": token, "action": "accept", "doi": "10.0000/25",
            "file:10.0000/25": self.pdf.name, "offset": "25"}, base_url=base)
        self.assertEqual(accepted.status_code, 303)
        self.assertIn("offset=25", accepted.location)
        self.assertIn("notice=accepted", accepted.location)
        self.assertIn("#pair-25", accepted.location)
        pending = client.get(accepted.location.split("#")[0], base_url=base)
        self.assertIn(b"PDF accepted and saved", pending.data)
        self.assertEqual(pending.data.count(b'value="accept">Accept</button>'), 4)
        self.assertNotIn(b'name="doi" value="10.0000/25"', pending.data)
        self.assertFalse((self.root / "registry/active.json").exists())
        self.assertIn(b"1 accepted across all pages", client.get(
            "/library/batch/confirm", base_url=base).data)
        changed = self.pdf.read_bytes()
        self.pdf.write_bytes(changed + b"\n")
        stale = client.post("/library/batch", data={
            "token": token, "action": "extract-accepted"}, base_url=base)
        self.assertEqual(stale.status_code, 400)
        self.assertIn(b"Accepted PDF changed", stale.data)
        self.pdf.write_bytes(changed)
        with patch.object(project_library, "attach", return_value={}) as attach:
            response = client.post("/library/batch", data={
                "token": token, "action": "extract-accepted"}, base_url=base)
            self.assertEqual(response.status_code, 303)
            app.extensions["batch_pdf"]["thread"].join(5)
            attach.assert_called_once()
            self.assertEqual(attach.call_args.args[1], "10.0000/25")
            self.assertEqual(attach.call_args.kwargs["expected_sha256"], releases.sha256(self.pdf.read_bytes()))
        self.pdf.unlink()
        missing = client.get("/library/batch/confirm", base_url=base)
        self.assertIn(b"Suggested PDF is no longer available", missing.data)
        self.assertEqual(client.get("/library/batch/confirm?offset=-1", base_url=base).status_code, 400)

    def test_last_acceptance_keeps_extraction_action_visible(self):
        project_batch.select(self.root, [self.row], True)
        project_batch.accept_pdf(self.root, self.pdf_root, self.row["doi"], self.pdf.name)
        client = self.app().test_client()
        response = client.get("/library/batch/confirm?offset=25&notice=accepted",
                              base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"All suggested PDFs have been accepted", response.data)
        self.assertIn(b"0 pending paper/PDF pairs; 1 accepted", response.data)
        self.assertIn(b'value="extract-accepted">Extract accepted PDFs', response.data)
        self.assertNotIn(b'value="accept">Accept</button>', response.data)
        self.assertFalse((self.root / "registry/active.json").exists())


if __name__ == "__main__":
    unittest.main()
