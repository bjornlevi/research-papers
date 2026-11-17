from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

import httpx
from jsonschema import ValidationError

from research_library import contracts, releases, retrieval, web_server
from fixtures import snapshots
from test_browser_library import Links
from test_collections import registry_fixture
from test_releases import fixture_bundle


class DocumentText(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.reading = False
        self.text = ""
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "pre" and dict(attrs).get("id") == "document-text":
            self.reading = True

    def handle_endtag(self, tag):
        if tag == "pre":
            self.reading = False

    def handle_data(self, data):
        if self.reading:
            self.text += data


class SourceDocumentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle, _ = fixture_bundle(self.root)
        first = self.bundle["units"][0]
        first["text"] = "Fictional text <script> & lipid. " * 200 + "successf"
        first["truncated"] = False
        second = deepcopy(first)
        second["id"] = "second-page"
        second["citation"]["locator"]["pdf_page"] = 2
        second["text"] = "ully reduced the fictional lipid.\n\n" + "More retained text. " * 180
        second["caveats"].append("OCR transcription; not human verified.")
        empty = deepcopy(second)
        empty["id"] = "empty-page"
        empty["citation"]["locator"]["pdf_page"] = 3
        empty["text"] = ""
        empty["truncated"] = True
        self.bundle["units"] = [empty, second, *self.bundle["units"]]
        self.expected = first["text"] + "\n\n" + second["text"] + "\n\n"
        self.release = self.root / "release"
        releases.export(self.bundle, self.release, profile="synthetic")
        self.snapshot = releases.validate(self.release, allowed_profiles={"synthetic"})

    def client(self, **kwargs):
        app = web_server.create_app(profiles={"synthetic"}, **kwargs)
        client = httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080")
        self.addCleanup(client.close)
        return client

    def test_document_pagination_recovers_exact_text_and_page_offsets(self):
        before = snapshots(self.release)
        offset, chunks, page_ids = 0, [], set()
        while offset is not None:
            args = {"source_id": "fictional:main", "offset": offset, "max_characters": 777}
            response = self.snapshot.call("get_source_document", args)
            contracts.validate_response("get_source_document", args, response)
            document = response["result"]
            self.assertEqual(document["total_characters"], len(self.expected))
            self.assertTrue(document["extraction_incomplete"])
            self.assertEqual(document["unit_count"], 3)
            self.assertLessEqual(len(document["text"]), 777)
            for segment in document["segments"]:
                page_ids.add(segment["id"])
                unit = self.snapshot._lookup("units", segment["id"])
                start, end = segment["start"] - offset, segment["end"] - offset
                unit_offset = segment["unit_offset"]
                self.assertEqual(document["text"][start:end], unit["text"][unit_offset:unit_offset + end - start])
                self.assertEqual(segment["citation"], unit["citation"])
            chunks.append(document["text"])
            offset = document["next_offset"]
        self.assertEqual("".join(chunks), self.expected)
        self.assertEqual(page_ids, {"fictional-unit-1", "second-page", "empty-page"})
        self.assertIn("successf\n\nully", "".join(chunks))
        self.assertEqual(snapshots(self.release), before)
        boundary = len(self.snapshot._lookup("units", "fictional-unit-1")["text"])
        result = self.snapshot.call("get_source_document", {
            "source_id": "fictional:main", "offset": boundary - 3, "max_characters": 10})["result"]
        self.assertEqual([part["citation"]["locator"]["pdf_page"] for part in result["segments"]], [1, 2])
        at_end = self.snapshot.call("get_source_document", {
            "source_id": "fictional:main", "offset": len(self.expected)})["result"]
        self.assertEqual(at_end["text"], "")
        self.assertIsNone(at_end["next_offset"])

    def test_browser_whole_document_matches_mcp_and_preserves_warnings(self):
        client = self.client(release=self.release)
        before = snapshots(self.release)
        sources = client.get("/sources")
        link = next(link for link in Links(sources.text).links if link.startswith("/document?"))
        response = client.get(link)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DocumentText(response.text).text, self.expected)
        self.assertIn("PDF page 3", response.text)
        self.assertIn("No text retained", response.text)
        self.assertIn("OCR transcription", response.text)
        self.assertIn("stored extraction itself is marked incomplete", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertIn(self.bundle["sources"][0]["sha256"], Links(response.text).fields["citation"])
        self.assertIn("Read whole document", client.get("/read?unit_id=second-page", follow_redirects=True).text)
        self.assertEqual(client.get("/document?source_id=fictional:main").status_code, 302)
        self.assertEqual(client.get("/document?source_id=missing").status_code, 404)
        self.assertEqual(client.get("/document?source_id=fictional:word").status_code, 400)
        self.assertEqual(client.get(link + "&source_id=other").status_code, 400)
        self.assertEqual(client.get("/document?source_id=fictional:main&release_id=" + "0" * 64).status_code, 410)
        self.assertEqual(snapshots(self.release), before)

    def test_api_and_registry_provenance_are_pinned_and_permissions_apply(self):
        store, first, _, _ = registry_fixture(self.root)
        store.replace("istopmm", self.release, version="doc-test", updated_at="2026-11-16")
        sid = store.current().release_id
        args = {"source_id": "fictional:main", "snapshot_id": sid,
                "collections": ["istopmm"], "max_characters": 8192}
        response = store.call("get_source_document", args)
        refs = response["collection_context"]["evidence_refs"]
        for index, segment in enumerate(response["result"]["segments"]):
            self.assertEqual(refs[index]["record_id"], segment["id"])
            self.assertEqual(refs[index]["collection_id"], "istopmm")
            self.assertEqual(refs[index]["release_id"], self.snapshot.release_id)
        client = self.client(registry=store.root, allowed_collections=["istopmm"],
                             default_collections=["istopmm"])
        api = client.get("/api/document", params={
            "source_id": "fictional:main", "collection": "istopmm", "snapshot_id": sid,
            "max_characters": 8192})
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json(), response)
        doc_url = f"/document?source_id=fictional:main&collection=istopmm&snapshot_id={sid}"
        self.assertEqual(DocumentText(client.get(doc_url).text).text, self.expected)
        store.replace("istopmm", first, version="doc-old", updated_at="2026-11-16")
        self.assertEqual(store.call("get_source_document", args), response)
        self.assertEqual(DocumentText(client.get(doc_url).text).text, self.expected)
        self.assertEqual(client.get("/api/document?source_id=fictional:main&collection=secret").status_code, 403)
        self.assertEqual(client.get("/api/document?source_id=fictional:main&collection=istopmm&offset=1").status_code, 400)
        self.assertEqual(client.get("/document?source_id=fictional:main&collection=secret").status_code, 403)
        curated = self.root / "curated"
        grants = {source["id"]: {"scopes": ["curated"], "basis": "Authored fixture"}
                  for source in self.bundle["sources"]}
        releases.export(self.bundle, curated, profile="curated", authorization=grants)
        with self.assertRaises(releases.AccessDeniedError):
            releases.validate(curated, allowed_profiles={"curated"}).call(
                "get_source_document", {"source_id": "fictional:main"})
        app = web_server.create_app(release=curated, profiles={"curated"})
        with app.test_client() as denied:
            self.assertEqual(denied.get("/document?source_id=fictional:main",
                                       base_url="http://127.0.0.1:8080").status_code, 403)

    def test_document_bounds_and_semantic_validation(self):
        for args in ({"offset": -1}, {"offset": True}, {"max_characters": 0},
                     {"max_characters": 8193}, {"offset": contracts.MAX_DOCUMENT_OFFSET + 1}):
            with self.assertRaises(ValidationError):
                self.snapshot.call("get_source_document", {"source_id": "fictional:main", **args})
        with self.assertRaises(retrieval.RetrievalError):
            self.snapshot.call("get_source_document", {"source_id": "fictional:main",
                                                      "offset": len(self.expected) + 1})
        response = self.snapshot.call("get_source_document", {"source_id": "fictional:main"})
        for key, value in (("next_offset", 0), ("source_id", "wrong"), ("offset", 1)):
            broken = deepcopy(response)
            broken["result"][key] = value
            with self.assertRaises(ValueError):
                contracts.validate_response("get_source_document", {"source_id": "fictional:main"}, broken)

    def test_blank_pages_and_separator_only_slices_keep_exact_offsets(self):
        first_length = len(self.snapshot._lookup("units", "fictional-unit-1")["text"])
        separator = self.snapshot.call("get_source_document", {
            "source_id": "fictional:main", "offset": first_length, "max_characters": 1})["result"]
        self.assertEqual(separator["text"], "\n")
        self.assertEqual(separator["segments"], [])
        self.assertEqual(separator["next_offset"], first_length + 1)
        for unit in self.bundle["units"]:
            if unit["citation"]["source_id"] == "fictional:main":
                unit["text"] = ""
        blank = self.root / "blank"
        releases.export(self.bundle, blank, profile="synthetic")
        snapshot = releases.validate(blank, allowed_profiles={"synthetic"})
        result = snapshot.call("get_source_document", {"source_id": "fictional:main"})["result"]
        self.assertEqual(result["text"], "\n\n\n\n")
        self.assertEqual(len(result["segments"]), 3)
        self.assertTrue(all(segment["start"] == segment["end"] for segment in result["segments"]))
