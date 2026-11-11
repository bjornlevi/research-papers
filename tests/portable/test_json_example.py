import importlib.util
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx

from fixtures import snapshots
from research_library import connection_check, releases
from test_browser_library import Links
from test_collections import registry_fixture
from test_connection import running_web
from test_http import running_server
from test_releases import fixture_bundle


EXAMPLE = Path(__file__).resolve().parents[2] / "examples/library_json.py"


class JSONExampleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle, _ = fixture_bundle(self.root)
        self.release = self.root / "release"
        releases.export(self.bundle, self.release, profile="synthetic")

    def run_example(self, web, *args):
        outside = self.root / "outside"
        outside.mkdir(exist_ok=True)
        script = outside / "library_json.py"
        shutil.copy2(EXAMPLE, script)
        return subprocess.run([sys.executable, "-I", "-S", str(script), "--base-url", web, *args],
                              cwd=outside, capture_output=True, text=True, timeout=60)

    def test_standalone_no_dependencies_single_release_exact_slices_and_reader(self):
        before = snapshots(self.release)
        with running_web(release=self.release, mcp_url="http://127.0.0.1:8000/mcp") as web:
            result = self.run_example(web, "--query", "screening")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertIsNone(data["collection"])
            self.assertIsNone(data["snapshot_id"])
            self.assertEqual(len(data["text_slices"]), 2)
            self.assertEqual(data["text_slices"][1]["offset"], 2048)
            for chunk in data["text_slices"]:
                record = chunk["record"]
                self.assertEqual(record["citation"], self.bundle["units"][0]["citation"])
                self.assertEqual(record["text"],
                                 self.bundle["units"][0]["text"][chunk["offset"]:chunk["offset"] + 2048])
            reader = Links(httpx.get(data["reader_url"]).text)
            self.assertEqual(reader.fields["excerpt"].split("\n\n")[0], data["text_slices"][0]["record"]["text"])
            failed = self.run_example(web, "--query", "zzznomatch987654321")
            self.assertEqual(failed.returncode, 1)
            self.assertIn("No matching retained passage", failed.stderr)
            failed = self.run_example(web, "--collection", "istopmm")
            self.assertEqual(failed.returncode, 1)
            self.assertIn("omit --collection", failed.stderr)
        self.assertEqual(snapshots(self.release), before)

    def test_registry_json_mcp_and_reader_agree_and_pagination_stays_pinned(self):
        store, first, _, _ = registry_fixture(self.root)
        allowed = {"istopmm", "test-literature"}
        with running_server(self.root, first, collection_registry=store.root,
                            allowed_collections=allowed) as (url, token, *_):
            with running_web(registry=store.root, allowed_collections=allowed, mcp_url=url) as web:
                result = self.run_example(web, "--query", "screening", "--collection", "istopmm")
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                mcp = anyio.run(connection_check.check, url, token, web, "screening", "istopmm")
                for key in ("snapshot_id", "release_id", "unit_id", "citation", "reader_url"):
                    self.assertEqual(data[key], mcp[key], key)
                params = {"collection": "istopmm", "snapshot_id": data["snapshot_id"],
                          "query": "screening", "limit": 1, "offset": data["next_search_offset"]}
                self.assertIsNotNone(params["offset"])
                old = httpx.get(web + "/api/search", params=params).json()
                store.replace("istopmm", self.release, version="new", updated_at="2026-10-28")
                self.assertEqual(httpx.get(web + "/api/search", params=params).json(), old)
                self.assertEqual(httpx.get(data["reader_url"]).status_code, 200)
                for cid in ("secret", "catalog"):
                    failed = self.run_example(web, "--query", "screening", "--collection", cid)
                    self.assertEqual(failed.returncode, 1)
                    self.assertIn("authorized evidence collection", failed.stderr)
                failed = self.run_example(web, "--query=---")
                self.assertEqual(failed.returncode, 1)
                self.assertIn("HTTP 400", failed.stderr)
                for suffix, code in (
                        ("/api/text?collection=istopmm&unit_id=missing", 404),
                        ("/api/text?collection=secret&unit_id=missing", 403),
                        ("/api/text?collection=istopmm&unit_id=missing&snapshot_id=" + "0" * 64, 410)):
                    response = httpx.get(web + suffix)
                    self.assertEqual(response.status_code, code)
                    self.assertFalse(response.json()["ok"])

    def test_example_rejects_mixed_evidence_versions(self):
        spec = importlib.util.spec_from_file_location("library_json", EXAMPLE)
        example = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(example)
        snapshot = releases.validate(self.release, allowed_profiles={"synthetic"})
        listed = snapshot.call("list_collections", {})
        hits = snapshot.call("search_evidence", {"query": "screening", "limit": 1})
        wrong = {"ok": True, "release_id": "0" * 64,
                 "result": snapshot.source_context(hits["result"]["items"][0]["id"])}
        with patch.object(example, "urlopen", side_effect=[
                BytesIO(json.dumps(value).encode()) for value in (listed, hits, wrong)]):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                example.retrieve("http://127.0.0.1:8081", "screening")


if __name__ == "__main__":
    unittest.main()
