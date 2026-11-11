"""Operator quality report, review, defect recording and derived-text repair."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from jsonschema import ValidationError

from . import __version__, quality, releases


def repair(snapshot, destination, *, unit_id=None, source_id=None, locator=None, text, reviewer, note):
    quality._operator(reviewer, note)
    if not text.strip():
        raise releases.ReleaseError("Correction must contain text; verify a genuinely blank location separately")
    if snapshot.profile == "curated":
        raise releases.ReleaseError("Curated-only releases cannot be repaired into extracted-text releases")
    bundle = deepcopy(snapshot._bundle)
    if unit_id is not None:
        unit = next((unit for unit in bundle["units"] if unit["id"] == unit_id), None)
        if unit is None:
            raise releases.ReleaseError("Unknown unit to repair")
        previous = quality.identity(unit)
        if unit["text"] == text and not unit["truncated"]:
            raise releases.ReleaseError("Correction does not change the retained text")
    else:
        source = next((source for source in bundle["sources"] if source["id"] == source_id), None)
        if source is None or locator is None:
            raise releases.ReleaseError("Adding missing text requires a known source and original locator")
        key = quality.locator_key(locator)
        if any(unit["citation"]["source_id"] == source_id and unit["citation"]["locator"] == locator
               for unit in bundle["units"]):
            raise releases.ReleaseError("Location exists; repair its unit ID instead")
        unit = {
            "id": source_id + ":quality-" + key[:16],
            "citation": {"source_id": source_id, "doi": source["doi"], "source_sha256": source["sha256"],
                         "source_url": source["url"], "provenance": source["provenance"],
                         "availability": "metadata_only", "locator": locator},
            "verification_status": "machine_extracted", "human_verification": "not_reviewed",
            "caveats": [], "source_issue_ids": [],
        }
        bundle["units"].append(unit)
        previous = "missing"
    unit.update(text=text, truncated=False, human_verification="not_reviewed", verification_status="not_reviewed")
    unit["caveats"].append(
        f"Operator correction by {reviewer} at {datetime.now(timezone.utc).isoformat()}; "
        f"previous unit SHA-256: {previous}; parent release: {snapshot.release_id}; {note}")
    if snapshot._manifest["assets"]:
        raise releases.ReleaseError("Repair a no-originals export; original-containing profiles are not public candidates")
    return releases.export(bundle, destination, profile=snapshot.profile,
                           authorization=snapshot._manifest["authorization"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--allow-profile", required=True, action="append", choices=sorted(releases.PROFILES))
    parser.add_argument("--quality-root", type=Path, default=Path("releases/data-quality"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report")
    commands.add_parser("check")
    inventory = commands.add_parser("inventory")
    paths = inventory.add_mutually_exclusive_group(required=True)
    paths.add_argument("--asset-map", type=Path, help="Source-ID to local original path JSON mapping")
    paths.add_argument("--papers", type=Path, help="Find checksum-matched originals below this folder")
    review = commands.add_parser("review")
    review.add_argument("finding_id")
    review.add_argument("--decision", required=True, choices=["verified", "verified_blank", "verified_nontext"])
    problem = commands.add_parser("problem")
    problem.add_argument("--unit-id", required=True)
    correction = commands.add_parser("repair")
    correction.add_argument("destination", type=Path)
    target = correction.add_mutually_exclusive_group(required=True)
    target.add_argument("--unit-id")
    target.add_argument("--source-id", help="Add a missing unit; requires --locator")
    correction.add_argument("--locator", help="JSON original locator for a missing unit")
    correction.add_argument("--text-file", required=True, type=Path)
    for selected in (review, problem, correction):
        selected.add_argument("--reviewer", required=True)
        selected.add_argument("--note", required=True)
    args = parser.parse_args()
    try:
        snapshot = releases.validate(args.release, allowed_profiles=set(args.allow_profile))
        if args.command == "inventory":
            # Build-only dependency; reporting/serving must work without PyMuPDF.
            try:
                from .build.quality_inventory import expected_locations
            except ModuleNotFoundError as error:
                if error.name != "pymupdf":
                    raise
                raise releases.ReleaseError("Original inventory requires the [build] extra; run make setup") from error
            from .catalog import file_digest
            paths = releases.read_json(args.asset_map.read_bytes()) if args.asset_map else {}
            originals = {}
            if args.papers:
                for path in sorted(args.papers.rglob("*")):
                    if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".pdf", ".docx", ".xlsx"}:
                        originals[file_digest(path)] = path
            for source in snapshot._bundle["sources"]:
                path = Path(paths[source["id"]]) if source["id"] in paths else originals.get(source["sha256"])
                if path is None or file_digest(path) != source["sha256"]:
                    raise releases.ReleaseError("Missing or changed original for " + source["id"])
                locators = expected_locations(path, source["format"])
                if file_digest(path) != source["sha256"]:
                    raise releases.ReleaseError("Original changed during inventory: " + source["id"])
                quality.inventory(args.quality_root, source, locators)
            result = {"inventoried_sources": len(snapshot._bundle["sources"]), "release_id": snapshot.release_id}
        elif args.command in {"report", "check"}:
            result = quality.report(snapshot, args.quality_root)
            print(json.dumps({"application_version": __version__, **result}, ensure_ascii=False))
            return 0 if args.command == "report" or result["ready"] and not snapshot._manifest["assets"] else 1
        elif args.command == "review":
            result = quality.review(snapshot, args.quality_root, args.finding_id, args.decision, args.reviewer, args.note)
        elif args.command == "problem":
            result = quality.problem(snapshot, args.quality_root, args.unit_id, args.reviewer, args.note)
        else:
            if bool(args.source_id) != bool(args.locator):
                raise releases.ReleaseError("--source-id and --locator must be supplied together")
            result = {"release_id": repair(
                snapshot, args.destination, unit_id=args.unit_id, source_id=args.source_id,
                locator=releases.read_json(args.locator.encode()) if args.locator else None,
                text=args.text_file.read_text(encoding="utf-8"), reviewer=args.reviewer, note=args.note)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ValidationError) as error:
        print(f"Quality operation failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
