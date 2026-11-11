"""Run source-free authenticated HTTP acceptance in a constrained local container."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parent / "portable"))
from test_releases import fixture_bundle
from test_mcp import protocol_cases
from research_library import http_auth, releases


def run(engine, *args):
    return subprocess.run([engine, *map(str, args)], check=True, text=True, capture_output=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman", choices=["podman", "docker"])
    parser.add_argument("--image", default="localhost/research-library:m5")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("Output must not exist")
    engine = args.engine
    metadata = json.loads(run(engine, "image", "inspect", args.image))[0]
    assert metadata["Config"]["User"] == "10001:10001"
    healthcheck = metadata.get("Healthcheck") if engine == "podman" else metadata["Config"].get("Healthcheck")
    assert healthcheck, "Image must retain healthcheck (Podman: build --format docker)"
    run(engine, "run", "--rm", "--read-only", "--network", "none", "--user", "0",
        "--entrypoint", "python",
        args.image, "-c",
        "from pathlib import Path;from importlib.metadata import distribution;"
        "assert not Path('/data').exists();assert not Path('/run/credentials').exists();"
        "assert not Path('/root/.cache/pip').is_dir();"
        "assert all(Path(str(p)).suffix not in {'.pdf','.png','.docx','.xlsx','.sqlite','.jsonl'} "
        "for p in distribution('research-library').files)")
    with tempfile.TemporaryDirectory(prefix="research-library-container-") as directory:
        root = Path(directory)
        bundle, _ = fixture_bundle(root)
        release = root / "release"
        releases.export(bundle, release, profile="synthetic")
        expected = protocol_cases(releases.validate(release, allowed_profiles={"synthetic"}))
        before = {p: p.read_bytes() for p in release.rglob("*") if p.is_file()}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}/mcp"
        secrets = root / "credentials"
        secrets.mkdir(mode=0o700)
        http_auth.issue(secrets / "tokens.json", root / "client-token", url, ["synthetic"])
        token = (root / "client-token").read_text().strip()
        name = "research-library-check-" + uuid4().hex[:12]
        command = [
            "run", "--detach", "--name", name, "--network", "host",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "512m", "--cpus", "2", "--pids-limit", "128",
            "--env", f"RESEARCH_LIBRARY_HEALTH_HOST=127.0.0.1:{port}",
            "--env", f"RESEARCH_LIBRARY_HTTP_PORT={port}",
            "--volume", f"{release}:/data/release:ro",
            "--volume", f"{secrets}:/run/credentials:ro",
        ]
        if engine == "podman":
            command.extend(["--userns", "keep-id:uid=10001,gid=10001"])
        else:
            if os.getuid() == 0:
                raise RuntimeError("Run Docker acceptance as a non-root host user for private bind-mount ownership")
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        command.extend([
            args.image, "--release", "/data/release", "--allow-profile", "synthetic",
            "--token-registry", "/run/credentials/tokens.json",
            "--port", str(port), "--base-url", url,
        ])

        def wait_ready():
            for _ in range(150):
                try:
                    if httpx.get(url.replace("/mcp", "/readyz"), timeout=1).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                state = json.loads(run(engine, "inspect", name))[0]["State"]
                if not state["Running"]:
                    raise RuntimeError(run(engine, "logs", name))
                time.sleep(0.1)
            raise TimeoutError("Container readiness timed out")

        async def exercise():
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                async with streamable_http_client(url, http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        assert len((await client.list_tools()).tools) == 12
                        for case in expected["cases"]:
                            response = await client.call_tool(case["tool"], case["arguments"])
                            assert response.structuredContent == case["response"]

        try:
            run(engine, *command)
            wait_ready()
            assert httpx.post(url, json={}).status_code == 401
            asyncio.run(exercise())
            script = (
                "import os,pathlib,importlib.util;"
                "assert os.getuid()!=0;"
                "assert importlib.util.find_spec('pymupdf') is None;"
                "assert not pathlib.Path('/build').exists();"
                "assert not pathlib.Path('/data/release/assets').exists();"
                "p=pathlib.Path('/home/evidence/forbidden');"
                "\ntry:p.write_text('must fail')"
                "\nexcept OSError:pass"
                "\nelse:raise AssertionError('root filesystem writable')"
            )
            run(engine, "exec", name, "python", "-c", script)
            mounted = json.loads(run(engine, "inspect", name))[0]
            limits = mounted["HostConfig"]
            assert limits["ReadonlyRootfs"]
            assert limits["Memory"] == 512 * 1024 * 1024
            assert limits["NanoCpus"] == 2000000000
            assert limits["PidsLimit"] == 128
            assert all(not item["RW"] for item in mounted["Mounts"] if item["Destination"] in {
                "/data/release", "/run/credentials"})
            run(engine, "exec", name, *healthcheck["Test"][1:])
            run(engine, "restart", "--time", "8", name)
            wait_ready()
            asyncio.run(exercise())
            logs = run(engine, "logs", name)
            assert token not in logs and "fictional-claim" not in logs
            assert all(p.read_bytes() == data for p, data in before.items())
            # Valid pinned data may continue serving, but readiness detects damaged backing files.
            records = release / "records.json"
            records.write_text("corrupt")
            assert httpx.get(url.replace("/mcp", "/readyz")).status_code == 503
            records.write_bytes(before[records])
            run(engine, "stop", "--time", "8", name)
            assert not json.loads(run(engine, "inspect", name))[0]["State"]["Running"]
            assert all(p.read_bytes() == data for p, data in before.items())
            report = {"passed": True, "image_id": metadata["Id"], "engine": engine,
                      "nonroot": True, "readonly": True, "restart": True,
                      "unauthorized_denied": True, "twelve_tool_parity": True,
                      "readiness_detected_corruption": True, "release_bytes_unchanged": True,
                      "memory_limit_mib": 512, "cpu_limit": 2}
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x") as stream:
                    json.dump(report, stream, indent=2)
                    stream.write("\n")
            print(json.dumps(report))
        finally:
            subprocess.run([engine, "rm", "--force", name], capture_output=True, text=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
