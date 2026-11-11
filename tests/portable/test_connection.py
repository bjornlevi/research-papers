from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import anyio
import httpx
from werkzeug.serving import WSGIRequestHandler, make_server

from research_library import connection_check, http_auth, releases, web_server
from test_browser_library import DefaultView, Links
from test_collections import registry_fixture
from test_http import running_server
from test_releases import fixture_bundle


class QuietHandler(WSGIRequestHandler):
    def log(self, kind, message, *args):
        pass


@contextmanager
def running_web(*, mcp_url, **kwargs):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = web_server.create_app(port=port, profiles={"synthetic"}, mcp_url=mcp_url, **kwargs)
    server = make_server("127.0.0.1", port, app, request_handler=QuietHandler, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle, _ = fixture_bundle(self.root)
        self.release = self.root / "release"
        releases.export(self.bundle, self.release, profile="synthetic")

    def assert_check_failure(self, url, web, message, *, query="screening", collection=None):
        output = io.StringIO()
        with redirect_stderr(output):
            code = connection_check.main([
                "--url", url, "--web-url", web, "--query", query,
                "--token-file", str(self.root / "token"),
                *(["--collection", collection] if collection else []),
            ])
        self.assertEqual(code, 1)
        self.assertIn(message, output.getvalue())

    def test_connection_page_configured_url_no_secrets_and_copy_controls(self):
        app = web_server.create_app(release=self.release, profiles={"synthetic"},
                                    mcp_url="https://evidence.example.test/mcp")
        with patch("research_library.local_client.read_token", side_effect=AssertionError("Must not read credentials")):
            with httpx.Client(transport=httpx.WSGITransport(app=app),
                              base_url="http://127.0.0.1:8080") as client:
                page = client.get("/connect")
                self.assertEqual(page.status_code, 200)
                self.assertIn("https://evidence.example.test/mcp", page.text)
                self.assertIn("research-mcp-check", page.text)
                self.assertNotIn("istopmm", page.text)
                self.assertIn("Python MCP SDK", page.text)
                self.assertIn("Claude Code 2.1.311", page.text)
                self.assertIn("Other applications and versions are untested", page.text)
                self.assertIn("Public HTTPS deployment is deferred", page.text)
                self.assertIn("does not probe MCP", page.text)
                self.assertIn('src="/reader.js"', page.text)
                fields = Links(page.text).fields
                self.assertIn("HTTP_URL=https://evidence.example.test/mcp", fields["claude-setup"])
                self.assertEqual(fields["reset"], "make reconnect HTTP_URL=https://evidence.example.test/mcp")
                self.assertEqual(fields["remove"], "make disconnect HTTP_URL=https://evidence.example.test/mcp")
                self.assertIn("deletes the managed token file", page.text)
                self.assertIn("Revoked-token records remain", page.text)
                self.assertNotIn("<details open", page.text)
                initial = DefaultView(page.text).text
                self.assertIn("Claude setup command", initial)
                self.assertIn("Reconnect or remove a connection", initial)
                self.assertNotIn("Remove command", initial)
                self.assertIn("--web-url http://127.0.0.1:8080", fields["sdk-check"])
                self.assertIn("--url https://evidence.example.test/mcp", fields["sdk-check"])
                prompt = Links(page.text).fields["prompt"]
                self.assertIn("checksum", prompt)
                self.assertIn("Core papers (operator-selected discovery seeds)", prompt)
                self.assertIn("References", prompt)
                self.assertIn("Citing papers", prompt)
                self.assertIn("All papers", prompt)
                self.assertIn("OCR", prompt)
                self.assertIn("omit collections and snapshot_id", prompt)
                self.assertIn("preserve the returned release_id", prompt)
                self.assertIn("do not invent a registry snapshot or stop retrieval", prompt)
                self.assertIn('href="/connect"', client.get("/").text)
                self.assertEqual(client.get("/connect?token=DO_NOT_ECHO").status_code, 400)
                self.assertEqual(client.post("/connect").status_code, 405)
                self.assertEqual(client.get("/connect", headers={"Host": "evil.test"}).status_code, 421)

    def test_default_connection_commands_are_short_and_removal_scope_is_explicit(self):
        app = web_server.create_app(release=self.release, profiles={"synthetic"})
        with httpx.Client(transport=httpx.WSGITransport(app=app),
                          base_url="http://127.0.0.1:8080") as client:
            response = client.get("/connect")
            fields = Links(response.text).fields
            self.assertEqual(fields["claude-setup"], "make connect")
            self.assertEqual(fields["reset"], "make reconnect")
            self.assertEqual(fields["remove"], "make disconnect")
            self.assertIn("in this project by default", response.text)
            self.assertIn("releases/http-local/research-papers-claude-token", fields["sdk-check"])
            self.assertNotIn("../AI-intro", response.text)
            self.assertIn("make setup\nmake mcp", response.text)
            self.assertNotIn("MGUS", response.text)
            self.assertNotIn("myeloma", response.text)
            self.assertNotIn("--collection istopmm", response.text)
            self.assertNotIn("--collection", fields["sdk-check"])
            self.assertIn("YOUR_SEARCH_TERM", fields["sdk-check"])
            self.assertIn("do not remove a separate stdio registration", response.text)
            self.assertIn("do not delete an entire configuration file", response.text)
            self.assertIn("Integrate an application without MCP", response.text)
            self.assertIn("python3 library_json.py --base-url http://127.0.0.1:8080", response.text)
            self.assertIn("Try it: ask for cited evidence", response.text)
            self.assertIn("Check the answer against the source", response.text)
            self.assertIn("shared cohorts", fields["example-compare"])
            self.assertIn("source/unit IDs", fields["example-inspect"])
            self.assertIn("metadata-only", fields["example-coverage"])
            class CollapsedFields(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.depth = 0
                    self.hidden = set()

                def handle_starttag(self, tag, attrs):
                    if tag == "details":
                        self.depth += 1
                    if tag == "textarea" and self.depth:
                        self.hidden.add(dict(attrs)["id"])

                def handle_endtag(self, tag):
                    if tag == "details":
                        self.depth -= 1

            parser = CollapsedFields()
            parser.feed(response.text)
            self.assertIn("sdk-check", parser.hidden)
            for key in ("prompt", "example-compare", "example-inspect", "example-coverage"):
                self.assertNotIn(key, parser.hidden)

    def test_connection_page_uses_configured_collections(self):
        root = self.root / "collections"
        root.mkdir()
        store, _, _, _ = registry_fixture(root)
        app = web_server.create_app(
            registry=store.root, profiles={"synthetic"},
            allowed_collections={"istopmm", "test-literature"}, default_collections=["istopmm"])
        with httpx.Client(transport=httpx.WSGITransport(app=app),
                          base_url="http://127.0.0.1:8080") as client:
            response = client.get("/connect")
            fields = Links(response.text).fields
            self.assertIn("--collection istopmm", fields["sdk-check"])
            self.assertIn("YOUR_SEARCH_TERM", fields["sdk-check"])
            self.assertIn("COLLECTION_REGISTRY=" + str(store.root), response.text)
            self.assertIn("DEFAULT_COLLECTIONS=istopmm", response.text)
            self.assertNotIn("myeloma", response.text)
            self.assertNotIn("MGUS", response.text)

    def test_sdk_search_context_reader_parity_single_release_and_cli(self):
        with running_server(self.root, self.release) as (url, token, *_):
            with running_web(release=self.release, mcp_url=url) as web:
                report = anyio.run(connection_check.check, url, token, web, "screening")
                self.assertTrue(report["search_context_reader_agree"])
                self.assertEqual(report["citation"]["source_sha256"], self.bundle["sources"][0]["sha256"])
                self.assertEqual(report["characters_compared"], 2048)
                self.assertIn("release_id=", report["reader_url"])
                self.assertNotIn(token, json.dumps(report))
                self.assertNotIn(self.bundle["units"][0]["text"][:50], json.dumps(report))
                output = io.StringIO()
                with redirect_stdout(output):
                    code = connection_check.main(["--url", url, "--web-url", web, "--query", "screening",
                                                  "--token-file", str(self.root / "token")])
                self.assertEqual(code, 0)
                self.assertTrue(json.loads(output.getvalue())["passed"])
                self.assert_check_failure(url, web, "No matching retained passage", query="zzznomatch987654321")
                self.assert_check_failure(url, web, "no named collections", collection="istopmm")

    def test_registry_scope_and_browser_mismatch(self):
        store, _, _, _ = registry_fixture(self.root)
        allowed = {"istopmm", "test-literature"}
        with running_server(self.root, self.release, collection_registry=store.root,
                            allowed_collections=allowed) as (url, token, *_):
            with running_web(registry=store.root, allowed_collections=allowed, mcp_url=url) as web:
                report = anyio.run(connection_check.check, url, token, web, "screening", "test-literature")
                self.assertEqual(report["snapshot_id"], store.current().release_id)
                self.assertEqual(report["collection"], "test-literature")
                self.assertIn("snapshot_id=", report["reader_url"])
                self.assertIn("collection=test-literature", report["reader_url"])
                self.assert_check_failure(url, web, "authorized evidence collection", collection="secret")
            changed = deepcopy(self.bundle)
            changed["units"][0]["text"] = "Different screening passage."
            different = self.root / "different"
            releases.export(changed, different, profile="synthetic")
            with running_web(release=different, mcp_url=url) as web:
                self.assert_check_failure(url, web, "HTTP request rejected", collection="istopmm")

    def test_revoked_expired_and_wrong_audience_credentials_fail_clearly(self):
        with running_server(self.root, self.release) as (url, token, registry, token_id, _):
            with running_web(release=self.release, mcp_url=url) as web:
                def check_failure(path, expected_status=401):
                    output = io.StringIO()
                    with redirect_stderr(output):
                        code = connection_check.main(["--url", url, "--web-url", web, "--query", "screening",
                                                      "--token-file", str(path)])
                    self.assertEqual(code, 1)
                    self.assertIn(str(expected_status), output.getvalue())
                    self.assertNotIn(path.read_text().strip(), output.getvalue())
                data = http_auth.read_registry(registry)
                next(entry for entry in data["tokens"] if entry["id"] == token_id)["revoked"] = True
                http_auth.write_registry(registry, data)
                check_failure(self.root / "token")
                expired = self.root / "expired-token"
                http_auth.issue(registry, expired, url, ["synthetic"])
                data = http_auth.read_registry(registry)
                data["tokens"][-1]["expires_at"] = int(time.time()) - 1
                http_auth.write_registry(registry, data)
                check_failure(expired)
                other = self.root / "other-token"
                http_auth.issue(registry, other, url.replace("127.0.0.1", "localhost"), ["synthetic"])
                check_failure(other)
                wrong_scope = self.root / "wrong-scope-token"
                http_auth.issue(registry, wrong_scope, url, ["curated"])
                check_failure(wrong_scope, expected_status=403)

    def test_cli_errors_do_not_echo_secret_paths_or_exception_payloads(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(connection_check.main([
                "--url", "http://127.0.0.1:8000/mcp", "--web-url", "http://127.0.0.1:8081",
                "--token-file", str(self.root / "DO_NOT_ECHO")]), 1)
        self.assertNotIn("DO_NOT_ECHO", output.getvalue())
        self.assertIn("permissions", output.getvalue())
        self.assertNotIn("DO_NOT_ECHO", connection_check.failure_message(RuntimeError("DO_NOT_ECHO")))


if __name__ == "__main__":
    unittest.main()
