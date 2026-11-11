"""Read-only MCP stdio transport for explicitly authorized evidence releases."""

import argparse
import json
import logging
from pathlib import Path
import sqlite3
import sys
from typing import Literal, cast

import anyio
from jsonschema import ValidationError
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.session import RequestResponder

from . import __version__, collections, contracts, releases, retrieval


DESCRIPTIONS = {
    "list_publications": "Paginated bibliographic metadata, not extracted evidence. Optional plain-text query "
                         "matches all words in title/authors/identifiers/types/disease. Filter disease, "
                         "publication_type or eligibility. Preserve provenance and unreviewed status.",
    "get_publication": "One catalog publication with citation, provider variants, acquisition provenance, "
                       "availability, typed notice/version links and review status; not scientific evidence. "
                       "On registry endpoints select exactly one collection with kind='catalog' from "
                       "list_collections, even when publication_id came from an evidence reference. "
                       "Keep the same snapshot_id; evidence collections do not hold catalog records.",
    "catalog_status": "Coverage by selected metadata catalog: dates, exact queries, providers, disease/type "
                      "counts, duplicates, incomplete discovery and unresolved identities. Not exhaustive.",
    "list_sources": "Paginated citation/source inventory for this evidence release, optionally filtered by paper. "
                    "Lists exact source hashes, use in evidence and recorded acquisition provenance. "
                    "source_url is a citation link, not proof of download origin; unknown acquisition fields "
                    "are null. This is corpus inventory, not an automatic list of sources used in one answer.",
    "list_collections": "List authorized evidence collections, versions, coverage and endpoint defaults. "
                        "Legacy single-release endpoints return an empty list.",
    "corpus_status": "Release identity, coverage, rights and lexical retrieval limitations; not participant data.",
    "search_evidence": "Bounded lexical source search. Plain text is safe by default, including LC-MGUS. "
                       "All words must match in one source unit; start with a few distinctive terms "
                       "and shorten if no results. Use mode='fts5' for explicit advanced syntax. "
                       "No match is not global scientific absence.",
    "get_claim": "Complete claim, numerical context, adjustment, citations and linked source issues.",
    "get_source_context": "Short source passage with original PDF or Office locator; permissions apply.",
    "get_table": "Page through source table records with headers, units, footnotes and caveats repeated.",
    "get_source_issue": "Source discrepancy, ambiguity, clarification or resolved acquisition note with anchors.",
    "get_related_evidence": "Qualified relationships, including reuse/overlap; no edge does not imply independence.",
}


def diagnostic(event: str, **fields: str) -> None:
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


