"""Local source-bound quality records and explicit public readiness checks."""

from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from . import contracts, releases


def identity(value):
    return releases.sha256(releases.encode(value))


def locator_key(locator):
    Draft202012Validator({"$ref": "#/$defs/locator", "$defs": contracts.DOCUMENT["$defs"]}).validate(locator)
    return identity(locator)


def source_key(source):
    return identity({"source_id": source["id"], "source_sha256": source["sha256"]})


def extraction_key(source, units):
    return identity({"source": source, "units": sorted(units, key=lambda unit: unit["id"])})


def unit_key(unit):
    return identity({key: unit[key] for key in ("id", "citation", "text", "truncated")})


def problem_unit_key(unit):
    citation = unit["citation"]
    return identity({"id": unit["id"], "text": unit["text"], "truncated": unit["truncated"],
                     "citation": {key: citation[key] for key in ("source_id", "source_sha256", "doi", "locator")}})


def _read(path):
    if path.is_symlink() or path.stat().st_size > releases.MAX_PAYLOAD_BYTES:
        raise releases.ReleaseError("Invalid quality record path or size")
    return releases.read_json(path.read_bytes())


def _write(root, section, record):
    from .collections import atomic_json
    root = Path(root)
    if root.is_symlink():
        raise releases.ReleaseError("Symlinked quality directory")
    folder = root / section
    atomic_json(folder, identity(record) + ".json", record)


def inventory(root, source, locators):
    keys = [locator_key(locator) for locator in locators]
    if not keys or len(set(keys)) != len(keys):
        raise releases.ReleaseError("Expected locations must be nonempty and unique")
    if any((locator["kind"] == "pdf") != (source["format"] == "pdf") for locator in locators):
        raise releases.ReleaseError("Expected locator format differs from source")
    if source["format"] == "pdf" and sorted(locator["pdf_page"] for locator in locators) != list(range(1, len(keys) + 1)):
        raise releases.ReleaseError("PDF inventory must cover every original page in order")
    record = {"schema_version": 1, "source_id": source["id"], "source_sha256": source["sha256"],
              "format": source["format"], "locators": sorted(locators, key=locator_key)}
    from .collections import atomic_json
    root = Path(root)
    if root.is_symlink():
        raise releases.ReleaseError("Symlinked quality directory")
    atomic_json(root / "inventory", source_key(source) + ".json", record)
    return record


def _inventory(root, source):
    path = root / "inventory" / (source_key(source) + ".json")
    if path.is_symlink():
        raise releases.ReleaseError("Symlinked quality inventory")
    if not path.exists():
        return None
    record = _read(path)
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "source_id", "source_sha256", "format", "locators"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or not isinstance(record["locators"], list)
            or (record["source_id"], record["source_sha256"], record["format"]) !=
            (source["id"], source["sha256"], source["format"])):
        raise releases.ReleaseError("Quality inventory source identity differs")
    keys = [locator_key(locator) for locator in record["locators"]]
    if not keys or len(keys) != len(set(keys)):
        raise releases.ReleaseError("Invalid quality inventory locations")
    if any((locator["kind"] == "pdf") != (source["format"] == "pdf") for locator in record["locators"]):
        raise releases.ReleaseError("Quality inventory locator format differs")
    if source["format"] == "pdf" and sorted(locator.get("pdf_page", 0) for locator in record["locators"]) != list(range(1, len(keys) + 1)):
        raise releases.ReleaseError("Incomplete PDF inventory")
    return record


def _events(root, section):
    folder = root / section
    if folder.is_symlink():
        raise releases.ReleaseError("Symlinked quality records")
    records = []
    for path in sorted(folder.glob("*.json")):
        record = _read(path)
        if path.stem != identity(record):
            raise releases.ReleaseError("Quality record checksum differs")
        expected = {"schema_version", "finding_id", "decision", "reviewer", "note", "at"} if section == "reviews" else {
            "schema_version", "source_key", "extraction_key", "unit_id", "reviewer", "note", "at"}
        if (not isinstance(record, dict) or set(record) != expected
                or type(record["schema_version"]) is not int
                or record["schema_version"] not in ({1} if section == "reviews" else {1, 2})
                or any(not isinstance(record[field], str) or not record[field].strip()
                       for field in expected - {"schema_version"})):
            raise releases.ReleaseError("Invalid quality operator record")
        when = datetime.fromisoformat(record["at"])
        if when.tzinfo is None:
            raise releases.ReleaseError("Quality record timestamp needs a timezone")
        if section == "reviews" and record["decision"] not in {"verified", "verified_blank", "verified_nontext"}:
            raise releases.ReleaseError("Invalid quality review decision")
        records.append(record)
    return records


