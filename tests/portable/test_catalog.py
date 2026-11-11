from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from jsonschema import ValidationError


from research_library import catalog, collections, contracts, literature, releases, retrieval, web_server
from fixtures import snapshots
from test_collections import collection_cases, registry_fixture
from test_http import running_server
from test_literature import epmc_page, epmc_record, pubmed_record


def run_fixture(root, name="run", records=None, provider="europepmc", disease="mgus"):
    records = records if records is not None else [epmc_record()]
    def handler(request):
        if provider == "europepmc":
            return httpx.Response(200, json=epmc_page(records, total=len(records)))
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {
                "count": str(len(records)), "idlist": [r["uid"] for r in records]}})
        return httpx.Response(200, json={"result": {
            "uids": [r["uid"] for r in records], **{r["uid"]: r for r in records}}})
    with closing(literature.MetadataClient(transport=httpx.MockTransport(handler), sleep=lambda value: None)) as client:
        literature.discover(root / name, provider=provider, disease=disease, through="2026-10-19",
                            page_size=100, client=client)
    return root / name


def catalog_fixture(root):
    run = run_fixture(root)
    document = catalog.build([run], as_of="2026-10-19", rationale="Authored synthetic test metadata.",
                             authorization_basis="Authored fictional fixture.", istopmm_dois=["10.0000/fictional"])
    path = root / "catalog.json"
    path.write_bytes(releases.encode(document))
    return document, path