class SDKDiagnostics(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        diagnostic("sdk_diagnostic", level=record.levelname)


class EvidenceServer(Server):
    async def _handle_message(self, message, session, lifespan_context, raise_exceptions=False):
        try:
            await super()._handle_message(message, session, lifespan_context, raise_exceptions)
        except anyio.get_cancelled_exc_class():
            # SDK 1.30's responder exits its cancel scope without forwarding its
            # suppression result. Keep client-request cancellation local, but let
            # transport shutdown cancellation propagate.
            if not isinstance(message, RequestResponder) or not message.cancelled:
                raise


def create_server(snapshot: releases.Snapshot | None,
                  store: releases.ReleaseStore | collections.CollectionStore | None) -> Server:
    server = EvidenceServer("research-library", version=__version__, instructions=(
        "Read-only published evidence, not participant data or clinical advice. Preserve citations, "
        "denominators, uncertainty and source issues. Shared cohorts are not independent replication. "
        "No match or absent relationship is not proof of scientific absence or independence."
        " On collection endpoints use collection_context.evidence_refs for exact collection and record IDs; "
        "pin collection_context.snapshot_id on pagination and follow-up calls and select one collection "
        "for record retrieval. Legacy endpoints retain their single-release scope."
        " Metadata catalog records are not passages or claims. Use list_publications/get_publication "
        "for bibliography and catalog_status for coverage; researcher_review=not_reviewed is not approval. "
        "A publication_id in an evidence reference is a publication identity, not a catalog record in that "
        "evidence collection. For get_publication select a kind='catalog' collection from list_collections "
        "and keep the same snapshot_id. Do not silently widen scope or guess missing identifiers."
    ))

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name, description=DESCRIPTIONS[name],
                inputSchema=contracts.schema(name, "input"),
                outputSchema=contracts.schema(name, "output"),
                annotations=types.ToolAnnotations(
                    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
                ),
            )
            for name in contracts.TOOLS
        ]

    # Contract validation provides stable, sanitized structured errors instead
    # of SDK validation messages that can echo source text or supplied arguments.
    async def execute_tool(name: str, arguments: dict):
        if name not in contracts.TOOLS:
            return types.CallToolResult(
                isError=True, content=[types.TextContent(type="text", text="Unknown read-only tool")],
            )
        tool = cast(contracts.ToolName, name)
        pinned = snapshot

        def failure(code: Literal["invalid_input", "not_found", "permission_denied",
                                  "invalid_release", "unavailable", "response_too_large", "internal_error"], message: str):
            diagnostic("tool_error", tool=name, code=code)
            response = contracts.failure(
                tool, {"code": code, "message": message},
                pinned.release_id if pinned else None,
            )
            return types.CallToolResult(
                isError=True, structuredContent=response,
                content=[types.TextContent(type="text", text=json.dumps(response, ensure_ascii=False))],
            )

        try:
            contracts.request(tool, arguments)
        except (ValidationError, ValueError, TypeError):
            return failure("invalid_input", "Arguments do not satisfy the advertised tool schema or bounds.")
        try:
            if isinstance(store, collections.CollectionStore):
                response = await anyio.to_thread.run_sync(
                    lambda: store.call(tool, arguments), abandon_on_cancel=True)
                return types.CallToolResult(
                    structuredContent=response,
                    content=[types.TextContent(type="text", text=json.dumps(response, ensure_ascii=False))])
            if store is not None:
                pinned = await anyio.to_thread.run_sync(store.current, abandon_on_cancel=True)
            if pinned is None:
                raise releases.ReleaseError("No validated release")
            response = await anyio.to_thread.run_sync(
                lambda: pinned.call(tool, arguments), abandon_on_cancel=True,
            )
            return types.CallToolResult(
                structuredContent=response,
                content=[types.TextContent(type="text", text=json.dumps(response, ensure_ascii=False))],
            )
        except collections.SnapshotUnavailable:
            return failure("unavailable", "Requested collection snapshot is not retained; start a new search.")
        except releases.AccessDeniedError:
            return failure("permission_denied", "The release profile does not permit this evidence access.")
        except retrieval.NotFoundError:
            return failure("not_found", "Unknown evidence identifier in this release.")
        except sqlite3.OperationalError as error:
            if tool == "search_evidence" and str(error).startswith((
                    "fts5: syntax error", "unterminated string", "no such column:",
                    "unknown special query:", "fts5: column queries are not supported")):
                return failure("invalid_input", "Invalid FTS5 syntax. Retry with mode='plain' for ordinary "
                               "text, or quote hyphenated terms and balance quotes in mode='fts5'.")
            return failure("invalid_release", "Evidence index operation failed; contact the operator.")
        except (releases.ReleaseError, OSError, sqlite3.DatabaseError):
            return failure("invalid_release", "Release validation or index access failed; contact the operator.")
        except retrieval.RetrievalError:
            return failure("invalid_input", "Invalid search input or pagination. Plain mode requires "
                           "a word or number; use mode='fts5' for advanced syntax. Collection endpoints "
                           "require snapshot_id for pagination/follow-up and exactly one collection for records.")
        except (ValueError, ValidationError):
            return failure("response_too_large", "Evidence cannot be returned within the tool contract bounds.")
        except Exception as error:
            # Protocol boundary: never let SDK exception formatting expose source
            # content. Cancellation (BaseException) must still propagate.
            diagnostic("internal_error", tool=name, exception_type=type(error).__name__)
            return failure("internal_error", "Unexpected evidence service failure; contact the operator.")

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        await anyio.lowlevel.checkpoint()
        result = await execute_tool(name, arguments)
        # A worker can finish concurrently with cancellation. Observe the canceled
        # request scope before the SDK attempts a second, normal response.
        await anyio.lowlevel.checkpoint()
        return result

    return server


async def serve(snapshot, store):
    server = create_server(snapshot, store)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    roots = parser.add_mutually_exclusive_group(required=True)
    roots.add_argument("--release", type=Path, help="Pin one validated release for this process")
    roots.add_argument("--store", type=Path, help="Pin the active validated release separately per tool call")
    roots.add_argument("--registry", type=Path, help="Serve independent authorized evidence collections")
    parser.add_argument("--allow-collection", action="append")
    parser.add_argument("--default-collection", action="append")
    parser.add_argument("--allow-profile", action="append", required=True, choices=sorted(releases.PROFILES))
    args = parser.parse_args()
    logger = logging.getLogger("mcp")
    logger.handlers = [SDKDiagnostics()]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        if bool(args.registry) != bool(args.allow_collection) or (args.default_collection and not args.registry):
            raise ValueError("--registry requires --allow-collection; collection flags require a registry")
        store = (collections.CollectionStore(args.registry, allowed_profiles=args.allow_profile,
                                            allowed_collections=args.allow_collection,
                                            defaults=args.default_collection) if args.registry else
                 releases.ReleaseStore(args.store, allowed_profiles=set(args.allow_profile))
                 if args.store else None)
        snapshot = (store.current() if store else
                    releases.validate(args.release, allowed_profiles=set(args.allow_profile)))
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        diagnostic("startup_failed", exception_type=type(error).__name__,
                   message="Cannot load an authorized valid release; check configuration with research-release validate.")
        return 1
    diagnostic("ready", release_id=snapshot.release_id, schema_version=contracts.SCHEMA_VERSION,
               transport="stdio")
    try:
        anyio.run(serve, None if store else snapshot, store)
    except KeyboardInterrupt:
        diagnostic("shutdown", reason="interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
