from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

from research_library import http_auth, local_client


class LocalClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "AI-intro"
        self.project.mkdir()
        self.registry = self.root / "private" / "tokens.json"
        self.token = self.root / "private" / "client-token"
        self.url = "http://127.0.0.1:8000/mcp"
        self.registration = None
        self.commands = []
        self.failure = None
        self.addCleanup(patch.stopall)
        patch("research_library.local_client.shutil.which", return_value="/bin/claude").start()
        patch("research_library.local_client.subprocess.run", side_effect=self.run_cli).start()

    def run_cli(self, arguments, **kwargs):
        self.assertEqual(kwargs["cwd"], self.project)
        self.assertTrue(kwargs["capture_output"])
        self.commands.append(arguments)
        command = arguments[2]
        if command == self.failure:
            return subprocess.CompletedProcess(arguments, 1, "", "DO_NOT_ECHO " + str(self.registration))
        if command == "get":
            return subprocess.CompletedProcess(arguments, 0 if self.registration else 1,
                                               self.registration or 'No MCP server named "research-library-http".', "")
        if command == "remove":
            self.assertIn("local", arguments)
            self.registration = None
        if command == "add":
            self.assertIn("local", arguments)
            self.registration = (
                "Scope: Local config (private to you in this project)\n"
                f"URL: {self.url}\n  {arguments[-1]}\n")
        return subprocess.CompletedProcess(arguments, 0, "DO_NOT_ECHO")

    def manage(self, action="connect"):
        return local_client.manage(action, self.project, "research-library-http",
                                   self.registry, self.token, self.url, ["synthetic"])

    def test_init_is_private_idempotent_and_rejects_corrupt_registry(self):
        local_client.initialize(self.registry)
        self.assertFalse(self.registry.stat().st_mode & 0o077)
        before = self.registry.read_bytes()
        local_client.initialize(self.registry)
        self.assertEqual(self.registry.read_bytes(), before)
        self.registry.write_text("{}")
        with self.assertRaises(ValidationError):
            local_client.initialize(self.registry)

    def test_connect_reuses_valid_token_and_only_changes_named_local_server(self):
        first = self.manage()
        token = self.token.read_bytes()
        self.assertTrue(first["registered"])
        self.assertTrue(first["token_rotated"])
        self.assertFalse(self.manage()["token_rotated"])
        self.assertEqual(self.token.read_bytes(), token)
        self.assertEqual(len(http_auth.read_registry(self.registry)["tokens"]), 1)
        self.assertEqual(sum(command[2] == "remove" for command in self.commands), 1)
        self.assertTrue(all("research-library-http" in command for command in self.commands))

    def test_reconnect_revokes_old_credentials_without_affecting_other_clients(self):
        self.manage()
        old = self.token.read_text().strip()
        other = self.root / "other-token"
        http_auth.issue(self.registry, other, self.url, ["synthetic"])
        self.assertTrue(self.manage("reconnect")["token_rotated"])
        registry = http_auth.read_registry(self.registry)
        self.assertTrue(local_client.token_entry(registry, old)["revoked"])
        self.assertFalse(local_client.token_entry(registry, other.read_text().strip())["revoked"])
        self.assertNotEqual(self.token.read_text().strip(), old)

    def test_disconnect_is_idempotent_and_revokes_legacy_registered_token(self):
        local_client.initialize(self.registry)
        legacy = self.root / "legacy-token"
        http_auth.issue(self.registry, legacy, self.url, ["synthetic"])
        token = legacy.read_text().strip()
        self.registration = f"Scope: Local config\nURL: {self.url}\nAuthorization: Bearer {token}\n"
        result = self.manage("disconnect")
        self.assertFalse(result["connected"])
        self.assertTrue(local_client.token_entry(http_auth.read_registry(self.registry), token)["revoked"])
        self.assertIsNone(self.registration)
        self.assertFalse(self.manage("disconnect")["connected"])

    def test_expired_token_refresh_and_failed_add_revoke_new_credential(self):
        self.manage()
        registry = http_auth.read_registry(self.registry)
        registry["tokens"][0]["expires_at"] = int(time.time()) - 1
        http_auth.write_registry(self.registry, registry)
        self.assertTrue(self.manage()["token_rotated"])
        self.failure = "add"
        with self.assertRaisesRegex(ValueError, "Claude MCP operation failed"):
            self.manage("reconnect")
        registry = http_auth.read_registry(self.registry)
        self.assertTrue(all(entry["revoked"] for entry in registry["tokens"]))

    def test_refuse_foreign_scope_url_unknown_token_and_cli_failures(self):
        self.manage()
        before = self.registry.read_bytes()
        for registration in (
            self.registration.replace("Local config", "User config"),
            self.registration.replace(self.url, "http://127.0.0.1:9999/mcp"),
            f"Scope: Local config\nURL: {self.url}\nAuthorization: Bearer UNKNOWN\n",
        ):
            self.registration = registration
            with self.assertRaises(ValueError):
                self.manage("reconnect")
            self.assertEqual(before, self.registry.read_bytes())
        self.failure = "get"
        with self.assertRaisesRegex(ValueError, "^Claude MCP operation failed"):
            self.manage()
        self.assertEqual(before, self.registry.read_bytes())

    def test_cli_does_not_echo_credentials_or_raw_errors(self):
        self.manage()
        self.failure = "add"
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["local_client", "reconnect", "--project", str(self.project),
                                "--name", "research-library-http", "--registry", str(self.registry),
                                "--token-file", str(self.token), "--url", self.url,
                                "--profile", "synthetic"]), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(local_client.main(), 1)
        self.assertNotIn("DO_NOT_ECHO", out.getvalue() + err.getvalue())
        self.assertNotIn(self.token.read_text().strip(), out.getvalue() + err.getvalue())

    def test_invalid_paths_permissions_and_missing_cli_fail_explicitly(self):
        with patch("research_library.local_client.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "CLI is required"):
                self.manage()
        self.manage()
        self.token.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private regular file"):
            self.manage()
        self.token.chmod(0o600)
        self.token.unlink()
        self.token.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "private regular file"):
            self.manage()
