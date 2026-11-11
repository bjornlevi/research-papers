from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

import httpx

from research_library import collections, quality, releases, web_server
from fixtures import snapshots
from test_collections import registry_fixture
from test_catalog import mixed_fixture
from test_releases import fixture_bundle


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.fields = {}
        self.field = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs["href"])
        if tag == "textarea":
            self.field = attrs["id"]
            self.fields[self.field] = ""

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.field = None

    def handle_data(self, data):
        if self.field:
            self.fields[self.field] += data


class DefaultView(HTMLParser):
    """Text and controls visible before any native disclosures are opened."""
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.text = ""
        self.controls = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        visible = self.stack[-1][2] if self.stack else True
        if tag == "summary" and self.stack and self.stack[-1][0] == "details":
            visible = self.stack[-1][1]
        visible = visible and tag not in {"script", "style"} and "hidden" not in attrs
        if tag in {"input", "select", "textarea", "button"} and attrs.get("name"):
            self.controls[attrs["name"]] = visible and attrs.get("type") != "hidden"
        if tag not in {"input", "meta", "link", "br", "hr", "img", "wbr", "source", "area", "base", "embed", "param", "track", "col"}:
            self.stack.append((tag, visible, visible and (tag != "details" or "open" in attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or self.stack[-1][2]:
            self.text += data


class BrowserLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle, _ = fixture_bundle(self.root)
        self.bundle["sources"][0]["bibliography"] = {
            "title": "Fictional <script> publication", "year": "2026", "acquired_from": None,
            "acquired_at": None, "acquisition_method": None,
        }
        self.bundle["units"][0]["caveats"].append("Separate OCR transcription; not human verified.")
        self.release = self.root / "release"
        releases.export(self.bundle, self.release, profile="synthetic")

    def client(self, **kwargs):
        app = web_server.create_app(profiles={"synthetic"}, **kwargs)
        client = httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080")
        self.addCleanup(client.close)
        return client

    def test_search_reader_text_slices_citations_and_no_writes(self):
        client = self.client(release=self.release)
        before = snapshots(self.release)
        self.assertIn("Enter words", client.get("/search").text)
        response = client.get("/search?query=screening")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fictional &lt;script&gt; publication", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertIn("OCR transcription", response.text)
        url = next(url for url in Links(response.text).links if url.startswith("/read?"))
        self.assertIn("release_id=", url)
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        fields = Links(response.text).fields
        self.assertEqual(fields["excerpt"].split("\n\n")[0], self.bundle["units"][0]["text"][:2048])
        self.assertIn(self.bundle["sources"][0]["sha256"], fields["citation"])
        self.assertIn("PDF page 1", fields["citation"])
        self.assertIn("http://127.0.0.1:8080/read?", fields["citation"])
        self.assertIn("0", response.text)
        next_slice = next(link for link in Links(response.text).links if "offset=2048" in link)
        response = client.get(next_slice)
        self.assertEqual(Links(response.text).fields["excerpt"].split("\n\n")[0],
                         self.bundle["units"][0]["text"][2048:4096])
        self.assertIn("Previous text slice", response.text)
        self.assertIn('src="/reader.js"', response.text)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        script = client.get("/reader.js")
        self.assertIn("Clipboard unavailable", script.text)
        self.assertIn("field.select()", script.text)
        self.assertNotIn("https://", script.text)
        self.assertIn("Read retained text", client.get("/sources").text)
        self.assertEqual(before, snapshots(self.release))

    def test_default_pages_hide_identifiers_and_help_without_hiding_actions(self):
        client = self.client(release=self.release)
        release_id = releases.validate(self.release, allowed_profiles={"synthetic"}).release_id
        source_hash = self.bundle["sources"][0]["sha256"]
        for path in ("/", "/sources", "/search", "/connect", "/publications", "/coverage", "/quality"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                view = DefaultView(response.text)
                self.assertNotIn(release_id, view.text)
                self.assertNotIn(source_hash, view.text)
                self.assertNotIn("machine extraction is not scientific validation", view.text)
                self.assertIn('src="/help.js"', response.text)
                self.assertIn('class="help"><summary aria-label="Help:', response.text)
                self.assertNotIn('<details class="help" open', response.text)
        sources = DefaultView(client.get("/sources").text)
        self.assertTrue(sources.controls["paper"])
        search = DefaultView(client.get("/search").text)
        self.assertTrue(search.controls["query"])
        self.assertNotIn("Keyword search of retained extracted text", search.text)
        reader = client.get("/read?unit_id=fictional-unit-1", follow_redirects=True)
        view = DefaultView(reader.text)
        self.assertNotIn(release_id, view.text)
        self.assertNotIn(source_hash, view.text)
        self.assertIn("Fictional screening", view.text)
        self.assertIn("OCR transcription", view.text)
        self.assertIn("Copy citation and excerpt", view.text)
        self.assertNotIn("Citation and stable link", view.text)
        self.assertIn(source_hash, Links(reader.text).fields["citation"])
        script = client.get("/help.js")
        self.assertEqual(script.status_code, 200)
        self.assertEqual(script.headers["content-type"], "text/javascript; charset=utf-8")
        for event in ("pointerenter", "pointerleave", "click", "Escape"):
            self.assertIn(event, script.text)
        self.assertNotIn("fetch(", script.text)

    def test_document_order_and_readable_office_locators(self):
        for number in (11, 3):
            unit = deepcopy(self.bundle["units"][1])
            unit["id"] = f"element-{number}"
            unit["citation"]["locator"] = {"kind": "word_paragraph", "body_element": number}
            unit["text"] = f"Word element {number}."
            self.bundle["units"].append(unit)
        target = self.root / "office"
        releases.export(self.bundle, target, profile="synthetic")
        client = self.client(release=target)
        response = client.get("/read?unit_id=fictional-unit-2")
        self.assertEqual(response.status_code, 302)
        response = client.get(response.headers["location"])
        self.assertIn("Word body element 2 (table)", response.text)
        self.assertIn("element-3", response.text)
        self.assertNotIn("unit_id=element-11", response.text)
        response = client.get("/read?unit_id=fictional-unit-3", follow_redirects=True)
        self.assertIn("Worksheet Data", response.text)
        self.assertIn("row 4", response.text)

    def test_registry_pagination_and_retained_reader_links(self):
        store, first, second, _ = registry_fixture(self.root)
        bundle = deepcopy(self.bundle)
        for number in range(8):
            unit = deepcopy(bundle["units"][0])
            unit["id"] = f"extra-{number}"
            unit["citation"]["locator"]["pdf_page"] = number + 2
            bundle["units"].append(unit)
        expanded = self.root / "expanded"
        releases.export(bundle, expanded, profile="synthetic")
        store.replace("istopmm", expanded, version="expanded", updated_at="2026-10-27")
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"})
        response = client.get("/search?query=screening&collection=istopmm")
        links = Links(response.text).links
        next_page = next(url for url in links if url.startswith("/search?") and "offset=5" in url)
        reader = next(url for url in links if url.startswith("/read?"))
        original_page = client.get(reader)
        sid = store.current().release_id
        self.assertIn(sid, reader)
        store.replace("istopmm", second, version="new", updated_at="2026-10-27")
        self.assertEqual(client.get(reader).text, original_page.text)
        self.assertIn(sid, client.get(next_page).text)
        self.assertIn("Next results", response.text)
        self.assertIn("Previous results", client.get(next_page).text)
        self.assertEqual(client.get("/search?query=screening&offset=5").status_code, 400)
        (store.root / "registries" / (sid + ".json")).unlink()
        expired = client.get(reader)
        self.assertEqual(expired.status_code, 410)
        self.assertIn("Start a new search", expired.text)
        self.assertIn("not silently replaced", expired.text)

    def test_combined_search_and_collection_permissions(self):
        store, first, second, _ = registry_fixture(self.root)
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"})
        response = client.get("/search?query=screening")
        self.assertEqual(response.status_code, 200)
        self.assertIn("collection=istopmm", response.text)
        self.assertIn("collection=test-literature", response.text)
        self.assertIn("Metadata-only publications are not searched", response.text)
        for url in ("/search?query=screening&collection=secret", "/read?unit_id=fictional-unit-1&collection=secret"):
            self.assertEqual(client.get(url).status_code, 403)
        response = client.get("/read?unit_id=fictional-unit-1&collection=istopmm")
        self.assertIn("snapshot_id=", response.headers["location"])
        self.assertIn("source_id=", response.headers["location"])

    def test_metadata_titles_and_metadata_text_rejection(self):
        store, document, _, _ = mixed_fixture(self.root)
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"})
        response = client.get("/search?query=screening")
        self.assertIn(document["publications"][0]["title"], response.text)
        self.assertNotIn('<option value="test-literature"', response.text)
        self.assertEqual(client.get("/search?query=screening&collection=test-literature").status_code, 403)
        self.assertEqual(client.get("/read?unit_id=fictional-unit-1&collection=test-literature").status_code, 400)

    def test_catalog_provenance_is_collapsed_but_counts_and_bibliography_remain(self):
        store, document, _, _ = mixed_fixture(self.root)
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"})
        coverage = client.get("/coverage")
        self.assertEqual(coverage.status_code, 200)
        view = DefaultView(coverage.text)
        catalog_id = store.current().snapshots["test-literature"].release_id
        self.assertNotIn(catalog_id, view.text)
        self.assertIn(catalog_id, coverage.text)
        self.assertIn("Search history and provenance", view.text)
        self.assertIn("Literature coverage", view.text)
        self.assertIn("Publications", view.text)
        self.assertIn("Metadata quality", view.text)
        self.assertIn("Publication breakdown", view.text)
        self.assertNotIn("Review eligibility", view.text)
        self.assertIn("Review eligibility", coverage.text)
        self.assertNotIn("Provider records", view.text)
        self.assertIn("Provider records", coverage.text)
        publications = client.get("/publications")
        view = DefaultView(publications.text)
        self.assertIn(document["publications"][0]["title"], view.text)
        self.assertIn("Authors", view.text)
        self.assertIn("More metadata and provenance", view.text)
        self.assertNotIn("Provenance", view.text)

    def test_combined_duplicate_membership_links_use_each_members_source(self):
        store, _, _, _ = registry_fixture(self.root)
        bundle = deepcopy(self.bundle)
        # Identical text/provenance may be stored with different record and source IDs.
        original = self.root / "original-membership"
        releases.export(bundle, original, profile="synthetic")
        def reidentify(value):
            if isinstance(value, dict):
                if value.get("source_id") == "fictional:main":
                    value["source_id"] = "other:main"
                for child in value.values():
                    reidentify(child)
            elif isinstance(value, list):
                for child in value:
                    reidentify(child)
        reidentify(bundle)
        bundle["sources"][0]["id"] = "other:main"
        bundle["units"][0]["id"] = "other-unit"
        other = self.root / "other-membership"
        releases.export(bundle, other, profile="synthetic")
        store.replace("istopmm", original, version="1", updated_at="2026-10-27")
        store.replace("test-literature", other, version="1", updated_at="2026-10-27")
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"})
        response = client.get("/search?query=screening")
        single = client.get("/search?query=screening&collection=istopmm")
        count = single.text.count("<article>")
        self.assertGreater(count, 0)
        self.assertEqual(response.text.count("<article>"), count)
        member_urls = [url for url in Links(response.text).links if url.startswith("/read?")]
        self.assertEqual(len(member_urls), 2 * count)
        for url in member_urls:
            self.assertEqual(client.get(url).status_code, 200)
        urls = [url for url in Links(response.text).links if url.startswith("/read?") and "other-unit" in url]
        self.assertEqual(len(urls), 1)
        self.assertIn("source_id=other%3Amain", urls[0])
        self.assertEqual(client.get(urls[0]).status_code, 200)

    def test_curated_denial_and_public_quality_gate(self):
        curated = self.root / "curated"
        grants = {source["id"]: {"scopes": ["curated"], "basis": "Authored fictional permission."}
                  for source in self.bundle["sources"]}
        releases.export(self.bundle, curated, profile="curated", authorization=grants)
        app = web_server.create_app(release=curated, profiles={"curated"})
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            self.assertEqual(client.get("/read?unit_id=fictional-unit-1").status_code, 403)
            self.assertIn("No matching retained passages", client.get("/search?query=screening").text)
        store, _, _, _ = registry_fixture(self.root)
        quality_root = self.root / "quality"
        document = deepcopy(store.current().document)
        public = collections.CollectionStore(
            self.root / "public", allowed_profiles={"synthetic"},
            allowed_collections={"istopmm", "test-literature"}, public_quality_root=quality_root)
        for cid, snapshot in store.current().snapshots.items():
            public.releases.install(store.root / "releases" / snapshot.release_id)
            for source in snapshot._bundle["sources"]:
                quality.inventory(quality_root, source, [unit["citation"]["locator"] for unit in snapshot._bundle["units"]
                                                        if unit["citation"]["source_id"] == source["id"]])
            for finding in quality.report(snapshot, quality_root)["findings"]:
                quality.review(snapshot, quality_root, finding["id"], "verified", "Fictional test operator",
                               "Compared authored synthetic originals; software test only.")
        public.publish(document)
        client = self.client(registry=public.root, allowed_collections={"istopmm", "test-literature"})
        response = client.get("/search?query=screening&collection=istopmm")
        url = next(url for url in Links(response.text).links if url.startswith("/read?"))
        self.assertEqual(client.get(url).status_code, 200)
        snapshot = store.current().snapshots["istopmm"]
        quality.problem(snapshot, quality_root, "fictional-unit-1", "Test operator", "Synthetic known error.")
        self.assertEqual(client.get(url).status_code, 503)
        self.assertEqual(client.get("/search?query=screening").status_code, 503)

    def test_bad_queries_missing_data_and_empty_text(self):
        client = self.client(release=self.release)
        for query in ("", "---"):
            response = client.get("/search", params={"query": query})
            self.assertEqual(response.status_code, 400)
            self.assertIn("Start a new search", response.text)
        response = client.get("/search?query=absentuniquetoken")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No matching retained passages", response.text)
        for url in ("/read?unit_id=missing", "/read?unit_id=fictional-unit-1&source_id=wrong"):
            self.assertEqual(client.get(url).status_code, 404)
        for url in ("/search?query=a&query=b", "/read?unit_id=a&unit_id=b", "/search?path=secret",
                    "/read?unit_id=fictional-unit-1&offset=-1", "/search?query=foo&offset=-1"):
            self.assertEqual(client.get(url).status_code, 400)
        self.assertEqual(client.get("/read?unit_id=fictional-unit-1&release_id=" + "0" * 64).status_code, 410)
        self.assertEqual(client.post("/search").status_code, 405)
        self.assertEqual(client.get("/read", headers={"Host": "evil.test"}).status_code, 421)
        bundle = deepcopy(self.bundle)
        bundle["units"][0]["text"] = ""
        path = self.root / "empty"
        releases.export(bundle, path, profile="synthetic")
        response = self.client(release=path).get("/read?unit_id=fictional-unit-1", follow_redirects=True)
        self.assertIn("No text retained", response.text)
        self.assertIn("does not prove it is blank", response.text)


if __name__ == "__main__":
    unittest.main()