def report(snapshot, root):
    root = Path(root)
    if root.is_symlink() or (root / "inventory").is_symlink():
        raise releases.ReleaseError("Symlinked quality directory")
    reviews = _events(root, "reviews")
    problems = _events(root, "problems")
    approved = {event["finding_id"]: event for event in sorted(reviews, key=lambda event: event["at"])}
    findings = []
    sources = snapshot._bundle["sources"]
    unit_groups = {source["id"]: [] for source in sources}
    for unit in snapshot._bundle["units"]:
        unit_groups[unit["citation"]["source_id"]].append(unit)
    extraction_keys = {source["id"]: extraction_key(source, unit_groups[source["id"]]) for source in sources}

    def add(source, units, code, message, *, locator=None, unit_id=None, evidence=None):
        details = {"source_key": source_key(source), "extraction_key": extraction_keys[source["id"]],
                   "code": code, "locator": locator, "unit_id": unit_id, "evidence": evidence}
        finding_id = identity(details)
        review = approved.get(finding_id)
        decisions = ({"verified"} if code in {"coverage_review", "ocr_review", "extraction_review"}
                     else {"verified_blank"} if code in {"missing_text", "missing_unit"} and source["format"] == "pdf"
                     else {"verified_nontext"} if code in {"missing_text", "missing_unit"}
                     else set())
        resolved = review is not None and review["decision"] in decisions
        findings.append({
            "id": finding_id, "source_id": source["id"], "source_sha256": source["sha256"],
            "publication_id": source["doi"], "title": source.get("bibliography", {}).get("title"),
            "document_role": (source["id"].rsplit(":", 1)[-1]
                              if source["id"].rsplit(":", 1)[-1] == "main"
                              or source["id"].rsplit(":", 1)[-1].startswith("supplement-") else "source_version"),
            "source_url": source["url"], "locator": locator, "unit_id": unit_id,
            "code": code, "message": message, "resolved": resolved,
            "review": review, "allowed_decisions": sorted(decisions),
            "next_action": ("Review completed." if resolved else
                            "Generate expected locations from the checksum-matched original." if code == "inventory_missing" else
                            "Create a corrected candidate; a review cannot waive this defect." if not decisions else
                            "Compare with the original; record verification or repair the text."),
        })

    for source in sources:
        units = unit_groups[source["id"]]
        if source["id"] in snapshot._manifest["assets"]:
            add(source, units, "original_included",
                "Original files are included in this release; export a no-originals public candidate.")
        expected = _inventory(root, source)
        if expected is None:
            add(source, units, "inventory_missing", "Expected original locations have not been inventoried.")
        else:
            add(source, units, "coverage_review",
                "Compare extraction coverage with the original, including figures/tables and reading order.",
                evidence=expected)
            actual = {}
            for unit in units:
                key = locator_key(unit["citation"]["locator"])
                actual.setdefault(key, []).append(unit)
            expected_keys = {locator_key(locator) for locator in expected["locators"]}
            for locator in expected["locators"]:
                if locator_key(locator) not in actual:
                    add(source, units, "missing_unit", "Expected original location has no retained text unit.", locator=locator,
                        evidence=expected)
            for key, matching in actual.items():
                if key not in expected_keys or len(matching) > 1:
                    add(source, units, "locator_mismatch", "Unexpected or duplicate original location.",
                        locator=matching[0]["citation"]["locator"], evidence=expected)
        for unit in units:
            locator = unit["citation"]["locator"]
            if not unit["text"].strip():
                add(source, units, "missing_text", "Retained text is empty.", locator=locator, unit_id=unit["id"])
            if unit["truncated"]:
                add(source, units, "truncated_text", "Stored extraction is marked truncated.",
                    locator=locator, unit_id=unit["id"])
            warnings = " ".join(unit["caveats"]).lower()
            if "ocr" in warnings or "[ocr" in unit["text"].lower():
                add(source, units, "ocr_review", "OCR text requires comparison with the original, regardless of confidence.",
                    locator=locator, unit_id=unit["id"])
            elif any(word in warnings for word in (
                    "sparse_text", "ocr_required", "blank_or_vector_only", "needs_visual_review", "extraction failed")):
                add(source, units, "extraction_review", "Extraction warning requires visual comparison.",
                    locator=locator, unit_id=unit["id"])
        for event in problems:
            if event["source_key"] == source_key(source):
                unit = next((unit for unit in units if unit["id"] == event["unit_id"]), None)
                if unit is not None and event["extraction_key"] == (
                        problem_unit_key(unit) if event["schema_version"] == 2 else unit_key(unit)):
                    add(source, units, "reported_defect", event["note"], unit_id=unit["id"],
                        locator=unit["citation"]["locator"], evidence=event)
    blockers = [finding for finding in findings if not finding["resolved"]]
    return {"schema_version": 1, "release_id": snapshot.release_id,
            "sources": len(sources), "units": len(snapshot._bundle["units"]),
            "ready": not blockers, "blockers": len(blockers), "findings": findings,
            "limitation": "No known unresolved defects is not proof of flawless OCR or scientific correctness."}


