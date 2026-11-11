from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from research_library import collections
from research_library.build import project_index, project_library


class ProjectIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        project_library.initialize(self.root, "Index test", "Authorized synthetic evidence")
        project_library.register(self.root, [{"doi": "10.0000/core", "title": "Core paper"}])
        self.folder = self.root / "discovery" / ("a" * 32)
        collections.atomic_json(self.folder, "job.json", {
            "status": "complete", "label": "References", "started_at": "2026-11-10T00:00:00Z"})

    def graph(self, count):
        graph = {
            "nodes": {str(index): {"record": {"title": f"Paper {index:05}",
                                            "identifiers": {"doi": f"10.0000/paper-{index}"}}}
                      for index in range(count)},
            "edges": [{"seed_doi": "10.0000/core", "direction": "background",
                       "target_provider_record": str(index)} for index in range(count)],
        }
        collections.atomic_json(self.folder, "neighborhood.json", graph)

    def test_large_library_filters_and_pages_without_reading_graph_or_validating_evidence(self):
        self.graph(50000)
        first = project_index.library_page(self.root)
        self.assertEqual(first["total"], 50001)
        self.assertEqual(len(first["rows"]), 50)
        self.assertTrue(first["rows"][0]["core"])
        original_read = Path.read_bytes

        def lightweight_read(path):
            if path.name == "neighborhood.json":
                raise AssertionError("Warm Library must not read provider graphs")
            return original_read(path)

        with patch.object(Path, "read_bytes", lightweight_read), \
                patch.object(project_library, "status", side_effect=AssertionError("Evidence validation")):
            started = time.perf_counter()
            second = project_index.library_page(self.root, offset=50)
            filtered = project_index.library_page(self.root, query="PAPER 49999")
            literal = project_index.library_page(self.root, query="%")
            self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(len(second["rows"]), 50)
        self.assertFalse({row["id"] for row in first["rows"]} & {row["id"] for row in second["rows"]})
        self.assertEqual(filtered["matching"], 1)
        self.assertEqual(filtered["rows"][0]["doi"], "10.0000/paper-49999")
        self.assertEqual(filtered["rows"][0]["relations"], {("background", "10.0000/core")})
        self.assertEqual(literal["matching"], 0)

    def test_graph_core_and_deleted_search_invalidate_index(self):
        self.graph(2)
        self.assertEqual(project_index.library_page(self.root)["total"], 3)
        self.graph(3)
        self.assertEqual(project_index.library_page(self.root)["total"], 4)
        project_library.register(self.root, [{"doi": "10.0000/paper-1", "title": "New core title"}])
        changed = project_index.library_page(self.root)
        self.assertEqual(changed["total"], 4)
        self.assertEqual(changed["core_count"], 2)
        self.assertEqual(project_index.library_page(self.root, "New core title")["matching"], 1)
        (self.folder / "neighborhood.json").unlink()
        self.assertEqual(project_index.library_page(self.root)["total"], 2)


if __name__ == "__main__":
    unittest.main()
