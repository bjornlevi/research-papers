import csv
from contextlib import redirect_stderr
import io
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import pymupdf

from research_library import web_server
from research_library.build import literature_text, provided_sources as provided
from research_library.build import literature_update as update
import test_literature_update

AWAITING = "doi:10.0000/awaiting"


def text_pdf(text="Authored provided publication page with reproducible software fixture text only."):
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), text)
        return document.tobytes()


def image_only_pdf():
    with pymupdf.open() as document:
        document.new_page().draw_rect((50, 50, 300, 200), color=(0, 0, 0), fill=(0, 0, 0))
        return document.tobytes()


class Fixture(unittest.TestCase):
    def setUp(self):
        fixture = test_literature_update.UpdateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.data_root = fixture.root / "local-data"
        for source, destination in (
            (fixture.acquisition, "releases/istopmm-first-tier-20261019"),
            (fixture.extraction, "releases/m9-ingestion-001"),
            (fixture.papers, "papers/literature/istopmm-first-tier-20261019"),
            (fixture.registry, "releases/m9-platform"),
        ):
            shutil.copytree(source, self.data_root / destination)
        self.registry = self.data_root / "releases/m9-platform"
        self.lookups = []
        self.real_lookup = provided.lookup_license

        def lookup(doi):
            self.lookups.append(doi)
            return {"license": "cc-by-nc", "license_source": "openalex", "license_checked_at": "2026-01-01T00:00:00Z"}

        patcher = patch.object(provided, "lookup_license", lookup)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.baseline = update.store_at(self.registry).current()
        header = ["id", "title", "authors", "publication_date", "journal", "doi", "citation_url", "background_of",
                  "impact_on", "acquisition_status", "pdf_sha256"]
        rows = [
            {"id": AWAITING, "title": "Authored awaiting publication", "publication_date": "2025-01-01",
             "doi": "10.0000/awaiting", "background_of": "10.0000/seed-0",
             "acquisition_status": "no_explicitly_licensed_pdf"},
            {"id": "doi:10.0000/fictional", "title": "Authored fictional publication", "doi": "10.0000/fictional",
             "publication_date": "2025-01-01", "acquisition_status": "downloaded"},
            {"id": "pmid:999", "title": "Authored PMID-only publication", "publication_date": "2025-01-01",
             "acquisition_status": "licensed_locations_failed"},
        ]
        with (self.data_root / "releases/istopmm-first-tier-20261019/bibliography.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, header)
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in header} for row in rows)

    def ledger(self):
        return provided.read_ledger(provided.root_of(self.data_root))


