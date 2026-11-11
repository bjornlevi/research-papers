from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import httpx
import pymupdf

from research_library import catalog, literature, releases
from research_library.build import citation_neighborhood as citation
from research_library.build import literature_update as update
from test_literature import epmc_record
import test_literature_ingest
from test_releases import fixture_bundle


class UpdateTests(unittest.TestCase):
    def setUp(self):
        helper = test_literature_ingest.LiteratureIngestTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.root, self.papers, self.extraction = helper.root, helper.papers, helper.workspace
        self.source = helper.source()
        self.work = {
            "id": "https://openalex.org/W100", "doi": "https://doi.org/10.0000/fictional",
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/123"},
            "title": "Authored fictional publication", "publication_date": "2025-01-01",
            "type": "article", "locations": [{"license": "cc-by", "pdf_url": "https://example.org/fictional.pdf"}],
            "referenced_works": [], "primary_location": None,
        }
        location = citation.licensed_locations(self.work)[0]
        self.source["acquisition"]["acquisition"].update(location)
        self.graph = {
            "schema_version": 1, "scope": "Exactly one hop from the 45 iStopMM main-publication DOIs. No neighbor expansion.",
            "acquired_at": literature.utcnow(), "limitations": ["Authored fictional software case only."],
            "seeds": [{"seed_doi": f"10.0000/seed-{i}", "providers": {"openalex": {
                "status": "complete_indexed_neighborhood"}}} for i in range(45)],
            "nodes": {"openalex:WORK:W100": {
                "work": self.work, "record": citation.openalex_record(self.work),
                "sources": [{"url": "https://example.org/provider", "retrieved_at": literature.utcnow(),
                             "sha256": "a" * 64}]}},
            "edges": [{"seed_doi": "10.0000/seed-0", "direction": "background",
                       "target_provider_record": "openalex:WORK:W100"}],
            "unresolved": [], "resolution_issues": [],
        }
        self.acquisition = self.root / "acquisition"
        update.write(self.acquisition, "neighborhood.json", self.graph)
        inventory = citation.publish_metadata(self.acquisition)
        self.source["aliases"] = ["doi:10.0000/fictional", "pmid:123"]
        manifest = helper.manifest([self.source])
        manifest["catalog_id"] = inventory["catalog_id"]
        update.write(self.extraction, "pilot.json", manifest)
        helper.extract()
        acquisition = {"catalog_id": inventory["catalog_id"], "items": {
            self.source["publication_id"]: {"status": "downloaded", **self.source["acquisition"],
                                             "filename": self.source["filename"],
                                             "sha256": self.source["sha256"], "bytes": self.source["bytes"]}}}
        update.write(self.acquisition, "acquisition.json", acquisition)
        bundle, grants = update.ingest.make_bundle(self.extraction, self.papers)
        release = self.root / "evidence"
        releases.export(bundle, release, profile="private_context", authorization=grants)
        self.registry = self.root / "registry"
        store = update.store_at(self.registry)
        evidence_id = store.releases.install(release)
        metadata_id = catalog.install(self.registry, self.acquisition / "catalog-001.sqlite")
        entries = [{"id": cid, "title": "Authored fixture " + cid, "version": "fixture",
                    "updated_at": date.today().isoformat(), "coverage": "Fictional software fixture only.",
                    "release_id": metadata_id if cid == "catalog" else evidence_id, "publications": {},
                    **({"kind": "catalog"} if cid == "catalog" else {})}
                   for cid in ("istopmm", "literature", "catalog")]
        self.sid = store.publish({"schema_version": 1, "collections": entries})
        self.workspace = self.root / "updates"
        self.config = update.initialize(
            self.workspace, self.acquisition, self.extraction, self.papers, self.registry,
            "Authored fictional fixtures; private build/retrieval validation only.")
        self.through = date.today().isoformat()
        self.calls = []

    def client(self, records=None, handler=None):
        def normal(request):
            self.calls.append(request)
            return httpx.Response(200, json={"hitCount": len(records or []),
                "resultList": {"result": records or []}, "nextCursorMark": "next"})
        client = literature.MetadataClient(
            transport=httpx.MockTransport(handler or normal), sleep=lambda _: None, clock=lambda: 0)
        self.addCleanup(client.close)
        return client

    def test_local_refresh_rejects_relocated_configuration(self):
        data_root = self.root / "copied-workspace"
        root = data_root / "releases/source-updates"
        update.write(root, "config.json", self.config)
        with patch.object(update, "discover", side_effect=AssertionError("Must not contact providers")):
            with self.assertRaisesRegex(ValueError, "points outside this data workspace"):
                update.refresh(root, data_root)

    def discover(self, records=None, name="run-1", **kwargs):
        return update.discover(self.workspace, name, mode="incremental", through=self.through,
                               client=self.client(records), **kwargs)

    def test_parameter_free_refresh_remembers_inputs_applies_valid_sources_and_keeps_denied_pdf_pending(self):
        data_root = self.root / "local-data"
        for source, destination in (
            (self.acquisition, data_root / "releases/istopmm-first-tier-20261019"),
            (self.extraction, data_root / "releases/m9-ingestion-001"),
            (self.papers, data_root / "papers/literature/istopmm-first-tier-20261019"),
            (self.registry, data_root / "releases/m9-platform"),
        ):
            shutil.copytree(source, destination)
        root = data_root / "releases/source-updates"
        graph = deepcopy(self.graph)
        for number in (101, 102):
            work = deepcopy(self.work)
            work.update(id=f"https://openalex.org/W{number}", doi=f"https://doi.org/10.0000/new-{number}")
            work["ids"] = {}
            work["locations"][0]["pdf_url"] = f"https://example.org/{number}.pdf"
            graph["nodes"][f"openalex:WORK:W{number}"] = {
                **deepcopy(graph["nodes"]["openalex:WORK:W100"]),
                "record": citation.openalex_record(work), "work": work}
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), "Authored new publication with a reproducible source page. Software fixture only.")
            data = document.tobytes()
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(403 if request.url.path == "/102.pdf" else 200, content=data)
        def execute():
            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch.object(update.literature, "MetadataClient", return_value=self.client()), \
                    patch.object(citation, "discover", return_value=deepcopy(graph)), \
                    patch.object(update.httpx, "Client", return_value=client):
                return update.refresh(root, data_root)
        first = execute()
        self.assertEqual(first["states"], {"unchanged": 1, "extracted": 1, "unavailable": 1})
        self.assertNotEqual(first["snapshot"], self.sid)
        state = update.read(root / "runs" / update.latest_run(root) / "state.json")
        denied = state["ingestion"]["doi:10.0000/new-102"]
        self.assertEqual(denied["acquisition"]["attempts"][0]["http_status"], 403)
        changes = update.read(root / "runs" / state["name"] / "changes.json")
        self.assertEqual(changes["pending_publications"]["doi:10.0000/new-102"]["status"], "unavailable")
        current = update.store_at(data_root / "releases/m9-platform").current()
        self.assertEqual(len(current.snapshots["literature"]._bundle["sources"]), 2)
        self.assertEqual(current.snapshots["istopmm"].release_id, self.config["baseline_releases"]["istopmm"])
        report = update.report(root, state["name"])
        self.assertNotIn("next_update_due", report)
        self.assertNotIn("next_reconciliation_due", report)
        self.assertNotIn("interval_days", report["policy"])
        self.assertNotIn("reconcile_days", report["policy"])
        with patch.object(update, "initialize_local", side_effect=AssertionError("Repeated initialization")), \
                patch.object(update.ingest, "extract_pdf", side_effect=AssertionError("Repeated extraction")):
            second = execute()
        self.assertNotEqual(second["snapshot"], first["snapshot"])
        self.assertEqual(requests.count("https://example.org/101.pdf"), 1)
        self.assertEqual(requests.count("https://example.org/102.pdf"), 2)

    def test_refresh_resumes_without_operator_supplied_id_or_date(self):
        with patch.object(update.literature, "MetadataClient", return_value=self.client()), \
                patch.object(citation, "discover", return_value=deepcopy(self.graph)), \
                patch.object(update, "activate", side_effect=OSError("Authored interruption")):
            with self.assertRaisesRegex(OSError, "interruption"):
                update.refresh(self.workspace, self.root)
        name = update.latest_run(self.workspace)
        self.assertEqual(update.store_at(self.registry).current().release_id, self.sid)
        with patch.object(citation, "discover", side_effect=AssertionError("Completed discovery repeated")), \
                patch.object(update, "process", side_effect=AssertionError("Completed ingestion repeated")):
            result = update.refresh(self.workspace, self.root)
        self.assertEqual(update.latest_run(self.workspace), name)
        self.assertNotEqual(result["snapshot"], self.sid)

    def test_unavailable_http_responses_are_distinct_from_invalid_pdf_content(self):
        record = {"id": "doi:10.0000/new", "title": "Authored fixture",
                  "citation_url": "https://example.org/new", "istopmm_overlap": "none",
                  "identity_status": "resolved"}
        locations = citation.licensed_locations(self.work)
        for status in (403, 404, 429, 503, 200):
            client = httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(status, content=b"not a PDF", headers={"Retry-After": "180"})))
            with client, patch.object(citation.time, "sleep", return_value=None):
                entry, data = citation.acquire_one(client, record, locations, lambda: None)
            self.assertIsNone(data)
            if status == 200:
                self.assertNotIn("http_status", entry["attempts"][0])
            else:
                self.assertEqual(entry["attempts"][0]["http_status"], status)

    def test_overlap_checkpoints_idempotence_no_download_and_explicit_activation(self):
        discovery = self.discover([epmc_record(types=["Retracted Publication"])])
        self.assertIn("UPDATE_DATE:[", self.calls[0].url.params["query"])
        expected_start = (date.today() - timedelta(days=30)).isoformat()
        self.assertEqual(discovery["from"], expected_start)
        self.assertEqual(self.discover([epmc_record(types=["Retracted Publication"])]), discovery)
        self.assertEqual(len(self.calls), 1)
        with patch.object(citation, "acquire_one", side_effect=AssertionError("Unchanged source redownloaded")):
            report = update.process(self.workspace, "run-1")
        self.assertEqual(report["counts"]["states"]["unchanged"], 1)
        before = update.tree_hashes(self.registry)
        changes = update.stage(self.workspace, "run-1")
        self.assertEqual(update.tree_hashes(self.registry), before)
        self.assertEqual(update.stage(self.workspace, "run-1"), changes)
        with self.assertRaisesRegex(ValueError, "approve"):
            update.activate(self.workspace, "run-1", self.registry)
        new = update.activate(self.workspace, "run-1", self.registry, approve=True)
        self.assertNotEqual(new, self.sid)
        self.assertEqual(update.store_at(self.registry).load(self.sid).release_id, self.sid)
        self.discover(name="successor")
        update.process(self.workspace, "successor")
        update.stage(self.workspace, "successor")
        successor = update.activate(self.workspace, "successor", self.registry, approve=True)
        self.assertNotEqual(successor, new)
        self.assertEqual(update.rollback(self.workspace, "successor", self.registry, approve=True), new)
        self.assertEqual(update.rollback(self.workspace, "run-1", self.registry, approve=True), self.sid)

    def test_interrupted_discovery_resumes_and_failures_do_not_advance_freshness(self):
        count = [0]
        def handler(request):
            count[0] += 1
            if count[0] == 1:
                return httpx.Response(200, json={"hitCount": 2, "nextCursorMark": "page2",
                    "resultList": {"result": [epmc_record("123")]}})
            if count[0] == 2:
                return httpx.Response(403)
            self.assertEqual(request.url.params["cursorMark"], "page2")
            second = epmc_record("456")
            second["doi"] = "10.0000/fictional"
            return httpx.Response(200, json={"hitCount": 2, "nextCursorMark": "end",
                "resultList": {"result": [second]}})
        client = self.client(handler=handler)
        with self.assertRaises(literature.DiscoveryError):
            update.discover(self.workspace, "resume", mode="incremental", through=self.through, client=client)
        report = update.report(self.workspace, "resume")
        self.assertIsNone(report["successful_through"])
        self.assertEqual(report["discovery"], "pending")
        result = update.discover(self.workspace, "resume", mode="incremental", through=self.through, client=client)
        self.assertEqual(len(result["records"]), 2)
        page = self.workspace / "runs/resume/pages/00000.json"
        page.write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            update.make_catalog(self.config, self.workspace / "runs/resume", result)

    def test_duplicate_cursor_count_and_changed_config_fail_closed(self):
        def handler(request):
            return httpx.Response(200, json={"hitCount": 2, "nextCursorMark": "same",
                "resultList": {"result": [epmc_record()]}})
        with self.assertRaisesRegex(ValueError, "Repeated"):
            update.discover(self.workspace, "duplicate", mode="incremental", through=self.through,
                            client=self.client(handler=handler))
        config = update.read(self.workspace / "config.json")
        config["policy"]["overlap_days"] = 31
        update.write(self.workspace, "config.json", config)
        with self.assertRaisesRegex(ValueError, "differ"):
            self.discover(name="duplicate")

    def test_reconciliation_finds_new_neighbor_and_absence_is_not_deleted(self):
        graph = deepcopy(self.graph)
        old = graph["nodes"].pop("openalex:WORK:W100")
        work = {**self.work, "id": "https://openalex.org/W101", "doi": "https://doi.org/10.0000/new"}
        graph["nodes"]["openalex:WORK:W101"] = {**old, "record": citation.openalex_record(work), "work": work}
        graph["edges"][0]["target_provider_record"] = "openalex:WORK:W101"
        with patch.object(citation, "discover", return_value=graph):
            result = update.discover(self.workspace, "reconcile", mode="reconcile", through=self.through,
                                     client=self.client(), manifest=self.root / "unused-seeds.csv")
        self.assertEqual(result["new_provider_records"], ["openalex:WORK:W101"])
        self.assertEqual(result["absent_provider_records"], ["openalex:WORK:W100"])
        self.assertIn("openalex:WORK:W100", result["graph"]["nodes"])
        self.assertNotIn("UPDATE_DATE", self.calls[0].url.params["query"])

    def test_notice_review_flags_preserve_reported_numbers_and_original_citations(self):
        bundle, _ = fixture_bundle(self.root)
        original_value = deepcopy(bundle["claims"][0]["numbers"])
        notice = literature.normalize("europepmc", epmc_record(types=["Retracted Publication"]), "mm")
        result, impacts = update.impact(bundle, {"records": {"epmc": {"record": notice}}}, {})
        self.assertTrue(impacts)
        self.assertEqual(result["claims"][0]["numbers"], original_value)
        self.assertEqual(result["claims"][0]["citation"], bundle["claims"][0]["citation"])
        self.assertIn("UPDATE REVIEW REQUIRED", result["tables"][0]["caveats"][-1])
        self.assertTrue(result["claims"][0]["source_issue_ids"])
        self.assertEqual(result["issues"][-1]["record"]["status"], "unresolved")

    def test_ambiguous_catalog_doi_matches_notices_by_doi_without_crashing(self):
        bundle, _ = fixture_bundle(self.root)
        notice = literature.normalize("europepmc", epmc_record(types=["Retracted Publication"]), "mm")

        class Conflicting:
            def get(self, identifier):
                raise update.retrieval.RetrievalError("Ambiguous publication alias; use the catalog record ID")

        class Missing:
            def get(self, identifier):
                raise update.retrieval.NotFoundError("Unknown catalog publication")

        result, impacts = update.impact(bundle, {"records": {"epmc": {"record": notice}}}, {}, Conflicting())
        self.assertTrue(impacts)
        self.assertEqual(result["issues"][-1]["record"]["status"], "unresolved")
        with self.assertRaises(update.retrieval.NotFoundError):
            update.impact(bundle, {"records": {}}, {}, Missing())

    def test_transfer_quota_persists_and_policy_cannot_exceed_authorization(self):
        folder = self.root / "budget"
        budget = update.TransferBudget(folder, 100)
        budget.charge(60)
        with self.assertRaisesRegex(ValueError, "quota"):
            update.TransferBudget(folder, 100).charge(41)
        self.assertEqual(update.read(folder / "transfer.json")["charged_bytes"], 60)
        policy = deepcopy(update.POLICY)
        policy["workers"] = 3
        with self.assertRaisesRegex(ValueError, "budget"):
            update.validate_policy(policy)

    def test_blocked_candidate_and_corrupt_artifact_never_activate(self):
        self.discover()
        update.process(self.workspace, "run-1")
        folder = self.workspace / "runs/run-1"
        state = update.read(folder / "state.json")
        state["ingestion"]["doi:10.0000/fictional"]["status"] = "failed"
        update.write(folder, "state.json", state)
        with self.assertRaisesRegex(ValueError, "blocked"):
            update.stage(self.workspace, "run-1")
        self.assertEqual(update.store_at(self.registry).current().release_id, self.sid)
        state["ingestion"]["doi:10.0000/fictional"]["status"] = "unchanged"
        update.write(folder, "state.json", state)
        update.stage(self.workspace, "run-1")
        (folder / "evidence/records.json").write_text("{}")
        with self.assertRaises(ValueError):
            update.activate(self.workspace, "run-1", self.registry, approve=True)
        self.assertEqual(update.store_at(self.registry).current().release_id, self.sid)

    def test_activation_recovers_after_atomic_pointer_commits_before_run_state(self):
        self.discover()
        update.process(self.workspace, "run-1")
        update.stage(self.workspace, "run-1")
        publish = update.collections.CollectionStore.publish
        def interrupted(store, document):
            publish(store, document)
            raise OSError("Authored interruption after atomic registry update")
        with patch.object(update.collections.CollectionStore, "publish", interrupted):
            with self.assertRaisesRegex(OSError, "interruption"):
                update.activate(self.workspace, "run-1", self.registry, approve=True)
        committed = update.store_at(self.registry).current().release_id
        self.assertEqual(update.activate(self.workspace, "run-1", self.registry, approve=True), committed)
        self.assertEqual(update.rollback(self.workspace, "run-1", self.registry, approve=True), self.sid)
        self.assertEqual(update.rollback(self.workspace, "run-1", self.registry, approve=True), self.sid)

    def test_backup_restore_checksums_and_fresh_target(self):
        with self.assertRaisesRegex(ValueError, "contain"):
            update.backup(self.registry, self.registry / "nested")
        backup = self.root / "backup"
        update.backup(self.registry, backup)
        restored = self.root / "restored"
        self.assertEqual(update.restore(backup, restored), self.sid)
        self.assertEqual(update.tree_hashes(self.registry), update.tree_hashes(restored))
        with self.assertRaisesRegex(ValueError, "new directory"):
            update.restore(backup, restored)
        (backup / "registry/active.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            update.restore(backup, self.root / "other")

    def test_successive_windows_keep_metadata_notices_and_resource_load_is_enforced(self):
        self.discover([epmc_record(types=["Retracted Publication"])])
        update.process(self.workspace, "run-1")
        update.stage(self.workspace, "run-1")
        later = self.discover([], name="run-2")
        self.assertIn("europepmc:MED:123", later["records"])
        update.process(self.workspace, "run-2")
        update.stage(self.workspace, "run-2")
        with patch.object(update, "POLICY", {**update.POLICY, "load_budget": {
                **update.POLICY["load_budget"], "max_seconds": 0}}):
            with self.assertRaisesRegex(ValueError, "budget"):
                update.benchmark(self.registry, self.root / "load.json")
        self.assertFalse(update.read(self.root / "load.json")["passed"])

    def test_out_of_scope_metadata_and_concurrent_writers_fail_explicitly(self):
        unrelated = epmc_record("999")
        unrelated["doi"] = "10.0000/unrelated"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.discover([unrelated])
        with patch.object(update.fcntl, "flock", side_effect=BlockingIOError):
            with self.assertRaisesRegex(ValueError, "Another update writer"):
                update.report(self.workspace, "run-1")
        self.assertIsNone(update.report(self.workspace, "run-1")["successful_through"])

    def test_changed_pdf_version_preserves_original_citations_and_cached_extraction(self):
        original_files = update.tree_hashes(self.papers)
        graph = deepcopy(self.graph)
        graph["nodes"]["openalex:WORK:W100"]["work"]["locations"][0]["pdf_url"] = "https://example.org/revision.pdf"
        graph["nodes"]["openalex:WORK:W100"]["work"]["locations"].append(
            {"license": "cc-by", "pdf_url": "https://example.org/new-location.pdf"})
        with patch.object(citation, "discover", return_value=graph):
            update.discover(self.workspace, "revision", mode="reconcile", through=self.through,
                            client=self.client(), manifest=self.root / "seeds.csv")
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), "Fictional revised source: 101 participants and 5 events. Software fixture only.")
            data = document.tobytes()
        failure = {"status": "licensed_locations_failed", "attempts": [
            {"url": "https://example.org/revision.pdf", "status": "failed", "error": "Authored 403 fixture."}]}
        with patch.object(citation, "acquire_one", return_value=(failure, None)):
            self.assertEqual(update.process(self.workspace, "revision")["counts"]["states"], {"failed": 1})
        urls = []
        def download(request):
            urls.append(str(request.url))
            return httpx.Response(200, content=data)
        client = httpx.Client(transport=httpx.MockTransport(download))
        self.addCleanup(client.close)
        with patch.object(update.httpx, "Client", return_value=client):
            result = update.process(self.workspace, "revision", retry_failed=True)
        self.assertEqual(urls, ["https://example.org/new-location.pdf"])
        self.assertEqual(result["counts"]["states"], {"extracted": 1})
        self.assertEqual(result["charged_transfer_bytes"], len(data))
        self.assertEqual(update.tree_hashes(self.papers), original_files)
        changes = update.stage(self.workspace, "revision")
        self.assertEqual(changes["added_evidence_publications"], ["doi:10.0000/fictional"])
        self.assertIn("changed_source_bytes", str(changes["review_impacts"]))
        diff = changes["source_version_diffs"]["doi:10.0000/fictional"]
        self.assertEqual(diff["old_sha256"], self.source["sha256"])
        self.assertEqual(diff["changed_original_pdf_pages"], [1, 2])
        old = update.store_at(self.registry).current().snapshots["literature"]._bundle
        new = releases.validate(self.workspace / "runs/revision/evidence", allowed_profiles={"private_context"})._bundle
        self.assertEqual(len(new["sources"]), 2)
        retained = next(unit for unit in new["units"] if unit["id"] == old["units"][0]["id"])
        self.assertEqual(retained["citation"], old["units"][0]["citation"])
        update.activate(self.workspace, "revision", self.registry, approve=True)
        self.discover(name="next")
        with patch.object(citation, "acquire_one", side_effect=AssertionError("Version redownloaded")), \
                patch.object(update.ingest, "extract_pdf", side_effect=AssertionError("Unchanged version re-extracted")):
            update.process(self.workspace, "next")
            update.stage(self.workspace, "next")

    def test_metadata_change_flags_numbers_without_inventing_a_correction(self):
        self.discover([epmc_record()])
        update.process(self.workspace, "run-1")
        update.stage(self.workspace, "run-1")
        changed = epmc_record()
        changed["abstractText"] = "Correction: reported denominator revised from 100 to 101. Authored software fixture."
        second = self.discover([changed], name="amended")
        self.assertEqual(second["changed_metadata_records"], ["europepmc:MED:123"])
        bundle, _ = fixture_bundle(self.root)
        result, impacts = update.impact(bundle, second, {})
        self.assertEqual(result["claims"][0]["numbers"], bundle["claims"][0]["numbers"])
        self.assertIn("changed_provider_metadata", str(impacts))

    def test_same_url_metadata_updates_probe_bytes_and_reuse_identical_extraction(self):
        self.discover([epmc_record()])
        update.process(self.workspace, "run-1")
        update.stage(self.workspace, "run-1")
        data = (self.papers / self.source["filename"]).read_bytes()
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), "Authored changed PDF at an unchanged URL: 101 participants, 5 events.")
            revised = document.tobytes()
        for name, content in (("same", data), ("changed", revised)):
            record = epmc_record()
            record["dateOfRevision"] = "2025-01-01" if name == "same" else "2025-01-02"
            self.discover([record], name=name)
            client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
            with patch.object(update.httpx, "Client", return_value=client):
                if name == "same":
                    with patch.object(update.ingest, "extract_pdf", side_effect=AssertionError("Identical bytes re-extracted")):
                        result = update.process(self.workspace, name)
                    self.assertEqual(result["counts"]["states"], {"unchanged": 1})
                else:
                    result = update.process(self.workspace, name)
                    self.assertEqual(result["counts"]["states"], {"extracted": 1})
            self.assertEqual(result["download_attempts"], 1)
            changes = update.stage(self.workspace, name)
            self.assertEqual(bool(changes["source_version_diffs"]), name == "changed")

    def test_commits_whole_pages_and_quarantines_missing_strong_ids(self):
        bad = epmc_record("999")
        bad["doi"] = "10.0000/unrelated"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.discover([epmc_record(), bad])
        saved = update.read(self.workspace / "runs/run-1/state.json")
        self.assertEqual(saved["pages"], [])
        self.assertEqual(saved["records"], {})
        missing = epmc_record("PMC123")
        missing.update(source="PMC")
        missing.pop("doi")
        missing.pop("pmid")
        result = self.discover([epmc_record(), missing])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(len(result["quarantined_provider_records"]), 1)

    def test_real_style_spaced_notice_links_do_not_expand_ordinary_commentary(self):
        original = epmc_record()
        original["commentCorrectionList"] = {"commentCorrection": [
            {"type": "Erratum in", "id": "456", "source": "MED"},
            {"type": "Comment in", "id": "999", "source": "MED"}]}
        notice = epmc_record("456", types=["Published Erratum"])
        notice["doi"] = "10.0000/correction"
        notice["commentCorrectionList"] = {"commentCorrection": [
            {"type": "Erratum for", "id": "123", "source": "MED"}]}
        queries = []
        def handler(request):
            query = request.url.params["query"]
            queries.append(query)
            record = notice if "EXT_ID:456" in query else original
            return httpx.Response(200, json={"hitCount": 1, "resultList": {"result": [record]}})
        result = update.discover(self.workspace, "notice", mode="incremental", through=self.through,
                                 client=self.client(handler=handler))
        self.assertFalse(any("999" in query for query in queries))
        self.assertLessEqual(len(queries), 3)
        self.assertEqual(result["records"]["europepmc:MED:456"]["record"]["relations"][0]["kind"], "corrects")
        self.assertEqual(len(result["records"]), 2)

    def test_pdf_attempt_quota_keeps_unprocessed_sources_pending_and_survives_resume(self):
        config = update.read(self.workspace / "config.json")
        config["policy"]["max_pdfs"] = 1
        update.write(self.workspace, "config.json", config)
        graph = deepcopy(self.graph)
        for number in (101, 102):
            work = deepcopy(self.work)
            work.update(id=f"https://openalex.org/W{number}", doi=f"https://doi.org/10.0000/new-{number}")
            work["ids"] = {}
            graph["nodes"][f"openalex:WORK:W{number}"] = {
                **deepcopy(graph["nodes"]["openalex:WORK:W100"]),
                "work": work, "record": citation.openalex_record(work)}
        with patch.object(citation, "discover", return_value=graph):
            update.discover(self.workspace, "quota", mode="reconcile", through=self.through,
                            client=self.client(), manifest=self.root / "seeds.csv")
        with patch.object(citation, "acquire_one", return_value=({"status": "licensed_locations_failed"}, None)) as acquire:
            first = update.process(self.workspace, "quota")
            second = update.process(self.workspace, "quota", retry_failed=True)
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(first["download_attempts"], second["download_attempts"])
        self.assertIn("quota_pending", second["counts"]["states"])
        changes = update.stage(self.workspace, "quota")
        self.assertEqual(len(changes["pending_publications"]), 2)
        self.assertTrue(all(item["status"] == "quota_pending" for item in changes["pending_publications"].values()))
        self.assertEqual(update.store_at(self.registry).current().release_id, self.sid)
