"""Multi-collection acceptance; optionally verify the real authorized local release."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / "portable"))
from fixtures import snapshots
from test_collections import collection_cases, registry_fixture
from test_http import running_server
from test_releases import fixture_bundle
from research_library import __version__, collections, contracts, releases


def run(command, root):
    result = subprocess.run([str(value) for value in command], cwd=root,
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)


def acceptance(root, *, real_release=None, python=None):
    python = Path(python or sys.executable)
    scripts = Path(__file__).resolve().parent
    started = time.monotonic()
    if real_release is None:
        store, first, _, _ = registry_fixture(root)
        profiles = ["synthetic"]
        expected = collection_cases(store)
        original = releases.validate(first, allowed_profiles=set(profiles))
        before = snapshots(first)
        source_path = first
        query = "screening"
    else:
        source_path = real_release.absolute()
        before = snapshots(source_path)
        original = releases.validate(source_path, allowed_profiles={"private_context"})
        profiles = ["private_context", "synthetic"]
        bundle, _ = fixture_bundle(root)
        fixture = root / "synthetic"
        releases.export(bundle, fixture, profile="synthetic")
        store = collections.CollectionStore(
            root / "registry", allowed_profiles=profiles,
            allowed_collections={"istopmm", "test-literature"}, defaults=["istopmm"])
        source_id = store.releases.install(source_path)
        # The second membership is a test view of the same authorized real bytes,
        # not a synthetic relabeling or a claim of broader literature coverage.
        entries = [{
            "id": cid, "title": title, "version": "acceptance-1", "updated_at": "2026-10-19",
            "coverage": coverage, "release_id": source_id, "publications": {},
        } for cid, title, coverage in (
            ("istopmm", "iStopMM", "Existing real, locally authorized iStopMM evidence."),
            ("test-literature", "Test overlap membership",
             "Test membership of the same authorized iStopMM release; not a broader literature dataset."),
        )]
        store.publish({"schema_version": 1, "collections": entries})
        query = "MGUS"
        sid = store.current().release_id
        common = {"snapshot_id": sid, "collections": ["istopmm"]}
        calls = [
            ("list_collections", {}), ("corpus_status", {}),
            ("search_evidence", {"query": query, "limit": 2}),
            ("search_evidence", {"query": query, "limit": 2,
                                 "collections": ["istopmm", "test-literature"]}),
            ("search_evidence", {"query": query, "limit": 2, "offset": 2, **common}),
            ("get_claim", {"claim_id": original._bundle["claims"][0]["id"], **common}),
            ("get_source_context", {"unit_id": original._bundle["units"][0]["id"],
                                    "max_characters": 256, **common}),
            ("get_table", {"table_id": original._bundle["tables"][0]["id"], "limit": 2, **common}),
            ("get_source_issue", {"issue_id": original._bundle["issues"][0]["record"]["id"], **common}),
            ("get_related_evidence", {"evidence_id": original._bundle["claims"][0]["id"], **common}),
            ("list_sources", {"limit": 2, **common}),
            ("list_publications", {}),
            ("catalog_status", {}),
        ]
        expected = {"cases": [
            {"tool": tool, "arguments": args, "response": store.call(tool, args)} for tool, args in calls
        ], "errors": [
            ["get_publication", {"publication_id": "missing", **common}, "not_found"],
            ["search_evidence", {"query": query, "collections": ["unauthorized"]}, "permission_denied"],
            ["get_claim", {"claim_id": "missing", **common}, "not_found"],
        ]}
        # One returned passage has both memberships, not two copies.
        combined = expected["cases"][3]["response"]
        self_refs = combined["collection_context"]["evidence_refs"]
        assert len(self_refs) == 2 * len(combined["result"]["items"])
        assert len({ref["result_index"] for ref in self_refs}) == len(combined["result"]["items"])
        # Replace only the test membership with genuinely synthetic data, then
        # retain old references and roll back without altering the real release.
        replacement = store.replace("test-literature", fixture, version="acceptance-2", updated_at="2026-10-19")
        assert store.current().snapshots["istopmm"].release_id == original.release_id
        assert store.call("search_evidence", {"query": "Fictional", "collections": ["test-literature"]})["result"]["items"]
        assert {ref["collection_id"] for ref in store.call(
            "search_evidence", {"query": query, "collections": ["istopmm", "test-literature"]}
        )["collection_context"]["evidence_refs"]} <= {"istopmm", "test-literature"}
        old = store.call("get_claim", calls[5][1])
        assert old["result"] == original.call("get_claim", {"claim_id": calls[5][1]["claim_id"]})["result"]
        assert store.rollback() == sid
        assert replacement != sid

    # Exact iStopMM-only ranking, source locators and values, not merely counts.
    baseline = original.call("search_evidence", {"query": query, "limit": 5})
    scoped = store.call("search_evidence", {"query": query, "limit": 5, "collections": ["istopmm"]})
    assert scoped["result"] == baseline["result"]
    assert scoped["collection_context"]["selected"][0]["release_id"] == original.release_id
    expected_file = root / "expected.json"
    expected_file.write_text(json.dumps(expected))
    profile_args = [value for profile in profiles for value in ("--allow-profile", profile)]
    run([
        python, scripts / "mcp_client.py", "--server", python.parent / "research-mcp",
        "--registry", store.root, "--allow-collection", "istopmm",
        "--allow-collection", "test-literature", "--default-collection", "istopmm",
        *profile_args, "--expected", expected_file, "--cwd", root,
        "--timeout-seconds", "120" if real_release else "20",
    ], root)
    with running_server(
        root, None, profiles=profiles, python=str(python), collection_registry=store.root,
        allowed_collections=["istopmm", "test-literature"], default_collections=["istopmm"],
        startup_timeout=120 if real_release else 15,
    ) as (url, token, _, _, log):
        run([python, scripts / "http_client.py", "--url", url,
             "--token-file", root / "token", "--expected", expected_file], root)
        log.seek(0)
        assert token not in log.read()
    assert snapshots(source_path) == before
    return {
        "passed": True, "application_version": __version__, "tool_schema_version": contracts.SCHEMA_VERSION,
        "scope": "real authorized iStopMM plus test membership" if real_release else "synthetic fixtures only",
        "original_release_id": original.release_id, "registry_snapshot_id": store.current().release_id,
        "source_bytes_unchanged": True, "single_collection_exact_fidelity": True,
        "stdio_http_parity": True, "seconds": round(time.monotonic() - started, 3),
        "broader_literature_coverage_claimed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-release", type=Path, help="Explicit private_context release; never required by CI")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("Output must be a new file")
    with tempfile.TemporaryDirectory(prefix="research-collections-check-") as temporary:
        report = acceptance(Path(temporary), real_release=args.real_release)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
