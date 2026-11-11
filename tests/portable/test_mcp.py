import asyncio
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.shared.session import RequestResponder

from research_library import releases
from research_library.server import EvidenceServer
from test_releases import fixture_bundle


def protocol_cases(snapshot):
    requests = [
        ("list_collections", {}),
        ("list_sources", {}),
        ("list_publications", {}),
        ("catalog_status", {}),
        ("corpus_status", {}),
        ("search_evidence", {"query": "screening", "limit": 1}),
        ("search_evidence", {"query": "screening", "limit": 1, "offset": 1}),
        ("search_evidence", {"query": "unmatchedxyz"}),
        ("search_evidence", {"query": "LC-MGUS prevalence decreased standard reference intervals",
                             "paper": "10.1001/jamaoncol.2025.1285", "limit": 5}),
        ("search_evidence", {"query": '"screening', "limit": 1}),
        ("search_evidence", {"query": "screen* OR unmatchedxyz", "mode": "fts5", "limit": 1}),
        ("get_claim", {"claim_id": "fictional-claim"}),
        ("get_source_context", {"unit_id": "fictional-unit-1", "max_characters": 15}),
        ("get_source_context", {"unit_id": "fictional-word-footnote"}),
        ("get_table", {"table_id": "fictional-table", "limit": 2}),
        ("get_table", {"table_id": "fictional-table", "limit": 2, "offset": 2}),
        ("get_source_issue", {"issue_id": "fictional-issue"}),
        ("get_related_evidence", {"evidence_id": "fictional-claim"}),
    ]
    return {"cases": [{"tool": name, "arguments": args, "response": snapshot.call(name, args)}
                      for name, args in requests],
            "errors": [
                ["get_publication", {"publication_id": "missing"}, "not_found"],
                ["get_claim", {"claim_id": "DO_NOT_ECHO"}, "not_found"],
                ["get_source_context", {"unit_id": "fictional-unit-1", "max_characters": 2049}, "invalid_input"],
                ["get_table", {"table_id": "fictional-table", "limit": 101}, "invalid_input"],
                ["get_source_issue", {"issue_id": "missing"}, "not_found"],
                ["get_related_evidence", {"evidence_id": "missing"}, "not_found"],
                ["search_evidence", {"query": '"DO_NOT_ECHO', "mode": "fts5"}, "invalid_input"],
                ["search_evidence", {"query": "DO_NOT_ECHO", "unexpected": 1}, "invalid_input"],
            ]}


