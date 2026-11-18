from pathlib import Path
import sqlite3
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
            started = time.perf_counter()
            for _ in range(6):
                detail = project_index.paper_detail(self.root, "doi:10.0000/paper-49999")
                self.assertEqual(detail["records"][0]["title"], "Paper 49999")
                self.assertEqual(detail["citing"][0]["doi"], "10.0000/core")
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

    def test_metadata_column_filters_sorting_and_missing_values_across_pages(self):
        graph = {
            "nodes": {str(n): {"record": {
                "title": f"Paper {n:03}", "identifiers": {"doi": f"10.0000/{n:03}"},
                "authors": [f"First {n:03}", "Not a first author"],
                "publication_date": f"{2000 + n}-01-01", "journal": f"Journal {n % 3}"}}
                for n in range(65)},
            "edges": [{"seed_doi": "10.0000/core", "direction": "background" if n % 2 else "impact",
                       "target_provider_record": str(n)} for n in range(65)],
        }
        graph["nodes"]["core"] = {"record": {
            "title": "Provider title must not replace selected Core title",
            "identifiers": {"doi": "10.0000/core"}, "authors": ["Core author"],
            "publication_date": "2023", "journal": "Core journal"}}
        graph["nodes"]["unknown"] = {"record": {"title": "", "identifiers": {}}}
        graph["edges"].extend([
            {"seed_doi": "10.0000/core", "direction": "background", "target_provider_record": "core"},
            {"seed_doi": "10.0000/core", "direction": "impact", "target_provider_record": "core"},
            {"seed_doi": "10.0000/core", "direction": "background", "target_provider_record": "unknown"},
        ])
        collections.atomic_json(self.folder, "neighborhood.json", graph)
        base = project_index.library_page(self.root, select_all=True)
        core = next(row for row in base["rows"] if row["core"])
        self.assertEqual(core["title"], "Core paper")
        self.assertEqual((core["first_author"], core["year"], core["journal"]),
                         ("Core author", "2023", "Core journal"))
        self.assertEqual(core["library_groups"], "Core papers; References; Citing papers")
        for field, column in project_index.SORT_COLUMNS.items():
            for order in ("asc", "desc"):
                with self.subTest(field=field, order=order):
                    rows = sorted(base["rows"], key=lambda row: (row["sort_title"], row["doi"], row["id"]))
                    expected = sorted([row for row in rows if row[column]],
                                      key=lambda row: row[column].casefold(), reverse=order == "desc")
                    expected.extend(row for row in rows if not row[column])
                    result = project_index.library_page(self.root, sort=field, order=order, offset=50, limit=10)
                    self.assertEqual([row["id"] for row in result["rows"]],
                                     [row["id"] for row in expected[50:60]])
        filtered = project_index.library_page(
            self.root, title_filter="PAPER", doi_filter="/064", first_author_filter="FIRST 064",
            year_filter="2064", journal_filter="Journal 1", relationship="impact", status="awaiting")
        self.assertEqual(filtered["matching"], 1)
        self.assertEqual(filtered["rows"][0]["doi"], "10.0000/064")
        self.assertEqual(project_index.library_page(self.root, query="First 064 Journal 1")["matching"], 1)
        self.assertEqual(project_index.library_page(self.root, query="Not a first author")["matching"], 0)
        self.assertEqual(project_index.library_page(self.root, journal_filter="%")["matching"], 0)
        self.assertEqual(project_index.library_page(self.root, first_author_filter="absent")["matching"], 0)
        self.assertEqual(project_index.library_page(self.root, relationship="core")["matching"], 1)

    def test_column_metadata_merges_by_doi_and_rebuilds_an_old_index(self):
        self.graph(2)
        project_index.library_page(self.root)
        with sqlite3.connect(self.root / "library-index.sqlite") as connection:
            connection.execute("UPDATE state SET signature='old schema'")
        graph = {
            "nodes": {
                "sparse": {"record": {"title": "Sparse", "identifiers": {"doi": "10.0000/shared"}}},
                "rich": {"record": {"title": "Rich", "identifiers": {"doi": "https://doi.org/10.0000/shared"},
                                    "authors": ["Ása First", "Second Person"],
                                    "publication_date": "2024-06-01", "journal": "Test Journal"}},
            },
            "edges": [{"seed_doi": "10.0000/core", "direction": direction, "target_provider_record": key}
                      for direction, key in (("background", "sparse"), ("impact", "rich"))],
        }
        collections.atomic_json(self.folder, "neighborhood.json", graph)
        row = project_index.library_page(self.root, first_author_filter="ása", year_filter="2024")["rows"][0]
        self.assertEqual(row["first_author"], "Ása First")
        self.assertEqual(row["journal"], "Test Journal")
        self.assertEqual(row["library_groups"], "References; Citing papers")
        self.assertEqual(project_index.library_page(self.root, query="ÁSA")["matching"], 1)

    def test_invalid_sorts_and_years_are_rejected(self):
        for options in ({"sort": "id; DROP TABLE papers"}, {"order": "sideways"},
                        {"year_filter": "20"}, {"year_filter": "2024 OR 1=1"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                project_index.library_page(self.root, **options)


if __name__ == "__main__":
    unittest.main()
