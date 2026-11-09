"""Independent installed-server acceptance client; imports no application code."""

import argparse
import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile

import anyio
from jsonschema import validate
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


async def sdk_session(command, arguments, cwd, expected, *, errors=True, timeout_seconds=20):
    with tempfile.TemporaryFile(mode="w+") as diagnostics:
        parameters = StdioServerParameters(command=command, args=arguments, cwd=str(cwd))
        async with stdio_client(parameters, errlog=diagnostics) as (reader, writer):
            async with ClientSession(reader, writer,
                                     read_timeout_seconds=timedelta(seconds=timeout_seconds)) as client:
                initialized = await client.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                assert initialized.capabilities.tools is not None
                assert initialized.capabilities.resources is None
                assert initialized.capabilities.prompts is None
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert set(tools) == ({case["tool"] for case in expected["cases"]} |
                                      {case[0] for case in expected.get("errors", [])})
                for tool in tools.values():
                    assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
                    assert tool.inputSchema["type"] == "object"
                    assert tool.outputSchema["type"] == "object"
                for case in expected["cases"]:
                    response = await client.call_tool(case["tool"], case["arguments"])
                    assert not response.isError, response
                    assert response.structuredContent == case["response"]
                    assert json.loads(response.content[0].text) == response.structuredContent
                    validate(response.structuredContent, tools[case["tool"]].outputSchema)
                for name, arguments, code in expected["errors"] if errors else []:
                    response = await client.call_tool(name, arguments)
                    assert response.isError, response
                    assert response.structuredContent["error"]["code"] == code, response
                    validate(response.structuredContent, tools[name].outputSchema)
                    assert "DO_NOT_ECHO" not in response.content[0].text
                if errors:
                    response = await client.call_tool("arbitrary_sql", {"sql": "DO_NOT_ECHO"})
                    assert response.isError
                    await client.send_ping()
        diagnostics.seek(0)
        lines = diagnostics.read().splitlines()
        assert lines
        for line in lines:
            assert line.startswith("{"), line
            event = json.loads(line)
            assert event["event"] in {"ready", "tool_error", "internal_error", "shutdown", "sdk_diagnostic"}, event
            assert "DO_NOT_ECHO" not in line


async def raw_session(command, arguments, cwd):
    """Check every stdout line and cancellation using independent JSON-RPC I/O."""
    with tempfile.TemporaryFile() as diagnostics:
        process = await asyncio.create_subprocess_exec(
            command, *arguments, cwd=cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=diagnostics,
        )
        async def send(message):
            process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
            await process.stdin.drain()

        async def receive():
            line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
            if not line:
                await asyncio.wait_for(process.wait(), timeout=10)
                diagnostics.seek(0)
                raise AssertionError(
                    f"Server closed unexpectedly (exit {process.returncode}): "
                    + diagnostics.read().decode(errors="replace")
                )
            message = json.loads(line)
            assert message["jsonrpc"] == "2.0"
            return message

        try:
            await send({"id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "independent-wire-test", "version": "1"},
            }})
            assert (await receive())["id"] == 1
            await send({"method": "notifications/initialized"})
            for index in range(25):
                request_id, ping_id = 2 + index * 2, 3 + index * 2
                await send({"id": request_id, "method": "tools/call", "params": {
                    "name": "get_source_context",
                    "arguments": {"unit_id": "fictional-unit-1"},
                }})
                await send({"method": "notifications/cancelled",
                            "params": {"requestId": request_id, "reason": "acceptance cancellation"}})
                await send({"id": ping_id, "method": "ping"})
                # A fast read may complete before cancellation. Either completion
                # or cancellation is legal; the session must remain responsive.
                while (await receive())["id"] != ping_id:
                    pass
            await send({"id": 100, "method": "tools/call", "params": {"name": 123}})
            result = await receive()
            assert result["id"] == 100 and "error" in result
            process.stdin.close()
            await process.stdin.wait_closed()
            await asyncio.wait_for(process.wait(), timeout=20)
            assert process.returncode == 0
            remainder = await process.stdout.read()
            for line in remainder.splitlines():
                assert json.loads(line)["jsonrpc"] == "2.0"
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10)


async def exercise(args):
    expected = json.loads(args.expected.read_text())
    root = args.registry or args.release
    arguments = ["--registry" if args.registry else "--release", str(root)]
    for profile in args.allow_profile or ["synthetic"]:
        arguments += ["--allow-profile", profile]
    for cid in args.allow_collection or []:
        arguments += ["--allow-collection", cid]
    for cid in args.default_collection or []:
        arguments += ["--default-collection", cid]
    before = hashes(root)
    with anyio.fail_after(max(90, args.timeout_seconds * 4)):
        await sdk_session(args.server, arguments, args.cwd, expected, timeout_seconds=args.timeout_seconds)
        async def concurrent():
            await sdk_session(args.server, arguments, args.cwd, expected, timeout_seconds=args.timeout_seconds)
        async with anyio.create_task_group() as group:
            for _ in range(2):
                group.start_soon(concurrent)
        if not args.registry:
            await raw_session(args.server, arguments, args.cwd)
    assert hashes(root) == before
    print("PASS: independent MCP initialization, all tools, schemas, errors, concurrent sessions, cancellation and shutdown")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    roots = parser.add_mutually_exclusive_group(required=True)
    roots.add_argument("--release", type=Path)
    roots.add_argument("--registry", type=Path)
    parser.add_argument("--allow-profile", action="append")
    parser.add_argument("--allow-collection", action="append")
    parser.add_argument("--default-collection", action="append")
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    anyio.run(exercise, parser.parse_args())
