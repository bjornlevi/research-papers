from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from research_library import collections, passage_context
from research_library.build import project_chunks, project_library, project_retention, project_state, project_text
from project_text_helpers import publish_all


class ProjectRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        project_library.initialize(self.root, "Retention test", "Synthetic test data")
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"},
                                             {"doi": "10.0000/two", "title": "Second"}])
        for name in ("one", "two"):
            pdf = self.root.parent / (name + ".pdf")
            with pymupdf.open() as document:
                document.new_page().insert_text((50, 50), f"Fictional research text for {name}.")
                document.save(pdf)
            project_library.attach(self.root, "10.0000/" + name, pdf, identity_confirmed=True)
        publish_all(self.root)

    def test_preview_identifies_history_without_changing_active_evidence(self):
        editorial = project_text.editorial_store(self.root).current()
        published = project_library.store(self.root).current()
        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        report = project_retention.preview(self.root)
        self.assertEqual(report["editorial"]["active_snapshot"], editorial.release_id)
        self.assertEqual(report["published"]["active_snapshot"], published.release_id)
        for key in ("editorial", "published"):
            store = report[key]
            self.assertTrue(store["historical"])
            paths = {item["path"] for item in store["historical"]}
            self.assertNotIn("registries/" + store["active_snapshot"] + ".json", paths)
            for release in store["active_releases"]["releases"]:
                self.assertNotIn("releases/" + release, paths)
        self.assertEqual(before, {path.relative_to(self.root) for path in self.root.rglob("*")})
        self.assertEqual(project_library.store(self.root).current().release_id, published.release_id)
        self.assertEqual(project_text.editorial_store(self.root).current().release_id, editorial.release_id)

    def test_preview_refuses_unrecognized_store_content(self):
        (self.root / "editorial" / "releases" / "unexpected").mkdir()
        with self.assertRaises(ValueError):
            project_retention.preview(self.root)

    def test_preview_refuses_symlinks(self):
        (self.root / "link").symlink_to(self.root.parent)
        with self.assertRaisesRegex(ValueError, "symlink"):
            project_retention.preview(self.root)

    def test_prune_preserves_current_state_and_verified_backup(self):
        published = project_library.store(self.root).current()
        snapshot = published.snapshots["core"]
        source = snapshot._bundle["sources"][0]
        source_id = source["id"]
        blocks = passage_context.document_blocks(snapshot.document_units(source_id),
                                                  title=source["bibliography"]["title"])
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": f"1-{len(blocks)}"}, activate=True)
        published = project_library.store(self.root).current()
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": ""}, activate=False)
        before = project_state.current(self.root)
        inventory = project_retention.preview(self.root)
        expected = sum(len(item["historical"]) for item in inventory.values() if item)
        backup = self.root.parent / "backup"
        result = project_retention.prune(self.root, backup, readers_stopped=True)
        self.assertEqual(result["removed_entries"], expected)
        self.assertGreater(result["removed_bytes"], 0)
        self.assertFalse(result["runs_removed"])
        self.assertEqual(project_state.current(self.root), before)
        for item in project_retention.preview(self.root).values():
            if item:
                self.assertEqual(item["historical"], [])
        self.assertIsNone(project_library.store(self.root)._state()["previous"])
        self.assertIsNone(project_text.editorial_store(self.root)._state()["previous"])
        restored = self.root.parent / "restored"
        project_state.restore(backup, restored)
        self.assertEqual(project_state.current(restored), before)

    def test_prune_refuses_without_confirmation_and_preserves_history_on_verification_error(self):
        backup = self.root.parent / "backup"
        before = project_retention.preview(self.root)
        with self.assertRaisesRegex(ValueError, "Stop the website"):
            project_retention.prune(self.root, backup)
        self.assertFalse(backup.exists())
        with patch.object(project_state, "restore", side_effect=ValueError("Invalid backup")):
            with self.assertRaisesRegex(ValueError, "Invalid backup"):
                project_retention.prune(self.root, backup, readers_stopped=True)
        self.assertTrue(backup.is_dir())
        self.assertEqual(project_retention.preview(self.root), before)
        with self.assertRaisesRegex(ValueError, "outside the project"):
            project_retention.prune(self.root, self.root / "backup", readers_stopped=True)

    def test_prune_refuses_stale_legacy_rag_without_anchors(self):
        published = project_library.store(self.root).current()
        source_id = published.snapshots["core"]._bundle["sources"][0]["id"]
        collections.atomic_json(self.root / "chunk-drafts", project_chunks.draft_name(source_id), {
            "content_identity": "older", "plans": {"unit": {"labels": {"1": "term"}}}})
        before = project_retention.preview(self.root)
        with self.assertRaisesRegex(ValueError, "term anchors"):
            project_retention.prune(self.root, self.root.parent / "backup", readers_stopped=True)
        self.assertEqual(project_retention.preview(self.root), before)


if __name__ == "__main__":
    unittest.main()
