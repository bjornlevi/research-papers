from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

from research_library import contracts, releases, retrieval
from fixtures import CLAIM, CONTEXT, ISSUE, RELATIONSHIP, TABLE, snapshots


def fixture_bundle(folder):
    sources, assets = [], {}
    for document, format_ in (("main", "pdf"), ("word", "docx"), ("sheet", "xlsx")):
        content = ("Authored fictional " + document).encode()
        path = folder / ("fictional." + format_)
        path.write_bytes(content)
        sid = "fictional:" + document
        assets[sid] = path
        sources.append({
            "id": sid, "doi": "10.0000/fictional", "sha256": releases.sha256(content),
            "format": format_, "url": None, "provenance": "Authored synthetic fixture; not clinical evidence.",
            "rights": "synthetic", "omission": "original_not_exported",
        })
    def cite(sid, locator):
        source = next(s for s in sources if s["id"] == sid)
        return {"source_id": sid, "doi": source["doi"], "source_sha256": source["sha256"],
                "source_url": source["url"], "provenance": source["provenance"],
                "availability": "metadata_only", "locator": locator}
    pdf = cite("fictional:main", {"kind": "pdf", "pdf_page": 1})
    word = cite("fictional:word", {"kind": "word_table", "body_element": 2})
    sheet = cite("fictional:sheet", {"kind": "worksheet", "worksheet": "Data", "row": 4})
    claim = deepcopy(CLAIM)
    claim["citation"] = pdf
    claim["numbers"][0]["citation"] = deepcopy(pdf)
    units = []
    for index, citation in enumerate((pdf, word, sheet), 1):
        unit = {**deepcopy(CONTEXT), "id": "fictional-unit-" + str(index), "citation": citation,
                "text": "Fictional screening context. " * 150}
        units.append(unit)
    units.append({**deepcopy(CONTEXT), "id": "fictional-word-footnote",
                  "citation": cite("fictional:word", {"kind": "word_package", "package_member": "word/footnotes.xml"}),
                  "text": "Fictional footnote, not a PDF page."})
    table = {**deepcopy(TABLE), "id": "fictional-table", "citation": word,
             "rows": [["Header retained in source cells", None], ["Nested table text\nA: 0", 0]] +
                     [[f"Row {i}", None] for i in range(120)],
             "footnotes": ["Fictional denominator and units must remain on every table page.",
                           "Nested text is preserved, not expanded into an inferred grid."]}
    issue = deepcopy(ISSUE)
    issue.update(id="fictional-issue", citation=pdf,
                 evidence=[{"quote": "Fictional quote.", "citation": deepcopy(pdf)}])
    related_claim = deepcopy(claim)
    related_claim["id"] = "fictional-review"
    related_claim["claim_kind"] = "secondary_evidence"
    relationship = deepcopy(RELATIONSHIP)
    relationship.update(id="fictional-edge", citation=pdf, target_id="fictional-review")
    bundle = {
        "schema_version": 1, "sources": sources, "claims": [claim, related_claim], "units": units,
        "tables": [table], "issues": [{"number": 1, "title": "Fictional issue",
                                      "paper_ids": ["10-0000-fictional"], "images": [], "record": issue}],
        "relationships": [{"from_id": claim["id"], "record": relationship}],
        "caveats": ["Synthetic publications only; no participant-level data."],
    }
    return bundle, assets


