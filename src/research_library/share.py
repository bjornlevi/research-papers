"""Share the MCP server with web AI clients through a temporary Cloudflare quick tunnel and a secret link.

Starts `cloudflared` (outbound-only; nothing is opened on this network), a separate MCP HTTP server on
127.0.0.1 for the tunnel's public HTTPS address, and issues one expiring bearer token that is carried in
the link path. Stopping (Ctrl-C) revokes the token; the temporary address also disappears.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import httpx

from . import http_auth, local_client

TUNNEL_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com(?![\w.-])")
WEB_ORIGINS = ("https://claude.ai", "https://chatgpt.com")
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "research-library-share-check", "version": "1"}}}


def say(message):
    print(f"[share {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def revoke(registry, token_path):
    if not token_path.exists():
        return
    try:
        token = local_client.read_token(token_path)
        current = http_auth.read_registry(registry)
        entry = local_client.token_entry(current, token)
        if entry is not None and not entry["revoked"]:
            entry["revoked"] = True
            http_auth.write_registry(registry, current)
    finally:
        token_path.unlink(missing_ok=True)


def start_tunnel(cloudflared, port, timeout=60):
    process = subprocess.Popen(
        [cloudflared, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
    found = []
    ready = threading.Event()

    def drain():
        for line in process.stderr:
            match = TUNNEL_URL.search(line)
            if match and not found:
                found.append(match[0])
                ready.set()
        ready.set()

    threading.Thread(target=drain, daemon=True).start()
    if not ready.wait(timeout) or not found:
        process.terminate()
        raise RuntimeError("cloudflared did not report a tunnel address; check the internet connection")
    return process, found[0]


def wait_ready(port, host, child, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("The MCP server stopped during startup; see the messages above")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/readyz", headers={"Host": host}, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("The MCP server did not become ready")


def check_public(link, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.post(link, json=INITIALIZE, timeout=15, headers={
                "Accept": "application/json, text/event-stream"})
            if response.status_code == 200 and "result" in response.json():
                return True
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(3)
    return False


def stop(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token-registry", required=True, type=Path)
    parser.add_argument("--state", type=Path, default=Path("releases/http-local/share"),
                        help="Private folder for the share token and link")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--profile", required=True, action="append")
    parser.add_argument("--cloudflared", default=shutil.which("cloudflared")
                        or str(Path(sys.executable).with_name("cloudflared")))
    parser.add_argument("server_args", nargs=argparse.REMAINDER,
                        help="After --: collection arguments passed to the MCP HTTP server")
    args = parser.parse_args()
    server_args = args.server_args[1:] if args.server_args[:1] == ["--"] else args.server_args
    if not 1 <= args.days <= 30:
        parser.error("--days must be 1 to 30")
    if not Path(args.cloudflared).is_file():
        parser.error("cloudflared not found; run make cloudflared")
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path, link_path = args.state / "token", args.state / "link"
    local_client.initialize(args.token_registry)
    revoke(args.token_registry, token_path)
    link_path.unlink(missing_ok=True)
    tunnel = server = None
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    try:
        say("Starting Cloudflare quick tunnel (outbound only)")
        tunnel, public = start_tunnel(args.cloudflared, args.port)
        host = public.removeprefix("https://")
        base = public + "/mcp"
        http_auth.issue(args.token_registry, token_path, base, args.profile, args.days * 86400)
        token = local_client.read_token(token_path)
        link = f"{public}/s/{token}/mcp"
        say(f"Tunnel {public}; starting the MCP server on 127.0.0.1:{args.port} (loading collections)")
        server = subprocess.Popen(
            [sys.executable, "-m", "research_library.http_server", *server_args,
             *(f"--allow-profile={profile}" for profile in args.profile),
             "--token-registry", str(args.token_registry), "--port", str(args.port), "--base-url", base,
             "--behind-tls-proxy", "--link-tokens", *(f"--allow-origin={origin}" for origin in WEB_ORIGINS)],
            start_new_session=True)
        wait_ready(args.port, host, server)
        say("Checking the link from the internet")
        if not check_public(link):
            raise RuntimeError("The server works locally but the public link did not answer; try again")
        descriptor = os.open(link_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(link + "\n")
        print(json.dumps({"link": link, "expires_in_days": args.days, "saved_in": str(link_path)}), flush=True)
        say("Sharing. Give this link to the person you share with (treat it like a password):\n\n  "
            + link + "\n\n"
            "  Claude (web/desktop): Settings > Connectors > Add custom connector > paste the link, no auth.\n"
            "  ChatGPT/Codex: Plugins > + > Add custom MCP server > paste the link, no authentication.\n\n"
            "Keep this running. Ctrl-C stops sharing and revokes the link; a restart gives a new link.")
        while not stopping.is_set():
            if server.poll() is not None or tunnel.poll() is not None:
                raise RuntimeError("The tunnel or MCP server stopped; run make share again")
            stopping.wait(2)
    except KeyboardInterrupt:
        pass
    except (RuntimeError, OSError, ValueError) as error:
        say(f"Sharing failed: {error}")
        return 1
    finally:
        stop(server)
        stop(tunnel)
        revoke(args.token_registry, token_path)
        link_path.unlink(missing_ok=True)
        say("Stopped; the link is revoked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
