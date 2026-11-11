"""Offline research benchmark preparation, approval gates and MCP-only execution."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import sys
import tempfile

import anyio
from jsonschema import Draft202012Validator, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import contracts, releases, retrieval


TEXT = {"type": "string", "minLength": 1, "pattern": "\\S"}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "benchmark_version", "status", "review", "release_id",
                 "tool_schema_version", "bundle_sha256", "source_hashes", "corpus_caveats", "thresholds", "items"],
    "properties": {
        "schema_version": {"const": 1}, "benchmark_version": TEXT,
        "status": {"enum": ["draft", "frozen"]},
        "review": {"type": ["object", "null"], "additionalProperties": False,
                   "required": ["reviewer", "approved_at", "kind"],
                   "properties": {"reviewer": TEXT, "approved_at": TEXT,
                                  "kind": {"enum": ["software", "scientific"]}}},
        "release_id": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"},
        "tool_schema_version": {"const": contracts.SCHEMA_VERSION},
        "bundle_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "source_hashes": {"type": "object", "minProperties": 1,
                          "additionalProperties": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
        "corpus_caveats": {"type": "array", "items": TEXT},
        "thresholds": {
            "type": "object", "additionalProperties": False,
            "required": ["hard_accuracy", "mean_recall_at_k", "k"],
            "properties": {"hard_accuracy": {"const": 1.0},
                           "mean_recall_at_k": {"type": "number", "minimum": 0, "maximum": 1},
                           "k": {"type": "integer", "minimum": 1, "maximum": 100}},
        },
        "items": {
            "type": "array", "minItems": 10, "maxItems": 20,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "split", "question", "scope", "mandatory_caveats", "calls"],
                "properties": {
                    "id": TEXT, "split": {"enum": ["development", "held_out"]},
                    "question": TEXT, "scope": {"enum": ["published_evidence", "corpus_no_match"]},
                    "mandatory_caveats": {"type": "array", "minItems": 1, "items": TEXT},
                    "calls": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["tool", "arguments"],
                        "properties": {
                            "tool": {"enum": list(contracts.TOOLS)}, "arguments": {"type": "object"},
                            "expected": {"type": "object"},
                            "relevant_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": TEXT},
                            "allowed_hits": {"type": "object", "minProperties": 1,
                                             "additionalProperties": {"type": "object"}},
                        },
                        "oneOf": [{"required": ["expected"]}, {"required": ["relevant_ids"]}],
                    }},
                },
            },
        },
    },
}


def validate(benchmark):
    Draft202012Validator(SCHEMA).validate(benchmark)
    ids = [item["id"] for item in benchmark["items"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate benchmark item ID")
    if {item["split"] for item in benchmark["items"]} != {"development", "held_out"}:
        raise ValueError("Both development and held-out splits are required")
    if benchmark["status"] == "frozen" and (benchmark["review"] is None or benchmark["release_id"] is None):
        raise ValueError("Frozen benchmark requires reviewer attestation and a bound release")
    if benchmark["status"] == "draft" and benchmark["review"] is not None:
        raise ValueError("Draft benchmark cannot claim approval")
    for item in benchmark["items"]:
        for call in item["calls"]:
            contracts.request(call["tool"], call["arguments"])
            if "relevant_ids" in call:
                if "allowed_hits" not in call or not set(call["relevant_ids"]) <= set(call["allowed_hits"]):
                    raise ValueError("Ranking requires canonical metadata for relevant and allowed source hits")
                if call["tool"] != "search_evidence" or call["arguments"].get("offset", 0) != 0:
                    raise ValueError("Ranking requires first-page source search")
                if call["arguments"].get("limit", 10) != benchmark["thresholds"]["k"]:
                    raise ValueError("Search limit must equal frozen ranking k")
            else:
                contracts.validate_response(call["tool"], call["arguments"], contracts.success(
                    call["tool"], "benchmark-reference", call["expected"], [],
                ))
            if item["scope"] == "corpus_no_match":
                expected = call.get("expected", {})
                if (call["tool"] != "search_evidence" or expected.get("items") != []
                        or expected.get("next_offset") is not None or expected.get("scope") != contracts.CORPUS_SCOPE):
                    raise ValueError("No-match case must require an empty, corpus-scoped search page")


def draft(bundle, specifications, benchmark_version):
    """Capture provisional expected records, never independent reviewer ground truth."""
    releases.validate_bundle(bundle)
    sections = {
        "get_claim": {r["id"]: r for r in bundle["claims"]},
        "get_source_issue": {r["record"]["id"]: r["record"] for r in bundle["issues"]},
        "get_table": {r["id"]: r for r in bundle["tables"]},
    }
    items = deepcopy(specifications)
    for item in items:
        for call in item["calls"]:
            tool, args = call["tool"], call["arguments"]
            if "relevant_ids" in call:
                if not set(call["relevant_ids"]) <= {r["id"] for r in bundle["units"]}:
                    raise ValueError("Unknown relevant source-unit ID")
                call["allowed_hits"] = {
                    r["id"]: {key: deepcopy(value) for key, value in r.items() if key not in ("text", "truncated")}
                    for r in bundle["units"]
                    if "paper" not in args or retrieval.paper_id(r["citation"]["doi"]) == retrieval.paper_id(args["paper"])
                }
                continue
            if tool in sections:
                identifier = args[{"get_claim": "claim_id", "get_source_issue": "issue_id", "get_table": "table_id"}[tool]]
                result = deepcopy(sections[tool][identifier])
                if tool == "get_table":
                    selected = contracts.request(tool, args)
                    offset, limit = selected["offset"], selected["limit"]
                    rows = result["rows"]
                    result["rows"] = rows[offset:offset + limit]
                    continuation = offset + len(result["rows"])
                    result["next_offset"] = continuation if continuation < len(rows) else None
                    result["truncated"] = result["next_offset"] is not None
            elif tool == "get_related_evidence":
                selected = contracts.request(tool, args)
                rows = [r["record"] for r in bundle["relationships"] if r["from_id"] == args["evidence_id"]]
                offset, limit = selected["offset"], selected["limit"]
                page = rows[offset:offset + limit]
                result = {"items": deepcopy(page), "scope": contracts.CORPUS_SCOPE,
                          "next_offset": offset + len(page) if offset + len(page) < len(rows) else None}
            elif tool == "search_evidence" and item["scope"] == "corpus_no_match":
                result = {"items": [], "next_offset": None, "scope": contracts.CORPUS_SCOPE}
            else:
                raise ValueError("Unsupported draft reference tool")
            call["expected"] = result
    benchmark = {
        "schema_version": 1, "benchmark_version": benchmark_version, "status": "draft", "review": None,
        "release_id": None, "tool_schema_version": contracts.SCHEMA_VERSION,
        "bundle_sha256": releases.sha256(releases.encode(bundle)),
        "source_hashes": {s["id"]: s["sha256"] for s in bundle["sources"]},
        "corpus_caveats": deepcopy(bundle["caveats"]),
        "thresholds": {"hard_accuracy": 1.0, "mean_recall_at_k": 0.9, "k": 10},
        "items": items,
    }
    validate(benchmark)
    return benchmark


def freeze(benchmark, snapshot, *, reviewer, approved_at, review_kind):
    validate(benchmark)
    if benchmark["status"] != "draft":
        raise ValueError("Only a draft can be frozen; never overwrite a frozen benchmark")
    if not reviewer.strip() or not approved_at.strip():
        raise ValueError("Reviewer identity and explicit approval timestamp are required")
    approved = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    if approved.tzinfo is None:
        raise ValueError("Approval timestamp must include a timezone")
    inventory = snapshot._bundle["sources"]
    if benchmark["source_hashes"] != {s["id"]: s["sha256"] for s in inventory}:
        raise ValueError("Release source inventory differs from benchmark references")
    if benchmark["corpus_caveats"] != snapshot._bundle["caveats"]:
        raise ValueError("Release corpus caveats differ from benchmark references")
    for item in benchmark["items"]:
        for call in item["calls"]:
            if "relevant_ids" in call:
                if not set(call["relevant_ids"]) <= {r["id"] for r in snapshot._bundle["units"]}:
                    raise ValueError("Ranking references were omitted by this release profile")
                canonical = {
                    r["id"]: {key: value for key, value in r.items() if key not in ("text", "truncated")}
                    for r in snapshot._bundle["units"]
                }
                if any(releases.encode(metadata) != releases.encode(canonical.get(identifier))
                       for identifier, metadata in call["allowed_hits"].items()):
                    raise ValueError("Allowed ranking metadata differs from the release")
            else:
                actual = snapshot.call(call["tool"], call["arguments"])["result"]
                if releases.encode(actual) != releases.encode(call["expected"]):
                    raise ValueError(f"Reference differs from release: {item['id']}; revise and re-review the draft")
    result = deepcopy(benchmark)
    result.update(status="frozen", release_id=snapshot.release_id,
                  review={"reviewer": reviewer, "approved_at": approved_at, "kind": review_kind})
    validate(result)
    return result


def score(benchmark, observations, *, split):
    validate(benchmark)
    if benchmark["status"] != "frozen":
        raise ValueError("Scoring requires a reviewed, frozen benchmark")
    if split not in ("development", "held_out"):
        raise ValueError("Select development or held_out explicitly")
    selected = [item for item in benchmark["items"] if item["split"] == split]
    expected_keys = {(item["id"], i) for item in selected for i in range(len(item["calls"]))}
    keys = [(row["item_id"], row["call_index"]) for row in observations]
    if len(keys) != len(set(keys)) or set(keys) != expected_keys:
        raise ValueError("Missing, duplicate or unexpected benchmark observations")
    indexed = {(r["item_id"], r["call_index"]): r["response"] for r in observations}
    checks, recalls = [], []
    for item in selected:
        for index, call in enumerate(item["calls"]):
            response = indexed[item["id"], index]
            contracts.validate_response(call["tool"], call["arguments"], response)
            if response["release_id"] != benchmark["release_id"]:
                raise ValueError("Mixed or unapproved release ID in observations")
            if "expected" in call:
                passed = (response["ok"] and response["caveats"] == benchmark["corpus_caveats"]
                          and releases.encode(response["result"]) == releases.encode(call["expected"]))
                checks.append({"item_id": item["id"], "call_index": index, "passed": passed})
            else:
                ids = [r["id"] for r in response["result"]["items"]] if response["ok"] else []
                valid_hits = (response["ok"] and response["caveats"] == benchmark["corpus_caveats"]
                              and len(ids) == len(set(ids)))
                if valid_hits:
                    for hit in response["result"]["items"]:
                        metadata = {key: value for key, value in hit.items() if key != "snippet"}
                        if releases.encode(metadata) != releases.encode(call["allowed_hits"].get(hit["id"])):
                            valid_hits = False
                checks.append({"item_id": item["id"], "call_index": index, "passed": valid_hits})
                relevant = set(call["relevant_ids"])
                recall = len(relevant & set(ids[:benchmark["thresholds"]["k"]])) / len(relevant)
                recalls.append({"item_id": item["id"], "call_index": index, "recall_at_k": recall})
    accuracy = sum(c["passed"] for c in checks) / len(checks) if checks else None
    recall = sum(c["recall_at_k"] for c in recalls) / len(recalls) if recalls else None
    passed = (accuracy == 1.0 and
              (recall is None or recall >= benchmark["thresholds"]["mean_recall_at_k"]))
    return {"schema_version": 1, "benchmark_version": benchmark["benchmark_version"],
            "benchmark_sha256": releases.sha256(releases.encode(benchmark)),
            "release_id": benchmark["release_id"], "split": split,
            "review_kind": benchmark["review"]["kind"],
            "hard_accuracy": accuracy, "mean_recall_at_k": recall, "passed": passed,
            "checks": checks, "ranking": recalls,
            "limitations": "Record fidelity only; not an assessment of free-text answers, hypotheses or scientific truth."}


async def collect(benchmark, *, split, release, profile):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "research_library.server", "--release", str(Path(release).resolve()),
                                      "--allow-profile", profile],
    )
    rows = []
    with tempfile.TemporaryFile(mode="w+") as diagnostics:
        async with stdio_client(parameters, errlog=diagnostics) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                for item in benchmark["items"]:
                    if item["split"] != split:
                        continue
                    for index, call in enumerate(item["calls"]):
                        response = await client.call_tool(call["tool"], call["arguments"])
                        if response.structuredContent is None:
                            raise ValueError("MCP response has no versioned structured evidence")
                        if response.isError == response.structuredContent.get("ok"):
                            raise ValueError("MCP isError disagrees with the evidence envelope")
                        rows.append({"item_id": item["id"], "call_index": index,
                                     "response": response.structuredContent})
    return rows, initialized.protocolVersion


def write_new(path, value):
    with Path(path).open("xb") as stream:
        stream.write(releases.encode(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("draft", help="Capture provisional references, not reviewer-approved ground truth")
    prepare.add_argument("--bundle", required=True, type=Path)
    prepare.add_argument("--specifications", required=True, type=Path)
    prepare.add_argument("--benchmark-version", required=True)
    prepare.add_argument("--output", required=True, type=Path)
    for name in ("freeze", "run"):
        command = sub.add_parser(name)
        command.add_argument("--benchmark", required=True, type=Path)
        command.add_argument("--release", required=True, type=Path)
        command.add_argument("--allow-profile", required=True, choices=sorted(releases.PROFILES))
        command.add_argument("--output", required=True, type=Path, help="New directory for run; new file for freeze")
        if name == "freeze":
            command.add_argument("--reviewer", required=True)
            command.add_argument("--approved-at", required=True)
            command.add_argument("--review-kind", required=True, choices=("software", "scientific"),
                                 help="Software review does not certify scientific reference answers")
        else:
            command.add_argument("--split", required=True, choices=("development", "held_out"))
    args = parser.parse_args()
    try:
        if args.command == "draft":
            benchmark = draft(releases.read_json(args.bundle.read_bytes()),
                              releases.read_json(args.specifications.read_bytes()), args.benchmark_version)
            write_new(args.output, benchmark)
        else:
            benchmark = releases.read_json(args.benchmark.read_bytes())
            validate(benchmark)
            snapshot = releases.validate(args.release, allowed_profiles={args.allow_profile})
            if args.command == "freeze":
                write_new(args.output, freeze(benchmark, snapshot, reviewer=args.reviewer,
                                             approved_at=args.approved_at, review_kind=args.review_kind))
            else:
                if benchmark["status"] != "frozen" or benchmark["release_id"] != snapshot.release_id:
                    raise ValueError("Run requires a frozen benchmark bound to the selected release")
                if args.output.exists():
                    raise ValueError("Run output directory already exists")
                async def execute():
                    with anyio.fail_after(120):
                        return await collect(benchmark, split=args.split, release=args.release, profile=args.allow_profile)
                observations, protocol = anyio.run(execute)
                result = score(benchmark, observations, split=args.split)
                result.update(protocol_version=protocol, sdk_version=version("mcp"),
                              python_version=sys.version.split()[0], executed_at=datetime.now(timezone.utc).isoformat())
                args.output.mkdir(parents=True)
                write_new(args.output / "results.json", result)
                write_new(args.output / "observations.json", observations)
                report = (f"# Retrieval evaluation: {result['benchmark_version']}\n\n"
                          f"Release: `{result['release_id']}`\n\nSplit: {args.split}\n\n"
                          f"Passed: {result['passed']}\n\nHard accuracy: {result['hard_accuracy']}\n\n"
                          f"Review kind: {result['review_kind']} (software is not scientific validation)\n\n"
                          f"Mean recall@k: {result['mean_recall_at_k']}\n\n{result['limitations']}\n\n"
                          + "\n".join(f"- {c['item_id']} call {c['call_index']}: {'PASS' if c['passed'] else 'FAIL'}"
                                      for c in result["checks"]) + "\n\n"
                          + "\n".join(f"- {c['item_id']} call {c['call_index']}: recall@k {c['recall_at_k']}"
                                      for c in result["ranking"]) + "\n")
                with (args.output / "report.md").open("x") as stream:
                    stream.write(report)
                print(json.dumps({"passed": result["passed"], "output": str(args.output)}))
                return 0 if result["passed"] else 1
        print(json.dumps({"command": args.command, "output": str(args.output)}))
        return 0
    except (OSError, ValueError, KeyError, ValidationError, TimeoutError) as error:
        print(f"Evaluation error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