class ProvidedSourceTests(Fixture):
    def test_receive_rejects_unlisted_downloaded_doi_less_and_non_pdf_and_deduplicates(self):
        data = text_pdf()
        for identifier, message in (("doi:10.0000/unknown", "listed"), ("doi:10.0000/fictional", "already"),
                                    ("pmid:999", "DOI")):
            with self.assertRaisesRegex(ValueError, message):
                provided.receive(self.data_root, identifier, data, method="upload")
        with self.assertRaisesRegex(ValueError, "did not return a PDF"):
            provided.receive(self.data_root, AWAITING, b"<html>Sign in</html>", method="link", url="https://example.org")
        with self.assertRaisesRegex(ValueError, "readable PDF"):
            provided.receive(self.data_root, AWAITING, b"%PDF-1.7 broken", method="upload")
        first = provided.receive(self.data_root, AWAITING, data, method="upload", filename="paper.pdf")
        self.assertEqual(first["status"], "received")
        self.assertEqual(provided.receive(self.data_root, AWAITING, text_pdf("Another copy text."), method="upload"),
                         first)
        self.assertTrue((provided.root_of(self.data_root) / "papers" / first["filename"]).is_file())
        self.assertEqual(provided.pending(self.data_root), [AWAITING + "|" + first["sha256"]])

    def test_fetch_follows_public_redirects_and_rejects_private_hosts_and_non_200(self):
        pdf = text_pdf()

        def handler(request):
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "https://cdn.example.org/paper.pdf"})
            if request.url.path == "/private":
                return httpx.Response(302, headers={"location": "http://internal.example/paper.pdf"})
            if request.url.path == "/denied":
                return httpx.Response(403)
            return httpx.Response(200, content=pdf)

        def resolve(host):
            if host == "internal.example":
                raise ValueError("Links to local or private network addresses are not allowed")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        data, final = provided.fetch("https://example.org/start", client=client, resolve=resolve)
        self.assertEqual((data, final), (pdf, "https://cdn.example.org/paper.pdf"))
        with self.assertRaisesRegex(ValueError, "private"):
            provided.fetch("https://example.org/private", client=client, resolve=resolve)
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            provided.fetch("https://example.org/denied", client=client, resolve=resolve)
        for url in ("file:///etc/passwd", "https://user:secret@example.org/a.pdf"):
            with self.assertRaisesRegex(ValueError, "http"):
                provided.fetch(url, client=client, resolve=resolve)
        with self.assertRaisesRegex(ValueError, "private"):
            provided.public_address("127.0.0.1")

    def test_apply_adds_snapshot_with_provenance_text_view_and_rollback(self):
        item = provided.receive(self.data_root, AWAITING, text_pdf(), method="link",
                                url="https://example.org/a.pdf", final_url="https://example.org/a.pdf")
        result = provided.apply(self.data_root)
        self.assertEqual(result["added"], [AWAITING])
        current = update.store_at(self.registry).current()
        self.assertEqual(current.release_id, result["snapshot"])
        self.assertEqual(current.snapshots["istopmm"].release_id, self.baseline.snapshots["istopmm"].release_id)
        sources = current.snapshots["literature"]._bundle["sources"]
        added = next(source for source in sources if source["sha256"] == item["sha256"])
        self.assertEqual(added["bibliography"]["acquisition_method"], "user_provided_private_copy")
        self.assertIn("License reported by OpenAlex: cc-by-nc.", added["provenance"])
        self.assertEqual(self.lookups, ["10.0000/awaiting"])
        self.assertEqual(self.ledger()["items"][AWAITING + "|" + item["sha256"]]["license"], "cc-by-nc")
        hit = current.snapshots["literature"].call("search_evidence", {"query": "reproducible"})["result"]["items"]
        self.assertTrue(any(entry["citation"]["source_sha256"] == item["sha256"] for entry in hit))
        self.assertEqual(self.ledger()["items"][AWAITING + "|" + item["sha256"]]["status"], "added")
        self.assertEqual(provided.apply(self.data_root)["snapshot"], None)

        built = literature_text.build(self.data_root)
        self.assertEqual(built["sources"], 2)
        output = literature_text.output_of(self.data_root)
        text = next(output.glob("10-0000-awaiting-*/text.md")).read_text()
        self.assertIn("## PDF page 1", text)
        self.assertIn(item["sha256"], text)
        self.assertIn("reproducible software fixture", text)
        self.assertIn("text.md", (output / "index.md").read_text())

        undone = provided.rollback(self.data_root)
        self.assertEqual((undone["snapshot"], undone["removed"]), (self.baseline.release_id, [AWAITING]))
        self.assertEqual(update.store_at(self.registry).current().release_id, self.baseline.release_id)
        self.assertEqual(self.ledger()["items"][AWAITING + "|" + item["sha256"]]["status"], "rolled_back")
        with self.assertRaisesRegex(ValueError, "No provided"):
            provided.rollback(self.data_root)

    def test_operator_licence_wins_over_lookup_and_lookup_reads_openalex(self):
        with self.assertRaisesRegex(ValueError, "too long"):
            provided.receive(self.data_root, AWAITING, text_pdf(), method="upload", license="x" * 201)
        item = provided.receive(self.data_root, AWAITING, text_pdf(), method="upload",
                                license="  publisher   permission ")
        self.assertEqual((item["license"], item["license_source"]), ("publisher permission", "operator"))
        provided.apply(self.data_root)
        self.assertEqual(self.lookups, [])
        sources = update.store_at(self.registry).current().snapshots["literature"]._bundle["sources"]
        added = next(source for source in sources if source["sha256"] == item["sha256"])
        self.assertIn("License given by the operator: publisher permission.", added["provenance"])

        def handler(request):
            self.assertEqual((request.url.host, request.url.path), ("api.openalex.org", "/works/https://doi.org/10.1/a"))
            return httpx.Response(200, json={"best_oa_location": None, "primary_location": {"license": None},
                                             "locations": [{"license": None}, {"license": "cc-by-nd"}]})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.assertEqual(self.real_lookup("10.1/a", client=client)["license"], "cc-by-nd")
        failing = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.addCleanup(failing.close)
        self.assertIsNone(self.real_lookup("10.1/a", client=failing))
        missing = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        self.addCleanup(missing.close)
        self.assertIsNone(self.real_lookup("10.1/b", client=missing)["license"])

    def test_image_only_pdf_is_ocrd_and_unreadable_pdf_fails_without_snapshot(self):
        ocr = update.literature_ocr
        tesseract = patch.object(ocr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "tesseract 5\n"))
        text = "Fictional OCR transcription of a scanned page"
        readable = {"text": text, "text_sha256": ocr.ingest.identity(text), "mean_word_confidence": 91.0,
                    "word_count": 7, "status": "ocr_text", "blank_check": None}
        item = provided.receive(self.data_root, AWAITING, image_only_pdf(), method="upload")
        with tesseract, patch.object(ocr, "recognize", return_value=readable), redirect_stderr(io.StringIO()):
            result = provided.apply(self.data_root)
        self.assertEqual(result["added"], [AWAITING])
        entry = self.ledger()["items"][AWAITING + "|" + item["sha256"]]
        self.assertEqual((entry["extraction"], entry["ocr"]["ocr_pages"]), ("extracted_with_ocr", 1))
        snapshot = update.store_at(self.registry).current().snapshots["literature"]
        unit = next(unit for unit in snapshot._bundle["units"] if item["sha256"] in unit["id"])
        self.assertIn(text, unit["text"])
        self.assertTrue(any("not human verified" in caveat for caveat in unit["caveats"]))
        literature_text.build(self.data_root)
        self.assertIn("OCR text, not human verified",
                      next(literature_text.output_of(self.data_root).glob("10-0000-awaiting-*/text.md")).read_text())

        provided.rollback(self.data_root)
        empty = {"text": "", "text_sha256": ocr.ingest.identity(""), "mean_word_confidence": None,
                 "word_count": 0, "status": "needs_visual_review", "blank_check": None}
        other = provided.receive(self.data_root, AWAITING, image_only_pdf() + b"\n%%", method="upload")
        with tesseract, patch.object(ocr, "recognize", return_value=empty), redirect_stderr(io.StringIO()):
            result = provided.apply(self.data_root)
        self.assertEqual((result["added"], result["snapshot"]), ([], None))
        self.assertRegex(result["failed"][0]["error"], "OCR")
        self.assertEqual(self.ledger()["items"][AWAITING + "|" + other["sha256"]]["status"], "failed")
        self.assertEqual(update.store_at(self.registry).current().release_id, self.baseline.release_id)

    def test_apply_waits_while_another_update_writer_holds_the_registry(self):
        provided.receive(self.data_root, AWAITING, text_pdf(), method="upload")
        held, release = threading.Event(), threading.Event()

        def writer():
            with update.writer_lock(self.registry):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=writer)
        thread.start()
        held.wait(10)
        try:
            self.assertEqual(provided.apply(self.data_root)["waiting"], 1)
        finally:
            release.set()
            thread.join()
        self.assertEqual(provided.apply(self.data_root)["added"], [AWAITING])

    def test_rebase_keeps_unstaged_update_run_and_skips_staged(self):
        root = self.data_root / "releases/source-updates"
        update.write(root, "latest.json", {"run": "update-1"})
        state = {"candidate": None, "activation_baseline": "old"}
        update.write(root / "runs/update-1", "state.json", state)
        self.assertFalse(update.rebase_pending_run(root, "other", "new"))
        self.assertTrue(update.rebase_pending_run(root, "old", "new"))
        self.assertEqual(update.read(root / "runs/update-1/state.json")["activation_baseline"], "new")
        update.write(root / "runs/update-1", "state.json", {"candidate": {"activated": False},
                                                             "activation_baseline": "new"})
        self.assertFalse(update.rebase_pending_run(root, "new", "newer"))


