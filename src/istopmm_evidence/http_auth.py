"""Operator-issued local/private bearer credentials; not an OAuth login service."""

import argparse
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from urllib.parse import urlsplit

import anyio
from jsonschema import Draft202012Validator, ValidationError
from mcp.server.auth.provider import AccessToken

from . import releases


REGISTRY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["version", "tokens"],
    "properties": {"version": {"const": 1}, "tokens": {
        "type": "array", "maxItems": 1000, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "sha256", "audience", "profiles", "expires_at", "revoked"],
            "properties": {
                "id": {"type": "string", "pattern": "^[a-f0-9]{24}$"},
                "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "audience": {"type": "string", "minLength": 1},
                "profiles": {"type": "array", "minItems": 1, "uniqueItems": True,
                             "items": {"enum": sorted(releases.PROFILES)}},
                "expires_at": {"type": "integer", "minimum": 1},
                "revoked": {"type": "boolean"},
            },
        },
    }},
}


def private_address(host):
    """True for a literal loopback or private-network IP address (not a DNS name, not 0.0.0.0)."""
    try:
        address = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    return (address.is_loopback or address.is_private) and not address.is_unspecified and not address.is_global


def resource_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path != "/mcp"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Resource URL must be an absolute http(s) URL ending in /mcp")
    if (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            and not private_address(parsed.hostname)):
        raise ValueError("Plain-HTTP resource URLs must use loopback or a private LAN IP address; otherwise use HTTPS")
    return value


def read_registry(path):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Credential registry must be a private file (chmod 600)")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Credential registry exceeds 1 MiB")
    registry = releases.read_json(path.read_bytes())
    Draft202012Validator(REGISTRY_SCHEMA).validate(registry)
    identifiers = [entry["id"] for entry in registry["tokens"]]
    digests = [entry["sha256"] for entry in registry["tokens"]]
    if len(set(identifiers)) != len(identifiers) or len(set(digests)) != len(digests):
        raise ValueError("Duplicate credential identity")
    for entry in registry["tokens"]:
        resource_url(entry["audience"])
    return registry


def write_registry(path, registry):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tokens-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(registry, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def issue(registry_path, token_path, audience, profiles, expires_in=86400):
    resource_url(audience)
    if not profiles or not set(profiles) <= releases.PROFILES or not 1 <= expires_in <= 2592000:
        raise ValueError("Choose known profiles and a lifetime of 1 second to 30 days")
    registry = read_registry(registry_path) if registry_path.exists() else {"version": 1, "tokens": []}
    if len(registry["tokens"]) >= 1000:
        raise ValueError("Credential registry is full")
    token = secrets.token_urlsafe(48)
    entry = {"id": secrets.token_hex(12), "sha256": sha256(token.encode()).hexdigest(),
             "audience": audience, "profiles": sorted(set(profiles)),
             "expires_at": int(time.time()) + expires_in, "revoked": False}
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    registry["tokens"].append(entry)
    write_registry(registry_path, registry)
    return entry["id"]


class FileTokenVerifier:
    def __init__(self, path, audience):
        self.path = path
        self.audience = resource_url(audience)
        read_registry(path)

    async def verify_token(self, token):
        registry = await anyio.to_thread.run_sync(read_registry, self.path)
        digest = sha256(token.encode()).hexdigest()
        for entry in registry["tokens"]:
            if secrets.compare_digest(digest, entry["sha256"]):
                if (entry["revoked"] or entry["expires_at"] <= int(time.time())
                        or entry["audience"] != self.audience):
                    return None
                return AccessToken(
                    token=token, client_id=entry["id"], subject=entry["id"],
                    expires_at=entry["expires_at"], resource=entry["audience"],
                    scopes=["evidence:read", *(f"profile:{p}" for p in entry["profiles"])],
                )
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("issue")
    create.add_argument("--registry", required=True, type=Path)
    create.add_argument("--token-file", required=True, type=Path)
    create.add_argument("--audience", required=True)
    create.add_argument("--profile", required=True, action="append", choices=sorted(releases.PROFILES))
    create.add_argument("--expires-in", type=int, default=86400)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--registry", required=True, type=Path)
    which = revoke.add_mutually_exclusive_group(required=True)
    which.add_argument("--token-id")
    which.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "issue":
            identifier = issue(args.registry, args.token_file, args.audience, args.profile, args.expires_in)
            print(json.dumps({"token_id": identifier, "token_file": str(args.token_file)}))
        else:
            registry = read_registry(args.registry)
            if args.token_file:
                digest = sha256(args.token_file.read_text().strip().encode()).hexdigest()
                entries = [entry for entry in registry["tokens"] if entry["sha256"] == digest]
            else:
                entries = [entry for entry in registry["tokens"] if entry["id"] == args.token_id]
            if not entries:
                raise ValueError("Unknown credential ID")
            entries[0]["revoked"] = True
            write_registry(args.registry, registry)
            print(json.dumps({"revoked": entries[0]["id"]}))
    except (OSError, ValueError, ValidationError) as error:
        print(f"Credential operation failed ({type(error).__name__}); check paths, permissions and arguments.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
