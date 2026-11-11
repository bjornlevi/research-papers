from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jsonschema import ValidationError

from research_library import research_cards, releases
from test_releases import fixture_bundle


def cards(snapshot):
    citation = snapshot.call("get_claim", {"claim_id": "fictional-claim"})["result"]["citation"]
    return {
        "schema_version": 1, "release_id": snapshot.release_id,
        "cards": [{
            "id": f"fictional-card-{index}", "question": "Fictional software test question",
            "rationale": "Fixture, not a proposed scientific conclusion.",
            "citations": [{"tool": "get_claim", "evidence_id": "fictional-claim",
                           "citation": deepcopy(citation)}],
            "uncertainties": ["Publication evidence cannot establish participant-level interactions."],
            "overlap_caveats": ["Shared cohorts are not replication; missing edges do not imply independence."],
            "analysis": "A future study would require an independently approved analysis.",
            "feasibility": "participant_data_reanalysis",
            "data_scope": "publication_only_no_participant_access",
            "novelty": {"status": "unverified", "searches": []},
        } for index in range(10)],
    }


class ResearchCardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        bundle, _ = fixture_bundle(self.root)
        self.path = self.root / "release"
        releases.export(bundle, self.path, profile="synthetic")
        self.snapshot = releases.validate(self.path, allowed_profiles={"synthetic"})
        self.cards = cards(self.snapshot)

    def test_ten_card_structure_and_citation_audit_not_scientific_review(self):
        result = research_cards.validate(self.cards, self.snapshot)
        self.assertEqual(result["card_count"], 10)
        self.assertEqual(result["scientific_validation"], "not_performed")
        searched = deepcopy(self.cards)
        searched["cards"][0]["novelty"] = {
            "status": "search_documented_not_proven",
            "searches": [{"searched_at": "2026-10-16T00:00:00Z", "source": "Fictional index",
                          "query": "Fictional query", "method": "Test only", "results": "Not a real novelty search"}],
        }
        research_cards.validate(searched, self.snapshot)

    def test_adversarial_cards_reject_invented_access_novelty_and_citations(self):
        for mutate in (
            lambda d: d["cards"][0].update(data_scope="participant_data_available"),
            lambda d: d["cards"][0]["novelty"].update(status="proven_novel"),
            lambda d: d["cards"][0]["novelty"].update(status="search_documented_not_proven"),
            lambda d: d["cards"][0]["citations"][0]["citation"].update(source_sha256="a" * 64),
            lambda d: d["cards"][0]["citations"][0]["citation"]["locator"].update(pdf_page=999),
            lambda d: d["cards"][0].update(uncertainties=[]),
            lambda d: d["cards"][0].update(overlap_caveats=[]),
            lambda d: d["cards"][0].update(feasibility="already_answered_by_patient_data"),
            lambda d: d["cards"][1].update(id=d["cards"][0]["id"]),
            lambda d: d.update(release_id="a" * 64),
        ):
            broken = deepcopy(self.cards)
            mutate(broken)
            with self.assertRaises((ValueError, ValidationError)):
                research_cards.validate(broken, self.snapshot)

    def test_card_cli_retains_audit_and_refuses_overwrite(self):
        source, target = self.root / "cards.json", self.root / "audit.json"
        source.write_text(json.dumps(self.cards))
        command = [sys.executable, "-m", "research_library.research_cards",
                   "--cards", str(source), "--release", str(self.path),
                   "--allow-profile", "synthetic", "--output", str(target)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(target.read_text())["scientific_validation"], "not_performed")
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
