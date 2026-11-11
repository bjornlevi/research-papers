import asyncio
import csv
import io
import json
import re
from datetime import timedelta
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pymupdf

from tools import project_library
from research_library import collections, literature, releases, web_server


class ProjectLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        project_library.initialize(self.root, "Fictional research", "Authorized private fictional test data.")

    def pdf(self, name, text):
        path = self.root.parent / name
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), text)
            document.save(path)
        return path

    def test_registration_validation_is_atomic(self):
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"}])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            project_library.register(self.root, [
                {"doi": "10.0000/two", "title": "Second"},
                {"doi": "10.0000/one", "title": "Duplicate"},
            ])
        self.assertEqual(len(project_library.status(self.root)["papers"]), 1)

    def test_openalex_key_settings_private_storage_and_removal(self):
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        settings = client.get("/settings", base_url=base).get_data(as_text=True)
        self.assertIn("https://openalex.org/settings/api-key", settings)
        self.assertIn('type="password"', settings)
        token = re.search(r'action="/settings/openalex-key">\s*<input type="hidden" name="token" value="([^"]+)"',
                          settings).group(1)
        key = "synthetic-private-key"
        self.assertEqual(client.post("/settings/openalex-key", data={"api_key": key}, base_url=base).status_code, 403)
        response = client.post("/settings/openalex-key", data={"api_key": key, "token": token}, base_url=base)
        self.assertEqual(response.status_code, 303)
        path = project_library.openalex_key_path(self.root)
        self.assertEqual(path.read_text().strip(), key)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        settings = client.get("/settings", base_url=base).get_data(as_text=True)
        self.assertNotIn(key, settings)
        self.assertIn("<strong>Configured</strong>", settings)
        self.assertEqual(client.post("/settings/openalex-key", data={
            "api_key": "bad key", "token": token}, base_url=base).status_code, 400)
        self.assertEqual(path.read_text().strip(), key)
        project_library.reset(self.root)
        self.assertTrue(path.exists())
        self.assertEqual(client.post("/settings/openalex-key", data={
            "action": "remove", "token": token}, base_url=base).status_code, 303)
        self.assertFalse(path.exists())

    def test_initial_csv_is_validated_before_creating_project(self):
        project = self.root.parent / "new-project"
        invalid = [{"doi": "10.0000/one", "title": "First"},
                   {"doi": "10.0000/one", "title": "Duplicate"}]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            project_library.initialize(project, "New", "Private permission", invalid)
        self.assertFalse(project.exists())
        project_library.initialize(project, "New", "Private permission", invalid[:1])
        self.assertEqual(project_library.status(project)["papers"][0]["status"], "awaiting_pdf")

    def test_web_settings_initialize_csv_and_confirm_factory_reset(self):
        project = self.root.parent / "web-project"
        app = web_server.create_app(
            registry=project / "registry", project_root=project, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base_url = "http://localhost:8080"
        self.assertEqual(client.get("/", base_url=base_url).status_code, 302)
        settings = client.get("/settings", base_url=base_url)
        self.assertEqual(settings.status_code, 200)
        self.assertIn(b"Initialize project", settings.data)
        response = client.post("/settings/initialize", data={
            "name": "Web project",
            "authorization": "Authorized fictional private data",
            "papers": (io.BytesIO(b"doi,title\n10.0000/web,Fictional web paper\n"), "papers.csv"),
        }, base_url=base_url, headers={"Origin": "http://127.0.0.1:8080"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(project_library.status(project)["papers"][0]["status"], "awaiting_pdf")
        self.assertIn(b"Fictional web paper", client.get("/settings", base_url=base_url).data)
        pdf = self.pdf("web-project.pdf", "Fictional evidence for testing settings reset.")
        project_library.attach(project, "10.0000/web", pdf, identity_confirmed=True)
        self.assertEqual(client.get("/", base_url=base_url).status_code, 200)
        rejected = client.post("/settings/reset", data={"confirmation": "yes"}, base_url=base_url)
        self.assertEqual(rejected.status_code, 400)
        self.assertTrue(project.exists())
        reset = client.post("/settings/reset", data={"confirmation": "RESET PROJECT"}, base_url=base_url)
        self.assertEqual(reset.status_code, 303)
        self.assertFalse(project.exists())
        self.assertTrue(pdf.exists())
        self.assertIn(b"Initialize project", client.get("/settings", base_url=base_url).data)
        reinitialized = client.post("/settings/initialize", data={
            "name": "Web project restarted",
            "authorization": "Authorized fictional private data",
            "papers": (io.BytesIO(b"doi,title\n10.0000/web,Fictional web paper\n"), "papers.csv"),
        }, base_url=base_url)
        self.assertEqual(reinitialized.status_code, 303)
        project_library.attach(project, "10.0000/web", pdf, identity_confirmed=True)
        self.assertEqual(client.get("/", base_url=base_url).status_code, 200)

    def test_web_settings_accept_json_core_manifest(self):
        project = self.root.parent / "json-web-project"
        app = web_server.create_app(
            registry=project / "registry", project_root=project, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        response = client.post("/settings/initialize", data={
            "name": "JSON project",
            "authorization": "Authorized fictional private data",
            "papers": (io.BytesIO(json.dumps({
                "schema_version": 1,
                "papers": [{"doi": "10.0000/json", "title": "Fictional JSON paper"}],
            }).encode()), "papers.json"),
        }, base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(project_library.status(project)["papers"][0]["doi"], "10.0000/json")

    def test_web_attach_pdf_from_shared_data_folder(self):
        project_library.register(self.root, [{"doi": "10.0000/folder", "title": "Folder paper"}])
        folder = self.root.parent / "pdf-data"
        folder.mkdir()
        pdf = self.pdf("folder.pdf", "Fictional finding about apricot evidence.")
        shutil.copyfile(pdf, folder / "folder.pdf")
        (folder / "outside.pdf").symlink_to(pdf)
        (folder / "notes.txt").write_text("Not a PDF")
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root, pdf_data_root=folder,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        settings = client.get("/settings", base_url=base).get_data(as_text=True)
        self.assertIn('value="folder.pdf"', settings)
        self.assertNotIn('value="outside.pdf"', settings)
        self.assertNotIn('value="notes.txt"', settings)
        self.assertNotIn('id="attach-paper"', settings)
        self.assertIn('<ul class="library-records">', settings)
        self.assertRegex(settings, r'<dd><details><summary>.*?awaiting_pdf.*?</summary><form')
        self.assertIn('type="hidden" name="doi" value="10.0000/folder"', settings)
        token = re.search(r'name="token" value="([^"]+)"', settings).group(1)
        form = {"doi": "10.0000/folder", "pdf": "folder.pdf", "token": token}
        self.assertEqual(client.post("/settings/attach-pdf", data=form, base_url=base).status_code, 400)
        form["confirm_identity"] = "yes"
        for invalid in ("../folder.pdf", str(pdf), "outside.pdf", "absent.pdf"):
            self.assertEqual(client.post("/settings/attach-pdf", data={**form, "pdf": invalid},
                                         base_url=base).status_code, 400)
        self.assertEqual(client.post("/settings/attach-pdf", data={**form, "token": "wrong"},
                                     base_url=base).status_code, 403)
        response = client.post("/settings/attach-pdf", data=form, base_url=base,
                               headers={"Origin": base})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "extracted")
        self.assertNotIn('action="/settings/attach-pdf"', client.get("/settings", base_url=base).get_data(as_text=True))
        result = project_library.store(self.root).call("search_evidence", {"query": "apricot"})
        self.assertTrue(result["result"]["items"])
        context = project_library.store(self.root).call("get_source_context", {
            "unit_id": result["result"]["items"][0]["id"], "collections": ["core"],
            "snapshot_id": result["collection_context"]["snapshot_id"],
        })
        self.assertIn("apricot", context["result"]["text"])
        client.post("/settings/reset", data={"confirmation": "RESET PROJECT"}, base_url=base)
        self.assertTrue((folder / "folder.pdf").is_file())

    def test_web_attach_pdf_missing_data_folder(self):
        project_library.register(self.root, [{"doi": "10.0000/folder", "title": "Folder paper"}])
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.root.parent / "missing", profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        settings = app.test_client().get("/settings", base_url="http://localhost:8080")
        self.assertEqual(settings.status_code, 200)
        self.assertIn(b"No PDFs found", settings.data)

    def test_connection_page_before_first_pdf_is_generic_and_pins_project(self):
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        response = app.test_client().get("/connect", base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("PROJECT_ROOT=" + str(self.root), text)
        self.assertIn("COLLECTION_REGISTRY=" + str(self.root / "registry"), text)
        self.assertIn("--collection core", text)
        self.assertIn("YOUR_SEARCH_TERM", text)
        self.assertIn("attach at least one PDF", text)
        self.assertNotIn("MGUS", text)
        self.assertNotIn("myeloma", text)

    def test_web_citation_discovery_directions_and_saved_results(self):
        project_library.register(self.root, [{"doi": "10.0000/core", "title": "Core paper"}])
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        text = client.get("/discovery", base_url=base).get_data(as_text=True)
        self.assertIn("Find references", text)
        self.assertIn("Find citing papers", text)
        token = re.search(r'name="token" value="([^"]+)"', text).group(1)
        self.assertEqual(client.post("/discovery", data={"direction": "impact"}, base_url=base).status_code, 403)

        def discover(root, output, *, directions):
            collections.atomic_json(output, "neighborhood.json", {
                "limitations": ["Provider-indexed only"], "seeds": [
                    {"seed_doi": "10.0000/core", "providers": {"openalex": {"status": "failed"}}}],
                "nodes": {"paper": {"record": {"title": "<script>Related paper</script>",
                                             "identifiers": {"doi": "10.0000/related"}}}},
                "edges": [{"direction": directions[0], "seed_doi": "10.0000/core",
                           "target_provider_record": "paper"}],
            })
            return {"edges": 1, "unresolved": 0, "seeds_with_provider_gaps": 1}

        with patch("research_library.build.project_library.discover", side_effect=discover) as mocked:
            for direction in ("background", "impact"):
                response = client.post("/discovery", data={"direction": direction, "token": token}, base_url=base)
                self.assertEqual(response.status_code, 303)
                app.extensions["citation_discovery"]["thread"].join(timeout=5)
                results = client.get(response.location, base_url=base)
                self.assertEqual(results.status_code, 200)
                self.assertIn(b"&lt;script&gt;Related paper&lt;/script&gt;", results.data)
                self.assertIn(b"1 core papers with provider gaps", results.data)
                self.assertEqual(mocked.call_args.kwargs["directions"], [direction])
                library = client.get("/library", base_url=base)
                self.assertEqual(library.status_code, 200)
                self.assertIn(b"1 core papers; 1 additional discovered records", library.data)
                self.assertIn(b"&lt;script&gt;Related paper&lt;/script&gt;", library.data)
                self.assertIn(b"Awaiting PDF", library.data)
                expected_category = (b"References" if direction == "background"
                                     else b"References; Citing papers")
                self.assertIn(b"<dt>Library</dt><dd>" + expected_category + b"</dd>", library.data)
                linked = '<a href="https://doi.org/10.0000/core">Core paper</a>'
                relation = (f"Core paper {linked} cites this paper." if direction == "background"
                            else f"This paper cites core paper {linked}.")
                self.assertIn(
                    relation.encode(),
                    library.data)
        self.assertEqual(len(project_library.load(self.root)["papers"]), 1)

    def test_project_library_discovery_persistence_pagination_and_core_status(self):
        project_library.register(self.root, [{"doi": "10.0000/core", "title": "Core paper"}])
        project_library.attach(self.root, "10.0000/core", self.pdf(
            "core.pdf", "Fictional core evidence for testing the project library extraction status and discovery relationships."),
                               identity_confirmed=True)
        run = "a" * 32
        output = self.root / "discovery" / run
        nodes = {"core": {"record": {"title": "Provider core title", "identifiers": {"doi": "10.0000/core"}}}}
        nodes.update({str(n): {"record": {"title": f"Related {n:03}", "identifiers": {"doi": f"10.0000/related-{n}"}}}
                      for n in range(55)})
        nodes["duplicate"] = nodes["0"]
        nodes["unknown"] = {"record": {"title": "", "identifiers": {}},
                            "reference": {"unstructured": "Unresolved reference <text>"}}
        collections.atomic_json(output, "job.json", {
            "status": "complete", "label": "Find references", "started_at": "2026-11-09T12:00:00Z"})
        collections.atomic_json(output, "neighborhood.json", {
            "nodes": nodes,
            "edges": [{"target_provider_record": key, "direction": "background", "seed_doi": "10.0000/core"}
                      for key in nodes],
        })
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        result = client.get("/library", base_url=base)
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"1 core papers; 56 additional discovered records", result.data)
        self.assertIn(b'<ul class="library-records" aria-label="Library papers">', result.data)
        self.assertIn(b'<h3><a href="https://doi.org/10.0000/core">Core paper</a></h3><dl>', result.data)
        self.assertIn(b'<dt>Library</dt><dd>Core papers</dd>', result.data)
        self.assertIn(b'<dt>Library</dt><dd>References</dd>', result.data)
        self.assertIn(b'<summary>Connections to your core papers</summary>', result.data)
        self.assertIn(b'Core paper <a href="https://doi.org/10.0000/core">Core paper</a> cites this paper.', result.data)
        self.assertIn(b'Find references', result.data)
        self.assertIn(b'2026-11-09T12:00:00Z</a>', result.data)
        self.assertNotIn(b'>Search 1</a>', result.data)
        self.assertIn(b">Extracted<", result.data)
        self.assertIn(b">Next</a>", result.data)
        last = client.get("/library?offset=50", base_url=base)
        self.assertIn(b"Metadata only", last.data)
        self.assertIn(b"Unresolved reference &lt;text&gt;", last.data)
        filtered = client.get("/library?query=related-54", base_url=base)
        self.assertIn(b"1 matching records", filtered.data)
        self.assertIn(b"Related 054", filtered.data)
        self.assertNotIn(b"Related 053", filtered.data)
        graph = releases.read_json((output / "neighborhood.json").read_bytes())
        for edge in graph["edges"]:
            edge["direction"] = "impact"
        collections.atomic_json(output, "neighborhood.json", graph)
        citing = client.get("/library?query=related-54", base_url=base)
        self.assertIn(b'<dt>Library</dt><dd>Citing papers</dd>', citing.data)
        sources = client.get("/sources", base_url=base)
        self.assertEqual(sources.status_code, 200)
        self.assertNotIn(b'<select name="collection">', sources.data)
        self.assertNotIn(b"Endpoint default", sources.data)
        self.assertIn(b"Showing AI evidence from <strong>Fictional research</strong>", sources.data)
        for route in ("/publications", "/coverage"):
            metadata = client.get(route, base_url=base)
            self.assertEqual(metadata.status_code, 200)
            self.assertNotIn(b'<select name="collection">', metadata.data)
            self.assertIn(b"No metadata catalog is configured.", metadata.data)
            self.assertIn(b"Project: <strong>Fictional research</strong>", metadata.data)
        self.assertEqual(len(project_library.load(self.root)["papers"]), 1)

    def test_web_discovery_failure_and_reset_guard(self):
        project_library.register(self.root, [{"doi": "10.0000/core", "title": "Core paper"}])
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        text = client.get("/discovery", base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', text).group(1)
        event = threading.Event()

        def fail(*args, **kwargs):
            event.wait(timeout=5)
            raise ValueError("Provider unavailable for this search")

        with patch("research_library.build.project_library.discover", side_effect=fail):
            response = client.post("/discovery", data={"direction": "impact", "token": token}, base_url=base)
            try:
                self.assertEqual(client.post("/settings/reset", data={"confirmation": "RESET PROJECT"},
                                             base_url=base).status_code, 409)
                self.assertEqual(client.post("/discovery", data={"direction": "impact", "token": token},
                                             base_url=base).status_code, 409)
                self.assertEqual(client.post("/settings/configure-metadata", data={"token": token},
                                             base_url=base).status_code, 409)
            finally:
                event.set()
                app.extensions["citation_discovery"]["thread"].join(timeout=5)
            result = client.get(response.location, base_url=base)
            self.assertIn(b"Provider unavailable for this search", result.data)

    def test_library_missing_titles_sort_last_and_use_saved_doi_metadata(self):
        run = "b" * 32
        output = self.root / "discovery" / run
        collections.atomic_json(output, "job.json", {
            "status": "complete", "label": "Find references", "started_at": "2026-11-10T12:00:00Z"})
        collections.atomic_json(output, "neighborhood.json", {
            "nodes": {
                "reference": {"record": {"title": "", "identifiers": {"doi": "10.0000/resolved"}}},
                "metadata": {"record": {"title": "Recovered <title>", "identifiers": {"doi": "10.0000/RESOLVED"}}},
                "missing": {"record": {"title": "", "identifiers": {"doi": "10.0000/missing"}}},
                "named": {"record": {"title": "Zebra paper", "identifiers": {"doi": "10.0000/named"}}},
            },
            "edges": [{"target_provider_record": key, "direction": "background", "seed_doi": "10.0000/core"}
                      for key in ("missing", "reference", "named")],
        })
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        response = client.get("/library", base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recovered &lt;title&gt;", response.data)
        self.assertLess(response.data.index(b"Zebra paper"), response.data.index(b"[Unresolved title]"))
        filtered = client.get("/library?query=Recovered", base_url="http://localhost:8080")
        self.assertIn(b"1 matching records", filtered.data)

    def test_configure_metadata_from_saved_searches_and_preserve_on_attachment(self):
        from tests.test_citation_neighborhood import work
        project_library.register(self.root, [
            {"doi": "10.0000/paper-1", "title": "Core one"},
            {"doi": "10.0000/paper-2", "title": "Core two"},
        ])
        project_library.attach(self.root, "10.0000/paper-1", self.pdf(
            "first.pdf", "Fictional first core evidence with enough native text for a private metadata catalog test."),
            identity_confirmed=True)
        with self.assertRaisesRegex(ValueError, "Complete a Find related papers search"):
            project_library.configure_metadata(self.root)

        def handler(request):
            if request.url.host == "api.crossref.org":
                identifier = request.url.path.removeprefix("/works/")
                return httpx.Response(200, json={"message": {
                    "DOI": identifier, "reference": [], "reference-count": 0}})
            if "/https://doi.org/" in request.url.path:
                return httpx.Response(200, json=work(int(request.url.path.rsplit("-", 1)[-1])))
            expression = request.url.params["filter"]
            values = ([work(101)] if expression == "cites:W1" else []
                      if expression.startswith("cites:") else
                      [work(int(value[1:])) for value in expression.split(":", 1)[1].split("|")])
            return httpx.Response(200, json={"meta": {"count": len(values)}, "results": values})

        client_class = literature.MetadataClient
        for direction, name in (("background", "a" * 32), ("impact", "b" * 32)):
            output = self.root / "discovery" / name
            with patch.object(project_library.literature, "MetadataClient", side_effect=lambda: client_class(
                    transport=httpx.MockTransport(handler), sleep=lambda _: None, clock=lambda: 0)):
                project_library.discover(self.root, output, directions=[direction])
            collections.atomic_json(output, "job.json", {
                "status": "complete", "direction": direction,
                "report": {"edges": 1, "unresolved": 0, "seeds_with_provider_gaps": 0},
                "label": "Synthetic search", "started_at": "2026-11-10T12:00:00Z"})
        older = self.root / "discovery" / ("c" * 32)
        old_graph = json.loads((output / "neighborhood.json").read_text())
        old_graph["acquired_at"] = "2020-01-01T00:00:00+00:00"
        old_graph["seeds"][0]["providers"]["openalex"] = {
            "status": "failed", "error": "Old numbered-page ceiling"}
        collections.atomic_json(older, "neighborhood.json", old_graph)
        collections.atomic_json(older, "job.json", {
            "status": "complete", "direction": "impact", "label": "Old incomplete search",
            "started_at": "2020-01-01T00:00:00Z"})
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        graph_path = self.root / "discovery" / ("a" * 32) / "neighborhood.json"
        graph = json.loads(graph_path.read_text())
        graph["unresolved"] = [{"openalex_id": "W123456", "seed_doi": "10.0000/paper-1", "direction": "background"}]
        collections.atomic_json(graph_path.parent, graph_path.name, graph)
        issues = client.get("/discovery?direction=background", base_url=base)
        self.assertEqual(issues.status_code, 200)
        self.assertIn(b'id="coverage-issues"', issues.data)
        self.assertIn(b'https://openalex.org/W123456', issues.data)
        self.assertIn(b'>View unresolved reference in OpenAlex</a>', issues.data)
        self.assertNotIn(b'>W123456</a>', issues.data)
        self.assertIn(b'OpenAlex record ID: <code>W123456</code>', issues.data)
        self.assertIn(b"Core one", issues.data)
        self.assertIn(b"Retry find references", issues.data)
        graph["unresolved"] = []
        collections.atomic_json(graph_path.parent, graph_path.name, graph)
        page = client.get("/publications", base_url=base)
        self.assertIn(b"Configure/update metadata catalog", page.data)
        token = re.search(r'name="token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        self.assertEqual(client.post("/settings/configure-metadata", base_url=base).status_code, 403)
        response = client.post("/settings/configure-metadata", data={"token": token}, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/settings/metadata-progress")
        app.extensions["metadata_update"]["thread"].join(timeout=10)
        status = client.get(response.location, base_url=base)
        self.assertIn(b"Metadata catalog updated", status.data)
        self.assertIn(b"from 2 saved searches", status.data)
        self.assertNotIn("Refresh", status.headers)
        metadata = client.get("/publications?query=Fictional", base_url=base)
        self.assertEqual(metadata.status_code, 200)
        self.assertIn(b"Fictional paper", metadata.data)
        self.assertNotIn(b"No metadata catalog is configured.", metadata.data)
        self.assertIn(b"Core library overlap", metadata.data)
        self.assertNotIn(b"Istopmm overlap", metadata.data)
        self.assertNotIn(b"<dt>Diseases</dt>", metadata.data)
        self.assertNotIn(b'<select name="collection">', metadata.data)
        coverage = client.get("/coverage", base_url=base)
        self.assertEqual(coverage.status_code, 200)
        self.assertNotIn(b'<button>Filter</button>', coverage.data)
        self.assertIn(b'<button>Filter</button>', metadata.data)
        self.assertNotIn(b'action="/settings/configure-metadata"', coverage.data)
        self.assertIn(b'open Publication metadata</a>', coverage.data)
        self.assertIn(b"1 of 2 core papers have attached PDFs and retained extracted text", coverage.data)
        self.assertIn(b"exclude separately attached Core papers", coverage.data)
        self.assertIn(b"not project-wide full-text totals", coverage.data)
        self.assertIn(b"Full-text availability unknown from saved metadata", coverage.data)
        self.assertIn(b"not a count of unavailable PDFs or failed downloads", coverage.data)
        self.assertNotIn(b"Full text not reported count", coverage.data)
        self.assertIn(b"Core papers overlap", coverage.data)
        self.assertNotIn(b"Istopmm overlap", coverage.data)
        self.assertNotIn(b"<dt>Diseases</dt>", coverage.data)
        self.assertNotIn(b"<dt>Disease</dt>", coverage.data)
        self.assertNotIn(b"1000-01-01", coverage.data)
        self.assertIn(b"Unrestricted (no publication-date filter)", coverage.data)
        self.assertIn(b"Searches complete", coverage.data)
        self.assertTrue((older / "neighborhood.json").is_file())
        self.assertIn(b'action="/settings/configure-metadata"', metadata.data)
        before = project_library.store(self.root).current()
        catalog_id = next(e["release_id"] for e in before.document["collections"] if e["id"] == "metadata")
        project_library.attach(self.root, "10.0000/paper-2", self.pdf(
            "second.pdf", "Fictional second core evidence with different native text to test catalog preservation."),
            identity_confirmed=True)
        after = project_library.store(self.root).current()
        self.assertEqual(catalog_id, next(
            e["release_id"] for e in after.document["collections"] if e["id"] == "metadata"))
        self.assertIn(b"Fictional paper", client.get("/publications?query=Fictional", base_url=base).data)
        pdf_root = self.root.parent / "pdf-data"
        source = after.snapshots["core"]._bundle["sources"][0]
        collections.atomic_json(pdf_root, "fixture-downloads.json", [{
            "status": "downloaded", "doi": source["doi"], "sha256": source["sha256"],
            "source_url": "https://example.org/original.pdf", "final_url": "https://example.org/final.pdf",
            "retrieved_at": "2026-01-01T12:00:00Z", "version_note": "Repository manuscript",
        }])
        source_app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root, pdf_data_root=pdf_root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        enriched = source_app.test_client().get("/sources", base_url=base)
        self.assertIn(b'href="https://example.org/final.pdf"', enriched.data)
        self.assertIn(b"SHA-256 and DOI matched", enriched.data)
        self.assertIn(b"Repository manuscript", enriched.data)
        self.assertIn(b"<dt>Year</dt><dd>2025</dd>", enriched.data)
        self.assertIn(b"Year metadata source", enriched.data)
        collections.atomic_json(pdf_root, "fixture-downloads.json", [{
            "status": "downloaded", "doi": source["doi"], "sha256": "0" * 64,
            "source_url": "https://example.org/wrong.pdf", "final_url": "https://example.org/wrong.pdf",
            "retrieved_at": "2026-01-01T12:00:00Z",
        }])
        self.assertNotIn(b"https://example.org/wrong.pdf",
                         source_app.test_client().get("/sources", base_url=base).data)
        self.assertEqual(client.post(
            "/settings/configure-metadata", data={"token": token}, base_url=base).status_code, 303)
        app.extensions["metadata_update"]["thread"].join(timeout=10)

    def test_metadata_update_progress_failure_and_reset_guard(self):
        app = web_server.create_app(
            registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        token = re.search(r'name="token" value="([^"]+)"',
                          client.get("/discovery", base_url=base).get_data(as_text=True)).group(1)
        waiting = threading.Event()
        started = threading.Event()

        def update(root, *, progress):
            progress("Building catalog from test records")
            started.set()
            waiting.wait(timeout=5)
            raise ValueError("Synthetic catalog failure")

        with patch("research_library.build.project_library.configure_metadata", side_effect=update) as mocked:
            response = client.post("/settings/configure-metadata", data={"token": token}, base_url=base)
            try:
                self.assertEqual(response.status_code, 303)
                self.assertTrue(started.wait(timeout=5))
                status = client.get(response.location, base_url=base)
                self.assertIn(b"Building catalog from test records", status.data)
                self.assertIn(b"<progress", status.data)
                self.assertEqual(status.headers["Refresh"], "2")
                self.assertEqual(client.post("/settings/reset", data={"confirmation": "RESET PROJECT"},
                                             base_url=base).status_code, 409)
                self.assertEqual(client.post("/settings/configure-metadata", data={"token": token},
                                             base_url=base).status_code, 303)
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(client.get("/publications", base_url=base).status_code, 302)
            finally:
                waiting.set()
                app.extensions["metadata_update"]["thread"].join(timeout=5)
        status = client.get("/settings/metadata-progress", base_url=base)
        self.assertIn(b"Synthetic catalog failure", status.data)
        self.assertNotIn("Refresh", status.headers)
        collections.atomic_json(self.root, "metadata-job.json", {"status": "running", "message": "Old update"})
        self.assertIn(b"interrupted", client.get("/settings/metadata-progress", base_url=base).data)

    def test_installed_cli_works_outside_source_tree(self):
        project = self.root.parent / "cli-project"
        papers = self.root.parent / "papers.csv"
        papers.write_text("doi,title\n10.0000/cli,Fictional paper\n", encoding="utf-8")
        pdf = self.pdf("cli.pdf", "Fictional research observation about grapefruit.")
        command = [str(Path(sys.executable).parent / "research-project"), "--root", str(project)]

        def run(*args):
            result = subprocess.run([*command, *args], cwd=self.root.parent, text=True,
                                    capture_output=True, check=True)
            return json.loads(result.stdout)

        self.assertEqual(run("init", "--name", "Fictional project", "--authorization",
                             "Authorized fictional data", "--papers", str(papers))["papers"], 1)
        self.assertEqual(run("status")["papers"][0]["status"], "awaiting_pdf")
        run("attach-pdf", "--doi", "10.0000/cli", "--pdf", str(pdf), "--confirm-identity")
        self.assertEqual(run("status")["papers"][0]["status"], "extracted")

    def test_metadata_manifest_roundtrip_excludes_private_data(self):
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"}])
        pdf = self.pdf("first.pdf", "Fictional research finding for private extraction and retrieval.")
        project_library.attach(self.root, "10.0000/one", pdf, identity_confirmed=True)
        destination = self.root.parent / "core-papers.json"
        self.assertEqual(project_library.export_manifest(self.root, destination), 1)
        self.assertEqual(json.loads(destination.read_text()), {
            "schema_version": 1, "papers": [{"doi": "10.0000/one", "title": "First"}]})
        fresh = self.root.parent / "fresh-project"
        project_library.initialize(fresh, "Other", "Authorized", project_library.read_manifest(destination))
        self.assertEqual(project_library.status(fresh)["papers"][0]["status"], "awaiting_pdf")
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_library.export_manifest(self.root, destination)

    def test_attach_search_and_retained_snapshot(self):
        project_library.register(self.root, [
            {"doi": "10.0000/one", "title": "First"},
            {"doi": "10.0000/two", "title": "Second"},
        ])
        pdf = self.pdf("first.pdf", "Fictional finding about marmalade and screening.")
        with self.assertRaisesRegex(ValueError, "Confirm"):
            project_library.attach(self.root, "10.0000/one", pdf, identity_confirmed=False)
        first = project_library.attach(self.root, "10.0000/one", pdf, identity_confirmed=True)
        index = Path(first["text_views"])
        self.assertTrue(index.is_file())
        self.assertIn("marmalade", next(index.parent.glob("*/text.md")).read_text())
        store = project_library.store(self.root)
        response = store.call("search_evidence", {"query": "marmalade"})
        self.assertEqual(len(response["result"]["items"]), 1)
        self.assertEqual(response["result"]["items"][0]["citation"]["locator"]["pdf_page"], 1)
        unit = response["result"]["items"][0]
        context = store.call("get_source_context", {"unit_id": unit["id"], "collections": ["core"],
                                                     "snapshot_id": first["snapshot"]})
        self.assertIn("marmalade", context["result"]["text"])
        self.assertIn("not independently verified", unit["citation"]["provenance"])
        app = web_server.create_app(registry=self.root / "registry", profiles=["private_context"],
                                    allowed_collections=["core"], default_collections=["core"])
        client = app.test_client()
        self.assertIn(b"Awaiting PDF", client.get("/library", base_url="http://127.0.0.1:8080").data)
        api = client.get("/api/search?collection=core&query=marmalade", base_url="http://127.0.0.1:8080")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(len(api.json["result"]["items"]), 1)
        connect = client.get("/connect", base_url="http://127.0.0.1:8080").get_data(as_text=True)
        self.assertIn("YOUR_SEARCH_TERM", connect)
        self.assertIn("--collection core", connect)
        self.assertNotIn("Search for MGUS screening", connect)
        async def verify_mcp():
            params = StdioServerParameters(
                command=str(Path(sys.executable).parent / "research-mcp"),
                args=["--registry", str(self.root / "registry"), "--allow-profile", "private_context",
                      "--allow-collection", "core", "--default-collection", "core"])
            with tempfile.TemporaryFile(mode="w+") as diagnostics:
                async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                        await client.initialize()
                        result = await client.call_tool("search_evidence", {"query": "marmalade"})
                        self.assertFalse(result.isError)
                        hit = result.structuredContent["result"]["items"][0]
                        context = await client.call_tool("get_source_context", {
                            "unit_id": hit["id"], "collections": ["core"], "snapshot_id": first["snapshot"]})
                        self.assertFalse(context.isError)
                        self.assertIn("marmalade", context.structuredContent["result"]["text"])
        asyncio.run(verify_mcp())
        with self.assertRaisesRegex(ValueError, "already"):
            project_library.attach(self.root, "10.0000/two", pdf, identity_confirmed=True)
        second_pdf = self.pdf("second.pdf", "Distinct fictional observation about oranges and screening.")
        second = project_library.attach(self.root, "10.0000/two", second_pdf, identity_confirmed=True)
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(len(store.load(first["snapshot"]).snapshots["core"]._bundle["sources"]), 1)
        self.assertEqual(len(store.current().snapshots["core"]._bundle["sources"]), 2)
        self.assertEqual({row["status"] for row in project_library.status(self.root)["papers"]}, {"extracted"})

    def test_invalid_pdf_does_not_activate(self):
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"}])
        path = self.root.parent / "bad.pdf"
        path.write_text("not a PDF")
        with self.assertRaisesRegex(ValueError, "PDF"):
            project_library.attach(self.root, "10.0000/one", path, identity_confirmed=True)
        self.assertIsNone(project_library.status(self.root)["snapshot"])

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is required for OCR")
    def test_scanned_pdf_ocr_is_searchable_and_labelled(self):
        project_library.register(self.root, [{"doi": "10.0000/scan", "title": "Scanned paper"}])
        with pymupdf.open() as original:
            page = original.new_page(width=612, height=792)
            page.insert_text((72, 100), "Fictional research study about tangerines and screening.",
                             fontsize=18)
            image = page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).tobytes("png")
        scan = self.root.parent / "scan.pdf"
        with pymupdf.open() as document:
            page = document.new_page(width=612, height=792)
            page.insert_image(page.rect, stream=image)
            document.save(scan)
        result = project_library.attach(self.root, "10.0000/scan", scan, identity_confirmed=True)
        self.assertIsNotNone(result["ocr"])
        hit = project_library.store(self.root).call("search_evidence", {"query": "tangerines"})
        self.assertEqual(len(hit["result"]["items"]), 1)
        self.assertTrue(any("not human verified" in caveat for caveat in hit["result"]["items"][0]["caveats"]))
        self.assertIn(project_library.status(self.root)["papers"][0]["status"],
                      {"extracted_with_ocr", "needs_visual_review"})

    def test_citation_export_retains_occurrences_and_escapes_spreadsheet_formulas(self):
        output = self.root / "discovery"
        output.mkdir()
        graph = {
            "nodes": {"crossref:example": {
                "record": {"title": "=2+2", "identifiers": {}, "publication_date": "",
                           "authors": ["Example Author"]},
                "reference": {"unstructured": "unknown reference"}}},
            "edges": [{"seed_doi": "10.0000/one", "direction": "background",
                       "target_provider_record": "crossref:example",
                       "sources": [{"url": "https://example.test"}]}],
        }
        (output / "neighborhood.json").write_text(json.dumps(graph))
        target = project_library.export_citations(output)
        with target.open(newline="") as stream:
            row, = csv.DictReader(stream)
        self.assertEqual(row["title"], "'=2+2")
        self.assertEqual(row["unstructured_reference"], "unknown reference")
        self.assertEqual(json.loads(row["edge_sources"])[0]["url"], "https://example.test")
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_library.export_citations(output)


if __name__ == "__main__":
    unittest.main()
