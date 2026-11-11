"""Source-workspace tests; PDF acquisition is intentionally not in the serving wheel."""

from contextlib import redirect_stderr
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import pymupdf

from research_library import catalog, literature, releases
from research_library.build import citation_neighborhood as citation


def work(number):
    return {
        "id": f"https://openalex.org/W{number}", "doi": f"https://doi.org/10.0000/paper-{number}",
        "ids": {}, "title": f"Fictional paper {number}", "type": "article",
        "publication_date": "2025-01-01", "language": "en", "authorships": [],
        "primary_location": None, "locations": [],
        "referenced_works": ["https://openalex.org/W100", "https://openalex.org/W2"] if number == 1 else
                            ["https://openalex.org/W1", "https://openalex.org/W999"] if number == 101 else [],
    }


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "seeds.csv"
        with self.manifest.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["number", "doi", "title", "filename"])
            writer.writeheader()
            writer.writerows({"number": number, "doi": f"10.0000/paper-{number}",
                              "title": f"Seed {number}", "filename": f"{number}.pdf"} for number in range(1, 46))
        self.requests = []

    def api(self, handler):
        client = literature.MetadataClient(
            transport=httpx.MockTransport(handler), sleep=lambda _: None, clock=lambda: 0)
        self.addCleanup(client.close)
        return citation.CachedAPI(self.root / "responses", client)

    def handler(self, request):
        self.requests.append(request)
        params = request.url.params
        if request.url.host == "api.crossref.org":
            identifier = request.url.path.removeprefix("/works/")
            refs = [{"DOI": "10.0000/paper-100", "key": "one"},
                    {"unstructured": "Unresolved authored fictional reference", "key": "two"}] if identifier.endswith("-1") else []
            return httpx.Response(200, json={"message": {"DOI": identifier, "reference": refs,
                                                       "reference-count": len(refs)}})
        if "/https://doi.org/" in request.url.path:
            number = int(request.url.path.rsplit("-", 1)[-1])
            return httpx.Response(404) if number == 45 else httpx.Response(200, json=work(number))
        expression = params["filter"]
        if expression.startswith("cites:"):
            rows = [work(101)] if expression == "cites:W1" else []
        elif expression.startswith("openalex_id:"):
            rows = [work(int(value[1:])) for value in expression.split(":", 1)[1].split("|")]
        else:
            rows = []
        return httpx.Response(200, json={"meta": {"count": len(rows), "next_cursor": None}, "results": rows})

    def discover(self, handler=None):
        with redirect_stderr(io.StringIO()):
            return citation.discover(self.root / "run", self.manifest, self.api(handler or self.handler))

    def test_first_hop_dedup_provenance_unknown_references_and_catalog(self):
        graph = self.discover()
        inventory = citation.publish_metadata(self.root / "run")
        snapshot = catalog.open_snapshot(self.root / "run" / "catalog-001.sqlite", validate_records=True)
        self.assertEqual(len(graph["seeds"]), 45)
        self.assertEqual(graph["seeds"][-1]["providers"]["openalex"]["status"], "seed_not_indexed")
        self.assertEqual(inventory["membership_count"], 4)
        background = next(row for row in inventory["memberships"] if row["id"] == "doi:10.0000/paper-100")
        self.assertEqual(background["background_of"], ["10.0000/paper-1"])
        self.assertEqual(background["impact_on"], [])
        self.assertEqual(snapshot.get(background["id"])["title"], "Fictional paper 100")
        self.assertEqual(len(snapshot.get(background["id"])["provenance"]), 2)
        self.assertEqual(snapshot.get("doi:10.0000/paper-2")["istopmm_overlap"], "matched_doi")
        self.assertFalse(any("W999" in str(request.url) for request in self.requests))
        self.assertEqual({edge["seed_doi"] for edge in graph["edges"]}, {"10.0000/paper-1"})
        self.assertTrue(any(node.get("reference", {}).get("unstructured") for node in graph["nodes"].values()))
        self.assertEqual(snapshot.get("doi:10.0000/paper-100")["diseases"], [])
        self.assertFalse(snapshot.document["runs"][0]["status"] == "complete")

    def test_cache_reuses_validated_response_and_rejects_tampering(self):
        api = self.api(self.handler)
        url = "https://api.openalex.org/works/https://doi.org/10.0000/paper-1"
        first = api.get(url)
        self.assertEqual(api.get(url), first)
        self.assertEqual(len(self.requests), 1)
        path = next((self.root / "responses").glob("*.json"))
        document = json.loads(path.read_text())
        document["payload"]["title"] = "Tampered"
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "checksum"):
            api.get(url)

    def test_directional_discovery_only_queries_requested_relationships(self):
        for direction in ("background", "impact"):
            self.requests.clear()
            with self.subTest(direction=direction), redirect_stderr(io.StringIO()):
                api = self.api(self.handler)
                api.root = self.root / ("responses-" + direction)
                graph = citation.discover(self.root / direction, self.manifest, api,
                                          directions=[direction])
                self.assertEqual({edge["direction"] for edge in graph["edges"]}, {direction})
                citing_queries = [r for r in self.requests if r.url.params.get("filter", "").startswith("cites:")]
                crossref_queries = [r for r in self.requests if r.url.host == "api.crossref.org"]
                self.assertEqual(bool(citing_queries), direction == "impact")
                self.assertEqual(bool(crossref_queries), direction == "background")

    def test_project_scope_accepts_configurable_seed_count(self):
        with self.manifest.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["number", "doi", "title", "filename"])
            writer.writeheader()
            writer.writerows({"number": n, "doi": f"10.0000/paper-{n}", "title": f"Seed {n}",
                              "filename": ""} for n in (1, 2))
        with redirect_stderr(io.StringIO()):
            graph = citation.discover(self.root / "project-run", self.manifest, self.api(self.handler),
                                      project_scope=True)
        self.assertEqual(len(graph["seeds"]), 2)
        self.assertIn("2 project core", graph["scope"])
        self.assertEqual({edge["direction"] for edge in graph["edges"]}, {"background", "impact"})
        with self.assertRaisesRegex(ValueError, "45"):
            citation.discover(self.root / "legacy", self.manifest, self.api(self.handler))

    def test_provider_failure_is_explicit_and_missing_title_retained(self):
        def handler(request):
            if request.url.params.get("filter") == "cites:W1":
                return httpx.Response(403)
            response = self.handler(request)
            if "/https://doi.org/10.0000/paper-3" in request.url.path:
                payload = response.json()
                payload["title"] = None
                return httpx.Response(200, json=payload)
            return response
        graph = self.discover(handler)
        self.assertEqual(graph["seeds"][0]["providers"]["openalex"]["status"], "failed")
        self.assertEqual(graph["nodes"]["openalex:WORK:W3"]["record"]["title"], "")
        self.assertEqual(graph["seeds"][0]["providers"]["crossref"]["status"], "complete_deposited_references")

    def test_citation_pagination_checks_counts_and_direction(self):
        def handler(request):
            if request.url.params.get("filter") == "cites:W1":
                return httpx.Response(200, json={"meta": {"count": 2, "next_cursor": "again"},
                                               "results": [work(101)]})
            return self.handler(request)
        graph = self.discover(handler)
        failure = graph["seeds"][0]["providers"]["openalex"]
        self.assertEqual(failure["status"], "failed")
        self.assertIn("Repeated citing publication", failure["error"])

    def test_authenticated_cursors_cross_page_limit_and_keep_key_out_of_provenance(self):
        secret = "synthetic-openalex-test-key"
        def handler(request):
            if request.url.host == "api.openalex.org":
                self.assertEqual(request.headers.get("Authorization"), "Bearer " + secret)
            else:
                self.assertNotIn("Authorization", request.headers)
            self.assertNotIn(secret, str(request.url))
            if request.url.params.get("filter") == "cites:W1":
                cursor = request.url.params["cursor"]
                self.assertNotIn("page", request.url.params)
                self.assertEqual(request.url.params["per-page"], "100")
                start = 0 if cursor == "*" else int(cursor)
                results = []
                for number in range(start, min(start + 100, 10001)):
                    record = work(number + 1000)
                    record["referenced_works"] = ["https://openalex.org/W1"]
                    results.append(record)
                return httpx.Response(200, json={
                    "meta": {"count": 10001, "next_cursor": str(start + 100)}, "results": results})
            return self.handler(request)

        with self.manifest.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["number", "doi", "title", "filename"])
            writer.writeheader()
            writer.writerow({"number": 1, "doi": "10.0000/paper-1", "title": "Seed", "filename": ""})
        api = self.api(handler)
        api.client.openalex_api_key = secret
        with redirect_stderr(io.StringIO()):
            graph = citation.discover(self.root / "cursor-run", self.manifest, api, project_scope=True)
        self.assertEqual(graph["seeds"][0]["providers"]["openalex"]["citations_retrieved"], 10001)
        for path in self.root.rglob("*.json"):
            self.assertNotIn(secret, path.read_text())

    def test_repeated_cursor_is_explicit_provider_failure(self):
        def handler(request):
            if request.url.params.get("filter") == "cites:W1":
                return httpx.Response(200, json={
                    "meta": {"count": 2, "next_cursor": "*"}, "results": [work(101)]})
            return self.handler(request)
        api = self.api(handler)
        api.client.openalex_api_key = "synthetic-test-key"
        with redirect_stderr(io.StringIO()):
            graph = citation.discover(self.root / "cursor-failure", self.manifest, api)
        self.assertIn("Nonadvancing OpenAlex citation cursor",
                      graph["seeds"][0]["providers"]["openalex"]["error"])

    def test_license_gate_does_not_treat_free_access_as_permission(self):
        value = work(100)
        value["locations"] = [
            {"is_oa": True, "license": None, "pdf_url": "https://example.org/free.pdf"},
            {"is_oa": True, "license": "other-oa", "pdf_url": "https://example.org/unknown.pdf"},
            {"license": "cc-by", "pdf_url": "https://example.org/authorized.pdf"},
            {"license": "cc-by", "pdf_url": "http://example.org/insecure.pdf"},
        ]
        self.assertEqual([row["url"] for row in citation.licensed_locations(value)],
                         ["https://example.org/authorized.pdf"])

    def test_pdf_validation_redirects_retries_and_failures(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Authored fictional PDF")
            data = document.tobytes()
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(503, headers={"Retry-After": "0"})
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/file.pdf"})
            return httpx.Response(200, content=data, headers={"Content-Type": "application/pdf"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            body, metadata = citation.download(client, "https://example.org/start", sleep=lambda _: None)
        self.assertEqual(body, data)
        self.assertEqual(metadata["pdf_pages"], 1)
        self.assertEqual(len(calls), 3)
        for response in (httpx.Response(200, content=b"<html>Denied</html>"), httpx.Response(403)):
            with self.subTest(response=response):
                with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
                    with self.assertRaises((ValueError, httpx.HTTPError)):
                        citation.download(client, "https://example.org/fail", sleep=lambda _: None)

    def test_acquisition_resume_file_dedup_and_complete_export_verification(self):
        def handler(request):
            response = self.handler(request)
            if request.url.params.get("filter", "").startswith("openalex_id:"):
                payload = response.json()
                for row in payload["results"]:
                    if row["id"] == "https://openalex.org/W100":
                        row["locations"] = [{"license": "cc-by", "pdf_url": "https://example.org/main.pdf"}]
                return httpx.Response(200, json=payload)
            return response
        self.discover(handler)
        citation.publish_metadata(self.root / "run")
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Authored fictional PDF")
            data = document.tobytes()
        with patch.object(citation, "download", return_value=(data, {"pdf_pages": 1, "final_url": "https://example.org/main.pdf"})) as mocked:
            with redirect_stderr(io.StringIO()):
                counts = citation.acquire(self.root / "run", self.root / "pdfs")
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(counts["downloaded"], 1)
        self.assertEqual(counts["existing_seed_collection"], 1)
        report = citation.verify_and_export(self.root / "run", self.root / "pdfs")
        self.assertEqual(report["unique_pdf_files"], 1)
        self.assertEqual(report["first_tier_records"], 4)
        self.assertEqual(report["unresolved_reference_occurrences"], 1)
        self.assertEqual(report["provider_identifier_only_records"], 0)
        with (self.root / "run" / "bibliography.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 4)
        self.assertTrue(any(row["unstructured_references"] for row in rows))
        with patch.object(citation, "download", side_effect=AssertionError("Resume redownloaded")):
            self.assertEqual(citation.acquire(self.root / "run", self.root / "pdfs"), counts)
        acquired = next((self.root / "pdfs").glob("*.pdf"))
        self.assertEqual(citation.save_pdf(self.root / "pdfs", data, catalog.file_digest(acquired), {}),
                         acquired.name)
        acquired.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            citation.acquire(self.root / "run", self.root / "pdfs")

    def test_official_pmc_distribution_requires_explicit_license_and_matching_identity(self):
        metadata = {"pmcid": "PMC123", "version": 1, "doi": "10.0000/paper-100",
                    "is_pmc_openaccess": True, "license_code": "CC BY",
                    "pdf_url": "s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf"}
        def handler(request):
            if request.url.path.endswith(".json"):
                return httpx.Response(200, json=metadata)
            return httpx.Response(200, text="""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
                <IsTruncated>false</IsTruncated><Contents><Key>PMC123.1/PMC123.1.json</Key></Contents>
                </ListBucketResult>""")
        candidate = {"url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/", "license": "cc-by"}
        record = {"aliases": ["doi:10.0000/paper-100"]}
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = citation.official_pmc_location(client, candidate, record, lambda: None)
            self.assertEqual(result["download_url"],
                             "https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/PMC123.1.pdf")
            self.assertEqual(result["official_pmc"]["license"], "cc-by")
            metadata["license_code"] = "UNKNOWN"
            with self.assertRaisesRegex(ValueError, "license"):
                citation.official_pmc_location(client, candidate, record, lambda: None)
            metadata["license_code"] = "CC BY"
            metadata["doi"] = "10.0000/different"
            with self.assertRaisesRegex(ValueError, "identifier mismatch"):
                citation.official_pmc_location(client, candidate, record, lambda: None)
        self.assertIsNone(citation.pmcid("https://example.org/PMC123/"))
        self.assertIsNone(citation.pmcid("https://pmc.ncbi.nlm.nih.gov.evil.test/PMC123/"))

    def test_identity_review_blocks_automatic_acquisition(self):
        record = {"id": "record:fictional", "title": "", "citation_url": "https://example.org",
                  "istopmm_overlap": "not_matched", "identity_status": "needs_review"}
        with patch.object(citation, "download", side_effect=AssertionError("Unsafe acquisition")):
            result, data = citation.acquire_one(None, record, [{"url": "https://example.org"}], lambda: None)
        self.assertEqual(result["status"], "identity_review_required")
        self.assertIsNone(data)

    def test_provider_work_id_is_not_an_unresolved_citation_occurrence(self):
        record = {"aliases": [], "provenance": [{"provider_record": "openalex:WORK:W123"}]}
        self.assertEqual(citation.bibliographic_resolution(record), "provider_work_identifier_only")
        record["provenance"][0]["provider_record"] = "crossref:REFERENCE:fictional"
        self.assertEqual(citation.bibliographic_resolution(record), "unresolved_reference_occurrence")
        record["aliases"] = ["doi:10.0000/fictional"]
        self.assertEqual(citation.bibliographic_resolution(record), "strong_identifier")


if __name__ == "__main__":
    unittest.main()
