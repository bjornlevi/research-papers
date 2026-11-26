import json
from pathlib import Path
import stat
import tempfile
import unittest

import pymupdf

from research_library import collections, releases
from research_library.build import project_chunks, project_edit, project_library, project_state, project_text
from project_text_helpers import publish_all


class ProjectStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        project_library.initialize(self.root, "State test", "Synthetic test data")
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"}])
        pdf = self.root.parent / "one.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text(
                (50, 50), "Fictional research text with sufficient words for a native extraction test.")
            document.save(pdf)
        project_library.attach(self.root, "10.0000/one", pdf, identity_confirmed=True)
        publish_all(self.root)

    def test_export_preserves_draft_published_and_rag_separately(self):
        published = project_library.store(self.root).current()
        source = published.snapshots["core"]._bundle["sources"][0]
        source_id = source["id"]
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": "1"}, activate=True)
        published = project_library.store(self.root).current()
        project_chunks.update_check(self.root, source_id=source_id, snapshot_id=published.release_id,
                                    form={"query": "fictional", "expected_page": "1",
                                          "expected_excerpt": "Fictional research text"})
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": ""}, activate=False)
        editorial = project_text.editorial_store(self.root).current()
        unit = project_edit.document_units(editorial.snapshots["core"]._bundle, source_id)[0]
        project_edit.correct(self.root, snapshot_id=editorial.release_id, source_id=source_id,
                             unit_id=unit["id"], text=unit["text"] + "\nUnpublished revision.",
                             bibliography={key: value or "" for key, value in source["bibliography"].items()},
                             source_url=source["url"] or "", tables=[{
                                 "id": "", "title": "Synthetic table", "headers": ["Value"],
                                 "units": [None], "rows": [["3"]], "footnotes": ["Synthetic note."],
                             }], reviewer="Tester",
                             reason="Test draft", checked=False)
        path = self.root.parent / "state.json"
        result = project_state.export(self.root, path)
        state = json.loads(path.read_text())
        self.assertEqual(result["published"], 1)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("Unpublished revision", state["editorial"]["bundle"]["units"][0]["text"])
        self.assertNotIn("Unpublished revision", state["published"]["bundle"]["units"][0]["text"])
        self.assertEqual(state["editorial"]["bundle"]["tables"][0]["footnotes"], ["Synthetic note."])
        self.assertEqual(state["published"]["bundle"]["tables"], [])
        self.assertEqual(state["published"]["chunk_plans"], published.snapshots["core"].chunk_plans)
        self.assertEqual(len(state["rag"][source_id]["checks"]), 1)
        self.assertIsNotNone(state["rag"][source_id]["draft"])
        self.assertEqual(state["rag"][source_id]["draft"]["plans"], {})
        self.assertTrue(state["published"]["chunk_plans"])
        self.assertIn("publication", state["audit"][source_id])
        self.assertIn("draft", state["audit"][source_id])
        with self.assertRaises(FileExistsError):
            project_state.export(self.root, path)
        restored = self.root.parent / "restored"
        result = project_state.restore(path, restored)
        self.assertEqual((result["editorial"], result["published"]), (1, 1))
        self.assertFalse(result["pdfs_restored"])
        current = project_state.current(restored)
        self.assertEqual(current, state)
        self.assertFalse((restored / "papers").exists())
        self.assertEqual(project_text.publication(
            restored, source_id)["event"]["after"]["content_identity"],
            state["audit"][source_id]["publication"]["after"]["content_identity"])
        self.assertTrue(project_library.store(restored).call(
            "search_evidence", {"query": "Fictional research text"})["result"]["items"])
        self.assertFalse(project_library.store(restored).call(
            "search_evidence", {"query": "Unpublished revision"})["result"]["items"])
        per_paper = self.root.parent / "per-paper"
        project_state.export_papers(self.root, per_paper)
        restored_papers = self.root.parent / "restored-papers"
        project_state.restore(per_paper, restored_papers)
        self.assertEqual(project_state.current(restored_papers), state)
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_state.restore(path, restored)

    def test_restore_rejects_bad_state_without_creating_project(self):
        state = project_state.current(self.root)
        source_id = state["editorial"]["bundle"]["sources"][0]["id"]
        path = self.root.parent / "bad-state.json"
        destination = self.root.parent / "restored"
        state["rag"][source_id] = {"draft": None, "checks": [
            {"id": "wrong", "query": "test", "page": 1, "excerpt": "Fictional text"}]}
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "search check identity"):
            project_state.restore(path, destination)
        self.assertFalse(destination.exists())
        state["rag"][source_id] = {"draft": {
            "content_identity": "older", "plans": {"page": {"labels": {"1": "term"}}}}, "checks": []}
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "term anchors"):
            project_state.restore(path, destination)
        self.assertFalse(destination.exists())
        state["rag"][source_id]["draft"]["term_anchors"] = {}
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "term anchor is missing"):
            project_state.restore(path, destination)
        self.assertFalse(destination.exists())

    def test_restore_keeps_unpublished_paper_unpublished(self):
        published = project_library.store(self.root).current()
        source_id = published.snapshots["core"]._bundle["sources"][0]["id"]
        project_text.unpublish(self.root, snapshot_id=published.release_id, source_id=source_id,
                               reviewer="Tester", reason="Withdraw test", checked=True)
        path = self.root.parent / "state.json"
        project_state.export(self.root, path)
        restored = self.root.parent / "restored"
        project_state.restore(path, restored)
        self.assertIsNone(project_text.publication(restored, source_id))
        self.assertEqual(project_state.current(restored), json.loads(path.read_text()))

    def test_per_paper_export_round_trip_and_missing_files(self):
        project_library.register(self.root, [{"doi": "10.0000/two", "title": "Second"}])
        pdf = self.root.parent / "two.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text(
                (50, 50), "Second fictional research paper with sufficient words for extraction.")
            document.save(pdf)
        project_library.attach(self.root, "10.0000/two", pdf, identity_confirmed=True)
        published = project_library.store(self.root).current()
        source_id = published.snapshots["core"]._bundle["sources"][0]["id"]
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": "1"}, activate=True)
        published = project_library.store(self.root).current()
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": ""}, activate=False)
        project_chunks.update_check(self.root, source_id=source_id, snapshot_id=published.release_id,
                                    form={"query": "fictional", "expected_page": "1",
                                          "expected_excerpt": "Fictional research text"})
        exported = self.root.parent / "per-paper"
        result = project_state.export_papers(self.root, exported)
        self.assertEqual(result["papers"], 2)
        self.assertEqual(stat.S_IMODE(exported.stat().st_mode), 0o700)
        papers = list((exported / "papers").glob("*.json"))
        self.assertEqual(len(papers), 2)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in papers))
        self.assertTrue((exported / "manifest.json").read_text().startswith("{\n"))
        restored = self.root.parent / "restored-papers"
        project_state.restore(exported, restored)
        self.assertEqual(project_state.current(self.root), project_state.current(restored))
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_state.export_papers(self.root, exported)
        papers[0].unlink()
        missing_destination = self.root.parent / "missing-paper"
        with self.assertRaisesRegex(ValueError, "do not match"):
            project_state.restore(exported, missing_destination)
        self.assertFalse(missing_destination.exists())

    def test_per_paper_export_only_changes_edited_paper(self):
        project_library.register(self.root, [{"doi": "10.0000/two", "title": "Second"}])
        pdf = self.root.parent / "two.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Another sufficiently long fictional article.")
            document.save(pdf)
        project_library.attach(self.root, "10.0000/two", pdf, identity_confirmed=True)
        before = self.root.parent / "before"
        project_state.export_papers(self.root, before)
        editorial = project_text.editorial_store(self.root).current()
        source = editorial.snapshots["core"]._bundle["sources"][0]
        source_id = source["id"]
        unit = project_edit.document_units(editorial.snapshots["core"]._bundle, source_id)[0]
        project_edit.correct(self.root, snapshot_id=editorial.release_id, source_id=source_id,
                             unit_id=unit["id"], text=unit["text"] + "\nRevision.",
                             bibliography={key: value or "" for key, value in source["bibliography"].items()},
                             source_url=source["url"] or "", tables=[], reviewer="Tester",
                             reason="Update", checked=False)
        after = self.root.parent / "after"
        project_state.export_papers(self.root, after)
        changed = [path.name for path in (before / "papers").iterdir()
                   if path.read_bytes() != (after / "papers" / path.name).read_bytes()]
        self.assertEqual(changed, [project_state._paper_filename(source_id)])

    def test_per_paper_restore_rejects_modified_identity_and_symlinks(self):
        exported = self.root.parent / "per-paper"
        project_state.export_papers(self.root, exported)
        path = next((exported / "papers").glob("*.json"))
        original = path.read_text()
        paper = json.loads(original)
        paper["source_id"] = "another source"
        path.write_text(json.dumps(paper))
        destination = self.root.parent / "bad-paper"
        with self.assertRaisesRegex(ValueError, "identity"):
            project_state.restore(exported, destination)
        self.assertFalse(destination.exists())
        path.unlink()
        other = self.root.parent / "outside.json"
        other.write_text(original)
        path.symlink_to(other)
        with self.assertRaises(releases.ReleaseError):
            project_state.restore(exported, destination)
        self.assertFalse(destination.exists())

    def test_export_rejects_unassociated_rag_files(self):
        collections.atomic_json(self.root / "chunk-drafts", "unknown.json", {})
        path = self.root.parent / "state.json"
        with self.assertRaisesRegex(ValueError, "Unassociated"):
            project_state.export(self.root, path)
        self.assertFalse(path.exists())

    def test_export_snapshot_can_rebuild_published_release(self):
        state = project_state.current(self.root)
        published = state["published"]
        destination = self.root.parent / "rebuilt"
        releases.export(published["bundle"], destination, profile="private_context",
                        authorization=published["authorization"], chunk_plans=published["chunk_plans"])
        snapshot = releases.validate(destination, allowed_profiles=["private_context"])
        self.assertEqual(snapshot._bundle, published["bundle"])
        self.assertEqual(snapshot.chunk_plans, published["chunk_plans"])


if __name__ == "__main__":
    unittest.main()
