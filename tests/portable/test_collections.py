from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from research_library import collections, contracts, releases, retrieval
from fixtures import snapshots
from test_http import running_server
from test_releases import fixture_bundle


def registry_fixture(root):
    bundle, _ = fixture_bundle(root)
    first = root / "first"
    releases.export(bundle, first, profile="synthetic")
    other = deepcopy(bundle)
    other["units"][0]["text"] = "Independent screening observation."
    other["claims"][0]["statement"] = "An independently curated fictional interpretation."
    second = root / "second"
    releases.export(other, second, profile="synthetic")
    store = collections.CollectionStore(root / "registry", allowed_profiles={"synthetic"},
                                        allowed_collections={"istopmm", "test-literature"},
                                        defaults=["istopmm"])
    entries = []
    for cid, path in (("istopmm", first), ("test-literature", second)):
        entries.append({
            "id": cid, "title": "Synthetic fixture " + cid, "version": "test-1",
            "updated_at": "2026-10-19", "coverage": "Fictional test data, not real iStopMM.",
            "release_id": store.releases.install(path), "publications": {},
        })
    store.publish({"schema_version": 1, "collections": entries})
    return store, first, second, other


def collection_cases(store):
    sid = store.current().release_id
    pinned = {"collections": ["istopmm"], "snapshot_id": sid}
    calls = [
        ("list_collections", {}),
        ("corpus_status", {}),
        ("search_evidence", {"query": "screening", "limit": 1}),
        ("search_evidence", {"query": "screening", "collections": ["istopmm", "test-literature"]}),
        ("search_evidence", {"query": "screening", "collections": ["test-literature"]}),
        ("search_evidence", {"query": "screening", "offset": 1, "limit": 1, **pinned}),
        ("get_claim", {"claim_id": "fictional-claim", **pinned}),
        ("get_source_context", {"unit_id": "fictional-unit-1", "max_characters": 15, **pinned}),
        ("get_table", {"table_id": "fictional-table", "limit": 2, **pinned}),
        ("get_source_issue", {"issue_id": "fictional-issue", **pinned}),
        ("get_related_evidence", {"evidence_id": "fictional-claim", **pinned}),
        ("list_sources", {"collections": ["istopmm", "test-literature"], "limit": 2}),
        ("list_publications", {}),
        ("catalog_status", {}),
    ]
    return {
        "cases": [{"tool": tool, "arguments": args, "response": store.call(tool, args)} for tool, args in calls],
        "errors": [
            ["get_publication", {"publication_id": "missing", **pinned}, "not_found"],
            ["list_collections", {"collections": ["DO-NOT-ECHO"]}, "invalid_input"],
            ["search_evidence", {"query": "screening", "collections": ["secret"]}, "permission_denied"],
            ["get_claim", {"claim_id": "DO_NOT_ECHO", **pinned}, "not_found"],
            ["get_claim", {"claim_id": "fictional-claim"}, "invalid_input"],
            ["get_claim", {"claim_id": "fictional-claim", "snapshot_id": sid,
                           "collections": ["istopmm", "test-literature"]}, "invalid_input"],
            ["search_evidence", {"query": '"DO_NOT_ECHO', "mode": "fts5"}, "invalid_input"],
            ["search_evidence", {"query": "x", "snapshot_id": "0" * 64}, "unavailable"],
        ],
    }


