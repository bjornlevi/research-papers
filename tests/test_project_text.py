import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from research_library import collections, web_server
from research_library.build import project_edit, project_library, project_purge, project_text


class ProjectTextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "library"
        project_library.initialize(self.root, "Portable library", "Synthetic private permission")
        project_library.register(self.root, [{"doi": "10.0000/text", "title": "Synthetic article"}])
        self.pdf = self.root.parent / "article.pdf"
        with pymupdf.open() as pdf:
            for number in [1, 2]:
                pdf.new_page().insert_text((50, 50), f"Drafttoken original article page {number} for clear text tests.")
            pdf.save(self.pdf)
        self.attached = project_library.attach(self.root, "10.0000/text", self.pdf, identity_confirmed=True)
        self.editorial = project_text.editorial_store(self.root).current()
        self.bundle = self.editorial.snapshots["core"]._bundle
        self.source = self.bundle["sources"][0]
        self.units = project_edit.document_units(self.bundle, self.source["id"])

    def publish(self, **changes):
        return project_text.publish(self.root, **{
            "snapshot_id": project_text.editorial_store(self.root).current().release_id,
            "source_id": self.source["id"], "reviewer": "Synthetic admin",
            "reason": "Checked synthetic transcription", "checked": True, **changes})

    def test_attachment_and_draft_edits_are_not_retrievable_until_explicit_publication(self):
        public = project_library.store(self.root)
        self.assertEqual(public.call("search_evidence", {"query": "Drafttoken"})["result"]["items"], [])
        self.assertEqual(public.current().snapshots["core"]._bundle["sources"], [])
        self.assertFalse(self.attached["retrievable"])
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "draft")
        initial = public.current().release_id
        args = {
            "snapshot_id": self.editorial.release_id, "source_id": self.source["id"], "unit_id": self.units[0]["id"],
            "text": project_edit.document_draft(self.units).replace("Drafttoken", "Cleantoken"),
            "whole_document": True, "bibliography": {k: v or "" for k, v in self.source["bibliography"].items()},
            "source_url": self.source["url"], "tables": [], "reviewer": "Draft admin", "reason": "Cleaned",
            "checked": False,
        }
        project_edit.correct(self.root, **args)
        self.assertEqual(public.current().release_id, initial)
        self.assertEqual(public.call("search_evidence", {"query": "Cleantoken"})["result"]["items"], [])
        result = self.publish()
        published = public.current()
        hits = published.call("search_evidence", {"query": "Cleantoken"})["result"]["items"]
        self.assertEqual(len(hits), 2)
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "published")
        self.assertEqual(published.snapshots["core"]._bundle["units"][0]["human_verification"], "reviewed")
        self.assertNotIn("not published for retrieval", published.snapshots["core"]._bundle["sources"][0]["provenance"])
        self.assertIn("publication audit", published.snapshots["core"]._bundle["sources"][0]["provenance"])
        self.assertEqual(project_text.editorial_store(self.root).current().snapshots["core"]._bundle["units"][0]["human_verification"], "not_reviewed")
        draft = project_text.editorial_store(self.root).current()
        args.update(snapshot_id=draft.release_id, text=args["text"].replace("Cleantoken", "Newdrafttoken"))
        project_edit.correct(self.root, **args)
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "draft_revision")
        self.assertEqual(public.call("search_evidence", {"query": "Newdrafttoken"})["result"]["items"], [])
        self.assertEqual(len(public.call("search_evidence", {"query": "Cleantoken"})["result"]["items"]), 2)
        self.assertEqual(public.load(initial).snapshots["core"]._bundle["units"], [])
        self.assertEqual(public.load(result["snapshot"]).snapshots["core"]._bundle, published.snapshots["core"]._bundle)
        self.assertTrue(self.pdf.is_file())

    def test_publication_validation_and_failed_activation_leave_retrieval_unchanged(self):
        before = project_library.store(self.root).current().release_id
        for changes in [{"checked": False}, {"reviewer": ""}, {"reason": ""}, {"snapshot_id": "0"*64},
                        {"source_id": "unknown"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.publish(**changes)
        with patch.object(collections.CollectionStore, "publish", side_effect=ValueError("Staging failure")):
            with self.assertRaisesRegex(ValueError, "Staging failure"):
                self.publish()
        self.assertEqual(project_library.store(self.root).current().release_id, before)
        self.assertFalse(list((self.root / "runs").glob("correction-publication-*")))

    def test_admin_can_import_utf8_draft_then_publish_through_web(self):
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/library/edit?doi=10.0000/text&mode=edit", base_url=base).get_data(as_text=True)
        form = {name: re.search(fr'name="{name}" value="([^"]*)"', html).group(1)
                for name in ("token", "doi", "unit_id", "source_id", "snapshot_id", "scope")}
        form.update({k: v or "" for k, v in self.source["bibliography"].items()})
        form.update(source_url=self.source["url"], reviewer="Web admin", reason="Imported cleanup", tables="[]")
        edited = project_edit.document_draft(self.units).replace("Drafttoken", "Importedtoken")
        bad = client.post("/library/edit", data=form | {"clear_text_file": (io.BytesIO(b"\xff"), "draft.txt")},
                          base_url=base)
        self.assertEqual(bad.status_code, 400)
        response = client.post("/library/edit", data=form | {"clear_text_file": (io.BytesIO(edited.encode()), "draft.md")},
                               base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(project_library.store(self.root).call("search_evidence", {"query": "Importedtoken"})["result"]["items"], [])
        html = client.get(response.location, base_url=base).get_data(as_text=True)
        form = {name: re.search(fr'name="{name}" value="([^"]*)"', html).group(1)
                for name in ("token", "doi", "unit_id", "source_id", "snapshot_id")}
        form.update(action="publish", reviewer="Web admin", reason="Checked against PDF")
        self.assertEqual(client.post("/library/edit", data=form, base_url=base).status_code, 400)
        self.assertEqual(client.post("/library/edit", data=form | {"checked": "yes"}, base_url=base).status_code, 303)
        self.assertEqual(len(project_library.store(self.root).call("search_evidence", {"query": "Importedtoken"})["result"]["items"]), 2)

    def test_migration_withdraws_active_text_but_preserves_original_snapshot_and_pdf(self):
        self.publish()
        previous = project_library.store(self.root).current()
        result = project_text.migrate(self.root, withdraw=True)
        self.assertEqual(result["previous_snapshot"], previous.release_id)
        self.assertEqual(project_library.store(self.root).current().snapshots["core"]._bundle["units"], [])
        self.assertEqual(project_library.store(self.root).load(previous.release_id).snapshots["core"]._bundle,
                         previous.snapshots["core"]._bundle)
        self.assertEqual(project_text.editorial_store(self.root).current().snapshots["core"]._bundle, self.bundle)
        self.assertTrue(self.pdf.is_file())

    def test_purge_removes_unpublished_editorial_content(self):
        project_purge.purge(self.root, "10.0000/text", self.root.parent / "no-shared-pdfs")
        self.assertEqual(project_text.editorial_store(self.root).current().snapshots["core"]._bundle["sources"], [])
        self.assertEqual(project_library.store(self.root).current().snapshots["core"]._bundle["sources"], [])

    def test_attachment_without_extraction_accepts_manual_clear_text(self):
        project_library.register(self.root, [{"doi": "10.0000/manual", "title": "Manual article"}])
        path = self.root.parent / "manual.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.new_page()
            pdf.save(path)
        with patch.object(project_library.literature_ingest, "extract_pdf", side_effect=AssertionError("Extraction called")):
            result = project_library.attach(self.root, "10.0000/manual", path,
                                             identity_confirmed=True, extract=False)
        self.assertEqual(result["extraction"], "not_extracted")
        current = project_text.editorial_store(self.root).current()
        source = next(s for s in current.snapshots["core"]._bundle["sources"] if s["doi"] == "10.0000/manual")
        units = project_edit.document_units(current.snapshots["core"]._bundle, source["id"])
        self.assertEqual([u["text"] for u in units], ["", ""])
        with self.assertRaisesRegex(ValueError, "Provide clear text"):
            project_text.publish(self.root, snapshot_id=current.release_id, source_id=source["id"],
                                 reviewer="Admin", reason="No text", checked=True)
        units[0]["text"] = "Manually supplied transcription."
        project_edit.correct(self.root, snapshot_id=current.release_id, source_id=source["id"],
            unit_id=units[0]["id"], text=project_edit.document_draft(units), whole_document=True,
            bibliography={k: v or "" for k, v in source["bibliography"].items()},
            source_url=source["url"], tables=[], reviewer="Admin", reason="Manual draft", checked=False)
        self.assertEqual(project_library.store(self.root).call("search_evidence", {"query": "Manually"})["result"]["items"], [])
        project_text.publish(self.root, snapshot_id=project_text.editorial_store(self.root).current().release_id,
                             source_id=source["id"], reviewer="Admin", reason="Checked including blank page", checked=True)
        self.assertEqual(len(project_library.store(self.root).call("search_evidence", {"query": "Manually"})["result"]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
