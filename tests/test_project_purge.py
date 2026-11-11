import io
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pymupdf

from research_library.build import project_library, project_purge
from research_library import collections, releases, web_server


class ProjectPurgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.pdf_root = self.root.parent / "pdf-data"
        self.pdf_root.mkdir()
        project_library.initialize(self.root, "Purge fixture", "Authorized private synthetic research")
        project_library.register(self.root, [
            {"doi": "10.0000/keep", "title": "Keep this core"},
            {"doi": "10.0000/remove", "title": "Remove this core"},
        ])
        self.pdfs = {}
        for doi in ("keep", "remove"):
            path = self.pdf_root / (doi + ".pdf")
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((50, 50),
                    f"Fictional {doi} evidence with sufficient native text to test retained core search and paper purge.")
                pdf.save(path)
            self.pdfs[doi] = path
            project_library.attach(self.root, "10.0000/" + doi, path, identity_confirmed=True)
        self.current_id = project_library.store(self.root).current().release_id

    def graph(self):
        nodes = {key: {"record": {"title": key, "identifiers": {"doi": "10.0000/" + key},
                                 "publication_date": "", "authors": []}, "sources": []}
                 for key in ("keep", "remove", "shared", "unique", "kept-neighbor")}
        graph = {
            "schema_version": 1, "acquired_at": "2026-11-10T12:00:00Z",
            "scope": "Synthetic citation search", "limitations": ["Provider-indexed only"],
            "nodes": nodes, "edges": [
                {"seed_doi": "10.0000/" + seed, "direction": direction,
                 "target_provider_record": target, "sources": []}
                for seed, direction, target in (
                    ("remove", "background", "unique"), ("remove", "impact", "shared"),
                    ("keep", "background", "shared"), ("keep", "impact", "kept-neighbor"),
                    ("keep", "background", "remove"),
                )],
            "seeds": [
                {"seed_doi": "10.0000/" + key, "title": key, "number": str(index), "providers": {}}
                for index, key in enumerate(("keep", "remove"), 1)],
            "unresolved": [{"seed_doi": "10.0000/remove", "openalex_id": "W123"},
                           {"seed_doi": "10.0000/keep", "openalex_id": "W456"}],
            "resolution_issues": [],
        }
        folder = self.root / "discovery" / ("a" * 32)
        collections.atomic_json(folder, "neighborhood.json", graph)
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "label": "Synthetic", "direction": "background",
            "started_at": "2026-11-10T12:00:00Z", "report": {}})
        collections.atomic_json(folder / "responses", "raw.json", {"private": "removed seed cached data"})
        return folder

    def app(self):
        return web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdf_root, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])

    def token(self, client):
        return re.search(r'name="token" value="([^"]+)"',
            client.get("/settings", base_url="http://localhost:8080").get_data(as_text=True)).group(1)

    def test_purge_removes_exclusive_relationships_pdfs_caches_and_old_snapshots(self):
        folder = self.graph()
        unique_pdf = self.pdf_root / "unique.pdf"
        unique_pdf.write_bytes(b"synthetic exclusive downloaded PDF")
        shared_pdf = self.pdf_root / "shared.pdf"
        shared_pdf.write_bytes(b"synthetic shared downloaded PDF")
        records = [{"doi": "10.0000/" + key, "sha256": releases.sha256(path.read_bytes()),
                    "status": "downloaded"} for key, path in (
                        ("remove", self.pdfs["remove"]), ("unique", unique_pdf), ("shared", shared_pdf))]
        collections.atomic_json(self.pdf_root, "fixture-downloads.json", records)
        original_units = project_library.store(self.root).current().snapshots["core"]._bundle["units"]
        retained_units = [unit for unit in original_units
                          if "keep" in unit["text"]]
        report = project_purge.purge(self.root, "10.0000/remove", self.pdf_root)
        self.assertEqual(report["doi"], "10.0000/remove")
        self.assertFalse(self.pdfs["remove"].exists())
        self.assertFalse(unique_pdf.exists())
        self.assertTrue(shared_pdf.exists())
        self.assertTrue(self.pdfs["keep"].exists())
        self.assertEqual(list(project_library.load(self.root)["papers"]), ["10.0000/keep"])
        current = project_library.store(self.root).current()
        self.assertEqual([source["doi"] for source in current.snapshots["core"]._bundle["sources"]],
                         ["10.0000/keep"])
        self.assertEqual(current.snapshots["core"]._bundle["units"], retained_units)
        self.assertFalse((self.root / "registry/registries" / (self.current_id + ".json")).exists())
        graph = json.loads((folder / "neighborhood.json").read_text())
        self.assertEqual({edge["target_provider_record"] for edge in graph["edges"]}, {"shared", "kept-neighbor"})
        self.assertNotIn("unique", graph["nodes"])
        self.assertNotIn("remove", graph["nodes"])
        self.assertEqual(len(graph["unresolved"]), 1)
        self.assertFalse((folder / "responses").exists())
        remaining_records = json.loads((self.pdf_root / "fixture-downloads.json").read_text())
        self.assertEqual([record["doi"] for record in remaining_records], ["10.0000/shared"])
        self.assertFalse(list(self.root.parent.glob(".project-purge-*")))
        client = self.app().test_client()
        response = client.get("/sources", base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Keep this core", response.data)
        self.assertNotIn(b"Remove this core", response.data)

    def test_validation_failure_keeps_original_data(self):
        with patch.object(project_purge.releases, "export", side_effect=ValueError("Staging failure")):
            with self.assertRaisesRegex(ValueError, "Staging failure"):
                project_purge.purge(self.root, "10.0000/remove", self.pdf_root)
        self.assertEqual(len(project_library.load(self.root)["papers"]), 2)
        self.assertTrue(self.pdfs["remove"].exists())
        self.assertEqual(project_library.store(self.root).current().release_id, self.current_id)
        self.assertFalse(list(self.root.parent.glob(".project-purge-*")))

    def test_purge_removes_exclusive_discovered_text_but_preserves_shared_evidence(self):
        self.graph()
        for key in ["shared", "unique"]:
            path = self.pdf_root / (key + ".pdf")
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((50, 50), f"Synthetic {key} discovered paper with retained searchable evidence.")
                pdf.save(path)
            project_library.attach(self.root, "10.0000/" + key, path,
                                   identity_confirmed=True, title=key)
        self.assertEqual(len(project_library.status(self.root)["papers"]), 2)
        self.assertEqual(len(project_library.status(self.root)["discovered"]), 2)
        project_purge.purge(self.root, "10.0000/remove", self.pdf_root)
        current = project_library.store(self.root).current()
        self.assertEqual({source["doi"] for source in current.snapshots["core"]._bundle["sources"]},
                         {"10.0000/keep", "10.0000/shared"})
        self.assertFalse((self.pdf_root / "unique.pdf").exists())
        self.assertTrue((self.pdf_root / "shared.pdf").exists())
        self.assertEqual([paper["doi"] for paper in project_library.status(self.root)["discovered"]],
                         ["10.0000/shared"])

    def test_purge_rebuilds_configured_catalog_without_removed_or_exclusive_records(self):
        from tests.test_citation_neighborhood import work
        from research_library.build import citation_neighborhood
        folder = self.graph()
        graph = json.loads((folder / "neighborhood.json").read_text())
        translated, nodes = {}, {}
        for number, (key, node) in enumerate(graph["nodes"].items(), 100):
            provider_work = work(number)
            provider_work["doi"] = "https://doi.org/10.0000/" + key
            provider_work["title"] = key
            record = citation_neighborhood.openalex_record(provider_work)
            provider_key = f"openalex:WORK:W{number}"
            translated[key] = provider_key
            nodes[provider_key] = {
                "record": record, "sources": [{"url": "https://api.openalex.org/works/W" + str(number),
                "retrieved_at": "2026-11-10T12:00:00Z", "sha256": "a" * 64}]}
        graph["nodes"] = nodes
        for edge in graph["edges"]:
            edge["target_provider_record"] = translated[edge["target_provider_record"]]
        collections.atomic_json(folder, "neighborhood.json", graph)
        project_library.configure_metadata(self.root)
        before = project_library.metadata_collections(self.root)
        self.assertEqual(before, ["metadata"])
        project_purge.purge(self.root, "10.0000/remove", self.pdf_root)
        catalog_store = collections.CollectionStore(
            self.root / "registry", allowed_profiles=["private_context"],
            allowed_collections=["core", "metadata"], defaults=["core"])
        catalog = catalog_store.current().snapshots["metadata"]
        records = [catalog.get(key) for key in catalog.matching_ids({})]
        titles = {record["title"] for record in records}
        self.assertIn("shared", titles)
        self.assertIn("kept-neighbor", titles)
        self.assertNotIn("unique", titles)
        self.assertNotIn("remove", titles)
        # Current snapshot remains usable after a new attachment.
        project_library.register(self.root, [{"doi": "10.0000/new", "title": "New paper"}])
        path = self.pdf_root / "new.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((50, 50),
                "A new fictional evidence source with different text for checking imports after permanent purge.")
            pdf.save(path)
        project_library.attach(self.root, "10.0000/new", path, identity_confirmed=True)
        self.assertEqual(project_library.metadata_collections(self.root), ["metadata"])

    def test_shared_checksum_used_by_retained_paper_is_not_deleted(self):
        digest = releases.sha256(self.pdfs["remove"].read_bytes())
        collections.atomic_json(self.pdf_root, "fixture-downloads.json", [
            {"status": "downloaded", "doi": "10.0000/remove", "sha256": digest},
            {"status": "downloaded", "doi": "10.0000/keep", "sha256": digest},
        ])
        project_purge.purge(self.root, "10.0000/remove", self.pdf_root)
        self.assertTrue(self.pdfs["remove"].exists())
        self.assertTrue((self.root / "papers" / (digest + ".pdf")).exists())

    def test_purge_last_core_and_reinitialize_evidence(self):
        project_purge.purge(self.root, "10.0000/remove", self.pdf_root)
        project_purge.purge(self.root, "10.0000/keep", self.pdf_root)
        self.assertEqual(project_library.status(self.root)["papers"], [])
        self.assertIsNone(project_library.status(self.root)["snapshot"])
        self.assertFalse(self.pdfs["keep"].exists())
        project_library.register(self.root, [{"doi": "10.0000/new", "title": "New core"}])
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "awaiting_pdf")

    def test_purge_rejects_symlink_before_deletion(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("Do not delete")
        (self.root / "unsafe").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            project_purge.purge(self.root, "10.0000/remove", self.pdf_root)
        self.assertEqual(outside.read_text(), "Do not delete")
        self.assertTrue(self.pdfs["remove"].exists())

    def test_purge_rejects_stale_preview_and_keeps_current_data(self):
        expected = project_purge.fingerprint(project_purge.plan(
            self.root, "10.0000/remove", self.pdf_root))
        project_library.register(self.root, [{"doi": "10.0000/added", "title": "New core"}])
        with self.assertRaisesRegex(ValueError, "changed after confirmation"):
            project_purge.purge(self.root, "10.0000/remove", self.pdf_root, expected_plan=expected)
        self.assertIn("10.0000/remove", project_library.load(self.root)["papers"])
        self.assertTrue(self.pdfs["remove"].exists())

    def test_preview_uses_cached_connections_not_graph_payloads_or_release_validation(self):
        folder = self.graph()
        preview = project_purge.plan(self.root, "10.0000/remove", self.pdf_root, prepare=False)
        expected = project_purge.fingerprint(preview)
        self.assertEqual(preview["related_removed"], 2)
        self.assertEqual(expected, project_purge.fingerprint(
            project_purge.plan(self.root, "10.0000/remove", self.pdf_root)))
        read_bytes = Path.read_bytes

        def lightweight_read(path):
            if path.name == "neighborhood.json":
                raise AssertionError("A cached preview must not read the full provider graph")
            return read_bytes(path)

        with patch.object(Path, "read_bytes", lightweight_read), \
                patch.object(project_purge, "_filtered_graph", side_effect=AssertionError("Graph rebuild")), \
                patch.object(releases, "validate", side_effect=AssertionError("Full release validation")):
            cached = project_purge.plan(self.root, "10.0000/remove", self.pdf_root, prepare=False)
        self.assertEqual(project_purge.fingerprint(cached), expected)
        self.assertEqual(project_purge.summary(preview), project_purge.summary(cached))
        graph = json.loads((folder / "neighborhood.json").read_text())
        graph["edges"][0]["seed_doi"] = "10.0000/keep"
        collections.atomic_json(folder, "neighborhood.json", graph)
        changed = project_purge.plan(self.root, "10.0000/remove", self.pdf_root, prepare=False)
        self.assertNotEqual(project_purge.fingerprint(changed), expected)
        self.assertEqual(changed["related_removed"], 1)

    def test_large_graph_filter_is_linear_and_does_not_mutate_input(self):
        count = 50000
        graph = {
            "nodes": {str(index): {"record": {"identifiers": {"doi": f"10.0000/paper-{index}"}}}
                      for index in range(count)},
            "edges": [{"seed_doi": "10.0000/keep", "target_provider_record": str(index)}
                      for index in range(count)],
            "seeds": [{"seed_doi": "10.0000/keep"}], "unresolved": [],
            "limitations": ["Original caveat"], "resolution_issues": [],
        }
        started = time.perf_counter()
        filtered = project_purge._filtered_graph(graph, {"10.0000/keep"})
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertEqual(len(filtered["nodes"]), count)
        filtered["limitations"].append("Purge caveat")
        self.assertEqual(graph["limitations"], ["Original caveat"])

    def test_add_and_purge_awaiting_paper_without_evidence_registry(self):
        project = self.root.parent / "empty-project"
        project_library.initialize(project, "Empty test", "Authorized")
        project_library.register(project, [
            {"doi": "10.0000/awaiting", "title": "Awaiting paper"},
            {"doi": "10.0000/untouched", "title": "Untouched paper"}])
        project_purge.purge(project, "10.0000/awaiting", self.pdf_root)
        self.assertEqual(list(project_library.load(project)["papers"]), ["10.0000/untouched"])
        self.assertIsNone(project_library.status(project)["snapshot"])

    def test_web_add_import_preview_confirmation_and_background_purge(self):
        app = self.app()
        client = app.test_client()
        base = "http://localhost:8080"
        token = self.token(client)
        settings = client.get("/settings", base_url=base)
        self.assertIn(b'class="danger remove-core"', settings.data)
        self.assertIn(b'aria-label="Remove Remove this core"', settings.data)
        self.assertIn(b'<dialog id="purge-dialog"', settings.data)
        self.assertIn(b'<script src="/settings.js" defer></script>', settings.data)
        self.assertIn("connect-src 'self'", settings.headers["Content-Security-Policy"])
        script = client.get("/settings.js", base_url=base)
        self.assertEqual(script.status_code, 200)
        self.assertIn(b'purgeDialog.showModal()', script.data)
        self.assertEqual(client.post("/settings/add-papers", data={"doi": "10.0000/new", "title": "New"},
                                     base_url=base).status_code, 403)
        response = client.post("/settings/add-papers", data={
            "token": token, "doi": "10.0000/new", "title": "New"}, base_url=base)
        self.assertEqual(response.status_code, 303)
        for filename, payload in (
            ("more.csv", b"doi,title\n10.0000/csv,CSV core\n"),
            ("more.json", b'{"schema_version":1,"papers":[{"doi":"10.0000/json","title":"JSON core"}]}'),
        ):
            self.assertEqual(client.post("/settings/import-papers", data={
                "token": token, "papers": (io.BytesIO(payload), filename)}, base_url=base).status_code, 303)
        invalid = client.post("/settings/import-papers", data={
            "token": token, "papers": (io.BytesIO(
                b"doi,title\n10.0000/valid,Valid\n10.0000/keep,Duplicate\n"), "bad.csv")}, base_url=base)
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn("10.0000/valid", project_library.load(self.root)["papers"])
        preview = client.post("/settings/purge-preview", data={
            "token": token, "doi": "10.0000/remove"}, base_url=base)
        self.assertEqual(client.post("/settings/purge-preview", data={
            "doi": "10.0000/remove"}, base_url=base).status_code, 403)
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"not reversible", preview.data)
        self.assertIn(str(self.pdfs["remove"]).encode(), preview.data)
        plan_id = re.search(r'name="plan_id" value="([^"]+)"', preview.get_data(as_text=True)).group(1)
        bad = client.post("/settings/purge-paper", data={
            "token": token, "doi": "10.0000/remove", "confirmation": "yes"}, base_url=base)
        self.assertEqual(bad.status_code, 400)
        stale = client.post("/settings/purge-paper", data={
            "token": token, "doi": "10.0000/remove", "confirmation": "10.0000/remove",
            "plan_id": "expired"}, base_url=base)
        self.assertEqual(stale.status_code, 400)
        self.assertIn(b"review a new purge preview", stale.data)
        started = threading.Event()
        release = threading.Event()
        original = project_purge.purge

        def pause(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            return original(*args, **kwargs)

        with patch.object(project_purge, "purge", side_effect=pause):
            response = client.post("/settings/purge-paper", data={
                "token": token, "doi": "10.0000/remove", "confirmation": "10.0000/remove",
                "plan_id": plan_id}, base_url=base)
            try:
                self.assertEqual(response.status_code, 303)
                self.assertTrue(started.wait(timeout=5))
                status = client.get(response.location, base_url=base)
                self.assertIn(b"<progress", status.data)
                self.assertEqual(client.post("/settings/add-papers", data={
                    "token": token, "doi": "10.0000/blocked", "title": "Blocked"}, base_url=base).status_code, 503)
            finally:
                release.set()
                app.extensions["core_purge"]["thread"].join(timeout=15)
        status = client.get(response.location, base_url=base)
        self.assertIn(b"permanently removed", status.data)
        self.assertNotIn("10.0000/remove", project_library.load(self.root)["papers"])


if __name__ == "__main__":
    unittest.main()
