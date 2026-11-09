"""SDK-only HTTP client, runnable outside the application checkout."""

import argparse
import json
from pathlib import Path

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def exercise(url, token, expected):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=35) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer, session_id):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert len(tools.tools) == 12
                for tool in tools.tools:
                    assert tool.outputSchema["type"] == "object"
                    if tool.name == "search_evidence":
                        assert tool.inputSchema["properties"]["mode"]["default"] == "plain"
                for case in expected["cases"]:
                    result = await client.call_tool(case["tool"], case["arguments"])
                    assert not result.isError, case["tool"]
                    assert result.structuredContent == case["response"], case["tool"]
                    assert json.loads(result.content[0].text) == result.structuredContent
                for tool, arguments, code in expected["errors"]:
                    result = await client.call_tool(tool, arguments)
                    assert result.isError
                    assert result.structuredContent["error"]["code"] == code
                    if tool == "search_evidence" and arguments.get("mode") == "fts5":
                        assert "mode='plain'" in result.structuredContent["error"]["message"]
                assert session_id()
    print("PASS: SDK-only HTTP client, twelve tools, exact transport parity and errors")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    args = parser.parse_args()
    anyio.run(exercise, args.url, args.token_file.read_text().strip(),
              json.loads(args.expected.read_text()))
