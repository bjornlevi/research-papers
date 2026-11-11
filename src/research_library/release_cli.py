"""Local operator commands for explicit export, validation, activation and rollback."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from jsonschema import ValidationError

from . import releases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("bundle", type=Path)
    export.add_argument("destination", type=Path)
    export.add_argument("--profile", choices=sorted(releases.PROFILES), default="curated")
    export.add_argument("--authorization", type=Path,
                        help="Per-source {scopes: [...], basis: ...}; never inferred from possession")
    export.add_argument("--asset-map", type=Path, help="Explicit source-ID to local original-path mapping")
    validate = sub.add_parser("validate")
    validate.add_argument("release", type=Path)
    install = sub.add_parser("install")
    install.add_argument("release", type=Path)
    for command in ("activate", "rollback", "status"):
        selected = sub.add_parser(command)
        if command == "activate":
            selected.add_argument("release_id")
    for command in (validate, install, *(sub.choices[name] for name in ("activate", "rollback", "status"))):
        command.add_argument("--allow-profile", action="append", required=True,
                             choices=sorted(releases.PROFILES))
        if command is not validate:
            command.add_argument("--store", type=Path, required=True)
            command.add_argument("--public-quality-root", type=Path,
                                 help="Create a separate, persistently quality-gated public store")
    args = parser.parse_args()
    try:
        if args.command == "export":
            bundle = releases.read_json(args.bundle.read_bytes())
            grants = releases.read_json(args.authorization.read_bytes()) if args.authorization else None
            assets = releases.read_json(args.asset_map.read_bytes()) if args.asset_map else None
            identifier = releases.export(bundle, args.destination, profile=args.profile,
                                         authorization=grants, asset_paths=assets)
        elif args.command == "validate":
            identifier = releases.validate(args.release, allowed_profiles=set(args.allow_profile)).release_id
        else:
            store = releases.ReleaseStore(args.store, allowed_profiles=set(args.allow_profile),
                                          public_quality_root=args.public_quality_root)
            if args.command == "install":
                identifier = store.install(args.release)
            elif args.command == "activate":
                identifier = store.activate(args.release_id).release_id
            elif args.command == "rollback":
                identifier = store.rollback().release_id
            else:
                identifier = store.current().release_id
        print(json.dumps({"command": args.command, "release_id": identifier}))
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        print(f"Release error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