class ReleaseTests(unittest.TestCase):
    def test_source_inventory_identity_usage_unknown_origin_and_pagination(self):
        self.bundle["sources"][0]["bibliography"] = {
            "title": "Fictional publication", "year": "2025",
            "acquired_from": "https://example.org/original.pdf",
            "acquisition_method": "synthetic_fixture", "acquired_at": None,
        }
        _, path = self.export()
        before = snapshots(path)
        snapshot = self.snapshot(path)
        first = snapshot.call("list_sources", {"limit": 1})["result"]
        self.assertEqual(first["next_offset"], 1)
        source = first["items"][0]
        self.assertEqual(source["source_id"], "fictional:main")
        self.assertIn("claims", source["used_in"])
        self.assertTrue(source["acquisition_details_recorded"])
        self.assertFalse(source["original_served"])
        self.assertEqual(source["bibliography"]["title"], "Fictional publication")
        unknown = snapshot.call("list_sources", {"limit": 1, "offset": 1})["result"]["items"][0]
        self.assertIsNone(unknown["bibliography"]["acquired_from"])
        self.assertFalse(unknown["acquisition_details_recorded"])
        self.assertEqual(snapshot.call("list_sources", {"paper": "10.0000/missing"})["result"]["items"], [])
        self.assertEqual(snapshots(path), before)

    def test_plain_search_punctuation_operators_and_explicit_fts_pagination(self):
        text = "LC-MGUS prevalence decreased standard reference intervals. Fictional screening."
        self.bundle["units"][0]["text"] = text
        self.bundle["units"][1]["text"] = text
        _, path = self.export()
        snapshot = self.snapshot(path)
        arguments = {"query": "LC-MGUS prevalence decreased standard reference intervals",
                     "paper": "10.0000/fictional", "limit": 1}
        first = snapshot.call("search_evidence", arguments)["result"]
        self.assertEqual(first["items"][0]["id"], "fictional-unit-1")
        self.assertEqual(first["next_offset"], 1)
        second = snapshot.call("search_evidence", {**arguments, "offset": 1})["result"]
        self.assertEqual(second["items"][0]["id"], "fictional-unit-2")
        self.assertIsNone(second["next_offset"])
        for query in ('"LC-MGUS', "LC-MGUS: (prevalence)", "LC-MGUS*"):
            with self.subTest(query=query):
                result = snapshot.call("search_evidence", {"query": query})["result"]
                self.assertEqual(len(result["items"]), 2)
        literal = snapshot.call("search_evidence", {"query": "screening OR nonexistent"})["result"]
        self.assertEqual(literal["items"], [])
        advanced = snapshot.call("search_evidence", {
            "query": '"LC-MGUS" OR nonexistent*', "mode": "fts5", "limit": 1,
        })["result"]
        self.assertEqual(advanced["items"][0]["id"], "fictional-unit-1")
        self.assertEqual(advanced["next_offset"], 1)
        for query in ("---", '""', "*():"):
            with self.assertRaisesRegex(retrieval.RetrievalError, "word or number"):
                snapshot.call("search_evidence", {"query": query})

    def test_cached_store_rechecks_every_backing_byte_without_revalidating_records(self):
        path = self.root / "cached-release"
        releases.export(self.bundle, path, profile="synthetic")
        store = releases.ReleaseStore(self.root / "cached-store", allowed_profiles={"synthetic"})
        identifier = store.install(path)
        store.activate(identifier)
        first = store.current()
        stored = store.root / "releases" / identifier
        with patch.object(releases, "validate", side_effect=AssertionError("Should reuse validated snapshot")):
            self.assertIs(store.current(), first)
            records = stored / "records.json"
            original = records.read_bytes()
            records.write_bytes(b"x" + original[1:])
            with self.assertRaises(releases.ReleaseError):
                store.current()
            records.write_bytes(original)
            extra = stored / "unlisted"
            extra.write_text("unexpected")
            with self.assertRaises(releases.ReleaseError):
                store.current()
            extra.unlink()
            moved = stored / "records-backup"
            records.rename(moved)
            records.symlink_to(moved)
            with self.assertRaises(releases.ReleaseError):
                store.current()
            records.unlink()
            moved.rename(records)
            manifest = stored / "manifest.json"
            raw = manifest.read_bytes()
            data = json.loads(raw)
            data["authorization"] = {"unexpected": "changed"}
            manifest.write_text(json.dumps(data))
            with self.assertRaises(releases.ReleaseError):
                store.current()
            manifest.write_bytes(raw)
            self.assertIs(store.current(), first)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle, self.assets = fixture_bundle(self.root)

    def export(self, name="release", **kwargs):
        path = self.root / name
        identifier = releases.export(self.bundle, path, profile="synthetic", **kwargs)
        return identifier, path

    def snapshot(self, path):
        return releases.validate(path, allowed_profiles={"synthetic"})

    def rewrite_manifest(self, path, mutate):
        target = path / "manifest.json"
        manifest = json.loads(target.read_bytes())
        mutate(manifest)
        manifest.pop("release_id")
        manifest["release_id"] = releases.sha256(releases.encode(manifest))
        target.write_bytes(releases.encode(manifest))

    def test_deterministic_export_and_no_workspace_mutation(self):
        before = snapshots(self.root)
        first, path = self.export()
        second, other = self.export("second")
        self.assertEqual(first, second)
        self.assertEqual(snapshots(path), snapshots(other))
        for name, digest in before.items():
            self.assertEqual(snapshots(self.root)[name], digest)
        manifest = json.loads((path / "manifest.json").read_bytes())
        self.assertEqual(set(manifest["files"]), {"records.json", "evidence/index.sqlite"})
        self.assertFalse(manifest["assets"])
        self.assertNotIn("created_at", manifest)
        with self.assertRaisesRegex(releases.ReleaseError, "already exist"):
            self.export()

    def test_python310_index_loading_without_deserialize(self):
        identifier, path = self.export()
        before = snapshots(path)
        original_connect = sqlite3.connect

        class LegacyConnection(sqlite3.Connection):
            def __getattribute__(self, name):
                if name == "deserialize":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        def connect(*args, **kwargs):
            return original_connect(*args, factory=LegacyConnection, **kwargs)

        with patch.object(releases.sqlite3, "connect", side_effect=connect):
            snapshot = self.snapshot(path)
            response = snapshot.call("search_evidence", {"query": "screening"})
        self.assertEqual(response["release_id"], identifier)
        self.assertTrue(response["result"]["items"])
        self.assertEqual(snapshots(path), before)

    def test_all_readonly_tools_preserve_release_provenance_and_locators(self):
        identifier, path = self.export(asset_paths=self.assets)
        before = snapshots(path)
        snapshot = self.snapshot(path)
        for file in path.rglob("*"):
            if file.is_file():
                file.chmod(0o444)
        cases = {
            "corpus_status": {},
            "search_evidence": {"query": "screening", "limit": 2},
            "get_claim": {"claim_id": "fictional-claim"},
            "get_source_context": {"unit_id": "fictional-unit-2", "max_characters": 17},
            "get_table": {"table_id": "fictional-table", "limit": 2},
            "get_source_issue": {"issue_id": "fictional-issue"},
            "get_related_evidence": {"evidence_id": "fictional-claim"},
        }
        for tool, arguments in cases.items():
            response = snapshot.call(tool, arguments)
            contracts.validate_response(tool, arguments, response)
            self.assertEqual(response["release_id"], identifier)
        context = snapshot.call("get_source_context", cases["get_source_context"])["result"]
        self.assertEqual(context["citation"]["locator"], {"kind": "word_table", "body_element": 2})
        self.assertTrue(context["truncated"])
        self.assertEqual(len(context["text"]), 17)
        footnote = snapshot.call("get_source_context", {"unit_id": "fictional-word-footnote"})["result"]
        self.assertEqual(footnote["citation"]["locator"]["kind"], "word_package")
        table = snapshot.call("get_table", cases["get_table"])["result"]
        next_page = snapshot.call("get_table", {"table_id": "fictional-table", "limit": 2, "offset": 2})["result"]
        self.assertEqual(table["footnotes"], next_page["footnotes"])
        self.assertEqual(table["headers"], next_page["headers"])
        self.assertEqual(table["units"], next_page["units"])
        self.assertEqual(table["rows"][1][1], 0)
        self.assertIsNone(next_page["rows"][0][1])
        self.assertTrue(table["truncated"])
        self.assertEqual(table["next_offset"], 2)
        self.assertEqual(snapshot.asset("fictional:main"), self.assets["fictional:main"].read_bytes())
        with self.assertRaises(retrieval.NotFoundError):
            snapshot.asset("../../etc/passwd")
        with self.assertRaises(retrieval.NotFoundError):
            snapshot.call("get_claim", {"claim_id": "unknown"})
        self.assertEqual(snapshots(path), before)

    def test_profiles_never_infer_rights_from_possession(self):
        self.bundle["sources"][0]["rights"] = "unknown"
        with self.assertRaises(releases.AccessDeniedError):
            self.export()
        with self.assertRaises(releases.AccessDeniedError):
            releases.export(self.bundle, self.root / "denied")
        grants = {source["id"]: {"scopes": ["curated"], "basis": "Test-authorized selected evidence."}
                  for source in self.bundle["sources"]}
        target = self.root / "curated"
        releases.export(self.bundle, target, authorization=grants)
        snapshot = releases.validate(target, allowed_profiles={"curated"})
        self.assertEqual(snapshot.call("corpus_status", {})["result"]["content_unit_count"], 0)
        claim = snapshot.call("get_claim", {"claim_id": "fictional-claim"})["result"]
        self.assertEqual(claim["citation"]["availability"], "metadata_only")
        with self.assertRaises(releases.AccessDeniedError):
            snapshot.call("get_source_context", {"unit_id": "fictional-unit-1"})
        with self.assertRaises(releases.AccessDeniedError):
            snapshot.asset("fictional:main")
        with self.assertRaises(releases.AccessDeniedError):
            releases.validate(target, allowed_profiles={"synthetic"})
        with self.assertRaises(releases.AccessDeniedError):
            releases.export(self.bundle, self.root / "context-denied",
                            profile="private_context", authorization=grants)
        for grant in grants.values():
            grant["scopes"] += ["context", "original"]
        for profile in ("private_context", "full_source"):
            destination = self.root / profile
            releases.export(self.bundle, destination, profile=profile,
                            authorization=grants, asset_paths=self.assets)
            snapshot = releases.validate(destination, allowed_profiles={profile})
            self.assertTrue(snapshot.call("get_source_context", {"unit_id": "fictional-unit-1"})["result"])
            self.assertEqual(bool(snapshot._manifest["assets"]), profile == "full_source")

    def test_reject_checksum_missing_unknown_extra_and_dangling_inputs(self):
        _, path = self.export()
        (path / "records.json").write_bytes(b"corrupt")
        with self.assertRaisesRegex(releases.ReleaseError, "size mismatch"):
            self.snapshot(path)
        _, path = self.export("missing")
        (path / "records.json").unlink()
        with self.assertRaisesRegex(releases.ReleaseError, "Missing release file"):
            self.snapshot(path)
        _, path = self.export("schema")
        self.rewrite_manifest(path, lambda m: m.update(schema_version=999))
        with self.assertRaisesRegex(releases.ReleaseError, "Unsupported"):
            self.snapshot(path)
        _, path = self.export("extra")
        (path / "unlisted.pdf").write_bytes(b"unexpected")
        with self.assertRaisesRegex(releases.ReleaseError, "Unlisted"):
            self.snapshot(path)
        for field, mutate in (
            ("source", lambda b: b["claims"][0]["citation"].update(source_id="unknown")),
            ("issue", lambda b: b["claims"][0]["source_issue_ids"].append("unknown")),
            ("relationship", lambda b: b["relationships"][0]["record"].update(target_id="unknown")),
            ("hash", lambda b: b["claims"][0]["numbers"][0]["citation"].update(source_sha256="b"*64)),
        ):
            broken = deepcopy(self.bundle)
            mutate(broken)
            with self.subTest(field=field):
                with self.assertRaises(releases.ReleaseError):
                    releases.export(broken, self.root / ("dangling-" + field), profile="synthetic")

    def test_traversal_and_symlinks_fail_even_with_valid_manifest_hash(self):
        _, path = self.export()
        self.rewrite_manifest(path, lambda m: m["files"].update(
            {"../escaped": {"sha256": "a"*64, "bytes": 0}}))
        with self.assertRaises(releases.ReleaseError):
            self.snapshot(path)
        _, path = self.export("symlink")
        records = (path / "records.json").read_bytes()
        (path / "records.json").unlink()
        outside = self.root / "outside.json"
        outside.write_bytes(records)
        (path / "records.json").symlink_to(outside)
        with self.assertRaisesRegex(releases.ReleaseError, "Symlink"):
            self.snapshot(path)
        with self.assertRaises(releases.ReleaseError):
            releases.safe_file(path, "/etc/passwd")
        for relative in ("a/../records.json", "./records.json", "a\\b", "a//b"):
            with self.assertRaises(releases.ReleaseError):
                releases.safe_file(path, relative)

    def test_corrupt_index_cannot_be_blessed_by_updating_file_checksums(self):
        _, path = self.export()
        with sqlite3.connect(path / "evidence/index.sqlite") as db:
            db.execute("DELETE FROM content_search")
        data = (path / "evidence/index.sqlite").read_bytes()
        self.rewrite_manifest(path, lambda m: m["files"].update({
            "evidence/index.sqlite": {"sha256": releases.sha256(data), "bytes": len(data)}}))
        with self.assertRaisesRegex(releases.ReleaseError, "differs from canonical"):
            self.snapshot(path)

    def test_invalid_activation_keeps_previous_and_rollback_restores(self):
        first, path = self.export()
        store = releases.ReleaseStore(self.root / "store", allowed_profiles={"synthetic"})
        self.assertEqual(store.install(path), first)
        store.activate(first)
        pinned = store.current()
        self.bundle["claims"][0]["statement"] = "Fictional second version."
        second, other = self.export("second")
        store.install(other)
        before = snapshots(self.root / "store/releases")
        store.activate(second)
        self.assertEqual(store.current().release_id, second)
        self.assertEqual(pinned.release_id, first)
        self.assertNotEqual(pinned.call("get_claim", {"claim_id": "fictional-claim"})["result"]["statement"],
                            store.current().call("get_claim", {"claim_id": "fictional-claim"})["result"]["statement"])
        with self.assertRaises(releases.ReleaseError):
            store.activate("b"*64)
        self.assertEqual(store.current().release_id, second)
        self.assertEqual(store.rollback().release_id, first)
        self.assertEqual(snapshots(self.root / "store/releases"), before)
        # Invalid stored bytes cannot become active and cannot disturb a pinned in-flight snapshot.
        invalid = self.root / "store/releases" / second / "records.json"
        invalid.write_bytes(b"broken")
        with self.assertRaises(releases.ReleaseError):
            store.activate(second)
        self.assertEqual(store.current().release_id, first)
        self.assertTrue(pinned.call("get_claim", {"claim_id": "fictional-claim"})["ok"])

    def test_activation_races_with_readers_without_mixed_request_ids(self):
        first, path = self.export()
        self.bundle["claims"][0]["statement"] = "Version two."
        second, other = self.export("second")
        store = releases.ReleaseStore(self.root / "store", allowed_profiles={"synthetic"})
        store.install(path)
        store.install(other)
        store.activate(first)
        def writer():
            for identifier in [second, first] * 4:
                store.activate(identifier)
        def reader():
            observed = []
            for _ in range(8):
                pinned = store.current()
                a = pinned.call("corpus_status", {})
                b = pinned.call("get_claim", {"claim_id": "fictional-claim"})
                self.assertEqual(a["release_id"], b["release_id"])
                self.assertIn(a["release_id"], {first, second})
                observed.append(a["release_id"])
            return observed
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(writer)] + [pool.submit(reader) for _ in range(3)]
            for future in futures:
                future.result()

    def test_rollback_recovers_when_the_current_release_is_corrupt(self):
        first, path = self.export()
        store = releases.ReleaseStore(self.root / "store", allowed_profiles={"synthetic"})
        store.install(path)
        store.activate(first)
        self.bundle["claims"][0]["statement"] = "Second version."
        second, other = self.export("second")
        store.install(other)
        store.activate(second)
        (self.root / "store/releases" / second / "records.json").write_bytes(b"broken")
        with self.assertRaises(releases.ReleaseError):
            store.current()
        self.assertEqual(store.rollback().release_id, first)
        self.assertEqual(store.current().release_id, first)

    def test_empty_search_invalid_fts_and_oversized_table_are_explicit(self):
        _, path = self.export()
        snapshot = self.snapshot(path)
        self.assertEqual(snapshot.call("search_evidence", {"query": "zzyyunknown"})["result"]["items"], [])
        with self.assertRaises(sqlite3.OperationalError):
            snapshot.call("search_evidence", {"query": '"unclosed', "mode": "fts5"})
        self.bundle["tables"][0]["rows"] = [["x"*4096, "x"*4096] for _ in range(100)]
        _, other = self.export("big")
        snapshot = self.snapshot(other)
        with self.assertRaisesRegex(ValueError, "serialized UTF-8"):
            snapshot.call("get_table", {"table_id": "fictional-table", "limit": 100})
        self.assertTrue(snapshot.call("get_table", {"table_id": "fictional-table", "limit": 1})["ok"])

    def test_cli_export_validate_install_activate_query_rollback(self):
        bundle = self.root / "bundle.json"
        bundle.write_bytes(releases.encode(self.bundle))
        path = self.root / "cli-release"
        def run(module, *args, expected=0):
            result = subprocess.run([sys.executable, "-m", "research_library." + module, *map(str, args)],
                                    cwd=self.root.parent, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            return result
        result = run("release_cli", "export", bundle, path, "--profile", "synthetic")
        identifier = json.loads(result.stdout)["release_id"]
        run("release_cli", "validate", path, "--allow-profile", "synthetic")
        store = self.root / "store"
        run("release_cli", "install", path, "--store", store, "--allow-profile", "synthetic")
        run("release_cli", "activate", identifier, "--store", store, "--allow-profile", "synthetic")
        result = run("cli", "--store", store, "--allow-profile", "synthetic", "tool",
                     "get_table", '{"table_id":"fictional-table","limit":1}')
        self.assertEqual(json.loads(result.stdout)["release_id"], identifier)
        run("cli", "--release", path, "claim", "fictional-claim", expected=1)
        run("release_cli", "rollback", "--store", store, "--allow-profile", "synthetic", expected=1)
