"""Loopback-only benchmark review UI; not a public deployment or approval endpoint."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qs
from uuid import uuid4

from jsonschema import ValidationError

from . import evaluation, releases


def render(benchmark, token, has_gallery=False):
    def text(value):
        return escape(str(value), quote=True)

    parts = ["""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Benchmark researcher review</title>
<style>
body{font:17px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem}
article{border:1px solid #aaa;padding:1rem;margin:2rem 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eee;color:#111;padding:1rem}
label{display:block;margin:1rem 0}input,select,textarea,button{font:inherit}
textarea{width:95%;min-height:6rem}summary,button{cursor:pointer}
</style><h1>Benchmark researcher review</h1>
<p><strong>Reviewer-only: includes held-out questions and reference answers.
Do not share this page with the evaluated agent.</strong></p>
<p>References are provisional records, not independently validated scientific answers.
Submitting feedback does not approve, freeze or modify the benchmark.</p>""",
             f"<p>Version: {text(benchmark['benchmark_version'])}; status: {text(benchmark['status'])}</p>",
             "<h2>Corpus caveats</h2><ul>"]
    parts.extend(f"<li>{text(c)}</li>" for c in benchmark["corpus_caveats"])
    parts.append("</ul>")
    if has_gallery:
        parts.append('<nav><a href="/issues/index.html">Review source issues with highlighted pages</a></nav>'
                     '<p>Source issues document discrepancies or clarifications. Benchmark checks test '
                     'faithful retrieval; submitting feedback does not fix a publication.</p>')
    for item in benchmark["items"]:
        parts.extend([
            f"<article><h2>{text(item['id'])} — {text(item['split'])}</h2>",
            f"<p>{text(item['question'])}</p><p>Scope: {text(item['scope'])}</p><ul>",
            *(f"<li>{text(c)}</li>" for c in item["mandatory_caveats"]), "</ul>",
        ])
        for call in item["calls"]:
            parts.append(f"<details><summary>Reference records: {text(call['tool'])}</summary>"
                         f"<pre>{text(json.dumps(call, ensure_ascii=False, indent=2))}</pre></details>")
        parts.append(
            '<form method="post" action="/feedback">'
            f'<input type="hidden" name="token" value="{text(token)}">'
            f'<input type="hidden" name="item_id" value="{text(item["id"])}">'
            '<label>Reviewer name <input name="reviewer" required maxlength="200"></label>'
            '<label>Assessment <select name="assessment" required>'
            '<option value="needs_review">Needs further review</option>'
            '<option value="looks_correct">Looks correct</option>'
            '<option value="correction_needed">Correction needed</option>'
            '</select></label>'
            '<label>Feedback / proposed correction <textarea name="notes" required maxlength="10000">'
            '</textarea></label><button>Save feedback</button></form></article>')
    parts.append("</html>")
    return "".join(parts).encode("utf-8")


def create_server(benchmark_path, feedback_dir, port=8765, highlights_dir=None):
    raw = benchmark_path.read_bytes()
    benchmark = releases.read_json(raw)
    evaluation.validate(benchmark)
    digest = sha256(raw).hexdigest()
    token = secrets.token_urlsafe(32)
    assets = {}
    if highlights_dir is not None:
        root = highlights_dir.resolve(strict=True)
        records = releases.read_json((root / "manifest.json").read_bytes())
        gallery = (root / "index.html").read_text(encoding="utf-8")
        for record in records:
            name = record["image"]
            if Path(name).name != name or not name.endswith(".png"):
                raise ValueError("Highlight manifest contains an invalid image name")
            image = root / name
            if image.is_symlink() or not image.is_file():
                raise ValueError("Highlight image must be an existing regular file")
            if sha256(image.read_bytes()).hexdigest() != record["image_sha256"]:
                raise ValueError("Highlight image checksum mismatch")
            assets["/issues/" + name] = (image, "image/png")
            href = escape("../../../" + record["original_source"], quote=True)
            gallery = gallery.replace(f"href='{href}'", "href='#originals-not-served'")
        gallery = gallery.replace(
            "<h1>Highlighted source-issue evidence</h1>",
            '<h1>Highlighted source-issue evidence</h1><p><a href="/">Benchmark checks</a></p>'
            '<p id="originals-not-served">Original documents are not served. Use your authorized '
            'local originals or the publication links in the evidence dossier.</p>')
        assets["/issues/index.html"] = (gallery.encode("utf-8"), "text/html; charset=utf-8")
        assets["/issues/manifest.json"] = ((root / "manifest.json").read_bytes(), "application/json")
        assets["/source-issue-dossier.md"] = (
            (root.parent / "source-issue-dossier.md").read_bytes(), "text/plain; charset=utf-8")
    page = render(benchmark, token, bool(assets))
    item_ids = {item["id"] for item in benchmark["items"]}
    feedback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, status, body, content_type="text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def trusted_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.trusted_host():
                return self.reply(403, b"Use the exact 127.0.0.1 URL printed by the server.")
            if self.path in assets:
                body, content_type = assets[self.path]
                if isinstance(body, Path):
                    try:
                        if body.is_symlink():
                            raise ValueError("Highlight image became a symlink")
                        body = body.read_bytes()
                    except (OSError, ValueError) as error:
                        print(f"Highlight read failed: {error}", file=sys.stderr)
                        return self.reply(500, b"Highlight unavailable. Check server diagnostics.")
                return self.reply(200, body, content_type)
            if self.path != "/":
                return self.reply(404, b"Not found.")
            self.reply(200, page)

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.trusted_host() or self.headers.get("Origin") != origin:
                return self.reply(403, b"Untrusted request origin.")
            if self.path != "/feedback":
                return self.reply(404, b"Not found.")
            if (self.headers.get("Content-Type") != "application/x-www-form-urlencoded"
                    or self.headers.get("Transfer-Encoding") is not None):
                return self.reply(415, b"Expected an encoded review form.")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    return self.reply(413, b"Feedback must be between 1 and 65536 bytes.")
                fields = parse_qs(self.rfile.read(size).decode("utf-8"), strict_parsing=True,
                                  max_num_fields=5)
                required = {"token", "item_id", "reviewer", "assessment", "notes"}
                if set(fields) != required or any(len(v) != 1 for v in fields.values()):
                    raise ValueError("Expected exactly one value per review field")
                data = {key: value[0] for key, value in fields.items()}
                if not secrets.compare_digest(data.pop("token"), token):
                    return self.reply(403, b"Invalid review token. Reload the page.")
                if (data["item_id"] not in item_ids
                        or data["assessment"] not in {"needs_review", "looks_correct", "correction_needed"}
                        or not 1 <= len(data["reviewer"].strip()) <= 200
                        or not 1 <= len(data["notes"].strip()) <= 10000):
                    raise ValueError("Invalid review fields")
            except (ValueError, UnicodeError):
                return self.reply(400, b"Invalid review form.")
            record = {
                "schema_version": 1, "benchmark_sha256": digest,
                "benchmark_version": benchmark["benchmark_version"],
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "approval_granted": False, **data,
            }
            try:
                with (feedback_dir / f"{uuid4().hex}.json").open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
            except OSError as error:
                print(f"Feedback storage failed: {error}", file=sys.stderr)
                return self.reply(500, b"Feedback was not saved. Check the server diagnostics.")
            self.reply(201, b'<!doctype html><title>Feedback saved</title>'
                       b'<h1>Feedback saved</h1><p>No benchmark approval was granted.</p>'
                       b'<a href="/">Back to questions</a>')

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--feedback-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--highlights-dir", type=Path,
                        help="Existing checked highlight gallery; only manifest-listed PNGs are served")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        server = create_server(args.benchmark, args.feedback_dir, args.port, args.highlights_dir)
    except (OSError, ValueError, ValidationError) as error:
        parser.exit(1, f"Review server error: {error}\n")
    print(f"Reviewer-only UI: http://127.0.0.1:{server.server_port}\n"
          f"Feedback directory: {args.feedback_dir.resolve()}\n"
          "Local access only; no authentication or public deployment. Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