class CollectionTests(unittest.TestCase):
    def test_source_inventory_pagination_scope_and_retained_snapshot(self):
        arguments = {"collections": ["istopmm", "test-literature"], "limit": 2}
        first = self.store.call("list_sources", arguments)
        sid = first["release_id"]
        items = []
        offset = 0
        while offset is not None:
            response = self.store.call("list_sources", {**arguments, "snapshot_id": sid, "offset": offset})
            refs = response["collection_context"]["evidence_refs"]
            for index, source in enumerate(response["result"]["items"]):
                self.assertEqual(refs[index]["record_id"], source["source_id"])
                self.assertEqual(refs[index]["publication_id"], source["publication_id"])
                items.append((refs[index]["collection_id"], source))
            offset = response["result"]["next_offset"]
        self.assertEqual(len(items), 6)
        self.assertEqual({cid for cid, _ in items}, {"istopmm", "test-literature"})
        with self.assertRaises(retrieval.RetrievalError):
            self.store.call("list_sources", {**arguments, "offset": 2})
        self.assertEqual(self.store.call("list_sources", {"paper": "10.0000/missing"})["result"]["items"], [])
        self.store.replace("test-literature", self.first, version="test-2", updated_at="2026-10-20")
        retained = self.store.call("list_sources", {**arguments, "snapshot_id": sid})
        self.assertEqual(retained, first)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store, self.first, self.second, self.other = registry_fixture(self.root)

    def test_default_scope_deduplication_and_stable_filtered_pagination(self):
        store = self.store
        before = snapshots(self.first)
        default = store.call("search_evidence", {"query": "screening"})
        self.assertEqual(len(default["result"]["items"]), 3)
        self.assertEqual({ref["collection_id"] for ref in default["collection_context"]["evidence_refs"]}, {"istopmm"})
        arguments = {"query": "screening", "collections": ["istopmm", "test-literature"]}
        combined = store.call("search_evidence", arguments)
        self.assertEqual(len(combined["result"]["items"]), 4)
        refs = combined["collection_context"]["evidence_refs"]
        self.assertEqual(len(refs), 6)
        self.assertTrue(any(sum(ref["result_index"] == i for ref in refs) == 2 for i in range(4)))
        sid = combined["collection_context"]["snapshot_id"]
        paged = [store.call("search_evidence", {**arguments, "snapshot_id": sid, "limit": 1, "offset": i})
                 ["result"]["items"][0] for i in range(4)]
        self.assertEqual(paged, combined["result"]["items"])
        self.assertIsNone(store.call("search_evidence", {**arguments, "snapshot_id": sid,
                                                       "limit": 1, "offset": 3})["result"]["next_offset"])
        second = store.call("search_evidence", {"query": "Independent", "collections": ["test-literature"]})
        self.assertEqual(len(second["result"]["items"]), 1)
        self.assertEqual(store.call("search_evidence", {"query": "Independent"})["result"]["items"], [])
        # The shared paper has one identity but independent claim interpretations remain scoped.
        status = store.call("corpus_status", {"collections": arguments["collections"]})["result"]
        self.assertEqual(status["paper_count"], 1)
        self.assertEqual(status["claim_count"], 4)
        for cid, expected in (("istopmm", "Fictional"), ("test-literature", "independently")):
            response = store.call("get_claim", {"claim_id": "fictional-claim", "collections": [cid], "snapshot_id": sid})
            self.assertIn(expected, response["result"]["statement"])
        self.assertEqual(snapshots(self.first), before)

    def test_independent_activation_old_followup_and_rollback(self):
        store = self.store
        original = store.current()
        original_bytes = snapshots(self.first)
        updated = deepcopy(self.other)
        updated["claims"][0]["statement"] = "Updated fictional interpretation."
        path = self.root / "updated"
        releases.export(updated, path, profile="synthetic")
        replacement = store.replace("test-literature", path, version="test-2", updated_at="2026-10-20")
        self.assertNotEqual(original.release_id, replacement)
        self.assertEqual(store.current().snapshots["istopmm"].release_id, original.snapshots["istopmm"].release_id)
        args = {"claim_id": "fictional-claim", "collections": ["test-literature"],
                "snapshot_id": original.release_id}
        self.assertIn("independently", store.call("get_claim", args)["result"]["statement"])
        args["snapshot_id"] = replacement
        self.assertIn("Updated", store.call("get_claim", args)["result"]["statement"])
        self.assertEqual(store.rollback(), original.release_id)
        self.assertEqual(snapshots(self.first), original_bytes)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: store.call("corpus_status", {})["release_id"], range(8)))
        self.assertEqual(set(results), {original.release_id})
        with self.assertRaises(collections.SnapshotUnavailable):
            store.load("0" * 64)

    def test_rights_isolation_alias_conflicts_and_fail_closed_integrity(self):
        restricted = collections.CollectionStore(
            self.store.root, allowed_profiles={"synthetic"}, allowed_collections={"istopmm"})
        listed = restricted.call("list_collections", {})["result"]["items"]
        self.assertEqual([item["id"] for item in listed], ["istopmm"])
        for tool, args in (("list_collections", {}), ("get_claim", {"claim_id": "fictional-claim"})):
            with self.assertRaises(releases.AccessDeniedError):
                restricted.call(tool, {**args, "collections": ["test-literature"],
                                       "snapshot_id": restricted.current().release_id})
        document = deepcopy(self.store.current().document)
        document["collections"][0]["publications"] = {
            "10.0000/fictional": {"id": "paper-a", "aliases": ["pmid:123"]}}
        document["collections"][1]["publications"] = {
            "10.0000/fictional": {"id": "paper-b", "aliases": ["pmid:123"]}}
        before = self.store._state()
        with self.assertRaisesRegex(releases.ReleaseError, "Conflicting"):
            self.store.publish(document)
        self.assertEqual(self.store._state(), before)
        snapshot = self.store.current()
        backing = self.store.root / "releases" / snapshot.snapshots["test-literature"].release_id / "records.json"
        original = backing.read_bytes()
        backing.write_bytes(b"x" + original[1:])
        with self.assertRaises(releases.ReleaseError):
            self.store.call("corpus_status", {})
        # Corruption in a collection outside the endpoint's allow-list does not expose it or affect that endpoint.
        self.assertTrue(restricted.call("corpus_status", {})["ok"])
        backing.write_bytes(original)
        registry = self.store.root / "registries" / (snapshot.release_id + ".json")
        registry.write_bytes(b"{}")
        with self.assertRaises(releases.ReleaseError):
            self.store.current()

    def test_manifest_bounds_symlinks_legacy_selection_and_profile_denial(self):
        original = self.store.current().document
        for mutate in (
            lambda d: d.update(schema_version=True),
            lambda d: d["collections"].append(deepcopy(d["collections"][0])),
            lambda d: d["collections"][0].update(id="../escape"),
            lambda d: d["collections"][0].update(updated_at="yesterday"),
            lambda d: d["collections"][0].update(publications={"absent": {"id": "x", "aliases": []}}),
        ):
            document = deepcopy(original)
            mutate(document)
            with self.assertRaises((ValueError, TypeError)):
                self.store.publish(document)
        legacy = releases.validate(self.first, allowed_profiles={"synthetic"})
        with self.assertRaises(releases.AccessDeniedError):
            legacy.call("corpus_status", {"collections": ["istopmm"]})
        denied = collections.CollectionStore(self.store.root, allowed_profiles={"curated"},
                                            allowed_collections={"istopmm"})
        with self.assertRaises(releases.AccessDeniedError):
            denied.current()
        for args in ({"query": "screening", "offset": 1},
                     {"claim_id": "fictional-claim"}):
            with self.assertRaises(retrieval.RetrievalError):
                self.store.call("search_evidence" if "query" in args else "get_claim", args)
        registries = self.store.root / "registries"
        moved = self.store.root / "moved"
        registries.rename(moved)
        registries.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(releases.ReleaseError):
            self.store.current()

    def test_publication_aliases_missing_doi_and_source_versions_are_not_conflated(self):
        document = deepcopy(self.store.current().document)
        for entry in document["collections"]:
            entry["publications"] = {"10.0000/fictional": {
                "id": "publication:fixture", "aliases": ["pmid:12345"]}}
        sid = self.store.publish(document)
        response = self.store.call("search_evidence", {
            "query": "screening", "collections": ["istopmm", "test-literature"], "snapshot_id": sid})
        self.assertEqual({ref["publication_id"] for ref in response["collection_context"]["evidence_refs"]},
                         {"publication:fixture"})
        revised = deepcopy(self.other)
        old_hash = revised["sources"][0]["sha256"]
        new_hash = releases.sha256(b"New version of authored fictional source")
        revised["sources"][0]["sha256"] = new_hash

        def update_citations(value):
            if isinstance(value, dict):
                if value.get("source_sha256") == old_hash:
                    value["source_sha256"] = new_hash
                for child in value.values():
                    update_citations(child)
            elif isinstance(value, list):
                for child in value:
                    update_citations(child)

        update_citations(revised)
        path = self.root / "revised"
        releases.export(revised, path, profile="synthetic")
        sid = self.store.replace("test-literature", path, version="test-2", updated_at="2026-10-20")
        query = {"query": "screening", "collections": ["istopmm", "test-literature"], "snapshot_id": sid}
        # Original and revised PDF footnotes/units cannot merge merely by publication.
        response = self.store.call("search_evidence", query)
        hashes = {item["citation"]["source_sha256"] for item in response["result"]["items"]}
        self.assertIn(new_hash, hashes)
        self.assertIn(old_hash, hashes)
        self.assertEqual(collections.publication("https://doi.org/10.0000/EXAMPLE"), "doi:10.0000/example")
        self.assertEqual(collections.publication("pmid:12345"), "pmid:12345")
        no_doi, _ = fixture_bundle(self.root)

        def replace_publication(value):
            if isinstance(value, dict):
                if "doi" in value:
                    value["doi"] = "pmid:12345"
                for child in value.values():
                    replace_publication(child)
            elif isinstance(value, list):
                for child in value:
                    replace_publication(child)

        replace_publication(no_doi)
        no_doi["issues"][0]["paper_ids"] = [retrieval.paper_id("pmid:12345")]
        path = self.root / "no-doi"
        releases.export(no_doi, path, profile="synthetic")
        no_doi_store = collections.CollectionStore(
            self.root / "no-doi-registry", allowed_profiles={"synthetic"},
            allowed_collections={"test-literature"})
        entry = deepcopy(document["collections"][1])
        entry.update(release_id=no_doi_store.releases.install(path), publications={})
        sid = no_doi_store.publish({"schema_version": 1, "collections": [entry]})
        response = no_doi_store.call("search_evidence", {
            "query": "screening", "collections": ["test-literature"], "snapshot_id": sid})
        self.assertEqual({ref["publication_id"] for ref in response["collection_context"]["evidence_refs"]},
                         {"pmid:12345"})

    def test_curated_context_remains_denied_in_mixed_collection_view(self):
        bundle, _ = fixture_bundle(self.root)
        path = self.root / "curated"
        grants = {source["id"]: {"scopes": ["curated"], "basis": "Authored fictional fixture"}
                  for source in bundle["sources"]}
        releases.export(bundle, path, profile="curated", authorization=grants)
        store = collections.CollectionStore(self.store.root, allowed_profiles={"synthetic", "curated"},
                                            allowed_collections={"istopmm", "test-literature"},
                                            defaults=["istopmm"])
        sid = store.replace("test-literature", path, version="curated", updated_at="2026-10-20")
        with self.assertRaises(releases.AccessDeniedError):
            store.call("get_source_context", {"unit_id": "fictional-unit-1",
                       "collections": ["test-literature"], "snapshot_id": sid})
        search = store.call("search_evidence", {"query": "screening",
                            "collections": ["istopmm", "test-literature"]})
        self.assertEqual({ref["collection_id"] for ref in search["collection_context"]["evidence_refs"]}, {"istopmm"})

    def test_sdk_only_stdio_and_authenticated_http_collection_parity(self):
        expected = self.root / "expected.json"
        expected.write_text(json.dumps(collection_cases(self.store)))
        scripts = Path(__file__).resolve().parents[1]
        result = subprocess.run([
            sys.executable, str(scripts / "mcp_client.py"),
            "--server", str(Path(sys.executable).parent / "research-mcp"),
            "--registry", str(self.store.root), "--expected", str(expected), "--cwd", str(self.root),
            "--allow-collection", "istopmm", "--allow-collection", "test-literature",
            "--default-collection", "istopmm",
        ], capture_output=True, text=True, cwd=self.root, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with running_server(self.root, None, collection_registry=self.store.root,
                            allowed_collections=["istopmm", "test-literature"],
                            default_collections=["istopmm"]) as (url, token, registry, _, log):
            result = subprocess.run([
                sys.executable, str(scripts / "http_client.py"), "--url", url,
                "--token-file", str(self.root / "token"), "--expected", str(expected),
            ], capture_output=True, text=True, cwd=self.root, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            log.seek(0)
            self.assertNotIn(token, log.read())

    def test_operator_and_retrieval_cli_from_another_directory(self):
        config = deepcopy(self.store.current().document)
        for entry, path in zip(config["collections"], (self.first, self.second)):
            entry.pop("release_id")
            entry["release"] = str(path)
        file = self.root / "config.json"
        file.write_text(json.dumps(config))
        target = self.root / "cli-registry"
        args = ["--registry", str(target), "--allow-profile", "synthetic",
                "--allow-collection", "istopmm", "--allow-collection", "test-literature",
                "--default-collection", "istopmm"]
        result = subprocess.run([sys.executable, "-m", "research_library.collections", *args, "publish", str(file)],
                                capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([
            sys.executable, "-m", "research_library.cli", *args, "search", "Independent",
            "--collection", "test-literature",
        ], capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["result"]["items"]), 1)
