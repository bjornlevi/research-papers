"""Manage one project-local Claude Code connection without exposing credentials."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from jsonschema import ValidationError

from . import http_auth, releases


def initialize(registry):
    if registry.is_symlink():
        raise ValueError("Credential registry must not be a symlink")
    if registry.exists():
        http_auth.read_registry(registry)
    else:
        http_auth.write_registry(registry, {"version": 1, "tokens": []})


def claude(project, arguments, *, missing=None):
    result = subprocess.run(
        ["claude", "mcp", *arguments], cwd=project, capture_output=True, text=True, timeout=90)
    if result.returncode:
        if missing and (result.stdout + result.stderr).strip().startswith(missing):
            return None
        # Claude may echo bearer headers even in error messages.
        raise ValueError("Claude MCP operation failed; check the CLI installation and local configuration")
    return result.stdout


def token_entry(registry, token):
    digest = sha256(token.encode()).hexdigest()
    return next((entry for entry in registry["tokens"] if entry["sha256"] == digest), None)


def read_token(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Client token must be a private regular file (chmod 600)")
    token = path.read_text().strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("Client token file is empty or malformed")
    return token


def manage(action, project, name, registry_path, token_path, url, profiles):
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise ValueError("Client project does not exist; set CLIENT_PROJECT to its directory")
    if not shutil.which("claude"):
        raise ValueError("Claude Code CLI is required; install it before connecting")
    http_auth.resource_url(url)
    if registry_path.absolute() == token_path.absolute():
        raise ValueError("Registry and token file must be different paths")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("Client name must contain only letters, numbers, underscores or hyphens")
    initialize(registry_path)
    registry = http_auth.read_registry(registry_path)
    own_token = read_token(token_path) if token_path.exists() or token_path.is_symlink() else None
    own_entry = token_entry(registry, own_token) if own_token else None
    if own_token and own_entry is None:
        raise ValueError("Client token is not in this registry; choose the matching paths")
    if own_entry and own_entry["audience"] != url:
        raise ValueError("Client token belongs to another URL; choose a different CLIENT_TOKEN_FILE")
    current = claude(project, ["get", name], missing=f'No MCP server named "{name}"')
    previous_token = None
    if current is not None:
        if "Scope: Local config" not in current:
            raise ValueError("Existing server is not project-local; refusing to change another scope")
        match = re.search(r"^\s*URL:\s*(\S+)\s*$", current, re.M)
        if not match or match[1] != url:
            raise ValueError("Existing registration uses another URL; select its HTTP_URL or a different CLIENT_NAME")
        bearer = re.search(r"^\s*Authorization:\s*Bearer\s+(\S+)\s*$", current, re.M | re.I)
        if bearer:
            previous_token = bearer[1]
        if previous_token is None or token_entry(registry, previous_token) is None:
            raise ValueError("Existing registration has no matching local bearer credential; "
                             "select its TOKEN_REGISTRY before replacing it")

    reusable = (own_entry is not None and not own_entry["revoked"]
                and own_entry["expires_at"] > int(time.time()) + 60
                and own_entry["audience"] == url and set(own_entry["profiles"]) == set(profiles))
    rotate = action != "connect" or not reusable
    for token in {own_token if rotate else None, previous_token if previous_token != own_token or rotate else None}:
        entry = token_entry(registry, token) if token else None
        if entry is not None:
            entry["revoked"] = True
    http_auth.write_registry(registry_path, registry)
    if current is not None:
        claude(project, ["remove", "--scope", "local", name])
    if rotate and own_token:
        token_path.unlink()
    if action == "disconnect":
        return {"action": action, "project": str(project), "server": name, "connected": False}
    if rotate:
        http_auth.issue(registry_path, token_path, url, profiles)
    token = read_token(token_path)
    try:
        claude(project, ["add", "--transport", "http", "--scope", "local", name, url,
                         "--header", "Authorization: Bearer " + token])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        registry = http_auth.read_registry(registry_path)
        token_entry(registry, token)["revoked"] = True
        http_auth.write_registry(registry_path, registry)
        raise
    return {"action": action, "project": str(project), "server": name, "url": url,
            "registered": True, "token_rotated": rotate,
            "next": "Start make mcp if needed, then restart Claude Code in this project and use /mcp."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "connect", "disconnect", "reconnect"))
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--profile", action="append", choices=sorted(releases.PROFILES))
    args = parser.parse_args()
    try:
        if args.action == "init":
            initialize(args.registry)
            return 0
        if not all((args.project, args.name, args.token_file, args.url, args.profile)):
            parser.error("Connection operations require project, name, token-file, URL and profile")
        result = manage(args.action, args.project, args.name, args.registry, args.token_file,
                        args.url, args.profile)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, ValidationError, subprocess.TimeoutExpired) as error:
        print(f"Local MCP connection failed ({type(error).__name__}); "
              + (str(error) if isinstance(error, ValueError) else
                 "check private credential paths, permissions and the Claude CLI."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
