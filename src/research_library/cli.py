"""Retrieve evidence without extraction dependencies or working-directory assumptions."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from jsonschema import ValidationError

from . import __version__, collections, contracts, releases, retrieval


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    roots = parser.add_mutually_exclusive_group()
    roots.add_argument("--data-root", type=Path,
                       help="Workspace containing evidence/index.sqlite (or RESEARCH_LIBRARY_DATA_ROOT)")
    roots.add_argument("--release", type=Path, help="Validated immutable release directory")
    roots.add_argument("--store", type=Path, help="Operator release store; pin active release per invocation")
    roots.add_argument("--registry", type=Path, help="Independent collection registry")
    parser.add_argument("--allow-collection", action="append")
    parser.add_argument("--default-collection", action="append")
    parser.add_argument("--allow-profile", action="append", choices=sorted(releases.PROFILES),
                        help="Explicit allowed release profile; required for --release/--store")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="Safe plain-text lexical search, not semantic similarity")
    search.add_argument("query")
    search.add_argument("--mode", choices=("plain", "fts5"), default="plain",
                        help="Plain text by default; fts5 enables advanced query syntax")
    search.add_argument("--collection", action="append")
    search.add_argument("--snapshot-id")
    claim = sub.add_parser("claim", help="Retrieve one complete claim by stable ID")
    claim.add_argument("claim_id")
    claims = sub.add_parser("claims", help="Return whole claims with numbers and caveats")
    claims.add_argument("query", nargs="?")
    for name in ("topic", "kind", "measure"):
        claims.add_argument("--" + name)
    results = sub.add_parser("results", help="Retrieve original deeper-pilot findings")
    results.add_argument("--measure")
    for command in (search, claims, results):
        command.add_argument("--paper")
        command.add_argument("--limit", type=int, default=10 if command is search else 100)
        command.add_argument("--offset", type=int, default=0)
    tool = sub.add_parser("tool", help="Invoke a bounded versioned release contract (not MCP)")
    tool.add_argument("name", choices=sorted(contracts.TOOLS))
    tool.add_argument("arguments", nargs="?", default="{}", help="JSON argument object")
    args = parser.parse_args()
    try:
        if args.release or args.store or args.registry:
            if not args.allow_profile:
                raise releases.AccessDeniedError("--allow-profile is required for release retrieval")
            if bool(args.registry) != bool(args.allow_collection) or (args.default_collection and not args.registry):
                raise ValueError("Collection registry requires explicit allowed collections")
            snapshot = (collections.CollectionStore(args.registry, allowed_profiles=args.allow_profile,
                                                   allowed_collections=args.allow_collection,
                                                   defaults=args.default_collection) if args.registry else
                        releases.validate(args.release, allowed_profiles=set(args.allow_profile)) if args.release
                        else releases.ReleaseStore(args.store, allowed_profiles=set(args.allow_profile)).current())
            if args.command == "tool":
                records = snapshot.call(args.name, json.loads(args.arguments))
            elif args.command == "claim":
                records = snapshot.call("get_claim", {"claim_id": args.claim_id})
            elif args.command == "search":
                arguments = {"query": args.query, "mode": args.mode,
                             "limit": args.limit, "offset": args.offset}
                if args.paper is not None:
                    arguments["paper"] = args.paper
                if args.collection:
                    arguments["collections"] = args.collection
                if args.snapshot_id:
                    arguments["snapshot_id"] = args.snapshot_id
                records = snapshot.call("search_evidence", arguments)
            else:
                raise ValueError("Release mode supports search, claim and tool; legacy listings use --data-root")
            print(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.command == "tool":
            raise ValueError("Versioned tools require --release or --store")
        if args.allow_collection or args.default_collection or (
                args.command == "search" and (args.collection or args.snapshot_id)):
            raise ValueError("Collection selection requires --registry")
        with closing(retrieval.open_index(args.data_root)) as db:
            if args.command == "claim":
                records = retrieval.get_claim(db, args.claim_id)
            else:
                if args.command == "search":
                    records = retrieval.search(db, args.query, paper=args.paper,
                                               limit=args.limit, offset=args.offset, mode=args.mode)
                elif args.command == "claims":
                    records = retrieval.claims(db, text=args.query, topic=args.topic, kind=args.kind,
                                               measure=args.measure, paper=args.paper,
                                               limit=args.limit, offset=args.offset)
                else:
                    records = retrieval.results(db, measure=args.measure, paper=args.paper,
                                                limit=args.limit, offset=args.offset)
        print(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        print(f"Evidence error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
