from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_library import retrieval
from research_library.build import evidence, knowledge
from fixtures import corpus, DOI, snapshots


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.context = corpus()
        self.root = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def cli(self, *args, legacy=False):
        if legacy:
            command = [sys.executable, "-m", "research_library.build.evidence", *args]
        else:
            command = [sys.executable, "-m", "research_library.cli", "--data-root", str(self.root), *args]
        return subprocess.run(command, cwd=self.root.parent, capture_output=True, text=True,
                              env={**os.environ, "RESEARCH_LIBRARY_DATA_ROOT": str(self.root)})

    def test_cli_shared_and_legacy_parity(self):
        with closing(retrieval.open_index(self.root)) as db:
            cases = [
                (("search", "screening", "--paper", DOI), retrieval.search(db, "screening", paper=DOI)),
                (("claims", "screening", "--measure", "subdistribution_hazard_ratio"),
                 retrieval.claims(db, text="screening", measure="subdistribution_hazard_ratio")),
                (("results", "--measure", "prevalence_ratio"),
                 retrieval.results(db, measure="prevalence_ratio")),
            ]
            self.assertEqual(knowledge.query(db, paper=DOI), retrieval.claims(db, paper=DOI))
            self.assertEqual(retrieval.get_claim(db, "fictional-claim-1"),
                             retrieval.claims(db, kind="observed_result")[0])
        for arguments, expected in cases:
            for legacy in (True, False):
                with self.subTest(arguments=arguments, legacy=legacy):
                    response = self.cli(*arguments, legacy=legacy)
                    self.assertEqual(response.returncode, 0, response.stderr)
                    self.assertEqual(json.loads(response.stdout), expected)

    def test_pagination_is_stable_and_preserves_context(self):
        with closing(retrieval.open_index(self.root)) as db:
            hits = retrieval.search(db, "screening")
            self.assertEqual(len(hits), 3)
            self.assertEqual([retrieval.search(db, "screening", limit=1, offset=i)[0]
                              for i in range(3)], hits)
            self.assertTrue(any("body_element" in hit["location"] for hit in hits))
            self.assertTrue(any("worksheet" in hit["location"] for hit in hits))
            claims = retrieval.claims(db)
            self.assertEqual(retrieval.claims(db, limit=1, offset=1), claims[1:])
            self.assertEqual(claims[0]["numbers"][0]["value"], 0)
            self.assertIsNone(claims[0]["numbers"][0]["reference_interval"])
            self.assertEqual(claims[1]["claim_kind"], "secondary_evidence")
            self.assertTrue(claims[1]["source_issues"])
            self.assertEqual(retrieval.search(db, "zzyyunknown"), [])
            self.assertEqual(retrieval.claims(db, text="zzyyunknown"), [])

    def test_modern_cli_plain_default_and_explicit_fts(self):
        for query, mode, count in (
            ('"screening', (), 3),
            ("screening OR nonexistent", (), 0),
            ("screen* OR nonexistent", ("--mode", "fts5"), 3),
        ):
            with self.subTest(query=query, mode=mode):
                response = self.cli("search", query, *mode)
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertEqual(len(json.loads(response.stdout)), count)
        with closing(retrieval.open_index(self.root)) as db:
            with self.assertRaisesRegex(retrieval.RetrievalError, "mode"):
                retrieval.search(db, "screening", mode="auto")

    def test_explicit_errors_and_parameterized_queries(self):
        with closing(retrieval.open_index(self.root)) as db:
            for limit in (0, 101, True, 1.5):
                with self.assertRaises(retrieval.RetrievalError):
                    retrieval.search(db, "screening", limit=limit)
            for offset in (-1, 1_000_001, False):
                with self.assertRaises(retrieval.RetrievalError):
                    retrieval.claims(db, offset=offset)
            for text in ("", " ", "x" * 2049):
                with self.assertRaises(retrieval.RetrievalError):
                    retrieval.search(db, text)
            for text in ('"unclosed', "screening; DROP TABLE claims;"):
                with self.assertRaises(sqlite3.OperationalError):
                    retrieval.search(db, text)
            with self.assertRaises(retrieval.NotFoundError):
                retrieval.get_claim(db, "' OR 1=1 --")
            with self.assertRaises(retrieval.RetrievalError):
                retrieval.claims(db, kind="invented")
            self.assertEqual(retrieval.claims(db, paper="' OR 1=1 --"), [])
            self.assertEqual(retrieval.results(db, measure="' OR 1=1 --"), [])
            self.assertEqual(len(retrieval.claims(db)), 2)
        for args in (("claim", "missing"), ("search", '"unclosed', "--mode", "fts5"),
                     ("claims", "--limit", "101")):
            response = self.cli(*args)
            self.assertNotEqual(response.returncode, 0)
            self.assertTrue(response.stderr)
            self.assertFalse(response.stdout)

    def test_missing_invalid_and_configured_roots(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(retrieval.RetrievalError, "Specify"):
                retrieval.open_index()
        with patch.dict(os.environ, {"RESEARCH_LIBRARY_DATA_ROOT": str(self.root)}):
            with closing(retrieval.open_index()) as db:
                self.assertEqual(len(retrieval.claims(db)), 2)
        with self.assertRaisesRegex(retrieval.RetrievalError, "directory"):
            retrieval.open_index(self.root / "missing")
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(retrieval.RetrievalError, "Missing evidence index"):
                retrieval.open_index(empty)
            folder = Path(empty) / "evidence"
            folder.mkdir()
            with sqlite3.connect(folder / "index.sqlite"):
                pass
            with self.assertRaisesRegex(retrieval.RetrievalError, "incomplete"):
                retrieval.open_index(empty)
            (folder / "index.sqlite").write_bytes(b"not sqlite")
            with self.assertRaises(sqlite3.DatabaseError):
                retrieval.open_index(empty)

    def test_readonly_concurrent_queries_do_not_change_files(self):
        before = snapshots(self.root)
        def read(_):
            with closing(retrieval.open_index(self.root)) as db:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("DELETE FROM claims")
                db.execute("PRAGMA query_only=OFF")
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("DELETE FROM claims")
                return retrieval.search(db, "screening"), retrieval.claims(db)
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(read, range(12)))
        self.assertTrue(all(response == responses[0] for response in responses))
        self.assertEqual(snapshots(self.root), before)

    def test_installed_build_tools_respect_root(self):
        with patch.dict(os.environ, {"RESEARCH_LIBRARY_DATA_ROOT": str(self.root)}):
            response = self.cli("claims", legacy=True)
            self.assertEqual(response.returncode, 0, response.stderr)
        with patch.object(evidence, "ROOT", self.root):
            with closing(evidence.open_index()) as db:
                self.assertEqual(len(retrieval.claims(db)), 2)