class ProvideWebTests(Fixture):
    def client(self, base="http://127.0.0.1:8080"):
        app = web_server.create_app(registry=self.registry, profiles={"private_context"},
                                    allowed_collections=["istopmm", "literature", "catalog"],
                                    default_collections=["istopmm", "literature"], data_root=self.data_root)
        client = httpx.Client(transport=httpx.WSGITransport(app=app), base_url=base)
        self.addCleanup(client.close)
        return client

    def token(self, page):
        return page.split('name="token" value="', 1)[1].split('"', 1)[0]

    def test_form_only_for_awaiting_doi_rows_and_upload_adds_paper(self):
        client = self.client()
        page = client.get("/library?download=awaiting").text
        self.assertEqual(page.count("Provide PDF or link"), 2)
        self.assertIn('value="doi:10.0000/awaiting"', page)
        self.assertNotIn('value="pmid:999"', page)
        self.assertIn("Providing a copy requires a DOI", page)
        self.assertIn('name="license"', page)
        files = {"pdf": ("paper.pdf", text_pdf(), "application/pdf")}
        form = {"publication_id": AWAITING, "token": "wrong"}
        self.assertEqual(client.post("/library/provide", data=form, files=files).status_code, 403)
        form["token"] = self.token(page)
        foreign = self.client("http://evil.example:8080")
        self.assertEqual(foreign.post("/library/provide", data=form, files=files).status_code, 421)
        cross = client.post("/library/provide", data=form, files=files, headers={"Origin": "http://evil.example"})
        self.assertEqual(cross.status_code, 403)
        both = client.post("/library/provide", data={**form, "url": "https://example.org/a.pdf"}, files=files)
        self.assertEqual(both.status_code, 400)
        self.assertIn("one of them", both.text)
        bad = client.post("/library/provide", data=form, files={"pdf": ("x.pdf", b"<html>", "application/pdf")})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("did not return a PDF", bad.text)
        accepted = client.post("/library/provide", data=form, files=files)
        self.assertEqual(accepted.status_code, 200)
        self.assertIn("PDF received", accepted.text)
        deadline = time.monotonic() + 120
        while provided.pending(self.data_root) and time.monotonic() < deadline:
            time.sleep(0.2)
        status = client.get("/library?query=10.0000/awaiting").text
        self.assertIn("Provided PDF/link", status)
        self.assertNotIn('value="doi:10.0000/awaiting"', status)
        self.assertEqual(update.store_at(self.registry).current().release_id,
                         self.ledger()["activations"][0]["snapshot"])


if __name__ == "__main__":
    unittest.main()
