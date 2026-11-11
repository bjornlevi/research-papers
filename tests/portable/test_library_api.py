from pathlib import Path
import tempfile
import unittest

import httpx

from fixtures import snapshots
from research_library import releases, web_server
from test_collections import registry_fixture
from test_releases import fixture_bundle


class LibraryAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle, _ = fixture_bundle(self.root)
        self.release = self.root / "release"
        releases.export(self.bundle, self.release, profile="synthetic")
        self.client = httpx.Client(transport=httpx.WSGITransport(app=web_server.create_app(
            release=self.release, profiles={"synthetic"})), base_url="http://127.0.0.1:8080")
        self.addCleanup(self.client.close)

    def test_paper_documents_units_and_complete_text_are_readonly(self):
        before = snapshots(self.release)
        publications = self.client.get("/api/publications").json()["result"]["items"]
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]["source_count"], 3)
        sources = self.client.get("/api/sources", params={"paper": "10.0000/fictional"}).json()["result"]["items"]
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[0]["source_sha256"], self.bundle["sources"][0]["sha256"])
        self.assertFalse(sources[0]["original_served"])
        units = self.client.get("/api/units", params={"source_id": "fictional:main"}).json()["result"]["items"]
        self.assertEqual(units[0]["citation"]["locator"], {"kind": "pdf", "pdf_page": 1})
        self.assertNotIn("text", units[0])
        retained = next(unit for unit in self.bundle["units"] if unit["id"] == units[0]["id"])
        text, offset = "", 0
        while True:
            response = self.client.get("/api/text", params={"unit_id": retained["id"], "offset": offset, "limit": 200})
            self.assertEqual(response.status_code, 200)
            data = response.json()["result"]
            text += data["record"]["text"]
            self.assertEqual(data["record"]["citation"], retained["citation"])
            if data["next_offset"] is None:
                break
            offset = data["next_offset"]
        self.assertEqual(text, retained["text"])
        snapshot = releases.validate(self.release, allowed_profiles={"synthetic"})
        context = snapshot.call("get_source_context", {"unit_id": retained["id"], "max_characters": 200})["result"]
        self.assertEqual(self.client.get("/api/text", params={
            "unit_id": retained["id"], "limit": 200}).json()["result"]["record"], context)
        search = self.client.get("/api/search?query=screening&limit=2").json()
        self.assertEqual(search, snapshot.call("search_evidence", {"query": "screening", "limit": 2}))
        office = self.client.get("/api/units?source_id=fictional:word").json()["result"]["items"]
        self.assertEqual(office[0]["citation"]["locator"]["kind"], "word_table")
        self.assertEqual(self.client.get("/api/search?query=nonexistentterm").json()["result"]["items"], [])
        self.assertEqual(snapshots(self.release), before)

    def test_invalid_missing_and_denied_requests_are_explicit(self):
        for path in ("/api/text?unit_id=missing", "/api/units?source_id=missing", "/api/unknown"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertFalse(response.json()["ok"])
        for path in ("/api/text?unit_id=fictional-unit-1&offset=-1",
                     "/api/text?unit_id=fictional-unit-1&offset=999999",
                     "/api/text?unit_id=fictional-unit-1&limit=2049",
                     "/api/sources?limit=0", "/api/search?query=",
                     "/api/sources?path=secret", "/api/sources?limit=1&limit=2"):
            self.assertEqual(self.client.get(path).status_code, 400, path)
        self.assertEqual(self.client.post("/api/sources").status_code, 405)
        self.assertEqual(self.client.get("/api/sources", headers={"Host": "evil.test"}).status_code, 421)
        self.assertEqual(self.client.get("/api/sources", headers={"Origin": "https://evil.test"}).status_code, 403)
        curated = self.root / "curated"
        grants = {source["id"]: {"scopes": ["curated"], "basis": "Authored fixture permission."}
                  for source in self.bundle["sources"]}
        releases.export(self.bundle, curated, profile="curated", authorization=grants)
        with httpx.Client(transport=httpx.WSGITransport(app=web_server.create_app(release=curated, profiles={"curated"})),
                        base_url="http://127.0.0.1:8080") as client:
            self.assertEqual(client.get("/api/text?unit_id=fictional-unit-1").status_code, 403)

    def test_ocr_labels_survive_and_corrupt_release_is_rejected(self):
        self.bundle["units"][0]["text"] = "[OCR transcription; not human verified]\nFictional page"
        self.bundle["units"][0]["caveats"].append("LOW CONFIDENCE; visual review advised.")
        target = self.root / "ocr"
        releases.export(self.bundle, target, profile="synthetic")
        with httpx.Client(transport=httpx.WSGITransport(app=web_server.create_app(release=target, profiles={"synthetic"})),
                        base_url="http://127.0.0.1:8080") as client:
            record = client.get("/api/text?unit_id=fictional-unit-1").json()["result"]["record"]
            self.assertEqual(record["text"], self.bundle["units"][0]["text"])
            self.assertIn("LOW CONFIDENCE; visual review advised.", record["caveats"])
        manifest = releases.read_json((target / "manifest.json").read_bytes())
        payload = next(iter(manifest["files"]))
        (target / payload).write_bytes(b"corrupt")
        with self.assertRaises((releases.ReleaseError, ValueError)):
            web_server.create_app(release=target, profiles={"synthetic"})

    def test_collection_pagination_and_retained_snapshot(self):
        root = self.root / "registry"
        root.mkdir()
        store, _, _, _ = registry_fixture(root)
        with httpx.Client(transport=httpx.WSGITransport(app=web_server.create_app(
                registry=store.root, profiles={"synthetic"},
                allowed_collections={"istopmm", "test-literature"})),
                        base_url="http://127.0.0.1:8080") as client:
            self.assertEqual(client.get("/api/collections").status_code, 200)
            self.assertEqual(client.get("/api/publications").status_code, 400)
            self.assertEqual(client.get("/api/publications?collection=secret").status_code, 403)
            first = client.get("/api/sources?collection=istopmm&limit=1").json()
            sid = first["snapshot_id"]
            self.assertEqual(client.get("/api/sources?collection=istopmm&offset=1").status_code, 400)
            params = {"collection": "istopmm", "snapshot_id": sid, "offset": 1, "limit": 1}
            page = client.get("/api/sources", params=params)
            self.assertEqual(page.status_code, 200)
            store.replace("istopmm", self.release, version="new", updated_at="2026-10-27")
            self.assertEqual(client.get("/api/sources", params=params).json(), page.json())
            self.assertEqual(client.get("/api/sources", params={
                "collection": "istopmm", "snapshot_id": "0" * 64}).status_code, 410)
            (store.root / "active.json").write_text("{}")
            self.assertEqual(client.get("/api/publications?collection=istopmm").status_code, 503)
