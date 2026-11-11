"""M8 mixed-catalog acceptance, with optional authorized real registry verification."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / "portable"))
from test_catalog import mixed_fixture
from test_http import running_server
from research_library import __version__, catalog, collections, contracts

ROOT = Path(__file__).resolve().parent


def backing_checksums(root):
    return {str(path.relative_to(root)): catalog.file_digest(path)
            for path in root.rglob("*") if path.is_file()}


def acceptance(registry, *, profiles, evidence_collection, catalog_collection, temporary, real=False,
               citation_neighborhood=False):
    start = time.monotonic()
    store = collections.CollectionStore(registry, allowed_profiles=profiles,
                                        allowed_collections=[evidence_collection, catalog_collection],
                                        defaults=[evidence_collection, catalog_collection])
    current = store.current()
    sid = current.release_id
    original = current.snapshots[evidence_collection]
    metadata = current.snapshots[catalog_collection]
    before = backing_checksums(store.root)
    assert collections.is_catalog(metadata)
    counts = metadata.coverage()
    assert counts["publication_count"] > 0
    if real:
        if citation_neighborhood:
            assert not counts["diseases"], "Citation adjacency must not invent disease-query memberships"
        else:
            assert set(counts["diseases"]) == {"mgus", "smm", "mm"}
            assert any("review" in t.lower() or "meta-analysis" in t.lower()
                       for t in counts["publication_types"])
    assert counts["full_text_acquired_count"] == 0
    common = {"snapshot_id": sid, "collections": [evidence_collection]}
    literature = {"snapshot_id": sid, "collections": [catalog_collection]}
    calls = [
        ("list_collections", {}), ("corpus_status", {}),
        ("catalog_status", literature),
        ("list_publications", {**literature, "limit": 2}),
        ("get_publication", {**literature, "publication_id": metadata.publication_keys()[0]}),
        ("search_evidence", {"query": "MGUS" if real else "screening", **common, "limit": 2}),
        ("search_evidence", {"query": "MGUS", **literature}),
        ("list_sources", {**common, "limit": 2}),
        ("get_claim", {"claim_id": original._bundle["claims"][0]["id"], **common}),
        ("get_source_context", {"unit_id": original._bundle["units"][0]["id"], "max_characters": 200, **common}),
        ("get_table", {"table_id": original._bundle["tables"][0]["id"], "limit": 1, **common}),
        ("get_source_issue", {"issue_id": original._bundle["issues"][0]["record"]["id"], **common}),
        ("get_related_evidence", {"evidence_id": original._bundle["claims"][0]["id"], **common}),
    ]
    expected = {"cases": [{"tool": tool, "arguments": args, "response": store.call(tool, args)}
                          for tool, args in calls], "errors": [
        ["list_publications", {"collections": ["unauthorized"]}, "permission_denied"],
        ["list_publications", {**literature, "query": "---"}, "invalid_input"],
        ["get_publication", {**literature, "publication_id": "missing"}, "not_found"],
        ["get_claim", {**literature, "claim_id": original._bundle["claims"][0]["id"]}, "not_found"],
    ]}
    keys = metadata.publication_keys()
    exhaustive = len(keys) <= 1000
    offsets = range(0, len(keys), 2) if exhaustive else sorted({0, len(keys) // 2, len(keys) - 1})
    for offset in offsets:
        result = store.call("list_publications", {**literature, "limit": 2, "offset": offset})["result"]
        assert [item["id"] for item in result["items"]] == keys[offset:offset + 2]
        assert result["next_offset"] == (offset + 2 if offset + 2 < len(keys) else None)
    assert not expected["cases"][6]["response"]["result"]["items"]
    # Single-collection evidence semantics are identical to the original release.
    for tool, args in calls:
        if args.get("collections") == [evidence_collection]:
            plain = {key: value for key, value in args.items() if key not in {"collections", "snapshot_id"}}
            expected_result = original.call(tool, plain)["result"]
            if tool == "list_sources":
                for item in expected_result["items"]:
                    item["publication_id"] = current._aliases[evidence_collection, item["doi"]]
            assert store.call(tool, args)["result"] == expected_result, tool
    path = temporary / "expected.json"
    path.write_text(json.dumps(expected))
    arguments = ["--registry", str(store.root), "--expected", str(path), "--cwd", str(temporary)]
    for profile in profiles:
        arguments.extend(["--allow-profile", profile])
    for cid in (evidence_collection, catalog_collection):
        arguments.extend(["--allow-collection", cid, "--default-collection", cid])
    command = [sys.executable, str(ROOT / "mcp_client.py"), "--server",
               str(Path(sys.executable).parent / "research-mcp"), "--timeout-seconds",
               "120" if real else "20", *arguments]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    with running_server(temporary, None, profiles=profiles, collection_registry=store.root,
                        allowed_collections=[evidence_collection, catalog_collection],
                        default_collections=[evidence_collection, catalog_collection],
                        startup_timeout=180 if real else 20) as (url, _, _, _, _):
        result = subprocess.run([sys.executable, str(ROOT / "http_client.py"), "--url", url,
                                 "--token-file", str(temporary / "token"), "--expected", str(path)],
                                capture_output=True, text=True, timeout=240)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    assert backing_checksums(store.root) == before
    return {
        "passed": True, "application_version": __version__, "tool_schema_version": contracts.SCHEMA_VERSION,
        "scope": "authorized real metadata and unchanged iStopMM" if real else "synthetic fixtures",
        "registry_snapshot_id": sid, "catalog_id": metadata.release_id, "coverage": counts,
        "twelve_tool_stdio_http_parity": True, "pagination_complete": exhaustive,
        "pagination_boundary_samples_passed": True,
        "evidence_exact_fidelity": True, "backing_bytes_unchanged": True,
        "scientific_approval": False, "seconds": round(time.monotonic() - start, 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--allow-profile", action="append")
    parser.add_argument("--evidence-collection", default="istopmm")
    parser.add_argument("--catalog-collection", default="literature")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--citation-neighborhood", action="store_true",
                        help="Expect first-hop metadata, not disease-query classifications")
    args = parser.parse_args()
    if args.registry and not args.allow_profile:
        parser.error("Real registry requires explicit --allow-profile")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        if args.registry:
            registry, profiles, cid = args.registry.absolute(), args.allow_profile, args.catalog_collection
        else:
            store, _, _, _ = mixed_fixture(root)
            registry, profiles, cid = store.root, ["synthetic"], "test-literature"
        result = acceptance(registry, profiles=profiles, evidence_collection=args.evidence_collection,
                            catalog_collection=cid, temporary=root, real=bool(args.registry),
                            citation_neighborhood=args.citation_neighborhood)
        if args.report:
            if args.report.exists():
                raise ValueError("Report already exists; choose a new path")
            args.report.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
