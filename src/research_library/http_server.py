"""Authenticated Streamable HTTP transport sharing the read-only stdio handlers."""

import argparse
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit

import anyio
from jsonschema import ValidationError
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn

from . import releases
from . import http_auth
from .http_auth import FileTokenVerifier, read_registry, resource_url
from .server import SDKDiagnostics, create_server, diagnostic

LINK_PATH = re.compile(r"/s/([A-Za-z0-9_-]{32,128})/mcp")


def rejection_reason(status, messages):
    """Classify SDK errors without exposing their potentially echoed input."""
    fallback = {401: "authentication_required", 403: "permission_denied",
                404: "session_not_found", 406: "unsupported_accept",
                415: "unsupported_content_type"}.get(status, "sdk_request_rejected")
    body = b"".join(message.get("body", b"") for message in messages
                    if message["type"] == "http.response.body")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return fallback
    error = payload.get("error") if isinstance(payload, dict) else None
    text = error.get("message") if isinstance(error, dict) else None
    if not isinstance(text, str):
        return fallback
    for prefix, reason in (
        ("Bad Request: Missing session ID", "missing_session_id"),
        ("Bad Request: Unsupported protocol version:", "unsupported_protocol_version"),
        ("Parse error:", "invalid_json"),
        ("Validation error:", "invalid_jsonrpc"),
        ("Too many open sessions", "session_limit"),
    ):
        if text.startswith(prefix):
            return reason
    return fallback


