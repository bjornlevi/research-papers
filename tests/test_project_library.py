import asyncio
import csv
import json
from datetime import timedelta
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pymupdf

from tools import project_library
from istopmm_evidence import web_server


class ProjectLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        project_library.initialize(self.root, "Fictional research", "Authorized private fictional test data.")

    def pdf(self, name, text):
        path = self.root.parent / name
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), text)
            document.save(path)
        return path

    def test_registration_validation_is_atomic(self):
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"}])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            project_library.register(self.root, [
                {"doi": "10.0000/two", "title": "Second"},
                {"doi": "10.0000/one", "title": "Duplicate"},
            ])
        self.assertEqual(len(project_library.status(self.root)["papers"]), 1)

    def test_initial_csv_is_validated_before_creating_project(self):
        project = self.root.parent / "new-project"
        invalid = [{"doi": "10.0000/one", "title": "First"},
                   {"doi": "10.0000/one", "title": "Duplicate"}]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            project_library.initialize(project, "New", "Private permission", invalid)
        self.assertFalse(project.exists())
        project_library.initialize(project, "New", "Private permission", invalid[:1])
        self.assertEqual(project_library.status(project)["papers"][0]["status"], "awaiting_pdf")

    def test_installed_cli_works_outside_source_tree(self):
        project = self.root.parent / "cli-project"
        papers = self.root.parent / "papers.csv"
        papers.write_text("doi,title\n10.0000/cli,Fictional paper\n", encoding="utf-8")
        pdf = self.pdf("cli.pdf", "Fictional research observation about grapefruit.")
        command = [str(Path(sys.executable).parent / "research-project"), "--root", str(project)]

        def run(*args):
            result = subprocess.run([*command, *args], cwd=self.root.parent, text=True,
                                    capture_output=True, check=True)
            return json.loads(result.stdout)

        self.assertEqual(run("init", "--name", "Fictional project", "--authorization",
                             "Authorized fictional data", "--papers", str(papers))["papers"], 1)
        self.assertEqual(run("status")["papers"][0]["status"], "awaiting_pdf")
        run("attach-pdf", "--doi", "10.0000/cli", "--pdf", str(pdf), "--confirm-identity")
        self.assertEqual(run("status")["papers"][0]["status"], "extracted")

    def test_metadata_manifest_roundtrip_excludes_private_data(self):
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"}])
        pdf = self.pdf("first.pdf", "Fictional research finding for private extraction and retrieval.")
        project_library.attach(self.root, "10.0000/one", pdf, identity_confirmed=True)
        destination = self.root.parent / "core-papers.json"
        self.assertEqual(project_library.export_manifest(self.root, destination), 1)
        self.assertEqual(json.loads(destination.read_text()), {
            "schema_version": 1, "papers": [{"doi": "10.0000/one", "title": "First"}]})
        fresh = self.root.parent / "fresh-project"
        project_library.initialize(fresh, "Other", "Authorized", project_library.read_manifest(destination))
        self.assertEqual(project_library.status(fresh)["papers"][0]["status"], "awaiting_pdf")
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_library.export_manifest(self.root, destination)

    def test_attach_search_and_retained_snapshot(self):
        project_library.register(self.root, [
            {"doi": "10.0000/one", "title": "First"},
            {"doi": "10.0000/two", "title": "Second"},
        ])
        pdf = self.pdf("first.pdf", "Fictional finding about marmalade and screening.")
        with self.assertRaisesRegex(ValueError, "Confirm"):
            project_library.attach(self.root, "10.0000/one", pdf, identity_confirmed=False)
        first = project_library.attach(self.root, "10.0000/one", pdf, identity_confirmed=True)
        index = Path(first["text_views"])
        self.assertTrue(index.is_file())
        self.assertIn("marmalade", next(index.parent.glob("*/text.md")).read_text())
        store = project_library.store(self.root)
        response = store.call("search_evidence", {"query": "marmalade"})
        self.assertEqual(len(response["result"]["items"]), 1)
        self.assertEqual(response["result"]["items"][0]["citation"]["locator"]["pdf_page"], 1)
        unit = response["result"]["items"][0]
        context = store.call("get_source_context", {"unit_id": unit["id"], "collections": ["core"],
                                                     "snapshot_id": first["snapshot"]})
        self.assertIn("marmalade", context["result"]["text"])
        self.assertIn("not independently verified", unit["citation"]["provenance"])
        app = web_server.create_app(registry=self.root / "registry", profiles=["private_context"],
                                    allowed_collections=["core"], default_collections=["core"])
        client = app.test_client()
        self.assertIn(b"Awaiting PDF", client.get("/library", base_url="http://127.0.0.1:8080").data)
        api = client.get("/api/search?collection=core&query=marmalade", base_url="http://127.0.0.1:8080")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(len(api.json["result"]["items"]), 1)
        connect = client.get("/connect", base_url="http://127.0.0.1:8080").get_data(as_text=True)
        self.assertIn("YOUR_SEARCH_TERM", connect)
        self.assertIn("--collection core", connect)
        self.assertNotIn("Search for MGUS screening", connect)
        async def verify_mcp():
            params = StdioServerParameters(
                command=str(Path(sys.executable).parent / "istopmm-mcp"),
                args=["--registry", str(self.root / "registry"), "--allow-profile", "private_context",
                      "--allow-collection", "core", "--default-collection", "core"])
            with tempfile.TemporaryFile(mode="w+") as diagnostics:
                async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                        await client.initialize()
                        result = await client.call_tool("search_evidence", {"query": "marmalade"})
                        self.assertFalse(result.isError)
                        hit = result.structuredContent["result"]["items"][0]
                        context = await client.call_tool("get_source_context", {
                            "unit_id": hit["id"], "collections": ["core"], "snapshot_id": first["snapshot"]})
                        self.assertFalse(context.isError)
                        self.assertIn("marmalade", context.structuredContent["result"]["text"])
        asyncio.run(verify_mcp())
        with self.assertRaisesRegex(ValueError, "already"):
            project_library.attach(self.root, "10.0000/two", pdf, identity_confirmed=True)
        second_pdf = self.pdf("second.pdf", "Distinct fictional observation about oranges and screening.")
        second = project_library.attach(self.root, "10.0000/two", second_pdf, identity_confirmed=True)
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(len(store.load(first["snapshot"]).snapshots["core"]._bundle["sources"]), 1)
        self.assertEqual(len(store.current().snapshots["core"]._bundle["sources"]), 2)
        self.assertEqual({row["status"] for row in project_library.status(self.root)["papers"]}, {"extracted"})

    def test_invalid_pdf_does_not_activate(self):
        project_library.register(self.root, [{"doi": "10.0000/one", "title": "First"}])
        path = self.root.parent / "bad.pdf"
        path.write_text("not a PDF")
        with self.assertRaisesRegex(ValueError, "PDF"):
            project_library.attach(self.root, "10.0000/one", path, identity_confirmed=True)
        self.assertIsNone(project_library.status(self.root)["snapshot"])

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is required for OCR")
    def test_scanned_pdf_ocr_is_searchable_and_labelled(self):
        project_library.register(self.root, [{"doi": "10.0000/scan", "title": "Scanned paper"}])
        with pymupdf.open() as original:
            page = original.new_page(width=612, height=792)
            page.insert_text((72, 100), "Fictional research study about tangerines and screening.",
                             fontsize=18)
            image = page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).tobytes("png")
        scan = self.root.parent / "scan.pdf"
        with pymupdf.open() as document:
            page = document.new_page(width=612, height=792)
            page.insert_image(page.rect, stream=image)
            document.save(scan)
        result = project_library.attach(self.root, "10.0000/scan", scan, identity_confirmed=True)
        self.assertIsNotNone(result["ocr"])
        hit = project_library.store(self.root).call("search_evidence", {"query": "tangerines"})
        self.assertEqual(len(hit["result"]["items"]), 1)
        self.assertTrue(any("not human verified" in caveat for caveat in hit["result"]["items"][0]["caveats"]))
        self.assertIn(project_library.status(self.root)["papers"][0]["status"],
                      {"extracted_with_ocr", "needs_visual_review"})

    def test_citation_export_retains_occurrences_and_escapes_spreadsheet_formulas(self):
        output = self.root / "discovery"
        output.mkdir()
        graph = {
            "nodes": {"crossref:example": {
                "record": {"title": "=2+2", "identifiers": {}, "publication_date": "",
                           "authors": ["Example Author"]},
                "reference": {"unstructured": "unknown reference"}}},
            "edges": [{"seed_doi": "10.0000/one", "direction": "background",
                       "target_provider_record": "crossref:example",
                       "sources": [{"url": "https://example.test"}]}],
        }
        (output / "neighborhood.json").write_text(json.dumps(graph))
        target = project_library.export_citations(output)
        with target.open(newline="") as stream:
            row, = csv.DictReader(stream)
        self.assertEqual(row["title"], "'=2+2")
        self.assertEqual(row["unstructured_reference"], "unknown reference")
        self.assertEqual(json.loads(row["edge_sources"])[0]["url"], "https://example.test")
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_library.export_citations(output)


if __name__ == "__main__":
    unittest.main()
