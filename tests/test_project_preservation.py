import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from research_library import collections, releases, web_server
from research_library.build import project_edit, project_library, project_preservation, project_text


class ProjectPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "library"
        project_library.initialize(self.root, "Original library", "Synthetic private permission")
        self.rows = [{"doi": "10.0000/one", "title": "First article"},
                     {"doi": "10.0000/two", "title": "Second article"}]
        project_library.register(self.root, self.rows)
        for index, row in enumerate(self.rows):
            pdf = self.root.parent / f"source-{index}.pdf"
            with pymupdf.open() as document:
                document.new_page().insert_text((50, 50),
                    f"Fictional preserved evidence for article number {index}, enough text for extraction.")
                document.save(pdf)
            project_library.attach(self.root, row["doi"], pdf, identity_confirmed=True)
        current = project_text.editorial_store(self.root).current()
        source = next(s for s in current.snapshots["core"]._bundle["sources"] if s["doi"] == "10.0000/one")
        unit = project_edit.document_units(current.snapshots["core"]._bundle, source["id"])[0]
        project_edit.correct(self.root, snapshot_id=current.release_id, source_id=source["id"],
            unit_id=unit["id"], text="Cleanedtext privately checked synthetic transcription.",
            bibliography={k: v or "" for k, v in source["bibliography"].items()},
            source_url=source["url"] or "",
            tables=[{"id": "", "title": "Synthetic table", "headers": ["Measure", "Count"],
                     "units": [None, "participants"], "rows": [["Example", 42]], "footnotes": ["Synthetic note"]}],
            reviewer="Admin", reason="Cleaned original draft", checked=False)
        current = project_text.editorial_store(self.root).current()
        project_text.publish(self.root, snapshot_id=current.release_id, source_id=source["id"],
                             reviewer="Admin", reason="Compared with source", checked=True)
        self.bundle = current.snapshots["core"]._bundle
        self.source = source

    def test_soft_reset_preserves_every_byte_and_restores_only_encountered_dois_as_drafts(self):
        collections.atomic_json(self.root / "discovery" / ("a" * 32), "job.json", {"status": "complete"})
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        result = project_preservation.soft_reset(self.root)
        archive = Path(result["archive"])
        self.assertFalse(self.root.exists())
        self.assertEqual({p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}, before)
        with patch.object(project_library.literature_ingest, "extract_pdf", side_effect=AssertionError("Re-extracted")):
            project_library.initialize(self.root, "New library", "New private authorization", self.rows[:1])
        editorial = project_text.editorial_store(self.root).current().snapshots["core"]._bundle
        self.assertEqual([s["doi"] for s in editorial["sources"]], ["10.0000/one"])
        self.assertEqual(editorial["units"][0]["text"], "Cleanedtext privately checked synthetic transcription.")
        self.assertEqual(editorial["tables"], self.bundle["tables"])
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "draft")
        self.assertEqual(project_library.store(self.root).current().snapshots["core"]._bundle["units"], [])
        self.assertFalse((self.root / "discovery").exists())
        self.assertEqual(len(project_edit.history(self.root, self.source["id"])), 2)
        self.assertEqual(releases.sha256((self.root / "papers" / (self.source["sha256"] + ".pdf")).read_bytes()),
                         self.source["sha256"])
        project_library.register(self.root, self.rows[1:])
        self.assertEqual(len(project_text.editorial_store(self.root).current().snapshots["core"]._bundle["sources"]), 2)
        self.assertEqual({p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}, before)

    def test_corrupt_preserved_pdf_rejects_initialization_without_partial_project(self):
        archive = Path(project_preservation.soft_reset(self.root)["archive"])
        (archive / "papers" / (self.source["sha256"] + ".pdf")).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            project_library.initialize(self.root, "New", "Private", self.rows)
        self.assertFalse(self.root.exists())
        self.assertTrue((archive / "project.json").is_file())

    def test_multiple_resets_reuse_latest_draft_and_do_not_restore_unrelated_articles(self):
        project_preservation.soft_reset(self.root)
        project_library.initialize(self.root, "Round two", "Private", self.rows[:1])
        current = project_text.editorial_store(self.root).current()
        source = current.snapshots["core"]._bundle["sources"][0]
        unit = current.snapshots["core"]._bundle["units"][0]
        tables = [{"id": t["id"], **{k: t[k] for k in project_edit.TABLE_FIELDS}}
                  for t in current.snapshots["core"]._bundle["tables"]]
        project_edit.correct(self.root, snapshot_id=current.release_id, source_id=source["id"],
            unit_id=unit["id"], text="Newest synthetic draft.",
            bibliography={k: v or "" for k, v in source["bibliography"].items()}, source_url=source["url"],
            tables=tables, reviewer="Admin", reason="Newer cleanup", checked=False)
        project_preservation.soft_reset(self.root)
        project_library.initialize(self.root, "Round three", "Private", self.rows)
        current = project_text.editorial_store(self.root).current().snapshots["core"]._bundle
        self.assertEqual(next(u["text"] for u in current["units"]
                              if u["citation"]["source_id"] == source["id"]), "Newest synthetic draft.")
        self.assertEqual(len(current["sources"]), 2)
        self.assertEqual(len(project_preservation.archives(self.root)), 2)

    def test_web_reset_needs_confirmation_token_and_returns_to_core_upload(self):
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/settings", base_url=base).get_data(as_text=True)
        form_html = html.split('action="/settings/soft-reset"', 1)[1]
        token = re.search(r'name="token" value="([^"]+)"', form_html).group(1)
        form = {"token": token, "confirmation": "SOFT RESET"}
        self.assertEqual(client.post("/settings/soft-reset", data=form | {"token": ""}, base_url=base).status_code, 403)
        self.assertEqual(client.post("/settings/soft-reset", data=form | {"confirmation": "yes"}, base_url=base).status_code, 400)
        with patch("research_library.build.project_preservation.soft_reset", side_effect=ValueError("Busy writer")):
            self.assertEqual(client.post("/settings/soft-reset", data=form, base_url=base).status_code, 400)
        self.assertTrue(self.root.exists())
        response = client.post("/settings/soft-reset", data=form, base_url=base)
        self.assertEqual(response.status_code, 303)
        html = client.get(response.location, base_url=base).get_data(as_text=True)
        self.assertIn("Initialize project", html)
        self.assertIn("preserved project(s) available", html)
        initialized = client.post("/settings/initialize", data={
            "name": "New library", "authorization": "Private",
            "papers": (io.BytesIO(b"doi,title\n10.0000/one,First article\n"), "papers.csv")}, base_url=base)
        self.assertEqual(initialized.status_code, 303)
        listing = client.get("/library", base_url=base)
        self.assertIn(b"Clear-text draft", listing.data)
        self.assertEqual(client.get("/api/search?query=Cleanedtext&collection=core", base_url=base).json["result"]["items"], [])

    def test_symlinked_archive_or_root_is_rejected(self):
        archive = project_preservation.archive_root(self.root)
        archive.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            project_preservation.soft_reset(self.root)
        self.assertTrue((self.root / "project.json").is_file())

    def test_discovery_reuses_material_without_promoting_it_to_core_or_publishing(self):
        project_preservation.soft_reset(self.root)
        project_library.initialize(self.root, "New", "Private", self.rows[:1])
        graph = {
            "nodes": {"neighbor": {"record": {
                "identifiers": {"doi": "10.0000/two"}, "title": "Second article",
                "authors": [], "publication_date": ""}}},
            "edges": [{"seed_doi": "10.0000/one", "direction": "background",
                       "target_provider_record": "neighbor", "sources": []}],
            "seeds": [{"seed_doi": "10.0000/one", "providers": {}}],
            "limitations": [], "unresolved": [],
        }
        output = self.root / "discovery" / ("b" * 32)
        def discovery(destination, *args, **kwargs):
            collections.atomic_json(destination, "neighborhood.json", graph)
            return graph
        with patch.object(project_library.citation_neighborhood, "discover", side_effect=discovery):
            project_library.discover(self.root, output, directions=["background"])
        status = project_library.status(self.root)
        self.assertEqual([p["doi"] for p in status["papers"]], ["10.0000/one"])
        self.assertEqual(status["discovered"][0]["doi"], "10.0000/two")
        self.assertEqual(status["discovered"][0]["status"], "draft")
        self.assertEqual(project_library.store(self.root).current().snapshots["core"]._bundle["units"], [])


if __name__ == "__main__":
    unittest.main()
