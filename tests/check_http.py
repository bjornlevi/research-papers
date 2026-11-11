"""Source-free local HTTP smoke/load/rollout acceptance with a retained metrics option."""

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import tempfile
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parent / "portable"))
from test_http import running_server
from test_releases import fixture_bundle
from research_library import releases


async def measure(url, token, store, first_id, second_id):
    latencies = []
    observed = set()

    async def worker():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(url, http_client=http) as (reader, writer, _):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    for _ in range(10):
                        start = time.monotonic()
                        result = await client.call_tool("get_claim", {"claim_id": "fictional-claim"})
                        latencies.append(time.monotonic() - start)
                        assert not result.isError
                        assert result.structuredContent["release_id"] in {first_id, second_id}
                        observed.add(result.structuredContent["release_id"])

    await asyncio.gather(*(worker() for _ in range(4)))
    assert observed == {first_id}
    store.activate(second_id)
    await worker()
    assert observed == {first_id, second_id}
    started = time.monotonic()
    store.rollback()
    rollback_seconds = time.monotonic() - started
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                restored = await client.call_tool("corpus_status")
                assert restored.structuredContent["release_id"] == first_id
    samples = latencies[:40]
    p95 = sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
    assert p95 < 5, p95
    return {"passed": True, "clients": 4, "calls": 40, "failed_calls": 0,
            "p95_seconds": p95, "p95_budget_seconds": 5,
            "rollback_seconds": rollback_seconds, "release_ids_observed": len(observed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("Output must be a new file")
    with tempfile.TemporaryDirectory(prefix="research-http-check-") as directory:
        root = Path(directory)
        bundle, _ = fixture_bundle(root)
        release = root / "first"
        releases.export(bundle, release, profile="synthetic")
        store = releases.ReleaseStore(root / "store", allowed_profiles={"synthetic"})
        first_id = store.install(release)
        bundle["claims"][0]["statement"] = "Fictional update"
        second = root / "second"
        releases.export(bundle, second, profile="synthetic")
        second_id = store.install(second)
        store.activate(first_id)
        before = {p: p.read_bytes() for p in (store.root / "releases").rglob("*") if p.is_file()}
        with running_server(root, release, store=store.root) as (url, token, *_):
            assert httpx.post(url, json={}).status_code == 401
            report = asyncio.run(measure(url, token, store, first_id, second_id))
            assert all(p.read_bytes() == data for p, data in before.items())
        report["release_bytes_unchanged"] = True
        report["scope"] = "fictional local acceptance; not a real-corpus or public-service SLA"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")
        print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
