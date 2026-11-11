from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import anyio
from jsonschema import ValidationError

from research_library import contracts, evaluation, releases
from test_releases import fixture_bundle


def specifications():
    cases = []
    for index in range(12):
        if index == 1:
            call = {"tool": "get_source_issue", "arguments": {"issue_id": "fictional-issue"}}
        elif index == 2:
            call = {"tool": "get_related_evidence", "arguments": {"evidence_id": "fictional-claim"}}
        elif index == 3:
            call = {"tool": "get_table", "arguments": {"table_id": "fictional-table", "limit": 2}}
        elif index == 4:
            call = {"tool": "search_evidence", "arguments": {"query": "screening", "limit": 10},
                    "relevant_ids": ["fictional-unit-1"]}
        elif index == 10:
            call = {"tool": "search_evidence", "arguments": {"query": "unmatchedzzqv", "limit": 10}}
        else:
            call = {"tool": "get_claim", "arguments": {"claim_id": "fictional-claim"}}
        cases.append({
            "id": f"fixture-{index}", "split": "development" if index < 8 else "held_out",
            "question": "Fictional evaluation question, not clinical evidence.",
            "scope": "corpus_no_match" if index == 10 else "published_evidence",
            "mandatory_caveats": ["Fixture only; no clinical inference."], "calls": [call],
        })
    return cases


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle, _ = fixture_bundle(self.root)
        self.path = self.root / "release"
        releases.export(self.bundle, self.path, profile="synthetic")
        self.snapshot = releases.validate(self.path, allowed_profiles={"synthetic"})
        self.draft = evaluation.draft(self.bundle, specifications(), "fixture-1")

    def freeze(self):
        return evaluation.freeze(self.draft, self.snapshot, reviewer="Fictional software-test attestation",
                                 approved_at="2026-10-16T10:00:00Z", review_kind="software")

    def observations(self, benchmark, split="development"):
        return [{"item_id": item["id"], "call_index": index,
                 "response": self.snapshot.call(call["tool"], call["arguments"])}
                for item in benchmark["items"] if item["split"] == split
                for index, call in enumerate(item["calls"])]

    def test_draft_gate_schema_references_and_explicit_split(self):
        self.assertEqual(self.draft["status"], "draft")
        self.assertIsNone(self.draft["review"])
        normalized_paper = specifications()
        normalized_paper[4]["calls"][0]["arguments"]["paper"] = "10-0000-fictional"
        normalized = evaluation.draft(self.bundle, normalized_paper, "normalized-paper-test")
        evaluation.validate(normalized)
        with self.assertRaisesRegex(ValueError, "frozen"):
            evaluation.score(self.draft, [], split="development")
        for mutate in (
            lambda b: b["items"][1].update(id=b["items"][0]["id"]),
            lambda b: b.update(status="frozen"),
            lambda b: b["thresholds"].update(hard_accuracy=0.9),
            lambda b: b["items"][0]["calls"][0]["arguments"].update(arbitrary_sql="not allowed"),
            lambda b: b["items"][4]["calls"][0]["arguments"].update(limit=5),
            lambda b: b["items"][10]["calls"][0].update(expected={"items": [], "next_offset": None, "scope": "No evidence exists"}),
        ):
            broken = deepcopy(self.draft)
            mutate(broken)
            with self.assertRaises((ValueError, ValidationError)):
                evaluation.validate(broken)
        with self.assertRaises(ValueError):
            evaluation.freeze(self.draft, self.snapshot, reviewer="", approved_at="2026-10-16", review_kind="software")
        changed = deepcopy(self.draft)
        changed["source_hashes"]["fictional:main"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "inventory"):
            evaluation.freeze(changed, self.snapshot, reviewer="Fixture", approved_at="2026-10-16T00:00:00Z",
                              review_kind="software")

    def test_fidelity_and_perturbations_are_separate_from_ranking(self):
        benchmark = self.freeze()
        rows = self.observations(benchmark)
        result = evaluation.score(benchmark, rows, split="development")
        self.assertTrue(result["passed"])
        self.assertEqual(result["review_kind"], "software")
        self.assertEqual(result["hard_accuracy"], 1)
        self.assertEqual(result["mean_recall_at_k"], 1)
        for mutate in (
            lambda r: r.update(caveats=[]),
            lambda r: r["result"]["numbers"][0].update(denominator=999),
            lambda r: r["result"]["numbers"][0].update(measure="hazard_ratio"),
            lambda r: r["result"].update(source_issue_ids=[]),
            lambda r: r["result"].update(caveats=[]),
            lambda r: r["result"]["citation"]["locator"].update(pdf_page=9),
        ):
            wrong = deepcopy(rows)
            mutate(wrong[0]["response"])
            self.assertFalse(evaluation.score(benchmark, wrong, split="development")["passed"])
        wrong = deepcopy(rows)
        wrong[2]["response"]["result"]["items"][0]["relationship"] = "independent_evidence"
        # An invalid enum must also fail; either schema or fidelity rejects it.
        try:
            result = evaluation.score(benchmark, wrong, split="development")
        except ValidationError:
            pass
        else:
            self.assertFalse(result["passed"])
        wrong = deepcopy(rows)
        wrong[4]["response"]["result"]["items"] = []
        self.assertEqual(evaluation.score(benchmark, wrong, split="development")["hard_accuracy"], 1)
        self.assertFalse(evaluation.score(benchmark, wrong, split="development")["passed"])
        wrong = deepcopy(rows)
        wrong[4]["response"]["result"]["items"][0]["citation"]["source_sha256"] = "a" * 64
        self.assertFalse(evaluation.score(benchmark, wrong, split="development")["passed"])
        for wrong in (rows[:-1], rows + rows[:1]):
            with self.assertRaisesRegex(ValueError, "observations"):
                evaluation.score(benchmark, wrong, split="development")
        wrong = deepcopy(rows)
        wrong[0]["response"]["release_id"] = "other-release"
        with self.assertRaisesRegex(ValueError, "release"):
            evaluation.score(benchmark, wrong, split="development")

    def test_unanswerable_scoped_no_match_and_nonoverwrite(self):
        benchmark = self.freeze()
        rows = self.observations(benchmark, "held_out")
        result = evaluation.score(benchmark, rows, split="held_out")
        self.assertTrue(result["passed"])
        target = self.root / "artifact.json"
        evaluation.write_new(target, result)
        with self.assertRaises(FileExistsError):
            evaluation.write_new(target, {"replaced": True})
        self.assertEqual(json.loads(target.read_text()), result)

    def test_actual_mcp_collection_and_cli_reports(self):
        benchmark = self.freeze()
        benchmark_file = self.root / "benchmark.json"
        evaluation.write_new(benchmark_file, benchmark)
        rows, protocol = anyio.run(
            lambda: evaluation.collect(benchmark, split="held_out", release=self.path, profile="synthetic"))
        self.assertEqual(protocol, "2025-11-25")
        self.assertTrue(evaluation.score(benchmark, rows, split="held_out")["passed"])
        output = self.root / "run-1"
        command = [sys.executable, "-m", "research_library.evaluation", "run",
                   "--benchmark", str(benchmark_file), "--release", str(self.path),
                   "--allow-profile", "synthetic", "--split", "development", "--output", str(output)]
        response = subprocess.run(command, capture_output=True, text=True, cwd=self.root, timeout=30)
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        result = json.loads((output / "results.json").read_text())
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["benchmark_sha256"]), 64)
        self.assertTrue((output / "report.md").is_file())
        repeated = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(repeated.returncode, 1)
        self.assertIn("already exists", repeated.stderr)
        benchmark["items"][0]["calls"][0]["expected"]["numbers"][0]["denominator"] = 999
        wrong_reference = self.root / "perturbed.json"
        evaluation.write_new(wrong_reference, benchmark)
        failed_output = self.root / "failed-run"
        command[command.index(str(benchmark_file))] = str(wrong_reference)
        command[command.index(str(output))] = str(failed_output)
        failed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertFalse(json.loads((failed_output / "results.json").read_text())["passed"])
