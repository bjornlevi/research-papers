import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient
from starlette.responses import JSONResponse

from research_library import http_auth, http_server, releases
from test_mcp import protocol_cases
from test_releases import fixture_bundle


@contextmanager
def running_server(root, release, profiles=("synthetic",), store=None, extra=(), python=None, tls=False,
                   collection_registry=None, allowed_collections=(), default_collections=(),
                   startup_timeout=15):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"{'https' if tls else 'http'}://127.0.0.1:{port}/mcp"
    verify = ssl.create_default_context(cafile=str(root / "cert.pem")) if tls else True
    registry = root / "credentials.json"
    token_file = root / "token"
    identifier = http_auth.issue(registry, token_file, url, list(profiles))
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen([
            python or sys.executable, "-m", "research_library.http_server",
            *(["--registry", str(collection_registry)] if collection_registry else
              ["--store", str(store)] if store else ["--release", str(release)]),
            *(argument for cid in allowed_collections for argument in ("--allow-collection", cid)),
            *(argument for cid in default_collections for argument in ("--default-collection", cid)),
            *(argument for p in profiles for argument in ("--allow-profile", p)),
            "--token-registry", str(registry), "--port", str(port), "--base-url", url, *extra,
        ], stdout=log, stderr=log, cwd=root)
        try:
            startup_deadline = time.monotonic() + startup_timeout
            while time.monotonic() < startup_deadline:
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read())
                try:
                    if httpx.get(url.replace("/mcp", "/readyz"), timeout=1, verify=verify).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                raise TimeoutError("HTTP server did not become ready")
            yield url, token_file.read_text().strip(), registry, identifier, log
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle, _ = fixture_bundle(self.root)
        self.release = self.root / "release"
        releases.export(self.bundle, self.release, profile="synthetic")
        self.snapshot = releases.validate(self.release, allowed_profiles={"synthetic"})

    def test_actual_http_sdk_parity_and_reconnect(self):
        expected = self.root / "expected.json"
        expected.write_text(json.dumps(protocol_cases(self.snapshot)))
        before = {p: p.read_bytes() for p in self.release.rglob("*") if p.is_file()}
        with running_server(self.root, self.release) as (url, token, registry, identifier, log):
            for _ in range(2):
                result = subprocess.run([
                    sys.executable, str(Path(__file__).resolve().parents[1] / "http_client.py"),
                    "--url", url, "--token-file", self.root / "token", "--expected", expected,
                ], capture_output=True, text=True, timeout=40, cwd=self.root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            log.seek(0)
            diagnostics = log.read()
            self.assertNotIn(token, diagnostics)
            self.assertNotIn("DO_NOT_ECHO", diagnostics)
        self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))

    def test_authentication_all_tools_expiry_revocation_audience_and_profiles(self):
        with running_server(self.root, self.release) as (url, token, registry, identifier, log):
            metadata = httpx.get(url.replace("/mcp", "/.well-known/oauth-protected-resource/mcp"))
            self.assertEqual(metadata.status_code, 200)
            self.assertEqual(metadata.json()["resource"], url)
            for case in protocol_cases(self.snapshot)["cases"]:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": case["tool"], "arguments": case["arguments"]}}
                for headers in ({}, {"Authorization": "Bearer invalid"}):
                    response = httpx.post(url, json=body, headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertIn("resource_metadata=", response.headers["www-authenticate"])
                    self.assertNotIn("fictional", response.text)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
            malformed = httpx.post(url, content='{"jsonrpc":"DO_NOT_ECHO"}', headers={
                **headers, "Content-Type": "application/json"})
            self.assertEqual(malformed.status_code, 400)
            self.assertNotIn("DO_NOT_ECHO", malformed.text)
            self.assertEqual(httpx.post(url, json={}, headers={**headers, "Origin": "https://evil.test"}).status_code, 403)
            self.assertEqual(httpx.post(url, json={}, headers={**headers, "Host": "evil.test"}).status_code, 421)
            self.assertEqual(httpx.get(url).status_code, 405)
            data = http_auth.read_registry(registry)
            original = json.loads(json.dumps(data))
            for change, status in (
                ({"expires_at": int(time.time()) - 1}, 401),
                ({"revoked": True}, 401),
                ({"audience": "http://127.0.0.1:9999/mcp"}, 401),
                ({"profiles": ["curated"]}, 403),
            ):
                data = json.loads(json.dumps(original))
                data["tokens"][0].update(change)
                http_auth.write_registry(registry, data)
                self.assertEqual(httpx.post(url, json={}, headers=headers).status_code, status)
            http_auth.write_registry(registry, original)
            self.assertEqual(httpx.get(url.replace("/mcp", "/papers/a.pdf"), headers=headers).status_code, 404)
            headers["Mcp-Session-Id"] = "unknown-session"
            response = httpx.post(url, headers=headers,
                                  json={"jsonrpc": "2.0", "id": 2, "method": "ping"})
            self.assertEqual(response.status_code, 404)
            registry.write_text("corrupt")
            self.assertEqual(httpx.post(url, json={}, headers=headers).status_code, 503)
            self.assertEqual(httpx.get(url.replace("/mcp", "/readyz")).status_code, 503)

    def test_rejection_diagnostics_are_specific_and_do_not_echo_input(self):
        registry = self.root / "registry"
        token_file = self.root / "token"
        http_auth.issue(registry, token_file, "http://127.0.0.1:8000/mcp", ["synthetic"])
        token = token_file.read_text().strip()
        app = http_server.create_app(release=self.release, profiles={"synthetic"}, registry=registry,
                                     base_url="http://127.0.0.1:8000/mcp")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        with patch.object(http_server, "diagnostic") as diagnostic:
            with TestClient(app, base_url="http://127.0.0.1:8000") as client:
                for body, reason in (
                    ({"jsonrpc": "2.0", "id": 1, "method": "ping"}, "missing_session_id"),
                    ({"jsonrpc": "DO_NOT_ECHO"}, "invalid_jsonrpc"),
                ):
                    response = client.post("/mcp", json=body, headers=headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(diagnostic.call_args.kwargs["reason"], reason)
                    self.assertNotIn("DO_NOT_ECHO", response.text)
                response = client.post("/mcp", content="DO_NOT_ECHO", headers={
                    **headers, "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(diagnostic.call_args.kwargs["reason"], "invalid_json")
                response = client.post("/mcp", json={}, headers=[
                    ("Authorization", f"Bearer {token}"), ("Authorization", "DO_NOT_ECHO")])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(diagnostic.call_args.kwargs["reason"], "duplicate_authorization")
            self.assertNotIn(token, str(diagnostic.call_args_list))
            self.assertNotIn("DO_NOT_ECHO", str(diagnostic.call_args_list))
        for message, expected in (
            ("Bad Request: Unsupported protocol version: DO_NOT_ECHO", "unsupported_protocol_version"),
            ("Unexpected DO_NOT_ECHO", "sdk_request_rejected"),
        ):
            messages = [{"type": "http.response.body",
                         "body": json.dumps({"error": {"message": message}}).encode()}]
            self.assertEqual(http_server.rejection_reason(400, messages), expected)
        self.assertEqual(http_server.rejection_reason(400, [
            {"type": "http.response.body", "body": b"DO_NOT_ECHO"}]), "sdk_request_rejected")

    def test_limits_timeout_concurrency_rate_and_chunked_size(self):
        registry = self.root / "registry"
        http_auth.issue(registry, self.root / "token", "http://127.0.0.1:8000/mcp", ["synthetic"])
        app = http_server.create_app(release=self.release, profiles={"synthetic"}, registry=registry,
                                     base_url="http://127.0.0.1:8000/mcp")
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            self.assertEqual(client.post("/mcp", content=b"x" * 65537).status_code, 413)
            self.assertEqual(client.post("/mcp", content=iter([b"x" * 40000, b"y" * 40000])).status_code, 413)
            original = app.app

            async def overload():
                entered = anyio.Event()
                release = anyio.Event()

                async def held(scope, receive, send):
                    if app.active == 8:
                        entered.set()
                    await release.wait()
                    await JSONResponse({"ok": True})(scope, receive, send)

                app.app = held
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://127.0.0.1:8000") as http:
                    tasks = [asyncio.create_task(http.get("/healthz")) for _ in range(8)]
                    try:
                        with anyio.fail_after(2):
                            await entered.wait()
                        self.assertEqual((await http.get("/healthz")).status_code, 503)
                    finally:
                        release.set()
                        results = await asyncio.gather(*tasks)
                        self.assertTrue(all(response.status_code == 200 for response in results))
                        app.app = original
                self.assertEqual(app.active, 0)
            client.portal.call(overload)
            app.recent = [time.monotonic()] * app.rate
            self.assertEqual(client.get("/healthz").status_code, 429)
            app.recent = []
            async def slow(scope, receive, send):
                await anyio.sleep(1)
            app.app = slow
            app.timeout = 0.01
            self.assertEqual(client.post("/mcp", content=b"{}").status_code, 504)
            app.app = original
            self.assertEqual(client.get("/healthz").status_code, 200)

    def test_bounded_sessions_and_idle_expiry(self):
        registry = self.root / "registry"
        http_auth.issue(registry, self.root / "token", "http://127.0.0.1:8000/mcp", ["synthetic"])
        app = http_server.create_app(
            release=self.release, profiles={"synthetic"}, registry=registry,
            base_url="http://127.0.0.1:8000/mcp", max_sessions=1, session_idle_timeout=0.15)
        headers = {"Authorization": f"Bearer {(self.root / 'token').read_text().strip()}",
                   "Accept": "application/json, text/event-stream"}
        opening = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                              "clientInfo": {"name": "bounded-test", "version": "1"}}}
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            first = client.post("/mcp", json=opening, headers=headers)
            self.assertEqual(first.status_code, 200)
            session = first.headers["mcp-session-id"]
            self.assertEqual(client.post("/mcp", json=opening, headers=headers).status_code, 503)
            time.sleep(0.3)
            expired = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
                                  headers={**headers, "Mcp-Session-Id": session})
            self.assertEqual(expired.status_code, 404)
            self.assertEqual(client.post("/mcp", json=opening, headers=headers).status_code, 200)

    def test_cancellation_session_ownership_and_reauthentication(self):
        registry = self.root / "registry"
        token_path = self.root / "token"
        audience = "http://127.0.0.1:8000/mcp"
        http_auth.issue(registry, token_path, audience, ["synthetic"])
        other_path = self.root / "other-token"
        http_auth.issue(registry, other_path, audience, ["synthetic"])
        token = token_path.read_text().strip()
        app = http_server.create_app(release=self.release, profiles={"synthetic"}, registry=registry,
                                     base_url=audience)
        original = releases.Snapshot.call

        def slow(snapshot, tool, arguments):
            if tool == "get_claim":
                time.sleep(0.2)
            return original(snapshot, tool, arguments)

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=audience,
                headers={"Authorization": f"Bearer {token}"},
            ) as http:
                async with streamable_http_client(audience, http_client=http) as (reader, writer, session_id):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        owner = session_id()
                        headers = {"Mcp-Session-Id": owner, "Accept": "application/json, text/event-stream"}
                        pending = asyncio.create_task(http.post(audience, headers=headers, json={
                            "jsonrpc": "2.0", "id": 50, "method": "tools/call",
                            "params": {"name": "get_claim", "arguments": {"claim_id": "fictional-claim"}},
                        }))
                        await asyncio.sleep(0.03)
                        cancelled = await http.post(audience, headers=headers, json={
                            "jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": 50, "reason": "Test cancellation"},
                        })
                        self.assertEqual(cancelled.status_code, 202)
                        response = await pending
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("error", response.json())
                        await client.send_ping()
                        other = await http.post(audience, headers={
                            "Authorization": f"Bearer {other_path.read_text().strip()}",
                            "Mcp-Session-Id": owner, "Accept": "application/json, text/event-stream",
                        }, json={"jsonrpc": "2.0", "id": 9, "method": "ping"})
                        self.assertEqual(other.status_code, 404)
                        expired = http_auth.read_registry(registry)
                        expired["tokens"][0]["expires_at"] = int(time.time()) - 1
                        http_auth.write_registry(registry, expired)
                        denied = await http.post(audience, headers={
                            "Mcp-Session-Id": owner, "Accept": "application/json, text/event-stream",
                        }, json={"jsonrpc": "2.0", "id": 10, "method": "ping"})
                        self.assertEqual(denied.status_code, 401)
                        expired["tokens"][0]["expires_at"] = int(time.time()) + 60
                        http_auth.write_registry(registry, expired)
                        await client.send_ping()
        with patch.object(releases.Snapshot, "call", slow), TestClient(app) as client:
            client.portal.call(exercise)

    def test_token_operator_and_secure_configuration(self):
        registry = self.root / "registry"
        token = self.root / "token"
        audience = "http://127.0.0.1:8000/mcp"
        identifier = http_auth.issue(registry, token, audience, ["synthetic"])
        self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(token.read_text().strip(), registry.read_text())
        with self.assertRaises(FileExistsError):
            http_auth.issue(registry, token, audience, ["synthetic"])
        for url in ("http://example.com/mcp", "https://example.com/other", "http://user:pass@localhost/mcp",
                    "http://130.208.105.163/mcp", "http://0.0.0.0:8000/mcp", "http://myhost.local:8000/mcp"):
            with self.assertRaises(ValueError):
                http_auth.resource_url(url)
        for url in ("http://192.168.1.10:8000/mcp", "http://10.0.0.5:8000/mcp", "http://[fd00::5]:8000/mcp"):
            self.assertEqual(http_auth.resource_url(url), url)
        registry.chmod(0o644)
        with self.assertRaises(ValueError):
            http_auth.read_registry(registry)
        registry.chmod(0o600)
        result = subprocess.run([sys.executable, "-m", "research_library.http_auth", "revoke",
                                 "--registry", registry, "--token-id", identifier],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(asyncio.run(http_auth.FileTokenVerifier(registry, audience).verify_token(token.read_text().strip())))

    def test_store_rollout_corruption_readiness_and_rollback(self):
        store = releases.ReleaseStore(self.root / "store", allowed_profiles={"synthetic"})
        first_id = store.install(self.release)
        self.bundle["claims"][0]["statement"] = "New fictional release"
        second = self.root / "second"
        releases.export(self.bundle, second, profile="synthetic")
        second_id = store.install(second)
        store.activate(first_id)
        with running_server(self.root, self.release, store=store.root) as (url, token, *_):
            async def exercise():
                async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                    async with streamable_http_client(url, http_client=http) as (reader, writer, session_id):
                        async with ClientSession(reader, writer) as client:
                            await client.initialize()
                            initial = await client.call_tool("corpus_status")
                            self.assertEqual(initial.structuredContent["release_id"], first_id)
                            store.activate(second_id)
                            updated = await client.call_tool("get_claim", {"claim_id": "fictional-claim"})
                            self.assertEqual(updated.structuredContent["release_id"], second_id)
                            self.assertEqual(updated.structuredContent["result"]["statement"], "New fictional release")
                            (store.root / "releases" / second_id / "records.json").write_text("corrupt")
                            failed = await client.call_tool("corpus_status")
                            self.assertEqual(failed.structuredContent["error"]["code"], "invalid_release")
                            response = await http.get(url.replace("/mcp", "/readyz"))
                            self.assertEqual(response.status_code, 503)
                            with self.assertRaises(ValueError):
                                store.activate(second_id)
                            store.rollback()
                            restored = await client.call_tool("corpus_status")
                            self.assertEqual(restored.structuredContent["release_id"], first_id)
                            self.assertEqual((await http.get(url.replace("/mcp", "/readyz"))).status_code, 200)
            asyncio.run(exercise())

    def test_secret_link_tokens_and_extra_origins_for_web_clients(self):
        registry = self.root / "share-registry"
        base = "https://share.example/mcp"
        http_auth.issue(registry, self.root / "share-token", base, ["synthetic"])
        token = (self.root / "share-token").read_text().strip()
        http_auth.issue(registry, self.root / "other-token", "https://other.example/mcp", ["synthetic"])
        other = (self.root / "other-token").read_text().strip()
        with self.assertRaises(ValueError):
            http_server.create_app(release=self.release, profiles={"synthetic"}, registry=registry,
                                   base_url=base, extra_origins=("http://claude.ai",))
        opening = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "link-test", "version": "1"}}}
        accept = {"Accept": "application/json, text/event-stream"}
        app = http_server.create_app(release=self.release, profiles={"synthetic"}, registry=registry,
                                     base_url=base, extra_origins=("https://claude.ai",), link_tokens=True)
        with TestClient(app, base_url="https://share.example") as client:
            ok = client.post(f"/s/{token}/mcp", json=opening, headers={**accept, "Origin": "https://claude.ai"})
            self.assertEqual(ok.status_code, 200)
            self.assertIn("result", ok.json())
            # A path secret overrides any supplied header and is checked like a bearer token.
            self.assertEqual(client.post(f"/s/{token}/mcp", json=opening,
                                         headers={**accept, "Authorization": "Bearer wrong"}).status_code, 200)
            self.assertEqual(client.post(f"/s/{other}/mcp", json=opening, headers=accept).status_code, 401)
            self.assertEqual(client.post("/s/" + "A" * 64 + "/mcp", json=opening, headers=accept).status_code, 401)
            self.assertEqual(client.post("/s/short/mcp", json=opening, headers=accept).status_code, 404)
            self.assertEqual(client.post("/mcp", json=opening, headers=accept).status_code, 401)
            self.assertEqual(client.post(f"/s/{token}/mcp", json=opening,
                                         headers={**accept, "Origin": "https://evil.example"}).status_code, 403)
        plain = http_server.create_app(release=self.release, profiles={"synthetic"}, registry=registry, base_url=base)
        with TestClient(plain, base_url="https://share.example") as client:
            self.assertEqual(client.post(f"/s/{token}/mcp", json=opening, headers=accept).status_code, 404)
            self.assertEqual(client.post("/mcp", json=opening, headers={**accept, "Origin": "https://claude.ai"}
                                         ).status_code, 403)
        refused = subprocess.run([
            sys.executable, "-m", "research_library.http_server", "--release", self.release,
            "--allow-profile", "synthetic", "--token-registry", registry, "--link-tokens",
        ], capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("https base URL", refused.stderr)

    def test_share_helpers_find_tunnel_address_and_revoke_token(self):
        from research_library import share
        line = "INF |  https://three-random-words.trycloudflare.com  |"
        self.assertEqual(share.TUNNEL_URL.search(line)[0], "https://three-random-words.trycloudflare.com")
        self.assertIsNone(share.TUNNEL_URL.search("https://api.trycloudflare.com.evil.example"))
        self.assertIsNone(share.TUNNEL_URL.search("POST https://api.trycloudflare.com/tunnel"))
        registry, token = self.root / "tokens", self.root / "share" / "token"
        token.parent.mkdir()
        http_auth.issue(registry, token, "https://x.trycloudflare.com/mcp", ["synthetic"])
        share.revoke(registry, token)
        self.assertFalse(token.exists())
        self.assertTrue(http_auth.read_registry(registry)["tokens"][0]["revoked"])
        share.revoke(registry, token)

    def test_verified_tls_boundary_and_external_bind_refusal(self):
        result = subprocess.run([
            sys.executable, "-m", "research_library.http_server", "--release", self.release,
            "--allow-profile", "synthetic", "--token-registry", self.root / "unused",
            "--host", "0.0.0.0",
        ], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires TLS", result.stderr)
        for extra, message in (
                (["--host", "192.168.1.10"], "--allow-plain-lan"),
                (["--host", "0.0.0.0", "--allow-plain-lan"], "matching"),
                (["--host", "0.0.0.0", "--allow-plain-lan", "--base-url", "http://130.208.105.163:8000/mcp"], "matching"),
                (["--host", "130.208.105.163", "--allow-plain-lan"], "private LAN IP"),
                (["--host", "192.168.1.10", "--allow-plain-lan", "--base-url", "http://192.168.1.11:8000/mcp"],
                 "matching"),
        ):
            refused = subprocess.run([
                sys.executable, "-m", "research_library.http_server", "--release", self.release,
                "--allow-profile", "synthetic", "--token-registry", self.root / "unused", *extra,
            ], capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0, extra)
            self.assertIn(message, refused.stderr, extra)
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", self.root / "key.pem", "-out", self.root / "cert.pem",
        ], check=True, capture_output=True)
        with running_server(self.root, self.release, tls=True, extra=[
                "--tls-cert", str(self.root / "cert.pem"), "--tls-key", str(self.root / "key.pem"),
        ]) as (url, token, *_):
            verify = ssl.create_default_context(cafile=str(self.root / "cert.pem"))
            self.assertEqual(httpx.post(url, json={}, verify=verify).status_code, 401)
            async def exercise():
                async with httpx.AsyncClient(verify=verify, headers={"Authorization": f"Bearer {token}"}) as http:
                    async with streamable_http_client(url, http_client=http) as (reader, writer, session_id):
                        async with ClientSession(reader, writer) as client:
                            await client.initialize()
                            response = await client.call_tool("corpus_status")
                            self.assertTrue(response.structuredContent["ok"])
            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
