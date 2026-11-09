"""Verify MCP authentication, cited retrieval and browser parity without a model."""

import argparse
from datetime import timedelta
from html.parser import HTMLParser
from importlib.metadata import version
import json
import logging
from pathlib import Path
import sys
from urllib.parse import urlencode

import anyio
import httpx
from jsonschema import ValidationError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup
else:
    from exceptiongroup import ExceptionGroup

from . import contracts
from .http_auth import resource_url
from .local_client import read_token
from .server import SDKDiagnostics, diagnostic


class CheckFailed(ValueError):
    pass


class ReaderFields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.selected = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "textarea":
            self.selected = dict(attrs).get("id")
            if self.selected:
                self.fields[self.selected] = ""

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.selected = None

    def handle_data(self, data):
        if self.selected:
            self.fields[self.selected] += data


async def check(url, token, web_url, query, collection=None):
    resource_url(url)
    resource_url(web_url + "/mcp")
    contracts.request("search_evidence", {"query": query, "limit": 1})
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=60) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as client:
                initialized = await client.initialize()
                tools = {tool.name for tool in (await client.list_tools()).tools}
                if tools != set(contracts.TOOLS):
                    raise CheckFailed("Unexpected tool set; check client/server versions.")

                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    data = result.structuredContent
                    if result.isError or not data or not data.get("ok"):
                        code = data.get("error", {}).get("code") if data else None
                        messages = {
                            "permission_denied": "Evidence access denied; check collection and profile permissions.",
                            "unavailable": "Requested evidence is unavailable; check readiness and the pinned snapshot.",
                            "invalid_input": "Invalid tool arguments; check query, collection and client/server versions.",
                        }
                        raise CheckFailed(messages.get(code, "Evidence tool failed; check readiness and permissions."))
                    return data

                listed = await call("list_collections", {})
                registry = listed.get("collection_context")
                scope, pin, cid = {}, {}, None
                if registry:
                    evidence = [item["id"] for item in listed["result"]["items"] if item["kind"] == "evidence"]
                    cid = collection or next(iter(evidence), None)
                    if cid not in evidence:
                        raise CheckFailed("Choose an authorized evidence collection, not a metadata catalog.")
                    sid = registry["snapshot_id"]
                    scope = {"snapshot_id": sid, "collections": [cid]}
                    pin = {"snapshot_id": sid, "collection": cid}
                elif collection:
                    raise CheckFailed("This single-release endpoint has no named collections.")
                await call("corpus_status", scope)
                hits = await call("search_evidence", {**scope, "query": query, "limit": 1})
                if not hits["result"]["items"]:
                    raise CheckFailed("No matching retained passage; try a known word or another evidence collection.")
                hit = hits["result"]["items"][0]
                context = await call("get_source_context", {**scope, "unit_id": hit["id"], "max_characters": 2048})
                record = context["result"]
                if record["id"] != hit["id"] or record["citation"] != hit["citation"]:
                    raise CheckFailed("Search and source-context citations differ.")
                if not registry:
                    pin = {"release_id": context["release_id"]}
                params = {**pin, "source_id": record["citation"]["source_id"], "unit_id": record["id"]}
                reader_url = web_url + "/read?" + urlencode(params)
                text_params = {**({key: value for key, value in pin.items() if key != "release_id"}),
                               "unit_id": record["id"], "limit": 2048}
                async with httpx.AsyncClient(timeout=60) as web:
                    response = await web.get(reader_url)
                    response.raise_for_status()
                    fields = ReaderFields(response.text).fields
                    if fields.get("excerpt") != record["text"] + "\n\n" + fields.get("citation", ""):
                        raise CheckFailed("Browser reader text differs from MCP source context.")
                    if not all(value in fields.get("citation", "") for value in (
                            record["id"], record["citation"]["source_id"],
                            record["citation"]["source_sha256"], record["citation"]["doi"])):
                        raise CheckFailed("Browser reader citation differs from MCP source context.")
                    response = await web.get(web_url + "/api/text", params=text_params)
                    response.raise_for_status()
                    browser = response.json()
                    expected_release = (context["collection_context"]["evidence_refs"][0]["release_id"]
                                        if registry else context["release_id"])
                    if (browser["result"]["record"]["citation"] != record["citation"]
                            or browser["result"]["record"]["text"] != record["text"]
                            or browser["release_id"] != expected_release
                            or registry and browser["snapshot_id"] != registry["snapshot_id"]):
                        raise CheckFailed("Browser API and MCP evidence identity/text differ.")
                return {
                    "passed": True, "application_version": initialized.serverInfo.version,
                    "client": "Python MCP SDK", "client_version": version("mcp"),
                    "tools": len(tools), "snapshot_id": pin.get("snapshot_id"),
                    "release_id": browser["release_id"], "collection": cid,
                    "unit_id": record["id"], "citation": record["citation"],
                    "characters_compared": len(record["text"]), "reader_url": reader_url,
                    "search_context_reader_agree": True, "scientific_approval": False,
                }


def failure_message(error):
    if isinstance(error, CheckFailed):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return {
            401: "Authentication failed (401): token missing, expired, revoked or for another audience.",
            403: "Access denied (403): check token profiles, collection permissions and origin.",
            410: "Snapshot unavailable (410): restore it or start a fresh request.",
            503: "Service not ready (503): ask the operator to check data and credential readiness.",
        }.get(error.response.status_code, "HTTP request rejected; check endpoint, readiness and permissions.")
    nested = getattr(error, "exceptions", ())
    if nested:
        return failure_message(nested[0])
    return "Connection check failed: check service URLs, private token-file permissions and server readiness."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--web-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--query", default="myeloma")
    parser.add_argument("--collection")
    args = parser.parse_args(argv)
    logger = logging.getLogger("mcp")
    logger.handlers = [SDKDiagnostics()]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        report = anyio.run(check, args.url, read_token(args.token_file),
                          args.web_url, args.query, args.collection)
    except (OSError, ValueError, ValidationError, httpx.HTTPError, McpError, ExceptionGroup) as error:
        # Transport task groups may wrap authentication errors; never print their payloads.
        diagnostic("client_check_failed", exception_type=type(error).__name__)
        print(failure_message(error), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
