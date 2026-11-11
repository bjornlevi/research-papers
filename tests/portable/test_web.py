from copy import deepcopy
import csv
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import httpx
from flask import Flask

from research_library import __version__, catalog, releases, web_server
from fixtures import snapshots
from test_collections import registry_fixture
from test_releases import fixture_bundle


class WebTests(unittest.TestCase):
    def test_saved_discovery_labels_use_current_terms_without_rewriting_jobs(self):
        for direction, old, expected in (
                ("background", "Find referenced papers", "Find references"),
                ("impact", "Find papers that reference my core library", "Find citing papers")):
            job = {"direction": direction, "label": old}
            self.assertEqual(web_server.discovery_label(job), expected)
            self.assertEqual(job["label"], old)
        coverage = web_server.discovery_coverage({
            "limitations": ["Requested directions: background. Unrequested directions were not searched."],
            "seeds": []})
        self.assertIn("Searched: References.", coverage)
        self.assertNotIn("directions: background", coverage)

    def test_discovery_coverage_is_readable_escaped_and_preserves_provider_gaps(self):
        rendered = web_server.discovery_coverage({
            "limitations": ["Provider-indexed only", "<script>unsafe</script>"],
            "seeds": [{"seed_doi": "10.0000/core", "title": "Core paper", "providers": {
                "openalex": {"status": "complete_indexed_neighborhood", "citations_reported": 12,
                             "citations_retrieved": 10, "requested_directions": ["impact"],
                             "source": {"sha256": "a" * 64, "url": "https://example.org/api"}},
                "crossref": {"status": "failed", "error": "<failed>"},
            }}],
        })
        for expected in ("<h4>Limitations</h4>", "<li>Provider-indexed only</li>",
                         "<h5>Core paper</h5>", "Complete within provider index",
                         "Citing papers retrieved", "<dd>10</dd>", "Citing papers reported by provider",
                         "<dd>12</dd>", "Citing papers", "Provider error", "&lt;failed&gt;",
                         "<summary>Provider details</summary>", "&lt;script&gt;unsafe&lt;/script&gt;"):
            self.assertIn(expected, rendered)
        self.assertNotIn("<pre>", rendered)
        self.assertNotIn("<script>", rendered)

    def test_incomplete_project_run_explains_status_from_saved_catalog(self):
        run = {"policy_version": "project-first-tier-1", "status": "incomplete", "id": "test-run"}
        coverage = {"scope": "Metadata. Incomplete coverage: 10 provider reference identifiers could not be resolved",
                    "runs": [run]}
        rendered = web_server.metadata_value(coverage, project_scope=True)
        runs = rendered[rendered.index("<dt>Runs</dt>"):]
        self.assertIn("<dt>Status</dt><dd>incomplete</dd><dt>Why incomplete</dt>", runs)
        self.assertIn("10 provider reference identifiers could not be resolved", runs)
        self.assertIn("<dt>Next steps</dt>", runs)
        self.assertIn('href="/discovery?direction=background#coverage-issues"', runs)
        self.assertIn('href="/publications"', runs)
        self.assertNotIn("why_incomplete", run)
        missing = web_server.metadata_value({"scope": "Metadata", "runs": [run]}, project_scope=True)
        self.assertIn("does not include a detailed reason", missing)
        complete = web_server.metadata_value(
            {"scope": "Metadata", "runs": [{"policy_version": "project-first-tier-1", "status": "complete"}]},
            project_scope=True)
        self.assertNotIn("Why incomplete", complete)

    def test_metadata_fields_are_structured_escaped_and_safely_linked(self):
        rendered = web_server.metadata_value([{
            "provider_record": "<script>unsafe</script>",
            "requests": [{"url": "https://example.org/paper?a=1&b=2", "retrieved_at": "2026-11-10"}],
            "reviewed": False, "missing": None, "empty": [],
        }])
        self.assertIn('<ul class="metadata-list">', rendered)
        self.assertIn('<dl class="metadata-fields">', rendered)
        self.assertIn("<dt>Provider record</dt>", rendered)
        self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", rendered)
        self.assertIn('href="https://example.org/paper?a=1&amp;b=2"', rendered)
        self.assertIn(">Open link (example.org)</a>", rendered)
        self.assertNotIn(">https://example.org/", rendered)
        self.assertIn("<dd>No</dd>", rendered)
        self.assertIn("Unknown / not recorded", rendered)
        self.assertIn("None recorded", rendered)
        for unsafe in ("javascript:alert(1)", "https://[invalid", "https://user:pass@example.org/"):
            self.assertNotIn("<a ", web_server.metadata_value(unsafe))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        bundle, _ = fixture_bundle(self.root)
        bundle["sources"][0]["bibliography"] = {
            "title": 'A <script>alert("x")</script> title', "year": "2025",
            "acquired_from": "https://example.org/file.pdf", "acquired_at": None,
            "acquisition_method": "synthetic fixture",
        }
        self.release = self.root / "release"
        releases.export(bundle, self.release, profile="synthetic")

    def client(self, **kwargs):
        app = web_server.create_app(profiles={"synthetic"}, **kwargs)
        self.assertIsInstance(app, Flask)
        client = httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080")
        self.addCleanup(client.close)
        return client

    def test_overview_sources_safety_and_readonly(self):
        before = snapshots(self.release)
        client = self.client(release=self.release)
        overview = client.get("/")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("http://127.0.0.1:8000/mcp", overview.text)
        self.assertIn("make mcp", overview.text)
        self.assertIn("make reconnect", overview.text)
        self.assertIn("make disconnect", overview.text)
        self.assertIn("CLIENT_PROJECT", overview.text)
        self.assertIn("Verify the running MCP server", overview.text)
        self.assertIn("http://127.0.0.1:8000/readyz", overview.text)
        self.assertIn("docs/verify-mcp.md", overview.text)
        self.assertIn("do not validate your client token", overview.text)
        self.assertIn("list_sources", overview.text)
        self.assertIn(f"({len(web_server.DESCRIPTIONS)} in version {__version__})", overview.text)
        self.assertIn("in a metadata catalog collection", overview.text)
        self.assertEqual(client.get("/style.css").headers["content-type"], "text/css; charset=utf-8")
        sources = client.get("/sources")
        self.assertEqual(sources.status_code, 200)
        self.assertIn("https://example.org/file.pdf", sources.text)
        self.assertIn("Unknown / not recorded", sources.text)
        self.assertIn("&lt;script&gt;", sources.text)
        self.assertNotIn("<script>", sources.text)
        self.assertNotIn("Fictional screening context", sources.text)
        self.assertEqual(sources.text.count('<article class="source-entry"><details>'),
                         sources.text.count('<article class="source-entry">'))
        self.assertIn("<summary>", sources.text)
        self.assertNotIn("<details open", sources.text)
        self.assertEqual(sources.headers["cache-control"], "no-store")
        self.assertEqual(sources.headers["referrer-policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", sources.headers["content-security-policy"])
        self.assertIn("No matching sources", client.get("/sources?paper=missing").text)
        for path in ("/mcp", "/papers/file.pdf", "/state.json", "/../records.json"):
            self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.post("/sources").status_code, 405)
        self.assertEqual(snapshots(self.release), before)

    def test_reject_bad_input_and_remote_hosts(self):
        client = self.client(release=self.release)
        for query in ("offset=-1", "offset=abc", "offset=1000001", "path=secret",
                      "collection=secret", "snapshot_id=invalid"):
            with self.subTest(query=query):
                # A single release cannot resolve registry snapshot selectors.
                response = client.get("/sources?" + query)
                self.assertEqual(response.status_code, 400)
        for headers, status in (({"Host": "evil.test:8080"}, 421),
                                ({"Origin": "https://evil.test"}, 403),
                                ({"Origin": "null"}, 403)):
            response = client.get("/", headers=headers)
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(client.get("/", headers={"Host": "localhost:8080"}).status_code, 200)
        self.assertEqual(client.get("/", headers={
            "Host": "127.0.0.1:8080", "Origin": "http://localhost:8080",
        }).status_code, 200)
        self.assertEqual(client.get("/", headers={
            "Host": "localhost:8080", "Origin": "http://127.0.0.1:8080",
        }).status_code, 200)

    def test_collection_pagination_and_retained_snapshot(self):
        root = self.root / "collections"
        root.mkdir()
        store, _, _, _ = registry_fixture(root)
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"})
        response = client.get("/sources?collection=test-literature")
        self.assertEqual(response.status_code, 200)
        self.assertIn("test-literature", response.text)
        self.assertEqual(client.get("/sources?collection=secret").status_code, 403)
        self.assertEqual(client.get("/sources?snapshot_id=" + "0" * 64).status_code, 410)
        self.assertEqual(client.get("/sources?offset=20").status_code, 200)
        # An expired/corrupt registry must not become an apparently empty source list.
        (store.root / "active.json").write_text("{}")
        self.assertEqual(client.get("/sources").status_code, 503)

    def test_real_pagination_links_pin_collection_snapshot(self):
        root = self.root / "paged"
        root.mkdir()
        store, _, _, _ = registry_fixture(root)
        bundle, _ = fixture_bundle(root)
        for index in range(24):
            source = deepcopy(bundle["sources"][0])
            source["id"] = f"fictional:extra-{index:02}"
            bundle["sources"].append(source)
        expanded = root / "expanded"
        releases.export(bundle, expanded, profile="synthetic")
        sid = store.replace("istopmm", expanded, version="expanded", updated_at="2026-10-19")
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"},
                             default_collections=["istopmm"])
        first = client.get("/sources")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.text.count('<article class="source-entry">'), 20)
        self.assertIn("offset=20", first.text)
        self.assertIn("snapshot_id=" + sid, first.text)
        second = client.get("/sources", params={"offset": 20, "snapshot_id": sid})
        self.assertEqual(second.text.count('<article class="source-entry">'), 7)
        self.assertIn("Previous", second.text)
        self.assertNotIn(">Next<", second.text)
        store.replace("istopmm", self.release, version="new", updated_at="2026-10-19")
        self.assertEqual(client.get("/sources", params={"offset": 20, "snapshot_id": sid}).text,
                         second.text)

    def test_dynamic_library_joins_inventory_extraction_and_new_downloads(self):
        inventory = self.root / "releases/istopmm-first-tier-20261019"
        inventory.mkdir(parents=True)
        columns = ["id", "title", "authors", "publication_date", "journal", "citation_url",
                   "acquisition_status", "pdf_sha256", "background_of", "impact_on"]
        with (inventory / "bibliography.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for number in range(55):
                writer.writerow({
                    "id": f"doi:10.0000/{number:02}", "title": f"Paper {number:02} <script>",
                    "authors": "Fictional Author", "citation_url": "javascript:alert(1)" if number == 0 else "https://example.org/paper",
                    "acquisition_status": "downloaded" if number < 2 else "no_explicitly_licensed_pdf",
                    "pdf_sha256": str(number), "background_of": "10.0000/seed",
                    "impact_on": "10.0000/seed" if number == 0 else "",
                })
        extraction_root = self.root / "releases/m9-ingestion-001"
        extraction_root.mkdir()
        (extraction_root / "pilot.json").write_text(json.dumps({"sources": [
            {"publication_id": f"doi:10.0000/{number:02}", "sha256": str(number)} for number in range(2)]}))
        path = extraction_root / "extraction.json"
        path.write_text(json.dumps({"items": {
            "doi:10.0000/00": {"status": "extracted", "pages": 4},
            "doi:10.0000/01": {"status": "partial", "pages": 2}}}))
        updates = self.root / "releases/source-updates/runs/new"
        updates.mkdir(parents=True)
        (updates / "state.json").write_text(json.dumps({
            "name": "new", "started_at": "2026-10-20T00:00:00+00:00", "ingestion": {
                "doi:10.0000/new": {"status": "extracted", "pages": 3, "source": {
                    "title": "New downloaded source", "publication_date": "2026", "sha256": "new"}}}}))
        client = self.client(release=self.release, data_root=self.root)
        before = snapshots(self.root)
        result = client.get("/library")
        self.assertEqual(result.status_code, 200)
        self.assertIn("56 listed sources; 3 downloaded; 2 extracted; 1 partially extracted.", result.text)
        self.assertIn("3 extracted PDF versions; 9 pages", result.text)
        self.assertIn("References; Citing papers", result.text)
        self.assertIn("Paper 00 &lt;script&gt;", result.text)
        self.assertNotIn("<script>", result.text)
        self.assertNotIn("javascript:", result.text)
        self.assertIn("offset=50", result.text)
        self.assertEqual(result.text.count("<tr>"), 51)
        second = client.get("/library?offset=50")
        self.assertEqual(second.text.count("<tr>"), 7)
        self.assertIn("Previous", second.text)
        self.assertNotIn(">Next<", second.text)
        self.assertIn("3 matching sources", client.get("/library?download=downloaded").text)
        self.assertIn("1 matching sources", client.get("/library?extraction=partial").text)
        self.assertIn("New downloaded source", client.get("/library?query=10.0000%2Fnew").text)
        self.assertIn("No matching sources", client.get("/library?query=absent").text)
        self.assertEqual(snapshots(self.root), before)
        path.write_text(json.dumps({"items": {"doi:10.0000/00": {"status": "partial", "pages": 4}}}))
        changed = client.get("/library")
        self.assertIn("1 extracted; 1 partially extracted.", changed.text)
        for query in ("offset=-1", "offset=no", "extraction=invalid", "download=invalid", "path=secret"):
            self.assertEqual(client.get("/library?" + query).status_code, 400)
        self.assertEqual(client.post("/library").status_code, 405)
        (updates / "state.json").write_text(json.dumps({
            "name": "new", "started_at": "2026-10-20T00:00:00+00:00", "ingestion": {
                "doi:10.0000/00": {"status": "extracted", "pages": 3, "source": {
                    "title": "Updated source version", "publication_date": "2026", "sha256": "revision"}}}}))
        revised = client.get("/library")
        self.assertIn("55 listed sources; 2 downloaded", revised.text)
        self.assertIn("2 extracted PDF versions; 7 pages", revised.text)
        self.assertIn("2 retained PDF versions", revised.text)
        (updates / "state.json").unlink()
        state = json.loads(path.read_text())
        state.update(manifest_sha256="fixture")
        state["items"]["doi:10.0000/00"]["sha256"] = "native-fixture"
        path.write_text(json.dumps(state))
        ocr_root = self.root / "releases/literature-ocr-001"
        ocr_root.mkdir()
        checkpoint = ocr_root / "ocr.json"
        checkpoint.write_text('{"status":"extracted_with_ocr"}')
        (ocr_root / "index.json").write_text(json.dumps({
            "manifest_sha256": "fixture", "items": {"doi:10.0000/00": {
                "source_sha256": "0", "native_sha256": "native-fixture", "file": "ocr.json",
                "sha256": catalog.file_digest(checkpoint), "status": "extracted_with_ocr",
                "low_confidence_pages": [2]}}}))
        completed = client.get("/library?extraction=extracted_with_ocr")
        self.assertEqual(completed.status_code, 200)
        self.assertIn("1 extracted with OCR; 0 need visual review", completed.text)
        self.assertIn("Low OCR confidence on PDF pages: 2", completed.text)
        self.assertIn("1 matching sources", completed.text)
        checkpoint.write_text("{}")
        self.assertEqual(client.get("/library").status_code, 503)
        path.unlink()
        self.assertEqual(client.get("/library").status_code, 503)

    def test_library_infers_workspace_and_reports_missing_inventory(self):
        root = self.root / "registry-case"
        root.mkdir()
        store, _, _, _ = registry_fixture(root)
        client = self.client(registry=store.root, allowed_collections={"istopmm", "test-literature"})
        self.assertEqual(client.get("/library").status_code, 503)
        release_client = self.client(release=self.release)
        self.assertIn("No local source inventory configured", release_client.get("/library").text)

    def test_catalog_pages_default_to_metadata_not_evidence_collections(self):
        from test_catalog import mixed_fixture

        root = self.root / "metadata-defaults"
        root.mkdir()
        store, document, _, _ = mixed_fixture(root)
        client = self.client(registry=store.root,
                             allowed_collections={"istopmm", "test-literature"},
                             default_collections=["istopmm"])
        for endpoint, heading in (("/publications", "Publication metadata"),
                                  ("/coverage", "Literature coverage")):
            response = client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertIn(f"<h2>{heading}</h2>", response.text)
            self.assertIn("All metadata catalogs", response.text)
            self.assertNotIn("No metadata catalog records", response.text)
            empty = client.get(endpoint + "?collection=istopmm")
            if endpoint == "/coverage":
                self.assertIn('class="coverage-metrics"', response.text)
                self.assertIn('<th scope="col">Count</th>', response.text)
                self.assertIn("<summary>Publication breakdown</summary>", response.text)
                self.assertNotIn("Discovery coverage &amp; gaps", response.text)
                self.assertEqual(empty.status_code, 200)
                self.assertIn("evidence coverage", empty.text)
                self.assertIn("1 of 1 evidence publications", empty.text)
                self.assertIn("each entire related catalog", empty.text)
                self.assertNotIn("No metadata catalog records", empty.text)
            else:
                self.assertEqual(empty.status_code, 200)
                self.assertIn("1 matching bibliographic records", empty.text)
                self.assertIn(document["publications"][0]["title"], empty.text)
                filtered = client.get(endpoint + "?collection=istopmm&query=absentword")
                self.assertIn("0 matching bibliographic records", filtered.text)
                self.assertNotIn("<article>", filtered.text)
                self.assertEqual(client.get(endpoint + "?collection=secret").status_code, 403)
        self.assertIn(document["publications"][0]["title"], client.get("/publications").text)
        sources = client.get("/sources?collection=istopmm")
        self.assertIn("<summary>" + document["publications"][0]["title"] + " &mdash; Main paper</summary>", sources.text)
        self.assertIn("Identifier-matched metadata catalog; evidence source record unchanged", sources.text)
        self.assertIn("Sources available to AI", client.get("/sources").text)
        self.assertIn("Found, downloaded, extracted", client.get("/").text)
        no_catalog = self.client(release=self.release)
        self.assertIn("No metadata catalog is configured", no_catalog.get("/publications").text)
        evidence_root = self.root / "evidence-only"
        evidence_root.mkdir()
        evidence_store, _, _, _ = registry_fixture(evidence_root)
        evidence_client = self.client(
            registry=evidence_store.root, allowed_collections={"istopmm", "test-literature"})
        for collection in ("istopmm", "test-literature"):
            coverage = evidence_client.get("/coverage", params={"collection": collection})
            self.assertEqual(coverage.status_code, 200)
            self.assertIn("evidence coverage", coverage.text)
            self.assertIn("No authorized discovery catalog has identifier-matched records", coverage.text)
        self.assertEqual(evidence_client.get("/coverage?collection=secret").status_code, 403)

    def test_library_impact_ranks_pending_sources_and_refreshes_saved_metrics(self):
        inventory = self.root / "releases/istopmm-first-tier-20261019"
        inventory.mkdir(parents=True)
        columns = ["id", "title", "acquisition_status", "pdf_sha256", "background_of",
                   "impact_on", "citation_url"]
        sources = [
            ("older", "Older highly cited", "licensed_locations_failed", "seed-a; seed-b", "seed-a", 100, "2000-01-01"),
            ("recent", "Recent influential", "no_explicitly_licensed_pdf", "seed-a", "", 20, "2026-01-01"),
            ("zero", "Zero citations", "no_explicitly_licensed_pdf", "", "", 0, "2025-01-01"),
            ("unknown", "Unknown <script>", "no_explicitly_licensed_pdf", "", "", None, ""),
            ("downloaded", "Already downloaded", "downloaded", "", "", 9999, "2025-01-01"),
            ("seed", "Original collection", "existing_seed_collection", "", "", 9998, "2025-01-01"),
        ] + [(f"filler-{i}", f"Filler {i:02}", "no_explicitly_licensed_pdf", "", "", 1, "2020-01-01")
             for i in range(50)]
        nodes = {}
        with (inventory / "bibliography.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for key, title, status, background, impact, count, published in sources:
                writer.writerow({"id": "doi:10.0000/" + key, "title": title,
                                 "acquisition_status": status, "background_of": background,
                                 "impact_on": impact})
                if count is not None:
                    nodes[key] = {"record": {"identifiers": {"doi": "https://doi.org/10.0000/" + key}},
                                  "work": {"id": "https://openalex.org/W1", "cited_by_count": count,
                                           "publication_date": published},
                                  "sources": [{"retrieved_at": "2026-10-19T00:00:00+00:00"}]}
        neighborhood = inventory / "neighborhood.json"
        snapshot = {"acquired_at": "2026-10-19T00:00:00+00:00", "nodes": nodes}
        neighborhood.write_text(json.dumps(snapshot))
        extraction = self.root / "releases/m9-ingestion-001"
        extraction.mkdir()
        (extraction / "pilot.json").write_text('{"sources":[]}')
        (extraction / "extraction.json").write_text('{"items":{}}')
        original_index = self.root / "evidence/index.sqlite"
        original_index.parent.mkdir()
        with sqlite3.connect(original_index) as connection:
            connection.executescript(
                "CREATE TABLE papers (paper_id TEXT, doi TEXT);"
                "CREATE TABLE pages (paper_id TEXT, document_id TEXT, source_pdf_sha256 TEXT, text TEXT);"
                "INSERT INTO papers VALUES ('seed','10.0000/seed');"
                "INSERT INTO pages VALUES ('seed','main','original-hash','Original native page text');"
                "INSERT INTO pages VALUES ('seed','main','original-hash','Second original page');"
                "INSERT INTO pages VALUES ('seed','supplement-01','supplement-hash','Supplement text');")
        client = self.client(release=self.release, data_root=self.root)
        original = client.get("/library?download=existing_seed_collection")
        self.assertEqual(original.status_code, 200)
        self.assertIn("2 pages; 1 retained PDF versions", original.text)
        self.assertIn("1 matching sources", original.text)
        self.assertNotIn("Not extracted</td>", original.text)
        before = snapshots(self.root)
        ranked = client.get("/library?download=awaiting&sort=citations")
        self.assertEqual(ranked.status_code, 200)
        self.assertIn("54 matching sources", ranked.text)
        self.assertIn("Citation counts available for 53", ranked.text)
        self.assertLess(ranked.text.index("Older highly cited"), ranked.text.index("Recent influential"))
        self.assertNotIn("Already downloaded", ranked.text)
        self.assertNotIn(">Original collection<", ranked.text)
        self.assertIn("100 citations", ranked.text)
        self.assertIn("captured 2026-10-19", ranked.text)
        self.assertIn("2 iStopMM connections", ranked.text)
        self.assertNotIn('<select name="sort"><option value="">', ranked.text)
        self.assertIn("download=awaiting&amp;sort=citations&amp;offset=50", ranked.text)
        last = client.get("/library?download=awaiting&sort=citations&offset=50")
        self.assertLess(last.text.index("Zero citations"), last.text.index("Unknown &lt;script&gt;"))
        self.assertIn("Unknown citations", last.text)
        self.assertNotIn("<script>", last.text)
        annual = client.get("/library?download=awaiting&sort=annual")
        self.assertLess(annual.text.index("Recent influential"), annual.text.index("Older highly cited"))
        connected = client.get("/library?download=awaiting&sort=connections")
        self.assertLess(connected.text.index("Older highly cited"), connected.text.index("Recent influential"))
        self.assertEqual(client.get("/library?sort=invalid").status_code, 400)
        self.assertEqual(snapshots(self.root), before)
        with (inventory / "bibliography.csv").open("a", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            for key, title in (("untitled", ""), ("whitespace", "   ")):
                writer.writerow({"id": "doi:10.0000/" + key, "title": title,
                                 "acquisition_status": "no_explicitly_licensed_pdf",
                                 "background_of": "seed-a; seed-b; seed-c"})
                nodes[key] = {
                    "record": {"identifiers": {"doi": "10.0000/" + key}},
                    "work": {"id": "https://openalex.org/W2", "cited_by_count": 999999,
                             "publication_date": "2026-01-01"},
                    "sources": [{"retrieved_at": "2026-10-19T00:00:00+00:00"}]}
        neighborhood.write_text(json.dumps(snapshot))
        for ordering in ("title", "citations", "annual", "connections"):
            with self.subTest(ordering=ordering):
                first = client.get("/library", params={"download": "awaiting", "sort": ordering})
                self.assertNotIn("[Unresolved reference]", first.text)
                tail = client.get("/library", params={
                    "download": "awaiting", "sort": ordering, "offset": 50})
                self.assertEqual(tail.text.count("[Unresolved reference]"), 2)
                self.assertLess(tail.text.index("Zero citations"), tail.text.index("[Unresolved reference]"))
        unresolved = client.get("/library?query=untitled")
        self.assertIn("1 matching sources", unresolved.text)
        self.assertIn("[Unresolved reference]", unresolved.text)
        nodes["recent"]["work"]["cited_by_count"] = 1000
        neighborhood.write_text(json.dumps(snapshot))
        changed = client.get("/library?download=awaiting")
        self.assertLess(changed.text.index("Recent influential"), changed.text.index("Older highly cited"))
        nodes["recent"]["work"]["cited_by_count"] = -1
        neighborhood.write_text(json.dumps(snapshot))
        self.assertEqual(client.get("/library?download=awaiting").status_code, 503)

    def test_source_summaries_distinguish_main_paper_and_supplements(self):
        bundle, _ = fixture_bundle(self.root)
        for number in (1, 2):
            source = deepcopy(bundle["sources"][0])
            source["id"] = f"fictional:supplement-{number:02}"
            bundle["sources"].append(source)
        destination = self.root / "supplement-labels"
        releases.export(bundle, destination, profile="synthetic")
        response = self.client(release=destination).get("/sources")
        self.assertEqual(response.status_code, 200)
        for label in ("Main paper", "Supplement 1", "Supplement 2"):
            self.assertIn(f"<summary>10.0000/fictional &mdash; {label}</summary>", response.text)
        self.assertIn("&mdash; Source document</summary>", response.text)
        self.assertNotIn("&mdash; Source fictional:word</summary>", response.text)

    def test_cli_and_configuration(self):
        command = [sys.executable, "-m", "research_library.web_server"]
        help_result = subprocess.run(command + ["--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertNotIn("--host", help_result.stdout)
        invalid = subprocess.run(command + ["--release", str(self.release), "--allow-profile",
                                            "synthetic", "--port", "0"],
                                 capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 1)
        with self.assertRaises(ValueError):
            web_server.create_app(release=self.release, profiles={"synthetic"},
                                  mcp_url="http://user:password@127.0.0.1:8000/mcp")
