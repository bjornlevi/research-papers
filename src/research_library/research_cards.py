"""Validate external research-card structure and citations, not scientific merit."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, ValidationError

from . import evaluation, releases


TEXT = evaluation.TEXT
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "release_id", "cards"],
    "properties": {
        "schema_version": {"const": 1},
        "release_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "cards": {"type": "array", "minItems": 10, "maxItems": 100, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "question", "rationale", "citations", "uncertainties",
                         "overlap_caveats", "analysis", "feasibility", "data_scope", "novelty"],
            "properties": {
                "id": TEXT, "question": TEXT, "rationale": TEXT, "analysis": TEXT,
                "uncertainties": {"type": "array", "minItems": 1, "items": TEXT},
                "overlap_caveats": {"type": "array", "minItems": 1, "items": TEXT},
                "feasibility": {"enum": ["published_synthesis", "participant_data_reanalysis", "new_data"]},
                "data_scope": {"const": "publication_only_no_participant_access"},
                "citations": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["tool", "evidence_id", "citation"],
                    "properties": {
                        "tool": {"enum": ["get_claim", "get_source_context", "get_table", "get_source_issue"]},
                        "evidence_id": TEXT, "citation": {"type": "object"},
                    },
                }},
                "novelty": {
                    "type": "object", "additionalProperties": False,
                    "required": ["status", "searches"],
                    "properties": {
                        "status": {"enum": ["unverified", "search_documented_not_proven"]},
                        "searches": {"type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["searched_at", "source", "query", "method", "results"],
                            "properties": {key: TEXT for key in
                                           ("searched_at", "source", "query", "method", "results")},
                        }},
                    },
                },
            },
        }},
    },
}


def validate(document, snapshot):
    Draft202012Validator(SCHEMA).validate(document)
    if document["release_id"] != snapshot.release_id:
        raise ValueError("Research cards refer to a different release")
    ids = [card["id"] for card in document["cards"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate research-card IDs")
    for card in document["cards"]:
        novelty = card["novelty"]
        if novelty["status"] == "search_documented_not_proven" and not novelty["searches"]:
            raise ValueError("Documented novelty search requires reproducible search records")
        for search in novelty["searches"]:
            when = datetime.fromisoformat(search["searched_at"].replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError("Literature-search timestamp must include timezone")
        for anchor in card["citations"]:
            argument = {"get_claim": "claim_id", "get_source_context": "unit_id",
                        "get_table": "table_id", "get_source_issue": "issue_id"}[anchor["tool"]]
            result = snapshot.call(anchor["tool"], {argument: anchor["evidence_id"]})["result"]
            if releases.encode(result["citation"]) != releases.encode(anchor["citation"]):
                raise ValueError("Research-card citation differs from canonical release evidence")
    return {
        "schema_version": 1, "release_id": snapshot.release_id,
        "input_sha256": releases.sha256(releases.encode(document)), "card_count": len(ids),
        "structural_checks_passed": True, "scientific_validation": "not_performed",
        "limitations": "Checks structure and exact citation identity, not prose truth, novelty, feasibility or expert merit.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", required=True, type=Path)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--allow-profile", required=True, choices=sorted(releases.PROFILES))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = validate(releases.read_json(args.cards.read_bytes()),
                          releases.validate(args.release, allowed_profiles={args.allow_profile}))
        evaluation.write_new(args.output, result)
    except (OSError, ValueError, ValidationError) as error:
        print(f"Research-card validation error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