def require_ready(snapshot, root):
    result = report(snapshot, root)
    if not result["ready"]:
        first = next(finding for finding in result["findings"] if not finding["resolved"])
        raise releases.ReleaseError(
            f"Public activation blocked: {result['blockers']} unresolved findings; "
            f"{first['source_id']} {first['locator']}: {first['message']} Run the quality report.")
    if snapshot._manifest["assets"]:
        raise releases.ReleaseError("Public evidence must not include original files; link to publication sources")
    return result


def review(snapshot, root, finding_id, decision, reviewer, note):
    result = report(snapshot, root)
    finding = next((item for item in result["findings"] if item["id"] == finding_id), None)
    if finding is None or decision not in finding["allowed_decisions"]:
        raise releases.ReleaseError("Unknown/stale finding or invalid review; defects must be repaired, not waived")
    _operator(reviewer, note)
    event = {"schema_version": 1, "finding_id": finding_id, "decision": decision,
             "reviewer": reviewer, "note": note, "at": datetime.now(timezone.utc).isoformat()}
    _write(root, "reviews", event)
    return event


def _operator(reviewer, note):
    if any(not isinstance(value, str) or not value.strip() or len(value) > 4096 for value in (reviewer, note)):
        raise releases.ReleaseError("A reviewer and a meaningful comparison/repair note are required")


def problem(snapshot, root, unit_id, reviewer, note):
    _operator(reviewer, note)
    unit = snapshot._lookup("units", unit_id)
    source = next(source for source in snapshot._bundle["sources"] if source["id"] == unit["citation"]["source_id"])
    event = {"schema_version": 2, "source_key": source_key(source), "extraction_key": problem_unit_key(unit),
             "unit_id": unit_id, "reviewer": reviewer, "note": note, "at": datetime.now(timezone.utc).isoformat()}
    _write(root, "problems", event)
    return event


def public_policy(store_root, quality_root=None):
    """Public stores stay gated even when subsequent commands omit the flag."""
    from .collections import atomic_json
    root = Path(store_root)
    path = root / "public-policy.json"
    if root.is_symlink() or path.is_symlink():
        raise releases.ReleaseError("Symlinked public store policy")
    if path.exists():
        policy = _read(path)
        if (not isinstance(policy, dict) or set(policy) != {"schema_version", "quality_root"}
                or type(policy["schema_version"]) is not int or policy["schema_version"] != 1
                or not isinstance(policy["quality_root"], str) or not Path(policy["quality_root"]).is_absolute()):
            raise releases.ReleaseError("Invalid public store policy")
        if quality_root is not None and str(Path(quality_root).absolute()) != policy["quality_root"]:
            raise releases.ReleaseError("Public store quality directory cannot be changed")
        return Path(policy["quality_root"])
    if quality_root is not None:
        if (root / "active.json").exists():
            raise releases.ReleaseError("Create a separate public store; do not convert active development in place")
        selected = Path(quality_root).absolute()
        atomic_json(root, path.name, {"schema_version": 1, "quality_root": str(selected)})
        return selected
    return None