class Boundary:
    """Bound finite HTTP requests before handing them to the SDK."""

    def __init__(self, app: ASGIApp, *, host, origin, max_body, max_inflight, timeout, rate,
                 extra_origins=(), link_tokens=False):
        self.app = app
        self.host = host
        self.origin = origin
        self.origins = {None, origin, *extra_origins}
        self.link_tokens = link_tokens
        self.max_body = max_body
        self.max_inflight = max_inflight
        self.timeout = timeout
        self.rate = rate
        self.active = 0
        self.recent = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.link_tokens:
            match = LINK_PATH.fullmatch(scope["path"])
            if match:
                # Secret-link access for web clients that cannot send headers: the path carries the bearer token.
                headers = [(key, value) for key, value in scope["headers"] if key.lower() != b"authorization"]
                headers.append((b"authorization", b"Bearer " + match[1].encode()))
                scope = {**scope, "path": "/mcp", "raw_path": b"/mcp", "headers": headers}
        request = Request(scope)

        def rejected(status, reason):
            diagnostic("http_rejected", status=str(status), reason=reason,
                       method=request.method if request.method in {"POST", "DELETE", "GET"} else "other",
                       endpoint="mcp" if request.url.path == "/mcp" else "other")

        async def fail(status, message):
            rejected(status, {
                "Untrusted host": "untrusted_host", "Untrusted origin": "untrusted_origin",
                "Duplicate authorization": "duplicate_authorization",
                "Request rate limit exceeded": "rate_limit",
                "Concurrent request limit exceeded": "concurrency_limit",
                "Invalid Content-Length": "invalid_content_length",
                "Request body limit exceeded": "body_limit",
                "Request deadline exceeded": "deadline_exceeded",
                "Response exceeds transport limit": "response_limit",
                "Service unavailable; contact the operator": "service_unavailable",
            }[message])
            await JSONResponse({"error": message}, status_code=status,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)

        if request.headers.get("host") != self.host:
            return await fail(421, "Untrusted host")
        if request.headers.get("origin") not in self.origins:
            return await fail(403, "Untrusted origin")
        if len(request.headers.getlist("authorization")) > 1:
            return await fail(400, "Duplicate authorization")
        if request.url.path == "/mcp" and request.method not in {"POST", "DELETE"}:
            return await Response(status_code=405, headers={"Allow": "POST, DELETE"})(scope, receive, send)
        now = time.monotonic()
        self.recent = [stamp for stamp in self.recent if stamp > now - 60]
        if len(self.recent) >= self.rate:
            return await fail(429, "Request rate limit exceeded")
        self.recent.append(now)
        if self.active >= self.max_inflight:
            return await fail(503, "Concurrent request limit exceeded")
        self.active += 1
        try:
            with anyio.fail_after(self.timeout):
                length = request.headers.get("content-length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError:
                        return await fail(400, "Invalid Content-Length")
                    if declared < 0 or declared > self.max_body:
                        return await fail(413, "Request body limit exceeded")
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_body:
                        return await fail(413, "Request body limit exceeded")
                    if not message.get("more_body", False):
                        break
                supplied = False

                async def replay():
                    nonlocal supplied
                    if not supplied:
                        supplied = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                messages = []
                size = 0

                async def bounded_send(message):
                    nonlocal size
                    size += len(message.get("body", b""))
                    if size > 262144:
                        raise OverflowError("HTTP response limit")
                    messages.append(message)

                await self.app(scope, replay, bounded_send)
            start = next((message for message in messages if message["type"] == "http.response.start"), None)
            if start is not None and start["status"] >= 400 and request.url.path == "/mcp":
                # SDK protocol-validation errors can echo malformed input.
                headers = {key.decode(): value.decode() for key, value in start["headers"]
                           if key.lower() in {b"www-authenticate", b"allow", b"retry-after"}}
                headers["Cache-Control"] = "no-store"
                rejected(start["status"], rejection_reason(start["status"], messages))
                return await JSONResponse({"error": "MCP request rejected"}, status_code=start["status"],
                                          headers=headers)(scope, receive, send)
            for message in messages:
                if message["type"] == "http.response.start":
                    message["headers"] += [(b"cache-control", b"no-store"),
                                           (b"x-content-type-options", b"nosniff")]
                await send(message)
        except TimeoutError:
            await fail(504, "Request deadline exceeded")
        except OverflowError:
            await fail(500, "Response exceeds transport limit")
        except Exception as error:
            # ASGI boundary: SDK/registry exceptions must not expose credentials,
            # source text or filesystem paths in responses or tracebacks.
            diagnostic("http_error", exception_type=type(error).__name__)
            await fail(503, "Service unavailable; contact the operator")
        finally:
            self.active -= 1


def create_app(*, release=None, store=None, collection_registry=None, allowed_collections=None,
               default_collections=None, profiles, registry, base_url,
               max_body=65536, max_inflight=8, timeout=30.0, rate=600, max_sessions=32,
               session_idle_timeout=300, extra_origins=(), link_tokens=False):
    resource_url(base_url)
    for value in extra_origins:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or value != f"https://{parts.netloc}":
            raise ValueError("Extra origins must be bare https origins")
    if sum(bool(root) for root in (release, store, collection_registry)) != 1 or not profiles:
        raise ValueError("Select one release or store and explicit allowed profiles")
    if not (1 <= max_body <= 1048576 and 1 <= max_inflight <= 64 and 0 < timeout <= 120
            and 1 <= rate <= 10000 and 1 <= max_sessions <= 1000 and session_idle_timeout > 0):
        raise ValueError("HTTP limits outside supported bounds")
    if bool(collection_registry) != bool(allowed_collections) or (default_collections and not collection_registry):
        raise ValueError("Collection registry requires explicit allowed collections")
    from .collections import CollectionStore
    selected_store = (CollectionStore(collection_registry, allowed_profiles=profiles,
                                      allowed_collections=allowed_collections, defaults=default_collections)
                      if collection_registry else
                      releases.ReleaseStore(store, allowed_profiles=set(profiles)) if store else None)
    snapshot = (selected_store.current() if selected_store else
                releases.validate(release, allowed_profiles=set(profiles)))
    verifier = FileTokenVerifier(registry, base_url)
    parsed = urlsplit(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    manager = StreamableHTTPSessionManager(
        create_server(None if selected_store else snapshot, selected_store),
        json_response=True, stateless=False,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[parsed.netloc],
            allowed_origins=[origin, *extra_origins]),
        max_request_body_size=max_body, max_sessions=max_sessions,
        session_idle_timeout=session_idle_timeout,
    )

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def health(request):
        return JSONResponse({"status": "alive"})

    async def ready(request):
        try:
            await anyio.to_thread.run_sync(
                lambda: selected_store.current() if selected_store else
                snapshot.verify_backing(release))
            await anyio.to_thread.run_sync(read_registry, registry)
        except (OSError, ValueError, sqlite3.Error, ValidationError):
            diagnostic("readiness_failed")
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    async def mcp_endpoint(scope: Scope, receive: Receive, send: Send):
        await manager.handle_request(scope, receive, send)

    endpoint = RequireAuthMiddleware(
        AuthContextMiddleware(mcp_endpoint),
        required_scopes=["evidence:read", *(f"profile:{p}" for p in sorted(profiles))],
        resource_metadata_url=AnyHttpUrl(f"{origin}/.well-known/oauth-protected-resource/mcp"),
    )
    protected = AuthenticationMiddleware(
        endpoint, backend=BearerAuthBackend(verifier, resource_server_url=AnyHttpUrl(base_url)))
    async def metadata(request):
        # RFC 9728 permits omitting authorization_servers. The SDK metadata
        # model requires one, but this deployment has no interactive OAuth issuer.
        return JSONResponse({
            "resource": base_url, "bearer_methods_supported": ["header"],
            "scopes_supported": ["evidence:read", *(f"profile:{p}" for p in sorted(profiles))],
            "resource_name": "Research Library private evidence (operator-issued bearer tokens)",
        })
    routes = [
        Route("/healthz", health, methods=["GET"]),
        Route("/readyz", ready, methods=["GET"]),
        Route("/mcp", protected, methods=["POST", "DELETE"]),
        Route("/.well-known/oauth-protected-resource/mcp", metadata, methods=["GET"]),
    ]
    application = Starlette(routes=routes, lifespan=lifespan)
    return Boundary(application, host=parsed.netloc, origin=origin, max_body=max_body,
                    max_inflight=max_inflight, timeout=timeout, rate=rate,
                    extra_origins=extra_origins, link_tokens=link_tokens)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    roots = parser.add_mutually_exclusive_group(required=True)
    roots.add_argument("--release", type=Path)
    roots.add_argument("--store", type=Path)
    roots.add_argument("--registry", type=Path, help="Collection registry, separate from token registry")
    parser.add_argument("--allow-collection", action="append")
    parser.add_argument("--default-collection", action="append")
    parser.add_argument("--allow-profile", required=True, action="append", choices=sorted(releases.PROFILES))
    parser.add_argument("--token-registry", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1",
                        help="127.0.0.1; 0.0.0.0 or a private LAN IP, with TLS or --allow-plain-lan")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--behind-tls-proxy", action="store_true")
    parser.add_argument("--allow-plain-lan", action="store_true",
                        help="Serve plain HTTP on a private LAN IP; bearer tokens are readable on that network")
    parser.add_argument("--link-tokens", action="store_true",
                        help="Also accept the bearer token in the path /s/<token>/mcp (requires an https base URL)")
    parser.add_argument("--allow-origin", action="append", default=[],
                        help="Additional browser origin to accept, e.g. https://claude.ai")
    parser.add_argument("--max-inflight", type=int, default=8)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--max-body", type=int, default=65536)
    parser.add_argument("--rate-per-minute", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("Supply both TLS certificate and key")
    plain_lan = False
    if args.host != "127.0.0.1":
        if args.host != "0.0.0.0" and not http_auth.private_address(args.host):
            parser.error("--host must be 127.0.0.1, 0.0.0.0 or a private LAN IP address")
        if args.tls_cert or args.behind_tls_proxy:
            pass
        elif args.allow_plain_lan:
            base = urlsplit(args.base_url)
            matches_host = (base.hostname == args.host.strip("[]") if args.host != "0.0.0.0"
                            else http_auth.private_address(base.hostname) and not base.hostname.startswith("127.")
                            and base.hostname != "::1")
            if base.scheme != "http" or not matches_host or (base.port or 80) != args.port:
                parser.error("--allow-plain-lan requires --base-url http://<private LAN IP>:<port>/mcp "
                             "matching --host (or any private LAN IP with 0.0.0.0) and --port")
            plain_lan = True
        else:
            parser.error("Non-loopback bind requires TLS, an explicitly isolated TLS proxy boundary, "
                         "or --allow-plain-lan with a private LAN IP")
    if args.tls_cert and not args.base_url.startswith("https://"):
        parser.error("TLS listener requires an https base URL")
    if args.link_tokens and not args.base_url.startswith("https://"):
        parser.error("--link-tokens requires an https base URL")
    if (args.behind_tls_proxy and args.host != "127.0.0.1"
            and not args.base_url.startswith("https://")):
        parser.error("External proxy deployment requires an https base URL")
    logger = logging.getLogger("mcp")
    logger.handlers = [SDKDiagnostics()]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        app = create_app(
            release=args.release, store=args.store, collection_registry=args.registry,
            allowed_collections=args.allow_collection, default_collections=args.default_collection,
            profiles=set(args.allow_profile),
            registry=args.token_registry, base_url=args.base_url, max_body=args.max_body,
            max_inflight=args.max_inflight, timeout=args.request_timeout, rate=args.rate_per_minute,
            extra_origins=tuple(args.allow_origin), link_tokens=args.link_tokens)
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        diagnostic("startup_failed", exception_type=type(error).__name__,
                   message="Validate release, private credential registry, URL and limits.")
        return 1
    diagnostic("http_starting", transport="streamable_http", url=args.base_url,
               **({"warning": "plain HTTP; bearer tokens are readable on the network"
                   + ("; listening on all interfaces" if args.host == "0.0.0.0" else "")} if plain_lan else {}))
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="critical",
                proxy_headers=False, server_header=False, timeout_keep_alive=5,
                timeout_graceful_shutdown=5, h11_max_incomplete_event_size=16384,
                ssl_certfile=str(args.tls_cert) if args.tls_cert else None,
                ssl_keyfile=str(args.tls_key) if args.tls_key else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
