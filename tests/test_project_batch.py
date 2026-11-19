from pathlib import Path
from html import unescape
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

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
            "status": "complete", "label": "Synthetic search", "direction": "background",
            "started_at": "2026-11-10T00:00:00Z",
            "report": {"edges": 60, "unresolved": 0, "seeds_with_provider_gaps": 0}})
        collections.atomic_json(folder, "neighborhood.json", {
            "limitations": [], "seeds": [], "unresolved": [],
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

    def test_paper_pages_link_exact_entries_and_show_known_citation_directions(self):
        graph_path = self.root / "discovery" / ("a" * 32) / "neighborhood.json"
        graph = releases.read_json(graph_path.read_bytes())
        graph["nodes"]["1"]["record"].update(
            authors=["First <author>", "Second author"], publication_date="2020-06-01",
            journal="Test journal", source_url="https://example.org/article")
        graph["nodes"]["openalex:W123"] = {"record": {
            "title": "No DOI <script>entry</script>", "identifiers": {}}}
        graph["edges"].append({"seed_doi": "10.0000/core", "direction": "background",
                               "target_provider_record": "openalex:W123"})
        collections.atomic_json(graph_path.parent, graph_path.name, graph)
        client = self.app().test_client()
        base = "http://localhost:8080"
        table = client.get("/library", base_url=base).get_data(as_text=True)
        self.assertIn('href="/library/paper?id=doi%3A10.0000%2F1">Neighbor 001</a>', table)
        self.assertIn('href="https://doi.org/10.0000/1">10.0000/1</a>', table)
        self.assertIn('>Actions</th>', table)
        self.assertNotIn('class="library-actions"', table)
        self.assertNotIn('PDF and text actions', table)
        self.assertNotIn('class="secondary locate-toggle"', table)
        header = re.search(r'<thead><tr>(.*?)</tr>', table, re.S).group(1)
        self.assertEqual(header.count('<th '), 8)
        filters = re.search(r'<tr class="column-filters">(.*?)</tr>', table, re.S).group(1)
        self.assertEqual(filters.count('<td>'), 8)
        self.assertTrue(filters.endswith(
            '<td><button type="submit" form="library-filters">Apply filters</button></td>'))
        self.assertNotIn('Open local PDF', table)
        rows = re.search(r'<tbody>(.*?)</tbody>', table, re.S).group(1)
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', rows, re.S):
            self.assertEqual(row.count('<td>'), 7)
            self.assertEqual(row.count('<th scope="row">'), 1)
        empty = client.get("/library?query=nonexistent-library-paper", base_url=base).text
        self.assertIn('<td colspan="8">', empty)
        detail = project_index.paper_detail(self.root, "doi:10.0000/1")
        self.assertEqual(detail["doi"], "10.0000/1")
        self.assertEqual(detail["references"], [])
        self.assertEqual([row["doi"] for row in detail["citing"]], ["10.0000/core"])
        self.assertEqual(len(detail["records"][0]["authors"]), 2)
        self.assertEqual(len(detail["searches"]), 1)
        core = project_index.paper_detail(self.root, "doi:10.0000/core")
        self.assertEqual(len(core["references"]), 31)
        self.assertEqual(len(core["citing"]), 30)
        self.assertEqual(len(core["searches"]), 1)
        citing = project_index.paper_detail(self.root, "doi:10.0000/0")
        self.assertEqual([row["doi"] for row in citing["references"]], ["10.0000/core"])
        self.assertEqual(citing["citing"], [])
        html = client.get("/library/paper?id=doi%3A10.0000%2F1", base_url=base).get_data(as_text=True)
        for expected in ("No extracted content", "not available as full-text",
                         'action="/settings/attach-pdf"', 'name="return_to" value="/library/paper"'):
            self.assertIn(expected, html)
        technical = client.get("/library/paper?id=doi%3A10.0000%2F1&tab=technical", base_url=base).text
        for expected in ("First &lt;author&gt;; Second author", "Test journal", "2020",
                         'href="https://example.org/article"'):
            self.assertIn(expected, technical)
        references = client.get("/library/paper?id=doi%3A10.0000%2F1&tab=references", base_url=base).text
        self.assertIn("References (0)", references)
        self.assertIn("None recorded in saved searches", references)
        incoming = client.get("/library/paper?id=doi%3A10.0000%2F1&tab=citing", base_url=base).text
        self.assertIn("Citing papers (1)", incoming)
        self.assertIn('href="/library/paper?id=doi%3A10.0000%2Fcore"', incoming)
        no_pdf = client.get("/library/paper?id=doi%3A10.0000%2F1&tab=pdf", base_url=base)
        self.assertIn(b"No local PDF", no_pdf.data)
        self.assertEqual(client.get("/library/paper/pdf?id=doi%3A10.0000%2F1", base_url=base).status_code, 404)
        unknown = client.get("/library/paper?id=openalex%3AW123", base_url=base)
        self.assertEqual(unknown.status_code, 200)
        self.assertIn(b"No DOI &lt;script&gt;entry&lt;/script&gt;", unknown.data)
        self.assertIn(b"resolved DOI and title are required", unknown.data)
        self.assertNotIn(b'action="/settings/find-pdf"', unknown.data)
        for query in ("", "?id=missing", "?id=doi%3A10.0000%2F1&unknown=value", "?id=doi%3A10.0000%2F1&tab=bad"):
            self.assertEqual(client.get("/library/paper" + query, base_url=base).status_code, 400)
        core_html = client.get("/library/paper?id=doi%3A10.0000%2Fcore&tab=technical", base_url=base)
        self.assertIn(b'action="/settings/purge-preview"', core_html.data)
        self.assertIn(b"not exhaustive", core_html.data)

    def test_find_and_attach_from_paper_page_return_to_discovered_paper(self):
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        detail_url = "/library/paper?id=doi%3A10.0000%2F1"
        html = client.get(detail_url, base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        form = {"token": token, "doi": "10.0000/1", "title": "Neighbor 001", "return_to": "/library/paper"}
        with patch.object(project_library, "find_pdf", return_value={
            "status": "local_found", "doi": "10.0000/1", "filename": self.pdf.name,
            "match_basis": "Synthetic local fixture", "attempts": [],
        }):
            started = client.post("/settings/find-pdf", data=form, base_url=base)
            self.assertEqual(started.status_code, 303)
            app.extensions["find_pdf"]["thread"].join(5)
        progress = client.get(started.location, base_url=base)
        self.assertIn(b'name="return_to" value="/library/paper"', progress.data)
        self.assertIn(f'href="{detail_url}">Return to paper'.encode(), progress.data)
        attached = client.post("/settings/attach-pdf", data={
            **form, "pdf": self.pdf.name, "confirm_identity": "yes"}, base_url=base)
        self.assertEqual(attached.status_code, 303)
        self.assertEqual(attached.location, detail_url)
        detail = client.get(attached.location, base_url=base)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Synthetic batch evidence", detail.data)
        self.assertIn(b"Not available to RAG/MCP", detail.data)
        self.assertEqual(list(project_library.load(self.root)["papers"]), ["10.0000/core"])

    def test_table_column_filters_sorting_and_batch_selection_preserve_the_view(self):
        graph_path = self.root / "discovery" / ("a" * 32) / "neighborhood.json"
        graph = releases.read_json(graph_path.read_bytes())
        for node in graph["nodes"].values():
            node["record"].update(authors=["First <author>", "Hidden coauthor"],
                                  publication_date="2020-06-01", journal="Synthetic <journal>")
        collections.atomic_json(graph_path.parent, graph_path.name, graph)
        client = self.app().test_client()
        base = "http://localhost:8080"
        view = {"query": "Neighbor", "title_filter": "Neighbor", "first_author_filter": "First",
                "year_filter": "2020", "journal_filter": "Synthetic", "sort": "title", "order": "desc"}
        response = client.get("/library?" + urlencode({**view, "offset": 50}), base_url=base)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<table class="library-table">', html)
        self.assertIn('aria-sort="descending"', html)
        self.assertIn('aria-label="Sort by Title, ascending"', html)
        self.assertIn('First &lt;author&gt;', html)
        self.assertIn('Synthetic &lt;journal&gt;', html)
        self.assertNotIn('Hidden coauthor', html)
        for name in ("title", "doi", "first_author", "year", "journal"):
            self.assertIn(f'name="{name}_filter" form="library-filters"', html)
        table = html.split('<tbody>', 1)[1]
        self.assertLess(table.index('>Neighbor 009</a>'), table.index('>Neighbor 000</a>'))
        self.assertNotIn('>Neighbor 010</a>', table)
        management_url = unescape(re.search(
            r'href="([^"]+)">Manage PDFs for these results</a>', html).group(1))
        manager = client.get(management_url, base_url=base)
        self.assertEqual(manager.status_code, 200)
        token = re.search(r'name="token" value="([^"]+)"', manager.text).group(1)
        management_view = parse_qs(urlsplit(management_url).query)
        self.assertEqual(management_view["library_offset"], ["50"])
        for name, value in view.items():
            self.assertEqual(management_view[name], [value])
        self.assertIn(b'page starting at record 51', manager.data)
        self.assertIn(b'name="offset" value="50"', manager.data)
        response = client.post("/library/batch", data={
            **view, "offset": 50, "token": token, "action": "page", "return_to": "/discovery"}, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(urlsplit(response.location).path, "/discovery")
        self.assertEqual(urlsplit(response.location).fragment, "batch-pdfs")
        self.assertEqual(parse_qs(urlsplit(response.location).query), management_view)
        self.assertEqual(client.get(response.location, base_url=base).status_code, 200)
        selected = {doi for doi, item in project_batch.load(self.root)["items"].items() if item["selected"]}
        self.assertEqual(selected, {f"10.0000/{n}" for n in range(10)})
        response = client.post("/library/batch", data={
            **view, "offset": 50, "token": token, "action": "clear", "return_to": "/discovery"}, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(parse_qs(urlsplit(response.location).query), management_view)
        self.assertFalse(any(item["selected"] for item in project_batch.load(self.root)["items"].values()))
        response = client.post("/library/batch", data={
            **view, "offset": 0, "token": token, "action": "page"}, base_url=base)
        self.assertEqual(response.status_code, 303)
        redirect_view = parse_qs(urlsplit(response.location).query)
        for name, value in view.items():
            self.assertEqual(redirect_view[name], [value])
        selected = {doi for doi, item in project_batch.load(self.root)["items"].items() if item["selected"]}
        self.assertEqual(selected, {f"10.0000/{n}" for n in range(10, 60)})
        response = client.post("/library/batch", data={
            **view, "token": token, "action": "all"}, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(sum(item["selected"] for item in project_batch.load(self.root)["items"].values()), 60)
        toggled = client.post("/library/batch", data={
            **view, "doi_filter": "/00", "doi": "10.0000/59", "token": token, "action": "toggle"},
            base_url=base, headers={"Accept": "application/json"})
        self.assertEqual(toggled.status_code, 200)
        self.assertFalse(toggled.get_json()["selected"])
        empty = client.get("/library?journal_filter=Absent", base_url=base)
        self.assertIn(b"No papers match these filters", empty.data)
        for query in ("sort=invalid", "order=sideways", "year_filter=20", "unknown=value"):
            self.assertEqual(client.get("/library?" + query, base_url=base).status_code, 400)

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
        report = self.app().test_client().get("/library/batch", base_url="http://localhost:8080")
        self.assertIn(b"The worker paused after a provider error", report.data)
        self.assertIn(b"DiscoveryError", report.data)
        self.assertNotIn(b"Resume PDF location from checkpoint", report.data)
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
        self.assertEqual(project_batch.stop_details(project_batch.load(self.root), None)["reason"], "manual_pause")
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

    def test_web_resume_location_preserves_checkpoint_without_repeating_attempts(self):
        first, failed, current, unmarked, later = [f"10.0000/{n}" for n in range(1, 6)]
        project_batch.select(self.root, [dict(self.row, doi=doi) for doi in (
            first, failed, current, unmarked, later)], True)
        job = project_batch.prepare_locate(self.root)
        job["processed"] = 2
        job["items"][first].update(status="not_found", result={"status": "no_pdf"})
        job["items"][failed].update(status="failed", error="Saved failure")
        job["items"][current]["status"] = "locating"
        job["items"][unmarked]["selected"] = False
        job["activity"] = project_batch.activity("locating", worker_id="previous-server", doi=current)
        project_batch.save(self.root, job)

        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        review = client.get("/library/batch", base_url=base)
        self.assertIn(b"Batch status: interrupted. 2 of 5 tasks processed", review.data)
        self.assertIn(b"Resume PDF location from checkpoint", review.data)
        for value in (b"Batch diagnostics", b"different web server instance", b"Locating a PDF",
                      current.encode(), job["activity"]["at"].encode()):
            self.assertIn(value, review.data)
        token = re.search(rb'name="token" value="([^"]+)"', review.data)[1].decode()
        form = {"token": token, "action": "resume-locate"}
        with patch.object(project_library, "find_pdf", side_effect=self.downloaded) as find:
            response = client.post("/library/batch", data=form, base_url=base)
            self.assertEqual(response.status_code, 303)
            worker = app.extensions["batch_pdf"]["thread"]
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([call.args[1] for call in find.call_args_list], [current, later])
        saved = project_batch.load(self.root)
        self.assertEqual((saved["status"], saved["processed"], saved["tasks"]), ("complete", 5, job["tasks"]))
        for doi in (first, failed, unmarked):
            self.assertEqual(saved["items"][doi], job["items"][doi])
        self.assertNotIn(b"Resume PDF location from checkpoint", client.get("/library/batch", base_url=base).data)
        self.assertIn(b"Previous batch stop", client.get("/library/batch", base_url=base).data)
        self.assertEqual(client.post("/library/batch", data=form, base_url=base).status_code, 400)

    def test_worker_exception_records_diagnostics_and_leaves_task_unprocessed(self):
        project_batch.select(self.root, [self.row], True)
        job = project_batch.prepare_locate(self.root)
        with patch.object(project_library, "find_pdf", side_effect=RuntimeError("Synthetic unexpected error")), \
                patch.object(project_batch, "diagnostic") as log:
            with self.assertRaises(RuntimeError):
                project_batch.run(self.root, self.pdf_root, job, threading.Event(), worker_id="worker")
        log.assert_called_once()
        self.assertEqual(log.call_args.args, ("batch_pdf_worker_stopped",))
        saved = project_batch.load(self.root)
        self.assertEqual((saved["status"], saved["processed"]), ("failed", 0))
        self.assertEqual(saved["stop"]["error_phase"], "locating")
        self.assertEqual(saved["stop"]["exception_type"], "RuntimeError")
        self.assertEqual(saved["stop"]["doi"], self.row["doi"])
        self.assertNotIn("Synthetic unexpected error", str(saved))
        report = self.app().test_client().get("/library/batch", base_url="http://localhost:8080")
        self.assertIn(b"The worker raised an error", report.data)
        self.assertIn(b"Stage at error", report.data)
        self.assertIn(b"Resume PDF location from checkpoint", report.data)

    def test_failed_checkpoint_save_does_not_advance_saved_progress(self):
        project_batch.select(self.root, [self.row], True)
        job = project_batch.prepare_locate(self.root)
        save = project_batch.save

        def fail_first_progress_save(root, state):
            if state["processed"] == 1:
                raise OSError("Synthetic save failure")
            return save(root, state)

        with patch.object(project_library, "find_pdf", side_effect=self.downloaded), \
                patch.object(project_batch, "save", side_effect=fail_first_progress_save), \
                patch.object(project_batch, "diagnostic") as log:
            project_batch.run(self.root, self.pdf_root, job, threading.Event())
        log.assert_called_once()
        saved = project_batch.load(self.root)
        self.assertEqual(saved["processed"], 0)
        self.assertEqual(saved["activity"]["phase"], "locating")
        self.assertEqual(saved["stop"]["error_phase"], "saving_checkpoint")

    def test_worker_stop_is_logged_even_if_queue_cannot_be_written(self):
        project_batch.select(self.root, [self.row], True)
        job = project_batch.prepare_locate(self.root)
        with patch.object(project_batch, "save", side_effect=OSError("Synthetic disk failure")), \
                patch.object(project_batch, "diagnostic") as log:
            project_batch.run(self.root, self.pdf_root, job, threading.Event())
        self.assertEqual([call.args[0] for call in log.call_args_list],
                         ["batch_pdf_worker_stopped", "batch_pdf_diagnostics_not_saved"])
        self.assertEqual(project_batch.load(self.root)["status"], "running")

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
        job["items"][self.row["doi"]]["status"] = "locating"
        project_batch.save(self.root, job)
        restarted = self.app().test_client()
        interrupted = restarted.get("/library/batch", base_url=base)
        self.assertIn(b"interrupted", interrupted.data)
        self.assertIn(b"No stop reason was recorded", interrupted.data)
        self.assertIn(b"Not recorded", interrupted.data)
        self.assertEqual(project_batch.stop_details(job, "server")["doi"], self.row["doi"])
        self.assertNotIn("Refresh", interrupted.headers)

    def test_web_toggles_filters_all_matching_selection_review_and_confirmation(self):
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        listing = client.get("/library?relationship=background&status=awaiting", base_url=base)
        self.assertIn(b"30 matching records", listing.data)
        self.assertIn(b">Actions</th>", listing.data)
        self.assertIn(b'<script src="/library.js" defer></script>', listing.data)
        script = client.get("/library.js", base_url=base)
        self.assertEqual(script.status_code, 200)
        self.assertIn(b"fetch(form.getAttribute('action')", script.data)
        token = re.search(r'name="token" value="([^"]+)"',
                          client.get("/discovery", base_url=base).text).group(1)
        self.assertEqual(client.post("/library/batch", data={"action": "all"}, base_url=base).status_code, 403)
        form = {"token": token, "action": "toggle", "doi": self.row["doi"], "relationship": "background",
                "status": "awaiting", "offset": "0"}
        response = client.post("/library/batch", data=form, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertIn("#locate-", response.location)
        self.assertTrue(project_batch.load(self.root)["items"][self.row["doi"]]["selected"])
        self.assertIn(b'1</span> papers marked for PDF location',
                      client.get("/library", base_url=base).data)
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
            client.get("/discovery", base_url=base).get_data(as_text=True)).group(1)
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

    def test_management_find_all_only_locates_awaiting_core_papers_and_reuses_results(self):
        project_library.register(self.root, [{"doi": "10.0000/second", "title": "Second Core paper"}])
        project_batch.select(self.root, [self.row], True)
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        settings = client.get("/discovery", base_url=base).get_data(as_text=True)
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
        self.assertIn(b"All suggested PDFs have been accepted or dismissed", response.data)
        self.assertIn(b"0 pending paper/PDF pairs; 1 accepted", response.data)
        self.assertIn(b'value="extract-accepted">Extract accepted PDFs', response.data)
        self.assertNotIn(b'value="accept">Accept</button>', response.data)
        self.assertFalse((self.root / "registry/active.json").exists())

    def test_reject_suggestion_dismisses_only_the_pending_match(self):
        doi = self.row["doi"]
        other = "10.0000/other"
        job = project_batch.select(self.root, [self.row, dict(self.row, doi=other)], True)
        missing = "no longer present.pdf"
        job["items"][doi].update(status="local_found", filename=missing,
                                 result={"final_url": "https://example.org/wrong.pdf"})
        job["items"][other].update(status="local_found", filename=self.pdf.name)
        project_batch.save(self.root, job)
        project_batch.accept_pdf(self.root, self.pdf_root, other, self.pdf.name)
        client = self.app().test_client()
        base = "http://localhost:8080"
        review = client.get("/library/batch/confirm", base_url=base)
        self.assertIn(b'class="library-record batch-pair"', review.data)
        self.assertIn(b">Reject suggestion</button>", review.data)
        token = re.search(rb'name="token" value="([^"]+)"', review.data)[1].decode()
        form = {"token": token, "action": "reject-suggestion", "doi": doi, "suggestion": missing}
        self.assertEqual(client.post("/library/batch", data=dict(form, token=""), base_url=base).status_code, 403)
        self.assertEqual(client.post("/library/batch", data=dict(form, suggestion="older.pdf"),
                                     base_url=base).status_code, 400)
        self.assertEqual(client.post("/library/batch", data=dict(form, doi=other, suggestion=self.pdf.name),
                                     base_url=base).status_code, 400)
        response = client.post("/library/batch", data=form, base_url=base, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Suggestion rejected. No file was deleted.", response.data)
        self.assertIn(b"0 pending paper/PDF pairs; 1 accepted", response.data)
        saved = project_batch.load(self.root)["items"]
        self.assertEqual(saved[doi]["status"], "suggestion_rejected")
        self.assertFalse(saved[doi]["selected"])
        self.assertNotIn("filename", saved[doi])
        self.assertNotIn("result", saved[doi])
        self.assertTrue(saved[other]["accepted_sha256"])
        self.assertTrue(self.pdf.is_file())
        queue = client.get("/library/batch", base_url=base)
        self.assertIn(b"PDF suggestion rejected", queue.data)
        self.assertIn(f'name="file:{doi}"'.encode(), queue.data)
        self.assertNotIn(b"https://example.org/wrong.pdf", queue.data)


if __name__ == "__main__":
    unittest.main()
