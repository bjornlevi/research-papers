import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from research_library import evaluation, review_server
from test_evaluation import specifications
from test_releases import fixture_bundle


class ReviewServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        bundle, _ = fixture_bundle(self.root)
        draft = evaluation.draft(bundle, specifications(), "review-fixture")
        draft["items"][0]["question"] = '<script>alert("unsafe")</script>'
        self.benchmark = self.root / "benchmark.json"
        self.benchmark.write_text(json.dumps(draft))
        self.feedback = self.root / "feedback"
        self.server = review_server.create_server(self.benchmark, self.feedback, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, method, path="/", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            connection.close()

    def form(self):
        status, page, headers = self.request("GET")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("held_out", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        return {"token": re.search('name="token" value="([^"]+)"', page)[1],
                "item_id": "fixture-0", "reviewer": "Test researcher",
                "assessment": "correction_needed", "notes": "Check denominator."}

    def post(self, form, origin=None):
        return self.request("POST", "/feedback", urlencode(form), {
            "Origin": origin or self.origin,
            "Content-Type": "application/x-www-form-urlencoded",
        })

    def test_page_and_persistent_feedback(self):
        form = self.form()
        before = self.benchmark.read_bytes()
        for _ in range(2):
            self.assertEqual(self.post(form)[0], 201)
        saved = list(self.feedback.glob("*.json"))
        self.assertEqual(len(saved), 2)
        record = json.loads(saved[0].read_text())
        self.assertEqual(record["notes"], form["notes"])
        self.assertFalse(record["approval_granted"])
        self.assertEqual(record["benchmark_sha256"], review_server.sha256(before).hexdigest())
        self.assertEqual(before, self.benchmark.read_bytes())

    def test_reject_untrusted_and_invalid_requests(self):
        form = self.form()
        self.assertEqual(self.post(form, "http://example.com")[0], 403)
        self.assertEqual(self.request("GET", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request("GET", "/benchmark.json")[0], 404)
        self.assertEqual(self.request("GET", "/../../papers/manifest.csv")[0], 404)
        self.assertEqual(self.request("POST", "/feedback")[0], 403)
        self.assertEqual(self.post({**form, "token": "wrong"})[0], 403)
        self.assertEqual(self.post({**form, "item_id": "../../outside"})[0], 400)
        self.assertEqual(self.post({**form, "notes": " "})[0], 400)
        self.assertEqual(self.post({**form, "notes": "a" * 65536})[0], 413)
        self.assertEqual(list(self.feedback.iterdir()), [])

    def test_storage_failure_is_not_success(self):
        form = self.form()
        self.feedback.rmdir()
        self.assertEqual(self.post(form)[0], 500)

    def test_existing_gallery_only_serves_selected_assets(self):
        highlights = self.root / "highlights"
        highlights.mkdir()
        image = b"fictional-highlight"
        (highlights / "page.png").write_bytes(image)
        (highlights / "private.pdf").write_bytes(b"not served")
        (highlights / "manifest.json").write_text(json.dumps([{
            "image": "page.png", "image_sha256": review_server.sha256(image).hexdigest(),
            "original_source": "papers/private.pdf",
        }]))
        (highlights / "index.html").write_text(
            "<h1>Highlighted source-issue evidence</h1>"
            "<a href='../../../papers/private.pdf'>Original source</a><img src='page.png'>")
        (self.root / "source-issue-dossier.md").write_text("Fictional issue context")
        server = review_server.create_server(self.benchmark, self.feedback, 0, highlights)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, status, fragment in (
                ("/", 200, b"/issues/index.html"),
                ("/issues/index.html", 200, b"#originals-not-served"),
                ("/issues/page.png", 200, image),
                ("/source-issue-dossier.md", 200, b"Fictional issue context"),
                ("/issues/private.pdf", 404, b"Not found"),
                ("/papers/private.pdf", 404, b"Not found"),
                ("/issues/../private.pdf", 404, b"Not found"),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", path)
                response = connection.getresponse()
                self.assertEqual(response.status, status)
                self.assertIn(fragment, response.read())
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
