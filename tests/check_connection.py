"""Local synthetic MCP/reader acceptance; optionally exercise the installed Claude CLI."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent / "portable"))
from test_collections import registry_fixture
from test_connection import running_web
from test_http import running_server
from research_library import connection_check, http_auth, local_client


def claude_status(project, name):
    result = subprocess.run(["claude", "mcp", "get", name], cwd=project,
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Claude health check failed; output withheld to protect bearer headers.")
    return bool(re.search(r"Status:.*\bConnected\b", result.stdout))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", action="store_true", help="Use only a temporary isolated Claude configuration")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="istopmm-connect-") as directory:
        root = Path(directory)
        store, first, _, _ = registry_fixture(root)
        allowed = {"istopmm", "test-literature"}
        with running_server(root, first, collection_registry=store.root,
                            allowed_collections=allowed) as (url, token, registry, _, _):
            with running_web(registry=store.root, allowed_collections=allowed, mcp_url=url) as web:
                report = anyio.run(connection_check.check, url, token, web, "screening", "istopmm")
                report["scope"] = "Local synthetic software acceptance; no deployment or scientific approval."
                report["claude"] = {"tested": False}
                if args.claude:
                    project = root / "client-project"
                    project.mkdir()
                    environment = {
                        "CLAUDE_CONFIG_DIR": str(root / "claude-config"),
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
                    }
                    with patch.dict(os.environ, environment):
                        result = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                                                check=True, timeout=20)
                        match = re.fullmatch(r"([0-9.]+) \(Claude Code\)\s*", result.stdout)
                        if not match:
                            raise RuntimeError("Cannot identify the installed Claude Code version.")
                        name, path = "istopmm-connection-test", root / "claude-token"
                        local_client.manage("connect", project, name, registry, path, url, ["synthetic"])
                        try:
                            if not claude_status(project, name):
                                raise RuntimeError("Claude Code did not report a connected MCP transport.")
                            data = http_auth.read_registry(registry)
                            local_client.token_entry(data, path.read_text().strip())["revoked"] = True
                            http_auth.write_registry(registry, data)
                            if claude_status(project, name):
                                raise RuntimeError("Claude Code still connected with a revoked credential.")
                            local_client.manage("reconnect", project, name, registry, path, url, ["synthetic"])
                            if not claude_status(project, name):
                                raise RuntimeError("Claude Code did not reconnect with a valid credential.")
                        finally:
                            local_client.manage("disconnect", project, name, registry, path, url, ["synthetic"])
                        report["claude"] = {
                            "tested": True, "version": match[1], "bearer_connection": True,
                            "revocation_denied": True, "reconnection": True,
                            "model_question_tested": False, "isolated_temporary_configuration": True,
                        }
                print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
