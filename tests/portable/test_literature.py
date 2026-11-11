from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import httpx

from research_library import literature, releases


def epmc_record(pid="123", types=None):
    return {
        "id": pid, "source": "MED", "pmid": pid, "doi": "10.0000/FICTIONAL",
        "title": "Authored fictional myeloma publication", "pubYear": "2025",
        "pubTypeList": {"pubType": types if types is not None else ["Journal Article"]},
        "language": "eng", "authorString": "Fictional A", "isOpenAccess": "Y",
    }


def epmc_page(records, total=2, mark="next"):
    return {"hitCount": total, "nextCursorMark": mark, "resultList": {"result": records}}


def pubmed_record(pid="123"):
    return {
        "uid": pid, "title": "Authored fictional myeloma publication",
        "pubtype": ["Journal Article"], "lang": ["eng"], "pubdate": "2025 Jan",
        "authors": [{"name": "Fictional A"}],
        "articleids": [{"idtype": "doi", "value": "10.0000/FICTIONAL"}],
        "attributes": ["Has Abstract"],
    }


class LiteratureTests(unittest.TestCase):
    def test_bibliography_records_metadata_origin_without_claiming_full_text(self):
        client, _ = self.client(lambda request: httpx.Response(
            200, json=epmc_page([epmc_record(types=["Case Reports"])], total=1)))
        self.discover(client)
        result = literature.bibliography(self.root / "run")
        self.assertEqual(result["record_count"], 1)
        source = result["items"][0]
        self.assertEqual(source["eligibility"]["decision"], "excluded")
        self.assertEqual(source["identifiers"]["doi"], "10.0000/fictional")
        self.assertFalse(source["used_as_evidence"])
        self.assertFalse(source["full_text_acquired"])
        self.assertIsNone(source["full_text_acquisition"])
        self.assertTrue(source["metadata_acquisition"][0]["url"].startswith(
            "https://www.ebi.ac.uk/europepmc/"))
        exported = subprocess.run([sys.executable, "-m", "research_library.literature",
                                   "bibliography", "--run", str(self.root / "run")],
                                  capture_output=True, text=True)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(json.loads((self.root / "run" / "bibliography.json").read_text()), result)
        (self.root / "run" / "page-000000.json").write_text("{}")
        with self.assertRaisesRegex(literature.DiscoveryError, "checksum"):
            literature.bibliography(self.root / "run")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def client(self, handler):
        delays = []
        client = literature.MetadataClient(
            sleep=delays.append, clock=lambda: 0, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client, delays

    def discover(self, client, **kwargs):
        return literature.discover(self.root / "run", client=client, provider="europepmc",
                                   disease="mgus", through="2026-10-19", page_size=1, **kwargs)

    def test_approved_policy_and_frozen_publication_type_references(self):
        # Minimal public bibliographic facts checked 2026-10-19; not scientific references.
        known_types = [
            ("pmid:42571962", ["Journal Article", "Review"], "included"),
            ("pmid:42431702", ["Journal Article", "Case Reports"], "excluded"),
        ]
        for pid, types, expected in known_types:
            with self.subTest(pid=pid):
                self.assertEqual(literature.eligibility(types)["decision"], expected)
        for types, expected in (
            (["Practice Guideline"], "included"), (["Preprint", "Journal Article"], "excluded"),
            (["Conference Abstract"], "excluded"), (["Letter", "Journal Article"], "excluded"),
            (["Published Erratum"], "excluded"), (["Retracted Publication"], "excluded"),
            (["Study Protocol"], "excluded"), ([], "needs_review"), (["Unknown"], "needs_review"),
        ):
            self.assertEqual(literature.eligibility(types)["decision"], expected)
        policy = literature.policy()
        self.assertEqual(policy["languages"], "all")
        self.assertEqual(policy["preprints"], "excluded")
        for disease in ("mgus", "smm", "mm"):
            for provider in ("pubmed", "europepmc"):
                query = literature.query(provider, disease, "1000-01-01", "2026-10-19")
                self.assertIn("2026-10-19", query)
                self.assertNotIn("language", query.lower())
        self.assertIn("smouldering", literature.query("pubmed", "smm", "2020-01-01", "2026-10-19"))
        self.assertNotIn('"MM"', literature.query("europepmc", "mm", "2020-01-01", "2026-10-19"))
        self.assertNotIn("MESH:", literature.query("europepmc", "mm", "2020-01-01", "2026-10-19"))
        self.assertNotIn("MESH:", literature.query("europepmc", "mgus", "2020-01-01", "2026-10-19"))
        self.assertIn("[MeSH Terms]", literature.query("pubmed", "mm", "2020-01-01", "2026-10-19"))

    def test_resume_pagination_provenance_and_idempotent_completed_run(self):
        seen = []

        def handler(request):
            seen.append(request.url.params["cursorMark"])
            records = [epmc_record("123" if len(seen) == 1 else "456")]
            return httpx.Response(200, json=epmc_page(records, mark="next" if len(seen) == 1 else "last"))

        client, delays = self.client(handler)
        first = self.discover(client, max_pages=1)
        self.assertFalse(first["complete"])
        self.assertEqual(first["discovered_unique"], 1)
        second = self.discover(client, max_pages=1)
        self.assertTrue(second["complete"])
        self.assertEqual(second["included"], 2)
        self.assertEqual(seen, ["*", "next"])
        self.assertTrue(delays)
        before = (self.root / "run/page-000000.json").read_bytes()
        self.assertTrue(self.discover(client)["complete"])
        self.assertEqual(len(seen), 2)
        self.assertEqual((self.root / "run/page-000000.json").read_bytes(), before)
        page = json.loads(before)
        self.assertEqual(releases.sha256(page["requests"][0]["raw"].encode()), page["requests"][0]["sha256"])
        self.assertIn("TITLE_ABS", page["requests"][0]["url"])
        for record in page["records"]:
            self.assertFalse(record["availability"]["redistribution_authorized"])
            self.assertFalse(record["availability"]["full_text_acquired"])
            self.assertEqual(record["extraction_status"], "not_extracted")
            self.assertEqual(record["study_design"], ["unknown"])

    def test_upstream_failure_retains_checkpoint_and_can_resume(self):
        failed = [False]

        def handler(request):
            if request.url.params["cursorMark"] == "*":
                return httpx.Response(200, json=epmc_page([epmc_record("123")]))
            if not failed[0]:
                return httpx.Response(503)
            return httpx.Response(200, json=epmc_page([epmc_record("456")], mark="last"))

        client, _ = self.client(handler)
        with self.assertRaisesRegex(literature.DiscoveryError, "bounded retries"):
            self.discover(client, max_pages=2)
        report = literature.report(self.root / "run")
        self.assertFalse(report["complete"])
        self.assertEqual(report["pages"], 1)
        self.assertEqual(report["discovered_unique"], 1)
        self.assertEqual(report["last_error"], "DiscoveryError")
        failed[0] = True
        self.assertTrue(self.discover(client)["complete"])

    def test_provider_schema_cursor_drift_and_checksum_errors_are_explicit(self):
        for payload in (
            {"error": "provider error"},
            {"hitCount": 1, "resultList": {}},
            {"hitCount": 1, "resultList": None},
            epmc_page([], total=1),
            epmc_page([{"id": "123", "source": "MED", "title": 42}], total=1),
            epmc_page([epmc_record()], total=3, mark="*"),
        ):
            with self.subTest(payload=payload):
                client, _ = self.client(lambda request: httpx.Response(200, json=payload))
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    literature.fetch_page(client, {
                        "provider": "europepmc", "disease": "mgus", "page_size": 1,
                        "from_date": "2020-01-01", "through_date": "2026-10-19",
                    }, {"mark": "*", "seen": 0, "total": None})
        client, _ = self.client(lambda request: httpx.Response(
            200, json=epmc_page([epmc_record()], total=1)))
        self.discover(client)
        path = self.root / "run/page-000000.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "checksum"):
            literature.report(self.root / "run")

    def test_rate_limit_retry_after_and_response_byte_bounds(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return (httpx.Response(429, headers={"Retry-After": "2"}) if len(attempts) == 1
                    else httpx.Response(200, json={"ok": True}))

        client, delays = self.client(handler)
        self.assertEqual(client.get("https://example.test/metadata", {})[0], {"ok": True})
        self.assertIn(2, delays)
        long, _ = self.client(lambda request: httpx.Response(429, headers={"Retry-After": "3600"}))
        with self.assertRaisesRegex(ValueError, "resume"):
            long.get("https://example.test/metadata", {})
        invalid, _ = self.client(lambda request: httpx.Response(200, content=b"{broken"))
        with self.assertRaisesRegex(ValueError, "JSON"):
            invalid.get("https://example.test/metadata", {})
        oversized, _ = self.client(lambda request: httpx.Response(
            200, content=b"x" * (literature.MAX_RESPONSE_BYTES + 1)))
        with self.assertRaisesRegex(ValueError, "byte limit"):
            oversized.get("https://example.test/metadata", {})

    def test_transport_errors_and_http_denial_are_not_empty_success(self):
        def handler(request):
            raise httpx.ConnectError("Authored fixture transport failure")
        client, delays = self.client(handler)
        with self.assertRaisesRegex(ValueError, "transport failed"):
            self.discover(client)
        self.assertEqual(literature.report(self.root / "run")["discovered_unique"], 0)
        self.assertFalse(literature.report(self.root / "run")["complete"])
        client, _ = self.client(lambda request: httpx.Response(403))
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            client.get("https://example.test/metadata", {})

    def test_pubmed_summary_ids_missing_types_and_safe_normalization(self):
        record = pubmed_record()
        result = literature.normalize("pubmed", record, "mm")
        self.assertEqual(result["identifiers"]["doi"], "10.0000/fictional")
        self.assertTrue(result["availability"]["abstract_reported"])
        self.assertFalse(result["availability"]["full_text_reported"])
        del record["pubtype"]
        self.assertEqual(literature.normalize("pubmed", record, "mm")["eligibility"]["decision"], "needs_review")
        record["articleids"].append({"idtype": "doi", "value": "conflicting"})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            literature.normalize("pubmed", record, "mm")
        preprint = epmc_record()
        preprint["source"] = "PPR"
        self.assertEqual(literature.normalize("europepmc", preprint, "mm")["eligibility"]["decision"], "excluded")
        unknown_language = epmc_record()
        unknown_language["language"] = 42
        with self.assertRaises(ValueError):
            literature.normalize("europepmc", unknown_language, "mm")

    def test_pubmed_adaptive_date_shards_and_resume(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            if "esummary" in request.url.path:
                return httpx.Response(200, json={"result": {"uids": ["123"], "123": pubmed_record()}})
            term = request.url.params["term"]
            total = "12000" if "2020-01-01" in term and "2020-01-02" in term else "1"
            ids = [] if total == "12000" else ["123"]
            return httpx.Response(200, json={"esearchresult": {"count": total, "idlist": ids}})

        client, _ = self.client(handler)
        config = {"provider": "pubmed", "disease": "mm", "page_size": 1}
        records, provenance, cursor, done = literature.fetch_page(client, config, {"windows": [{
            "start": "2020-01-01", "end": "2020-01-02", "offset": 0, "total": None,
        }]})
        self.assertFalse(done)
        self.assertEqual(cursor["windows"][0]["start"], "2020-01-02")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(provenance), 3)
        record, _, next_cursor, done = literature.fetch_page(client, config, cursor)
        self.assertTrue(done)
        self.assertEqual(next_cursor["windows"], [])
        complete = literature.discover(
            self.root / "sharded", provider="pubmed", disease="mm", start="2020-01-01",
            through="2020-01-02", page_size=1, max_pages=2, client=client)
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["discovered_unique"], 1)
        self.assertEqual(complete["duplicates"], 1)
        state, keys, duplicates = literature.validate_run(self.root / "sharded", retain_metadata=False)
        self.assertEqual(len(keys), 1)
        self.assertEqual(duplicates, 1)
        self.assertEqual(state["status"], "complete")

    def test_missing_title_is_explicit_unreviewed_metadata_not_a_failed_import(self):
        for provider, record in (("pubmed", pubmed_record()), ("europepmc", epmc_record())):
            for title in (None, "", "  "):
                with self.subTest(provider=provider, title=title):
                    record["title"] = title
                    result = literature.normalize(provider, record, "mgus")
                    self.assertEqual(result["title"], "")
                    self.assertEqual(result["eligibility"]["decision"], "needs_review")
                    self.assertIn("no title", result["eligibility"]["reason"])
                    self.assertFalse(result["availability"]["full_text_acquired"])
            record["title"] = 42
            with self.assertRaises(literature.DiscoveryError):
                literature.normalize(provider, record, "mgus")
        excluded = epmc_record(types=["Case Reports"])
        excluded.pop("title")
        self.assertEqual(literature.normalize("europepmc", excluded, "mm")["eligibility"]["decision"], "excluded")

    def test_explicit_europepmc_date_order_is_pinned_on_resume(self):
        queries = []
        def handler(request):
            queries.append(request.url.params["query"])
            self.assertNotIn("sort", request.url.params)
            return httpx.Response(200, json=epmc_page([epmc_record()], total=1))
        client, _ = self.client(handler)
        kwargs = dict(provider="europepmc", disease="mm", through="2026-10-19", client=client)
        result = literature.discover(self.root / "dated", order="publication-date", **kwargs)
        self.assertTrue(result["complete"])
        self.assertTrue(queries[0].endswith(" sort_date:y"))
        with self.assertRaisesRegex(literature.DiscoveryError, "Resume configuration differs"):
            literature.discover(self.root / "dated", **kwargs)

    def test_pubmed_bad_summary_and_esearch_ceiling_fail_explicitly(self):
        for summary in ({"result": {}}, {"result": {"uids": ["123"], "123": pubmed_record("456")}}):
            client, _ = self.client(lambda request: httpx.Response(
                200, json=summary if "esummary" in request.url.path else
                {"esearchresult": {"count": "1", "idlist": ["123"]}}))
            with self.assertRaises(ValueError):
                literature.fetch_page(client, {"provider": "pubmed", "disease": "mm", "page_size": 1},
                                      {"windows": [{"start": "2020-01-01", "end": "2020-01-01",
                                                    "offset": 0, "total": None}]})
        client, _ = self.client(lambda request: httpx.Response(200, json={
            "esearchresult": {"count": "12000", "idlist": []}}))
        with self.assertRaisesRegex(ValueError, "one-day"):
            literature.fetch_page(client, {"provider": "pubmed", "disease": "mm", "page_size": 1},
                                  {"windows": [{"start": "2020-01-01", "end": "2020-01-01",
                                                "offset": 0, "total": None}]})

    def test_config_immutability_and_cli_incomplete_exit_status(self):
        client, _ = self.client(lambda request: httpx.Response(200, json=epmc_page([epmc_record()])))
        self.discover(client)
        with self.assertRaisesRegex(ValueError, "configuration"):
            literature.discover(self.root / "run", client=client, provider="europepmc",
                                disease="mm", through="2026-10-19", page_size=1)
        response = subprocess.run([
            sys.executable, "-m", "research_library.literature", "report",
            "--run", str(self.root / "run"),
        ], capture_output=True, text=True, cwd=self.root)
        self.assertEqual(response.returncode, 2, response.stderr)
        self.assertFalse(json.loads(response.stdout)["complete"])

    def test_duplicate_pages_and_changing_provider_counts_cannot_pass(self):
        for total in (2, 3):
            with tempfile.TemporaryDirectory() as temporary:
                requests = []
                def handler(request):
                    self.assertNotIn("sort", request.url.params)
                    requests.append(1)
                    return httpx.Response(200, json=epmc_page([epmc_record()],
                        total=2 if len(requests) == 1 else total, mark="next" if len(requests) == 1 else "last"))
                client, _ = self.client(handler)
                with self.assertRaises(ValueError):
                    literature.discover(Path(temporary) / "run", provider="europepmc", disease="mm",
                                        through="2026-10-19", page_size=1, max_pages=2, client=client)
                report = literature.report(Path(temporary) / "run")
                self.assertFalse(report["complete"])
                self.assertEqual(report["pages"], 1)

    def test_no_results_complete_and_boundaries_are_explicit(self):
        client, _ = self.client(lambda request: httpx.Response(200, json=epmc_page([], total=0)))
        report = self.discover(client)
        self.assertTrue(report["complete"])
        self.assertEqual(report["discovered_unique"], 0)
        for page_size, max_pages in ((0, 1), (101, 1), (True, 1), (1, 0), (1, 1001)):
            with self.assertRaises(ValueError):
                literature.discover(self.root / "invalid", provider="pubmed", disease="mm",
                                    through="2026-10-19", page_size=page_size, max_pages=max_pages, client=client)