def mixed_fixture(root):
    store, first, _, _ = registry_fixture(root)
    document, path = catalog_fixture(root)
    config = deepcopy(store.current().document)
    entry = config["collections"][1]
    entry.update(kind="catalog", release_id=catalog.install(store.root, path), publications={})
    store.publish(config)
    return store, document, path, first


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_deterministic_dedup_provenance_and_overlap(self):
        first = run_fixture(self.root)
        second = run_fixture(self.root, "pubmed", [pubmed_record()], provider="pubmed", disease="smm")
        args = dict(as_of="2026-10-19", rationale="Test starter", authorization_basis="Fictional")
        doc = catalog.build([first, second, first], istopmm_dois=["10.0000/fictional"], **args)
        self.assertEqual(doc, catalog.build([second, first], istopmm_dois=["10.0000/fictional"], **args))
        self.assertEqual(len(doc["publications"]), 1)
        record = doc["publications"][0]
        self.assertEqual(record["id"], "doi:10.0000/fictional")
        self.assertEqual(record["diseases"], ["mgus", "smm"])
        self.assertEqual(record["istopmm_overlap"], "matched_doi")
        self.assertEqual(len(record["provenance"]), 2)
        self.assertTrue(all(p["requests"][0]["response_sha256"] for p in record["provenance"]))
        counts = catalog.coverage(doc)
        self.assertEqual(counts["duplicates_collapsed"], 1)
        self.assertEqual(counts["full_text_acquired_count"], 0)
        self.assertEqual(counts["unreviewed_count"], 1)
        self.assertEqual(record["availability"]["full_text_acquired"], False)

    def test_collisions_conflicts_unknown_ids_and_notices(self):
        record = epmc_record()
        record["doi"] = "10.0000/first"
        first = run_fixture(self.root, records=[record])
        other = pubmed_record()
        other["articleids"][0]["value"] = "10.0000/second"
        second = run_fixture(self.root, "second", [other], provider="pubmed")
        doc = catalog.build([first, second], as_of="2026-10-19", rationale="Test", authorization_basis="Fictional")
        self.assertEqual(len(doc["publications"]), 2)
        self.assertTrue(all(r["id"].startswith("record:") for r in doc["publications"]))
        self.assertTrue(all(r["identity_status"] == "needs_review" for r in doc["publications"]))
        with self.assertRaises(retrieval.RetrievalError):
            catalog.CatalogSnapshot(doc).get("pmid:123")
        notice = epmc_record("456", ["Published Erratum"])
        notice["doi"] = "10.0000/correction"
        notice["commentCorrectionList"] = {"commentCorrection": [
            {"type": "ErratumFor", "source": "MED", "id": "123"}]}
        normalized = literature.normalize("europepmc", notice, "mm")
        self.assertEqual(normalized["relations"][0]["kind"], "corrects")
        for kind in catalog.RELATIONS:
            modified = deepcopy(doc)
            modified["publications"][0]["relations"] = [{
                "kind": kind, "target": "pmid:123", "provenance": "Authored fixture"}]
            catalog.validate(modified)
        no_id = epmc_record("999")
        no_id.pop("doi")
        no_id.pop("pmid")
        third = run_fixture(self.root, "third", [no_id])
        unknown = catalog.build([third], as_of="2026-10-19", rationale="Test", authorization_basis="Fictional")
        self.assertTrue(unknown["publications"][0]["id"].startswith("record:"))

    def test_tamper_dangling_provenance_and_byte_limits(self):
        document, path = catalog_fixture(self.root)
        invalid = deepcopy(document)
        invalid["publications"][0]["availability"]["full_text_acquired"] = True
        with self.assertRaises(ValidationError):
            catalog.validate(invalid)
        invalid = deepcopy(document)
        invalid["publications"][0]["provenance"][0]["run_id"] = "0" * 64
        with self.assertRaises(releases.ReleaseError):
            catalog.validate(invalid)
        target = self.root / "store"
        identifier = catalog.install(target, path)
        snapshot = catalog.CatalogSnapshot(document)
        saved = target / "catalogs" / (identifier + ".json")
        snapshot.verify_backing(saved)
        saved.write_text("{}")
        with self.assertRaises(releases.ReleaseError):
            snapshot.verify_backing(saved)
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(releases.ReleaseError):
            catalog.read(link)

    def test_mixed_registry_isolation_status_and_catalog_updates(self):
        store, doc, path, first = mixed_fixture(self.root)
        before = snapshots(first)
        selected = {"collections": ["test-literature"]}
        response = store.call("list_publications", selected)
        sid = response["release_id"]
        self.assertEqual(len(response["result"]["items"]), 1)
        self.assertEqual(store.call("list_publications", {})["result"]["items"], [])
        self.assertEqual(store.call("search_evidence", {**selected, "query": "Fictional"})["result"]["items"], [])
        self.assertEqual(store.call("list_sources", selected)["result"]["items"], [])
        both = {"collections": ["istopmm", "test-literature"]}
        self.assertEqual(store.call("corpus_status", both)["result"]["paper_count"], 1)
        self.assertEqual(store.call("catalog_status", both)["result"]["items"][0]["publication_count"], 1)
        item = store.call("get_publication", {**selected, "snapshot_id": sid,
                                            "publication_id": "pmid:123"})["result"]
        self.assertEqual(item["id"], doc["publications"][0]["id"])
        for arguments in ({"query": "---"}, {"disease": "smm"}, {"query": "no-match"}):
            if arguments.get("query") == "---":
                with self.assertRaises(retrieval.RetrievalError):
                    store.call("list_publications", {**selected, **arguments})
            else:
                self.assertEqual(store.call("list_publications", {**selected, **arguments})["result"]["items"], [])
        self.assertEqual(len(store.call("list_publications", {**selected, "query": "Fictional",
                                                              "publication_type": "Journal Article",
                                                              "eligibility": "included"})["result"]["items"]), 1)
        changed = deepcopy(doc)
        changed["selection_rationale"] = "A new provisional selection"
        replacement = self.root / "replacement.json"
        replacement.write_bytes(releases.encode(changed))
        updated = store.replace("test-literature", replacement, version="2", updated_at="2026-10-19")
        self.assertNotEqual(updated, sid)
        self.assertEqual(store.call("list_publications", {**selected, "snapshot_id": sid}), response)
        self.assertEqual(store.rollback(), sid)
        self.assertEqual(snapshots(first), before)
        restricted = collections.CollectionStore(store.root, allowed_profiles={"synthetic"},
                                                allowed_collections={"istopmm"})
        with self.assertRaises(releases.AccessDeniedError):
            restricted.call("list_publications", selected)

    def test_catalog_pagination_and_duplicates_between_memberships(self):
        record = epmc_record()
        other = epmc_record("456")
        other["doi"] = "10.0000/second"
        run = run_fixture(self.root, records=[record, other])
        doc = catalog.build([run], as_of="2026-10-19", rationale="Test", authorization_basis="Fictional")
        path = self.root / "metadata.json"
        path.write_bytes(releases.encode(doc))
        store = collections.CollectionStore(self.root / "store", allowed_profiles={"synthetic"},
                                            allowed_collections={"first", "second"})
        release_id = catalog.install(store.root, path)
        entries = [{"id": cid, "kind": "catalog", "title": cid, "version": "1", "updated_at": "2026-10-19",
                    "coverage": "Fictional metadata", "release_id": release_id, "publications": {}}
                   for cid in ("first", "second")]
        sid = store.publish({"schema_version": 1, "collections": entries})
        first = store.call("list_publications", {"limit": 1})
        self.assertEqual(len(first["result"]["items"]), 1)
        self.assertEqual(len(first["collection_context"]["evidence_refs"]), 2)
        self.assertEqual(first["result"]["next_offset"], 1)
        with self.assertRaises(retrieval.RetrievalError):
            store.call("list_publications", {"offset": 1})
        second = store.call("list_publications", {"offset": 1, "snapshot_id": sid})
        self.assertEqual(len(second["result"]["items"]), 1)
        self.assertIsNone(second["result"]["next_offset"])
        self.assertNotEqual(first["result"]["items"][0]["id"], second["result"]["items"][0]["id"])

    def test_cli_reproducible_catalog_and_explicit_relationships(self):
        run = run_fixture(self.root)
        relations = [{"source": "doi:10.0000/fictional", "kind": kind, "target": "pmid:456",
                      "provenance": "Authored fictional relationship"}
                     for kind in sorted(catalog.RELATIONS)]
        links = self.root / "links.json"
        links.write_text(json.dumps(relations))
        destination = self.root / "export.json"
        command = [sys.executable, "-m", "research_library.literature", "catalog", "--run", str(run),
                   "--as-of", "2026-10-19", "--rationale", "Fixture", "--metadata-authorization-basis",
                   "Authored fixture", "--relationships", str(links), "--output", str(destination)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        coverage = json.loads(result.stdout)
        self.assertEqual(coverage["unresolved_links"], 1)
        document = catalog.read(destination)
        self.assertEqual(len(document["publications"][0]["relations"]), 5)
        report = subprocess.run([sys.executable, "-m", "research_library.literature",
                                 "catalog-report", str(destination)], capture_output=True, text=True)
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(json.loads(report.stdout), coverage)
        rerun = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(rerun.returncode, 1)
        self.assertEqual(catalog.read(destination), document)
        sqlite_path = self.root / "export.sqlite"
        sqlite_command = [*command[:-1], str(sqlite_path)]
        sql_result = subprocess.run(sqlite_command, capture_output=True, text=True)
        self.assertEqual(sql_result.returncode, 0, sql_result.stderr)
        sqlite_snapshot = catalog.open_snapshot(sqlite_path, validate_records=True)
        self.assertEqual(sqlite_snapshot.get(document["publications"][0]["id"]), document["publications"][0])
        sqlite_report = subprocess.run([sys.executable, "-m", "research_library.literature",
                                        "catalog-report", str(sqlite_path)], capture_output=True, text=True)
        self.assertEqual(sqlite_report.returncode, 0, sqlite_report.stderr)
        self.assertEqual(json.loads(sqlite_report.stdout), json.loads(sql_result.stdout))
        # Journal containers cannot silently normalize malformed upstream data.
        value = epmc_record()
        value["journalInfo"] = []
        with self.assertRaises(literature.DiscoveryError):
            literature.normalize("europepmc", value, "mgus")

    def test_mixed_stdio_http_and_website(self):
        self.check_mixed_transports()

    def test_sqlite_mixed_stdio_http_and_website(self):
        self.check_mixed_transports(sqlite=True)

    def check_mixed_transports(self, sqlite=False):
        store, doc, path, _ = mixed_fixture(self.root)
        if sqlite:
            path = self.root / "catalog.sqlite"
            catalog.build([self.root / "run"], as_of=doc["as_of"], rationale=doc["selection_rationale"],
                          authorization_basis=doc["metadata_authorization_basis"],
                          istopmm_dois=["10.0000/fictional"], sqlite_output=path)
            store.replace("test-literature", path, version="sqlite-1", updated_at="2026-10-19")
        expected = collection_cases(store)
        pinned = {"snapshot_id": store.current().release_id, "collections": ["test-literature"]}
        for tool, args in (("list_publications", pinned), ("catalog_status", pinned),
                           ("get_publication", {**pinned, "publication_id": doc["publications"][0]["id"]})):
            expected["cases"].append({"tool": tool, "arguments": args, "response": store.call(tool, args)})
        output = self.root / "expected.json"
        output.write_text(json.dumps(expected))
        tests = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(tests / "mcp_client.py"), "--server",
                                 str(Path(sys.executable).parent / "research-mcp"),
                                 "--registry", str(store.root), "--allow-collection", "istopmm",
                                 "--allow-collection", "test-literature", "--default-collection", "istopmm",
                                 "--expected", str(output), "--cwd", str(self.root)],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with running_server(self.root, None, collection_registry=store.root,
                            allowed_collections=["istopmm", "test-literature"],
                            default_collections=["istopmm"]) as (url, token, _, _, _):
            result = subprocess.run([sys.executable, str(tests / "http_client.py"), "--url", url,
                                     "--token-file", str(self.root / "token"), "--expected", str(output)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        app = web_server.create_app(registry=store.root, profiles={"synthetic"},
                                     allowed_collections=["istopmm", "test-literature"])
        with httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            page = client.get("/publications?collection=test-literature")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Authored fictional", page.text)
            self.assertIn("not_reviewed", page.text)
            self.assertEqual(client.get("/coverage?collection=test-literature").status_code, 200)

    def test_sqlite_json_parity_rollback_and_page_materialization(self):
        store, _, _, first = mixed_fixture(self.root)
        before = snapshots(first)
        records = [epmc_record(), epmc_record("456")]
        records[1]["doi"] = "10.0000/second"
        records[1]["title"] = "Risque café Straße foo_bar 123"
        run = run_fixture(self.root, "two-records", records)
        args = dict(as_of="2026-10-19", rationale="Fictional", authorization_basis="Authored fixtures")
        document = catalog.build([run], **args)
        path = self.root / "large.sqlite"
        result = catalog.build([run, run], sqlite_output=path, **args)
        snapshot = catalog.open_snapshot(path, validate_records=True)
        other = self.root / "deterministic.sqlite"
        self.assertEqual(result, catalog.build([run], sqlite_output=other, **args))
        self.assertEqual(path.read_bytes(), other.read_bytes())
        expected = catalog.coverage(document)
        expected["catalog_id"] = result["catalog_id"]
        self.assertEqual(result, expected)
        json_snapshot = catalog.CatalogSnapshot(document)
        self.assertEqual(list(snapshot.identities()), list(json_snapshot.identities()))
        for query in ("", "foo_bar", "café", "STRASSE", "123", "foo", "risk", "10.0000/second"):
            arguments = {"query": query}
            self.assertEqual(snapshot.matching_ids(arguments), json_snapshot.matching_ids(arguments))
        for arguments in ({"disease": "smm"}, {"eligibility": "included"},
                          {"publication_type": "Journal Article", "query": "Fictional"}):
            self.assertEqual(snapshot.matching_ids(arguments), json_snapshot.matching_ids(arguments))
        with self.assertRaises(retrieval.RetrievalError):
            snapshot.matching_ids({"query": "---"})
        for record in document["publications"]:
            self.assertEqual(snapshot.get(record["id"]), record)
            for identifier in record["aliases"]:
                self.assertEqual(snapshot.get(identifier), record)
        old = store.current().release_id
        new = store.replace("test-literature", path, version="sqlite", updated_at="2026-10-19")
        selected = {"collections": ["test-literature"]}
        served = store.current().snapshots["test-literature"]
        with patch.object(served, "get", wraps=served.get) as get:
            page = store.call("list_publications", {**selected, "limit": 1})
            self.assertEqual(get.call_count, 1)
        self.assertEqual(page["result"]["next_offset"], 1)
        with patch.object(served, "matching_ids", return_value=served.publication_keys()):
            with self.assertRaisesRegex(releases.ReleaseError, "offset ceiling"):
                store.call("list_publications", {**selected, "snapshot_id": new,
                                                 "offset": retrieval.MAX_OFFSET, "limit": 1})
        following = store.call("list_publications", {**selected, "snapshot_id": new, "offset": 1, "limit": 1})
        self.assertEqual(following["result"]["items"], document["publications"][1:])
        self.assertEqual(store.call("list_sources", selected)["result"]["items"], [])
        self.assertEqual(store.call("search_evidence", {**selected, "query": "fictional"})["result"]["items"], [])
        self.assertEqual(store.rollback(), old)
        self.assertEqual(store.call("list_publications", {**selected, "snapshot_id": new}),
                         store.load(new).call("list_publications", selected))
        self.assertEqual(snapshots(first), before)
        with self.assertRaises(releases.ReleaseError):
            catalog.build([run], sqlite_output=path, **args)
        stored = catalog.stored_path(store.root, result["catalog_id"])
        with stored.open("r+b") as stream:
            stream.seek(-1, 2)
            value = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([value[0] ^ 1]))
        with self.assertRaises(releases.ReleaseError):
            store.load(new)

    def test_sqlite_ambiguous_aliases_and_invalid_storage(self):
        one = epmc_record()
        one["doi"] = "10.0000/first"
        two = pubmed_record()
        two["articleids"][0]["value"] = "10.0000/second"
        runs = [run_fixture(self.root, records=[one]),
                run_fixture(self.root, "pubmed", [two], provider="pubmed")]
        path = self.root / "ambiguous.sqlite"
        catalog.build(runs, as_of="2026-10-19", rationale="Fictional",
                      authorization_basis="Authored fixture", sqlite_output=path)
        snapshot = catalog.open_snapshot(path, validate_records=True)
        self.assertTrue(all(not aliases for _, aliases in snapshot.identities()))
        with self.assertRaises(retrieval.RetrievalError):
            snapshot.get("pmid:123")
        with self.assertRaises(retrieval.NotFoundError):
            snapshot.get("missing")
        link = self.root / "link.sqlite"
        link.symlink_to(path)
        with self.assertRaises(releases.ReleaseError):
            catalog.open_snapshot(link)
        corrupt = self.root / "invalid.sqlite"
        corrupt.write_bytes(b"not a database")
        with self.assertRaises(releases.ReleaseError):
            catalog.open_snapshot(corrupt)
        broken_index = self.root / "broken-index.sqlite"
        broken_index.write_bytes(path.read_bytes())
        with closing(sqlite3.connect(broken_index)) as connection:
            connection.execute("DELETE FROM publication_search")
            connection.commit()
        with self.assertRaisesRegex(releases.ReleaseError, "full-text index"):
            catalog.install(self.root / "invalid-index-store", broken_index)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE publications SET eligibility='included'")
            connection.commit()
        with self.assertRaises(releases.ReleaseError):
            catalog.install(self.root / "invalid-store", path)

    def test_malformed_provider_doi_is_audited_not_repaired_or_used_as_identity(self):
        record = epmc_record()
        record["doi"] = "10.123/provider-typo"
        run = run_fixture(self.root, records=[record])
        args = dict(as_of="2026-10-19", rationale="Fictional", authorization_basis="Authored fixture")
        document = catalog.build([run], **args)
        item = document["publications"][0]
        self.assertEqual(item["id"], "pmid:123")
        self.assertNotIn("doi:10.123/provider-typo", item["aliases"])
        self.assertEqual(item["identity_status"], "needs_review")
        self.assertEqual(item["eligibility"], "needs_review")
        self.assertEqual(item["provenance"][0]["metadata"]["identifiers"]["doi"], "10.123/provider-typo")
        self.assertEqual(document["issues"][0]["kind"], "invalid_identifier")
        self.assertEqual(catalog.coverage(document)["identity_issues"], 1)
        path = self.root / "invalid-identifier.sqlite"
        catalog.build([run], sqlite_output=path, **args)
        sql = catalog.open_snapshot(path, validate_records=True)
        self.assertEqual(sql.get("pmid:123"), item)
        self.assertEqual(list(sql.identities()), [("pmid:123", [])])

    def test_sqlite_exceeds_json_byte_limit_without_larger_tool_responses(self):
        run = run_fixture(self.root)
        groups, runs = catalog.load_occurrences([run])
        original = next(iter(groups.values()))[0]
        path = self.root / "field.sqlite"
        with closing(sqlite3.connect(self.root / "spool.sqlite")) as connection:
            spool = catalog.Occurrences(connection)
            for index in range(4200):
                occurrence = deepcopy(original)
                record = occurrence["record"]
                record["provider_id"] = str(index + 1)
                record["identifiers"] = {"pmid": str(index + 1)}
                record["title"] = "x" * 4096
                spool.add(occurrence)
            result = catalog.write_sqlite(
                path, spool, runs, as_of="2026-10-19", rationale="Authored scale fixture",
                authorization_basis="Fictional", istopmm_dois=(), relationships=())
        self.assertGreater(path.stat().st_size, catalog.MAX_CATALOG_BYTES)
        snapshot = catalog.open_snapshot(path, validate_records=True)
        self.assertEqual(result["publication_count"], 4200)
        self.assertEqual(len(snapshot.matching_ids({})), 4200)
        self.assertLess(len(releases.encode(snapshot.get("pmid:1"))), contracts.MAX_RESPONSE_BYTES)