class MCPTests(unittest.TestCase):
    def test_sdk_cancellation_stays_request_local_without_hiding_shutdown(self):
        async def exercise():
            server = EvidenceServer("cancellation-regression")
            responder = Mock(spec=RequestResponder)
            responder.cancelled = True
            with patch.object(Server, "_handle_message", new=AsyncMock(side_effect=asyncio.CancelledError)):
                await server._handle_message(responder, None, None)
                responder.cancelled = False
                with self.assertRaises(asyncio.CancelledError):
                    await server._handle_message(responder, None, None)
                with self.assertRaises(asyncio.CancelledError):
                    await server._handle_message(object(), None, None)
            with patch.object(Server, "_handle_message", new=AsyncMock(side_effect=RuntimeError("failure"))):
                with self.assertRaisesRegex(RuntimeError, "failure"):
                    await server._handle_message(responder, None, None)
        asyncio.run(exercise())

    def test_store_activation_and_permission_errors_over_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, _ = fixture_bundle(root)
            bundle["tables"][0]["rows"] = [["x" * 4096, "x" * 4096] for _ in range(100)]
            first = root / "first"
            first_id = releases.export(bundle, first, profile="synthetic")
            bundle["claims"][0]["statement"] = "Fictional updated release"
            second = root / "second"
            second_id = releases.export(bundle, second, profile="synthetic")
            store = releases.ReleaseStore(root / "store", allowed_profiles={"synthetic"})
            store.install(first)
            store.install(second)
            store.activate(first_id)
            curated = root / "curated"
            grants = {source["id"]: {"scopes": ["curated"], "basis": "Authored fictional fixture"}
                      for source in bundle["sources"]}
            releases.export(bundle, curated, profile="curated", authorization=grants)

            async def exercise():
                server = str(Path(sys.executable).parent / "research-mcp")
                with tempfile.TemporaryFile(mode="w+") as diagnostics:
                    params = StdioServerParameters(command=server, args=[
                        "--store", str(store.root), "--allow-profile", "synthetic"], cwd=str(root))
                    async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                            await client.initialize()
                            initial = await client.call_tool("corpus_status")
                            self.assertEqual(initial.structuredContent["release_id"], first_id)
                            oversized = await client.call_tool("get_table", {"table_id": "fictional-table", "limit": 100})
                            self.assertTrue(oversized.isError)
                            self.assertEqual(oversized.structuredContent["error"]["code"], "response_too_large")
                            store.activate(second_id)
                            updated = await client.call_tool("get_claim", {"claim_id": "fictional-claim"})
                            self.assertEqual(updated.structuredContent["release_id"], second_id)
                            self.assertEqual(updated.structuredContent["result"]["statement"], "Fictional updated release")
                            (store.root / "releases" / second_id / "records.json").write_text("corrupt")
                            failed = await client.call_tool("corpus_status")
                            self.assertTrue(failed.isError)
                            self.assertEqual(failed.structuredContent["error"]["code"], "invalid_release")
                            store.rollback()
                            restored = await client.call_tool("corpus_status")
                            self.assertEqual(restored.structuredContent["release_id"], first_id)
                    params = StdioServerParameters(command=server, args=[
                        "--release", str(curated), "--allow-profile", "curated"], cwd=str(root))
                    async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                            await client.initialize()
                            denied = await client.call_tool("get_source_context", {"unit_id": "fictional-unit-1"})
                            self.assertTrue(denied.isError)
                            self.assertEqual(denied.structuredContent["error"]["code"], "permission_denied")
                            usable = await client.call_tool("get_claim", {"claim_id": "fictional-claim"})
                            self.assertFalse(usable.isError)
                            self.assertEqual(usable.structuredContent["result"]["citation"]["availability"], "metadata_only")
            asyncio.run(exercise())

    def test_independent_protocol_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, _ = fixture_bundle(root)
            # Nonzero estimate, CI and adjustment must survive the transport intact.
            number = bundle["claims"][0]["numbers"][0]
            number.update(value=1.23, ci_95=[1.01, 1.49], reported_value="1.23 (1.01-1.49)",
                          adjustment="Fictional age and sex adjustment", denominator=72)
            path = root / "release"
            releases.export(bundle, path, profile="synthetic")
            snapshot = releases.validate(path, allowed_profiles={"synthetic"})
            expected = root / "expected.json"
            expected.write_text(json.dumps(protocol_cases(snapshot)))
            for item in path.rglob("*"):
                item.chmod(0o555 if item.is_dir() else 0o444)
            path.chmod(0o555)
            try:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve().parents[1] / "mcp_client.py"),
                     "--server", str(Path(sys.executable).parent / "research-mcp"),
                     "--release", str(path), "--expected", str(expected), "--cwd", str(root)],
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                path.chmod(0o755)
                for item in path.rglob("*"):
                    item.chmod(0o755 if item.is_dir() else 0o644)

    def test_startup_fails_closed_without_protocol_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, _ = fixture_bundle(root)
            path = root / "release"
            releases.export(bundle, path, profile="synthetic")
            for target, profile in ((root / "missing", "synthetic"), (path, "curated")):
                result = subprocess.run(
                    [sys.executable, "-m", "research_library.server", "--release", str(target),
                     "--allow-profile", profile], input="", capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr)["event"], "startup_failed")
            (path / "records.json").write_text("corrupt DO_NOT_ECHO")
            result = subprocess.run(
                [sys.executable, "-m", "research_library.server", "--release", str(path),
                 "--allow-profile", "synthetic"], input="", capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("DO_NOT_ECHO", result.stderr)
