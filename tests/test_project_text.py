import asyncio
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from html import unescape
from urllib.parse import quote
from urllib.parse import urlencode

import pymupdf
from mcp.shared.memory import create_connected_server_and_client_session

from research_library import collections, passage_context, server, web_server
from research_library.build import literature_text, project_chunks, project_edit, project_library, project_purge, project_text


class ProjectTextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "library"
        project_library.initialize(self.root, "Portable library", "Synthetic private permission")
        project_library.register(self.root, [{"doi": "10.0000/text", "title": "Synthetic article"}])
        self.pdf = self.root.parent / "article.pdf"
        with pymupdf.open() as pdf:
            for number in [1, 2]:
                pdf.new_page().insert_text((50, 50), f"Drafttoken original article page {number} for clear text tests.")
            pdf.save(self.pdf)
        self.attached = project_library.attach(self.root, "10.0000/text", self.pdf, identity_confirmed=True)
        self.editorial = project_text.editorial_store(self.root).current()
        self.bundle = self.editorial.snapshots["core"]._bundle
        self.source = self.bundle["sources"][0]
        self.units = project_edit.document_units(self.bundle, self.source["id"])

    def publish(self, **changes):
        return project_text.publish(self.root, **{
            "snapshot_id": project_text.editorial_store(self.root).current().release_id,
            "source_id": self.source["id"], "reviewer": "Synthetic admin",
            "reason": "Checked synthetic transcription", "checked": True, **changes})

    def test_exact_evidence_through_mcp(self):
        edited = (
            "The text mentions Table 1 and Figure 1C but gives no values.\n\n"
            "## Table 1: Results\n\n| Target | Study group (n = 10) |\n| --- | --- |\n"
            "| Example | 7 (70%) a |\n| Other | 3 |\n\n**a** Tested subset only.\n\n"
            "**Figure 1** Selected examples. **(A)** First control. **(B)** Second control. "
            "**(C)** Third observation."
        )
        project_edit.correct(self.root, snapshot_id=self.editorial.release_id, source_id=self.source["id"],
            unit_id=self.units[0]["id"], text=edited,
            bibliography={key: value or "" for key, value in self.source["bibliography"].items()},
            source_url=self.source["url"], tables=[], reviewer="Manager", reason="Compared with original",
            checked=False)
        self.publish()
        registry = project_library.store(self.root)
        baseline = registry.current()
        snapshot = baseline.snapshots["core"]
        units = snapshot.document_units(self.source["id"])
        blocks = passage_context.document_blocks(units, title=self.source["bibliography"]["title"])
        self.assertIn("Tested subset only", blocks[2][0]["text"][blocks[2][1]:blocks[2][2]])
        ranges = ["1", "2-3", *(str(i) for i in range(4, len(blocks) + 1))]
        project_chunks.update(self.root, source_id=self.source["id"], snapshot_id=baseline.release_id,
                              form={"ranges": ", ".join(ranges)},
                              activate=True)

        async def exercise():
            async with create_connected_server_and_client_session(server.create_server(None, registry)) as client:
                for query, label, expected in (
                    ("What does Table 1 report for Example in the study group?", "Table 1: Example",
                     ("Study group (n = 10)", "7 (70%) a", "Tested subset only")),
                    ("What does Figure 1C show?", "Figure 1C", ("Selected examples", "Third observation")),
                ):
                    response = (await client.call_tool("search_evidence", {"query": query})).structuredContent
                    hit = response["result"]["items"][0]
                    self.assertEqual(hit["exact_evidence"]["label"], label)
                    for text in expected:
                        self.assertIn(text, hit["exact_evidence"]["text"])
                    self.assertNotIn("First control", hit["exact_evidence"]["text"])
                    self.assertEqual(hit["citation"]["locator"], {"kind": "pdf", "pdf_page": 1})
                    old = (await client.call_tool("search_evidence", {
                        "query": query, "snapshot_id": baseline.release_id,
                    })).structuredContent
                    self.assertFalse(any("exact_evidence" in hit for hit in old["result"]["items"]))
                missing = (await client.call_tool("search_evidence", {"query": "Figure 1Z"})).structuredContent
                self.assertFalse(any("exact_evidence" in hit for hit in missing["result"]["items"]))

        asyncio.run(exercise())

    def test_git_markdown_tracking_backfills_and_keeps_each_version(self):
        subprocess.run(["git", "init", "-q", str(self.root.parent)], check=True)
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        settings = client.get("/settings", base_url="http://localhost:8080").get_data(as_text=True)
        token = re.search(
            r'<form method="post" action="/settings/git-markdown">.*?name="token" value="([^"]+)"',
            settings, re.S).group(1)
        denied = client.post("/settings/git-markdown",
                             data={"token": token, "enabled": "true"}, base_url="http://localhost:8080")
        self.assertEqual(denied.status_code, 400)
        enabled = client.post("/settings/git-markdown",
                              data={"token": token, "enabled": "true", "confirm_rights": "yes"},
                              base_url="http://localhost:8080")
        self.assertEqual(enabled.status_code, 200)
        self.assertIn("1 current versions exported", enabled.get_data(as_text=True))
        output = self.root.parent / "project-markdown"
        initial = next(output.glob("*/draft-*.md"))
        self.assertIn("Drafttoken original article page 1", initial.read_text())
        project_library.register(self.root, [{"doi": "10.0000/extracted", "title": "Extracted article"}])
        extracted_pdf = self.root.parent / "extracted.pdf"
        native_text = "Freshly extracted synthetic page with enough native text for this fixture."
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((50, 50), native_text)
            pdf.save(extracted_pdf)
        attached = project_library.attach(self.root, "10.0000/extracted", extracted_pdf, identity_confirmed=True)
        self.assertIsNone(attached["ocr"])
        extracted = next(output.glob("*/extracted-*.md"))
        self.assertIn(native_text, extracted.read_text())
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.root.parent), "ls-files", "--error-unmatch",
             extracted.relative_to(self.root.parent).as_posix()],
            capture_output=True, check=False).returncode, 0)

        current = project_text.editorial_store(self.root).current()
        project_edit.correct(
            self.root, snapshot_id=current.release_id, source_id=self.source["id"], unit_id=self.units[0]["id"],
            text="Edited markdown with a structured table.",
            bibliography={key: value or "" for key, value in self.source["bibliography"].items()},
            source_url=self.source["url"], tables=[{
                "id": "", "title": "Synthetic values", "headers": ["Measure", "Value"],
                "units": [None, None], "rows": [["Count", 3]], "footnotes": ["Synthetic fixture."],
            }], reviewer="Synthetic admin", reason="Track markdown versions", checked=False)
        current = project_text.editorial_store(self.root).current()
        latest_draft_path = (output / literature_text.slug(self.source)
                             / f"draft-{current.snapshots['core'].release_id}.md")
        self.assertTrue(latest_draft_path.is_file())
        latest_draft = latest_draft_path.read_text()
        self.assertIn("Edited markdown with a structured table.", latest_draft)
        self.assertIn("## Structured tables", latest_draft)
        self.assertIn("| Count | 3 |", latest_draft)
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.root.parent), "ls-files", "--error-unmatch",
             latest_draft_path.relative_to(self.root.parent).as_posix()],
            capture_output=True, check=False).returncode, 0)
        self.assertIn(initial.name, (output / "index.md").read_text())

        current = project_text.editorial_store(self.root).current()
        publication = project_text.publish(
            self.root, snapshot_id=current.release_id, source_id=self.source["id"],
            reviewer="Synthetic admin", reason="Publish tracked markdown", checked=True)
        published = (output / literature_text.slug(self.source)
                     / f"published-{publication['release']}.md")
        self.assertIn("Edited markdown with a structured table.", published.read_text())
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.root.parent), "ls-files", "--error-unmatch",
             published.relative_to(self.root.parent).as_posix()],
            capture_output=True, check=False).returncode, 0)
        self.assertIn("project-markdown", str(published))

        disabled = client.post("/settings/git-markdown",
                               data={"token": token, "enabled": "false"},
                               base_url="http://localhost:8080")
        self.assertEqual(disabled.status_code, 200)
        self.assertTrue(initial.is_file())
        self.assertTrue(published.is_file())

    def test_visual_editor_assets_fallback_and_safe_markdown_reading(self):
        markdown = ("## Checked heading\n\n**Bold** and *italic* [source](https://example.org/paper)\n\n"
                    "- First item\n- Second item\n\n<script>alert('unsafe')</script>\n\n"
                    "| Measure | Value |\n|---|---:|\n| Example | 42 |\n\n"
                    "[unsafe](javascript:alert(1))\n\n![private](https://example.org/private.png)")
        current = project_text.editorial_store(self.root).current()
        project_edit.correct(self.root,
            snapshot_id=current.release_id, source_id=self.source["id"], unit_id=self.units[0]["id"],
            text=markdown, bibliography={k: v or "" for k, v in self.source["bibliography"].items()},
            source_url=self.source["url"], tables=[], reviewer="Manager", reason="Formatting correction", checked=False)
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        editor = client.get("/library/paper?id=doi%3A10.0000%2Ftext&mode=edit", base_url=base)
        for expected in (b'src="/paper-editor.js"', b'class="paper-edit-form"', b'id="visual-editor" hidden',
                         b'id="edit-text" name="text"', b'data-command="bold"', b'data-command="italic"',
                         b'data-command="bullets"', b'data-command="numbers"', b'data-command="link"',
                         b'data-command="undo"', b'data-command="redo"', b"Reading width", b"Full width"):
            self.assertIn(expected, editor.data)
        self.assertIn('style-src \'self\'', editor.headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'", editor.headers["Content-Security-Policy"])
        asset = client.get("/paper-editor.js", base_url=base)
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.mimetype, "text/javascript")
        self.assertIn(b"Page markers are missing", asset.data)
        licenses = client.get("/paper-editor-licenses.txt", base_url=base)
        self.assertEqual(licenses.status_code, 200)
        self.assertIn(b"@tiptap/core", licenses.data)
        html = client.get("/library/paper?id=doi%3A10.0000%2Ftext", base_url=base).text
        article = html.split('<article id="document-text"', 1)[1].split("</article>", 1)[0]
        for expected in ("<h2>Checked heading</h2>", "<strong>Bold</strong>", "<em>italic</em>",
                         'href="https://example.org/paper"', "<li>First item</li>", "&lt;script&gt;",
                         '<div class="table-scroll markdown-table-scroll" role="region" '
                         'aria-label="Scrollable table" tabindex="0"><table>',
                         "<th>Measure</th>", '<td style="text-align:right">42</td>', "</table></div>"):
            self.assertIn(expected, article)
        for unexpected in ("<script>", "<img", 'href="javascript:'):
            self.assertNotIn(unexpected, article)
        self.assertEqual(project_text.editorial_store(self.root).current().snapshots["core"]._bundle["units"][0]["text"],
                         markdown)
        self.assertEqual(project_library.store(self.root).call(
            "search_evidence", {"query": "Checked heading"})["result"]["items"], [])

    def test_paper_page_reads_full_draft_and_distinguishes_active_rag_content_after_correction(self):
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        url = "/library/paper?id=doi%3A10.0000%2Ftext"
        response = client.get(url, base_url=base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        html = response.get_data(as_text=True)
        for expected in ("Synthetic article", 'id="extracted-content"', 'id="document-text"',
                         "Drafttoken original article page 1", "Drafttoken original article page 2",
                         "Not available to RAG/MCP", ">Edit</a>", ">Publish</a>",
                         'href="' + url + '&amp;mode=edit'):
            self.assertIn(expected, html)
        self.assertNotIn("Read the active RAG/MCP version", html)
        self.assertNotIn("PDF page 1", html)
        self.assertNotIn("Structured tables", html)
        self.assertNotIn("All papers in Library", html)
        self.assertNotIn('href="#extracted-content"', html)
        self.assertNotIn('action="/library/paper/publication"', html)
        for key in ("content", "references", "citing", "pdf", "technical", "publication"):
            self.assertIn(f'&amp;tab={key}"', html)
        editor_url = unescape(re.search(r'href="([^"]+)">Edit</a>', html).group(1))
        editor = client.get(editor_url, base_url=base).get_data(as_text=True)
        self.assertIn("Edit mode", editor)
        self.assertIn("Whole document text / OCR", editor)
        self.assertIn('aria-label="Paper views"', editor)
        self.assertIn('src="/paper-editor.js"', editor)
        self.publish()
        current = project_text.editorial_store(self.root).current()
        project_edit.correct(self.root,
            snapshot_id=current.release_id, source_id=self.source["id"], unit_id=self.units[0]["id"],
            text=project_edit.document_draft(self.units).replace("Drafttoken", "Correctedtoken"),
            whole_document=True, bibliography={k: v or "" for k, v in self.source["bibliography"].items()},
            source_url=self.source["url"], tables=[], reviewer="Manager", reason="Compared with original",
            checked=False)
        html = client.get(url, base_url=base).get_data(as_text=True)
        text = html.split('id="document-text"', 1)[1].split("</article>", 1)[0]
        self.assertIn("Correctedtoken", text)
        self.assertNotIn("Drafttoken", text)
        self.assertIn("AI still uses the previously published version", html)
        active_url = unescape(re.search(r'href="([^"]+)">Open RAG/MCP', html).group(1))
        active = client.get(active_url, base_url=base)
        self.assertEqual(active.status_code, 200)
        self.assertIn(b"Drafttoken original article page 1", active.data)
        self.assertIn(b"Retrieval by PDF page", active.data)
        self.assertIn("<summary>Synthetic article · from block 1</summary>", active.text)
        self.assertNotIn("Sections: Synthetic article", active.text)
        self.assertNotIn(b"Correctedtoken", active.data)
        self.assertEqual(project_library.store(self.root).call(
            "search_evidence", {"query": "Correctedtoken"})["result"]["items"], [])
        self.publish()
        html = client.get(url, base_url=base).get_data(as_text=True)
        self.assertIn("latest text below is published for AI retrieval", html)
        self.assertTrue(project_library.store(self.root).call(
            "search_evidence", {"query": "Correctedtoken"})["result"]["items"])

    def test_warm_paper_tabs_reuse_snapshots_without_skipping_integrity_checks(self):
        from research_library import releases
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        url = "/library/paper?id=doi%3A10.0000%2Ftext"
        self.assertEqual(client.get(url, base_url=base).status_code, 200)
        original_current = collections.CollectionStore.current
        calls = []

        def tracked_current(registry):
            calls.append(registry.root)
            return original_current(registry)

        with patch.object(collections.CollectionStore, "current", tracked_current), \
                patch.object(releases, "validate", side_effect=AssertionError("Reloaded immutable evidence")):
            for tab in ("content", "references", "citing", "pdf", "technical", "publication"):
                calls.clear()
                response = client.get(url + "&tab=" + tab, base_url=base)
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(len(calls), 2)
                self.assertEqual(len(calls), len(set(calls)))
        # A cached snapshot must still detect changed backing bytes.
        registry = project_text.editorial_store(self.root)
        snapshot = registry.current().snapshots["core"]
        payload = registry.root / "releases" / snapshot.release_id / "records.json"
        self.assertTrue(payload.is_file())
        payload.write_bytes(payload.read_bytes() + b"\n")
        self.assertEqual(client.get(url, base_url=base).status_code, 503)

    def test_attachment_and_draft_edits_are_not_retrievable_until_explicit_publication(self):
        public = project_library.store(self.root)
        self.assertEqual(public.call("search_evidence", {"query": "Drafttoken"})["result"]["items"], [])
        self.assertEqual(public.current().snapshots["core"]._bundle["sources"], [])
        self.assertFalse(self.attached["retrievable"])
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "draft")
        initial = public.current().release_id
        args = {
            "snapshot_id": self.editorial.release_id, "source_id": self.source["id"], "unit_id": self.units[0]["id"],
            "text": project_edit.document_draft(self.units).replace("Drafttoken", "Cleantoken"),
            "whole_document": True, "bibliography": {k: v or "" for k, v in self.source["bibliography"].items()},
            "source_url": self.source["url"], "tables": [], "reviewer": "Draft admin", "reason": "Cleaned",
            "checked": False,
        }
        project_edit.correct(self.root, **args)
        self.assertEqual(public.current().release_id, initial)
        self.assertEqual(public.call("search_evidence", {"query": "Cleantoken"})["result"]["items"], [])
        result = self.publish()
        published = public.current()
        hits = published.call("search_evidence", {"query": "Cleantoken"})["result"]["items"]
        self.assertEqual(len(hits), 2)
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "published")
        self.assertEqual(published.snapshots["core"]._bundle["units"][0]["human_verification"], "reviewed")
        self.assertNotIn("not published for retrieval", published.snapshots["core"]._bundle["sources"][0]["provenance"])
        self.assertIn("publication audit", published.snapshots["core"]._bundle["sources"][0]["provenance"])
        self.assertEqual(project_text.editorial_store(self.root).current().snapshots["core"]._bundle["units"][0]["human_verification"], "not_reviewed")
        draft = project_text.editorial_store(self.root).current()
        args.update(snapshot_id=draft.release_id, text=args["text"].replace("Cleantoken", "Newdrafttoken"))
        project_edit.correct(self.root, **args)
        self.assertEqual(project_library.status(self.root)["papers"][0]["status"], "draft_revision")
        self.assertEqual(public.call("search_evidence", {"query": "Newdrafttoken"})["result"]["items"], [])
        self.assertEqual(len(public.call("search_evidence", {"query": "Cleantoken"})["result"]["items"]), 2)
        self.assertEqual(public.load(initial).snapshots["core"]._bundle["units"], [])
        self.assertEqual(public.load(result["snapshot"]).snapshots["core"]._bundle, published.snapshots["core"]._bundle)
        self.assertTrue(self.pdf.is_file())

    def test_library_actions_link_only_available_retained_pdfs(self):
        project_library.register(self.root, [{"doi": "10.0000/missing", "title": "Awaiting PDF article"}])
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        pdf_url = "/library/paper/pdf?id=doi%3A10.0000%2Ftext"
        html = client.get("/library", base_url=base).text
        self.assertIn(f'<td><a href="{pdf_url}">Open local PDF</a></td>', html)
        self.assertNotIn('/library/paper/pdf?id=doi%3A10.0000%2Fmissing', html)
        response = client.get(pdf_url, base_url=base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        response.close()
        (self.root / "papers" / (self.source["sha256"] + ".pdf")).unlink()
        self.assertNotIn("Open local PDF", client.get("/library", base_url=base).text)

    def test_draft_renders_only_images_from_its_checksum_bound_pdf_page(self):
        doi = "10.0000/figure"
        project_library.register(self.root, [{"doi": doi, "title": "Synthetic figure"}])
        path = self.root.parent / "figure.pdf"
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        pixmap.clear_with(120)
        image = pixmap.tobytes("jpeg")
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), "This synthetic page has enough native text for the figure fixture.")
            xref = page.insert_image(pymupdf.Rect(50, 80, 90, 120), stream=image)
            pdf.save(path)
        project_library.attach(self.root, doi, path, identity_confirmed=True)
        current = project_text.editorial_store(self.root).current()
        bundle = current.snapshots["core"]._bundle
        source = next(item for item in bundle["sources"] if item["doi"] == doi)
        unit = project_edit.document_units(bundle, source["id"])[0]
        query = {"id": "doi:" + doi, "page": 1, "sha256": source["sha256"], "xref": xref}
        url = "/library/paper/pdf-page?" + urlencode(query)
        project_edit.correct(
            self.root, snapshot_id=current.release_id, source_id=source["id"], unit_id=unit["id"],
            text=f"![Synthetic figure]({url})\n\n![Remote figure](https://example.org/figure.jpg)",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url="", tables=[], reviewer="Synthetic admin", reason="Add figure", checked=False)
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        paper = client.get("/library/paper?" + urlencode({"id": query["id"], "tab": "content"}), base_url=base)
        self.assertIn(f'<img src="{url.replace("&", "&amp;")}"'.encode(), paper.data)
        self.assertNotIn(b"https://example.org/figure.jpg", paper.data)
        asset = client.get(url, base_url=base)
        self.assertEqual(asset.mimetype, "image/jpeg")
        self.assertEqual(asset.data, image)
        for change in ({"sha256": "0" * 64}, {"xref": xref + 100}, {"page": 2}):
            rejected = client.get("/library/paper/pdf-page?" + urlencode(query | change), base_url=base)
            self.assertIn(rejected.status_code, (400, 404))

    def test_paper_tabs_show_inline_tables_and_private_checksum_bound_pdf(self):
        project_edit.correct(self.root,
            snapshot_id=self.editorial.release_id, source_id=self.source["id"], unit_id=self.units[0]["id"],
            text=self.units[0]["text"], bibliography={k: v or "" for k, v in self.source["bibliography"].items()},
            source_url=self.source["url"], tables=[{
                "id": "", "title": "Inline <table>", "headers": ["Measure", "Value"],
                "units": [None, None], "rows": [["Synthetic", 42]], "footnotes": ["Checked transcription"]}],
            reviewer="Manager", reason="Transcribed table", checked=False)
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        url = "/library/paper?id=doi%3A10.0000%2Ftext"
        html = client.get(url, base_url=base).text
        article = html.split('<article id="document-text"', 1)[1].split("</article>", 1)[0]
        self.assertLess(article.index("original article page 1"), article.index("Inline &lt;table&gt;"))
        self.assertLess(article.index("Inline &lt;table&gt;"), article.index("original article page 2"))
        self.assertIn("<td>42</td>", article)
        self.assertNotIn("PDF page 1", html)
        self.assertNotIn("Structured tables", html)
        self.assertNotIn("Correction history", html)
        technical = client.get(url + "&tab=technical", base_url=base).text
        self.assertIn("Correction history", technical)
        self.assertIn("Paper information", technical)
        self.assertNotIn('id="document-text"', technical)
        pdf_tab = client.get(url + "&tab=pdf", base_url=base)
        self.assertIn(b'class="paper-pdf-page"', pdf_tab.data)
        self.assertIn("img-src 'self'", pdf_tab.headers["Content-Security-Policy"])
        pdf_url = "/library/paper/pdf?id=doi%3A10.0000%2Ftext"
        with client.get(pdf_url, base_url=base) as pdf:
            self.assertEqual(pdf.status_code, 200)
            self.assertEqual(pdf.mimetype, "application/pdf")
            self.assertEqual(pdf.data, self.pdf.read_bytes())
            self.assertIn("inline", pdf.headers["Content-Disposition"])
            self.assertIn("frame-ancestors 'none'", pdf.headers["Content-Security-Policy"])
            self.assertEqual(pdf.headers["Cache-Control"], "no-store")
        with client.get(pdf_url, base_url=base, headers={"Range": "bytes=0-9"}) as partial:
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.data, self.pdf.read_bytes()[:10])
        image_url = "/library/paper/pdf-page?id=doi%3A10.0000%2Ftext&page=1"
        image = client.get(image_url, base_url=base)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.mimetype, "image/png")
        self.assertTrue(image.data.startswith(b"\x89PNG"))
        for page_number in ("0", "-1", "3", "no"):
            self.assertEqual(client.get(image_url.rsplit("=", 1)[0] + "=" + page_number,
                                        base_url=base).status_code, 400)
        self.assertEqual(client.get(pdf_url, base_url="http://untrusted:8080").status_code, 421)
        self.assertEqual(client.get(pdf_url, base_url=base, headers={"Origin": "https://example.com"}).status_code, 403)
        self.assertEqual(client.get(pdf_url + "&path=/etc/passwd", base_url=base).status_code, 400)
        stored_pdf = self.root / "papers" / (self.source["sha256"] + ".pdf")
        original = stored_pdf.read_bytes()
        stored_pdf.write_bytes(original + b"changed")
        self.assertEqual(client.get(pdf_url, base_url=base).status_code, 503)
        stored_pdf.unlink()
        stored_pdf.symlink_to(self.pdf)
        self.assertNotEqual(client.get(pdf_url, base_url=base).status_code, 200)
        stored_pdf.unlink()
        self.assertIn(b"No local PDF", client.get(url + "&tab=pdf", base_url=base).data)
        self.assertEqual(client.get(pdf_url, base_url=base).status_code, 404)

    def test_legacy_editor_redirects_to_shared_paper_page_and_preserves_options(self):
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        legacy = "/library/edit?doi=10.0000%2Ftext"
        canonical = "/library/paper?id=doi%3A10.0000%2Ftext"
        before = project_text.editorial_store(self.root).current().release_id
        response = client.get(legacy, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, canonical)
        self.assertEqual(client.get(legacy, base_url=base, follow_redirects=True).data,
                         client.get(canonical, base_url=base).data)
        unit_id = self.units[-1]["id"]
        encoded_unit = quote(unit_id, safe="")
        response = client.get(legacy + "&unit_id=" + unit_id, base_url=base)
        self.assertEqual(response.location, canonical + "&unit_id=" + encoded_unit + "#" + encoded_unit)
        for options, expected in (
                ("mode=edit", "&mode=edit"),
                ("mode=edit&scope=page&unit_id=" + unit_id, "&unit_id=" + encoded_unit + "&mode=edit&scope=page"),
                ("download=text", "&download=text")):
            with self.subTest(options=options):
                response = client.get(legacy + "&" + options, base_url=base)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.location, canonical + expected)
                redirected = client.get(legacy + "&" + options, base_url=base, follow_redirects=True)
                self.assertEqual(redirected.status_code, 200)
                self.assertEqual(redirected.headers["Cache-Control"], "no-store")
                if options == "download=text":
                    self.assertEqual(redirected.mimetype, "text/plain")
                    self.assertEqual(redirected.text, project_edit.document_draft(self.units))
                else:
                    self.assertIn(b'aria-label="Paper views"', redirected.data)
                    self.assertIn(b'id="edit-text"', redirected.data)
                    self.assertIn(b'src="/paper-editor.js"', redirected.data)
        for options in ("mode=invalid", "scope=invalid", "download=invalid", "unit_id=missing", "unknown=yes"):
            with self.subTest(invalid=options):
                self.assertEqual(client.get(legacy + "&" + options, base_url=base,
                                            follow_redirects=True).status_code, 400)
        self.assertEqual(client.get(canonical + "&tab=technical&mode=edit", base_url=base).status_code, 400)
        self.assertEqual(project_text.editorial_store(self.root).current().release_id, before)

    def test_tab_editor_saves_and_publishes_back_to_paper_content(self):
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        url = "/library/paper?id=doi%3A10.0000%2Ftext"
        html = client.get(url, base_url=base).text
        editor_url = unescape(re.search(r'href="([^"]+)">Edit</a>', html).group(1))
        editor = client.get(editor_url, base_url=base).text
        form = {name: unescape(re.search(fr'name="{name}" value="([^"]*)"', editor).group(1))
                for name in ("token", "doi", "unit_id", "source_id", "snapshot_id", "scope")}
        form.update({k: v or "" for k, v in self.source["bibliography"].items()})
        form.update(text=project_edit.document_draft(self.units).replace("Drafttoken", "Tabedited"),
                    source_url=self.source["url"], tables="[]", reviewer="Manager", reason="Compared with original")
        invalid = client.post("/library/edit", data=form | {"tables": "invalid"}, base_url=base)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b'aria-label="Paper views"', invalid.data)
        self.assertIn(b'src="/paper-editor.js"', invalid.data)
        self.assertIn(b"Tabedited", invalid.data)
        saved = client.post("/library/edit", data=form, base_url=base)
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(saved.location, url)
        html = client.get(saved.location, base_url=base).text
        self.assertIn("Tabedited", html)
        html = client.get(url + "&tab=publication", base_url=base).text
        publish = {name: unescape(re.search(fr'name="{name}" value="([^"]*)"', html).group(1))
                   for name in ("token", "id", "snapshot_id")}
        publish.update(action="publish", reviewer="Manager", reason="Checked against PDF", checked="yes")
        result = client.post("/library/paper/publication", data=publish, base_url=base)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.location, url + "&tab=publication")
        html = client.get(result.location, base_url=base).text
        self.assertIn(">Published</a>", html)
        self.assertIn("<dt>Published by</dt><dd>Manager</dd>", html)
        self.assertIn("<dt>Published at (UTC)</dt>", html)
        self.assertIn("Checked against PDF", html)
        self.assertIn(b"published for AI retrieval", client.get(url, base_url=base).data)
        withdraw = {name: unescape(re.search(fr'name="{name}" value="([^"]*)"', html).group(1))
                    for name in ("token", "id", "snapshot_id")}
        withdraw.update(action="unpublish", reviewer="Manager", reason="Needs review", checked="yes")
        self.assertEqual(client.post("/library/paper/publication", data={**withdraw, "token": "wrong"},
                                     base_url=base).status_code, 403)
        denied = client.post("/library/paper/publication", data={**withdraw, "checked": ""},
                             base_url=base)
        self.assertEqual(denied.status_code, 400)
        self.assertIn(b"Confirm removal", denied.data)
        self.assertIn(b'aria-current="page">Published</a>', denied.data)
        removed = client.post("/library/paper/publication", data=withdraw, base_url=base)
        self.assertEqual(removed.status_code, 303)
        html = client.get(removed.location, base_url=base).text
        self.assertIn('aria-current="page">Publish</a>', html)
        self.assertNotIn('<button class="danger">Unpublish</button>', html)
        self.assertIn("Tabedited", client.get(url, base_url=base).text)
        self.assertEqual(project_library.store(self.root).call(
            "search_evidence", {"query": "Tabedited"})["result"]["items"], [])

    def test_rag_suggestions_follow_sections_not_page_boundaries(self):
        pages = [{**self.units[0], "text": "## Results\n\nThe result continues"},
                 {**self.units[1], "text": "on the next page.\n## References\n\nA reference."}]
        blocks = passage_context.document_blocks(pages)
        self.assertEqual(project_chunks.suggestions(blocks), [[1, 2], [3, 3]])
        self.assertEqual([block[3][0] for block in blocks], [[["Results"]], [["Results"]], [["References"]]])

    def test_rag_draft_preview_activation_and_republication(self):
        source_id = self.source["id"]
        first_page = self.units[0]["id"]

        def correct(text):
            project_edit.correct(self.root, snapshot_id=project_text.editorial_store(self.root).current().release_id,
                source_id=source_id, unit_id=first_page, text=text,
                bibliography={key: value or "" for key, value in self.source["bibliography"].items()},
                source_url=self.source["url"], tables=[], reviewer="Manager", reason="Compared with original",
                checked=False)

        correct("## Results\n\nNeedle first paragraph.\n\nShared bridge.\n\n**Figure 1** Needle final paragraph.")
        self.publish()
        registry = project_library.store(self.root)
        before = registry.current()
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        url = "/library/paper?id=doi%3A10.0000%2Ftext&tab=rag"
        html = client.get(url, base_url=base).text
        self.assertLess(html.index(">Published</a>"), html.index(">RAG/MCP</a>"))
        self.assertIn("Retrieval by PDF page", html)
        self.assertIn("Start with the suggested ranges", html)
        workflow = ["Live retrieval", "1. Review searchable passages", "2. Test a search", "3. Save or apply",
                    "Reference: numbered source text", "<details><summary>See what live search can return"]
        positions = [html.index(marker) for marker in workflow]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('value="activate">Apply to live search</button>', html)
        self.assertIn('<script src="/rag-editor.js" defer></script>', html)
        self.assertIn('<details id="rag-advanced" open>', html)
        self.assertIn('data-rag-block="1"', html)
        self.assertEqual(client.get("/rag-editor.js", base_url=base).mimetype, "text/javascript")
        self.assertIn("Sections: Results", html)
        self.assertIn("DOI 10.0000/text", html)
        form = {name: unescape(re.search(fr'name="{name}" value="([^"]*)"', html).group(1))
                for name in ("token", "id", "snapshot_id")}
        label = "Persistent rare outcome (uncommon result): frequency and counts"
        form.update({"ranges": "1-2, 2-4", "labels": f"2: {label}", "query": "needle"})
        expected_plan = {first_page: {"ranges": [[1, 2], [2, 4]], "labels": {"2": label}}}
        endpoint = "/library/paper/rag"
        self.assertIn("Test query (required for preview only)", html)
        for blank in ("", " \t "):
            rejected = client.post(endpoint, data=form | {"action": "preview", "query": blank}, base_url=base)
            self.assertEqual(rejected.status_code, 400)
            self.assertIn("Enter a test query before previewing draft search.", rejected.text)
            self.assertIn(form["ranges"], rejected.text)
            self.assertIn(form["labels"], rejected.text)
            self.assertNotIn("minLength", rejected.text)
        self.assertEqual(registry.current().release_id, before.release_id)
        rejected = client.post(endpoint, data=form | {"action": "preview", "ranges": "1, 3", "labels": ""}, base_url=base)
        self.assertEqual(rejected.status_code, 400)
        for invalid in ("3: not a chunk start", "2: repeated\n2: repeated", "2: " + "x" * 201):
            rejected = client.post(endpoint, data=form | {"action": "preview", "labels": invalid}, base_url=base)
            self.assertEqual(rejected.status_code, 400)
        preview = client.post(endpoint, data=form | {"action": "preview"}, base_url=base)
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Draft search preview", preview.data)
        self.assertIn("Check that the top results contain the evidence", preview.text)
        self.assertLess(preview.text.index("3. Save or apply"), preview.text.index("Draft search preview"))
        self.assertIn(b"Figure 1", preview.data)
        self.assertIn(b"PDF pages 1, 2", preview.data)
        self.assertEqual(client.post(endpoint, data=form | {"action": "save", "query": ""}, base_url=base).status_code, 303)
        self.assertEqual(registry.current().release_id, before.release_id)
        saved = client.get(url, base_url=base).text
        self.assertIn(f"2: {label}", saved)
        self.assertIn("You have a saved draft that is not live.", saved)
        frequency = {"query": "How common was rare outcome?"}
        self.assertFalse(registry.call("search_evidence", frequency)["result"]["items"])
        labeled_preview = client.post(endpoint, data=form | frequency | {"action": "preview"}, base_url=base)
        self.assertEqual(labeled_preview.status_code, 200)
        self.assertIn(f"Extra search terms: {label}", labeled_preview.text)
        check_form = form | frequency | {
            "action": "add-check", "expected_page": "1", "expected_excerpt": "Shared   bridge.",
        }
        rejected = client.post(endpoint, data=check_form | {"expected_page": "2"}, base_url=base)
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("must appear on the selected published PDF page", rejected.text)
        saved_check = client.post(endpoint, data=check_form, base_url=base)
        self.assertEqual(saved_check.status_code, 200)
        self.assertIn("Search check saved.", saved_check.text)
        comparison = client.post(endpoint, data=form | {"action": "run-checks"}, base_url=base)
        self.assertEqual(comparison.status_code, 200)
        self.assertIn("<td>Not in top 5</td><td>Rank 1</td>", comparison.text)
        self.assertEqual(registry.current().release_id, before.release_id)
        self.assertEqual(client.post(endpoint, data=form | {"action": "activate", "query": ""}, base_url=base).status_code, 303)
        active = registry.current()
        self.assertNotEqual(active.release_id, before.release_id)
        live = client.get(url, base_url=base).text
        self.assertIn("Search and MCP use custom chunks", live)
        self.assertIn("No action needed if live retrieval works for you", live)
        self.assertNotIn("You have a saved draft that is not live.", live)
        query = {"query": "needle"}
        hits = registry.call("search_evidence", query)["result"]["items"]
        self.assertEqual(len(hits), 2)
        for hit in hits:
            self.assertEqual(hit["citation"]["locator"], {"kind": "pdf", "pdf_page": 1})
            context = registry.call("get_source_context", {
                "unit_id": hit["id"], "snapshot_id": active.release_id,
            })["result"]
            self.assertIn("Shared bridge", context["text"])
            self.assertEqual(context["text"].count("Needle"), 1)
            metadata = hit["passage_context"]
            window = registry.call("get_source_context", {"unit_id": hit["id"], "snapshot_id": active.release_id,
                                                           "max_characters": 10})["result"]
            tail = registry.call("get_source_context", {"unit_id": hit["id"], "snapshot_id": active.release_id,
                                                         "offset": window["next_offset"]})["result"]
            self.assertEqual(window["text"] + tail["text"], context["text"])
            self.assertIsNone(tail["next_offset"])
            self.assertEqual(context["passage_context"], metadata)
            self.assertEqual(metadata["paper_title"], "Synthetic article")
            self.assertEqual(metadata["section_paths"], [["Results"]])
            final = "final paragraph" in context["text"]
            self.assertEqual(metadata["elements"], (
                [{"kind": "figure", "label": "Figure 1", "role": "label"}] if final else []))
            self.assertEqual(metadata["block_range"], {"first": 2, "last": 4} if final else {"first": 1, "last": 2})
            self.assertEqual(metadata["block_scope"], "document")
            self.assertEqual([span["citation"]["locator"]["pdf_page"] for span in metadata["source_spans"]],
                             [1, 2] if final else [1])
            pages = {unit["id"]: unit["text"] for unit in before.snapshots["core"].document_units(source_id)}
            self.assertEqual(context["text"], "\n".join(
                pages[span["unit_id"]][span["start"]:span["end"]] for span in metadata["source_spans"]))
        self.assertEqual(active.snapshots["core"].document_units(source_id),
                         before.snapshots["core"].document_units(source_id))
        self.assertEqual(before.snapshots["core"].call("search_evidence", query)["result"]["items"][0]["id"],
                         first_page)
        filtered = active._search(["core"], {"query": "needle", "limit": 10, "offset": 0, "mode": "plain"},
                                  papers={"doi:10.0000/text"})[0]["items"]
        self.assertEqual({hit["id"] for hit in filtered}, {hit["id"] for hit in hits})
        self.assertEqual({hit["id"]: hit["passage_context"] for hit in filtered},
                         {hit["id"]: hit["passage_context"] for hit in hits})
        labeled = registry.call("search_evidence", frequency)["result"]["items"]
        self.assertEqual(len(labeled), 1)
        paraphrase = {"query": "What proportion had an uncommon result?"}
        self.assertFalse(registry.call("search_evidence", paraphrase)["result"]["items"])
        paraphrase = {"query": "What proportion had uncommon result?"}
        self.assertEqual(registry.call("search_evidence", paraphrase)["result"]["items"], labeled)
        persistence = {"query": "How often did uncommon result persist?"}
        self.assertEqual(registry.call("search_evidence", persistence)["result"]["items"], labeled)
        self.assertFalse(registry.call("search_evidence", paraphrase | {
            "mode": "fts5", "query": '"What proportion had uncommon result"',
        })["result"]["items"])
        hit, = labeled
        self.assertEqual(hit["passage_context"]["editorial_label"], label)
        self.assertEqual(hit["passage_context"]["block_range"], {"first": 2, "last": 4})
        self.assertNotIn(label, hit["snippet"])
        self.assertIn("Needle final paragraph", hit["snippet"])
        self.assertEqual(registry.call("search_evidence", frequency | {"paper": "10.0000/text"})["result"]["items"],
                         labeled)
        self.assertFalse(registry.call("search_evidence", frequency | {"paper": "10.0000/other"})["result"]["items"])
        self.assertFalse(registry.call("search_evidence", {"query": "rare AND outcome", "mode": "fts5"})["result"]["items"])
        for scope in ({"doi:10.0000/text"}, {"doi:10.0000/other"}):
            matches, _ = active._search(["core"], paraphrase | {"limit": 1, "offset": 0, "mode": "plain"}, papers=scope)
            self.assertEqual(matches["items"], labeled if "doi:10.0000/text" in scope else [])
        broad = {"query": "needle", "limit": 1}
        first = registry.call("search_evidence", broad)["result"]
        self.assertEqual(first["next_offset"], 1)
        last = registry.call("search_evidence", broad | {
            "offset": first["next_offset"], "snapshot_id": active.release_id,
        })["result"]
        self.assertEqual(first["items"] + last["items"], hits)
        self.assertIsNone(last["next_offset"])
        continuing = registry.call("search_evidence", {"query": "Drafttoken needle"})["result"]["items"][0]
        self.assertEqual(len(continuing["passage_context"]["source_spans"]), 2)
        self.assertEqual(continuing["passage_context"]["section_paths"], [["Results"]])
        project_library.register(self.root, [{"doi": "10.0000/other", "title": "Another synthetic paper"}])
        other_pdf = self.root.parent / "other.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((50, 50), "This unrelated synthetic paper has sufficient native text to extract.")
            pdf.save(other_pdf)
        project_library.attach(self.root, "10.0000/other", other_pdf, identity_confirmed=True)
        editorial = project_text.editorial_store(self.root).current()
        other = next(s for s in editorial.snapshots["core"]._bundle["sources"] if s["doi"] == "10.0000/other")
        self.publish(source_id=other["id"])
        self.assertEqual(registry.current().snapshots["core"].chunk_plans, expected_plan)
        current = registry.current()
        project_text.unpublish(self.root, snapshot_id=current.release_id, source_id=other["id"],
                               reviewer="Manager", reason="Synthetic withdrawal", checked=True)
        self.assertEqual(registry.current().snapshots["core"].chunk_plans, expected_plan)
        correct("Newneedle revised paragraph.\n\nAnother reported result.")
        self.publish()
        self.assertEqual(registry.current().snapshots["core"].chunk_plans, {})
        self.assertEqual(registry.call("search_evidence", {"query": "Newneedle"})["result"]["items"][0]["id"],
                         first_page)
        html = client.get(url, base_url=base).text
        self.assertIn("Previous active chunks are stale", html)
        self.assertIn("saved draft is stale", html)
        self.assertIn("Review the ranges for this publication", html)
        self.assertIn("Extra search terms: Needs review", html)
        self.assertIn(label, html)
        self.assertIn("Shared bridge.", html)
        current_form = {name: unescape(re.search(fr'name="{name}" value="([^"]*)"', html).group(1))
                        for name in ("token", "id", "snapshot_id")}
        current_form["ranges"] = unescape(re.search(r'name="ranges"[^>]*>([^<]*)', html).group(1))
        stale = client.post(endpoint, data=current_form | {"action": "run-checks"}, base_url=base)
        self.assertEqual(stale.status_code, 200)
        self.assertIn("<td>Review expected excerpt</td><td>Review expected excerpt</td>", stale.text)
        self.assertEqual(client.post(endpoint, data=current_form | {"action": "save"}, base_url=base).status_code, 303)
        saved = client.get(url, base_url=base).text
        self.assertIn("Extra search terms: Needs review", saved)
        first = current_form["ranges"].split(",")[0].split("-")[0].strip()
        self.assertEqual(client.post(endpoint, data=current_form | {
            "action": "save", "labels": f"{first}: {label}",
        }, base_url=base).status_code, 303)
        self.assertNotIn("Extra search terms: Needs review", client.get(url, base_url=base).text)
        check_id = re.search(r'value="remove-check:([a-f0-9]+)"', saved).group(1)
        removed = client.post(endpoint, data=current_form | {"action": f"remove-check:{check_id}"}, base_url=base)
        self.assertEqual(removed.status_code, 200)
        self.assertIn("Search check removed.", removed.text)

    def test_search_terms_follow_a_unique_passage_not_its_old_number(self):
        page = {"text": "Other\nSame passage", "citation": {"locator": {"pdf_page": 2}}}
        before = [(page, 6, 18, None)]
        after = [(page, 0, 5, None), *before]
        old = project_chunks.passage_anchors(before, [[1, 1]])["1"] | {"terms": "synonym"}
        self.assertEqual(project_chunks.suggest_terms(after, [[1, 1], [2, 2]], [old]),
                         ({"2": "synonym"}, []))
        self.assertEqual(project_chunks.suggest_terms(after, [[1, 2]], [old]), ({}, [old]))

    def test_unpublish_preserves_editorial_history_other_sources_and_old_snapshots(self):
        project_library.register(self.root, [{"doi": "10.0000/other", "title": "Other source"}])
        other_pdf = self.root.parent / "other.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((50, 50), "Other synthetic publication retained when first paper is unpublished.")
            pdf.save(other_pdf)
        project_library.attach(self.root, "10.0000/other", other_pdf, identity_confirmed=True)
        current = project_text.editorial_store(self.root).current()
        other = next(source for source in current.snapshots["core"]._bundle["sources"]
                     if source["doi"] == "10.0000/other")
        project_text.publish(self.root, snapshot_id=current.release_id, source_id=other["id"],
                             reviewer="Other manager", reason="Checked", checked=True)
        self.publish()
        registry = project_library.store(self.root)
        before = registry.current()
        editorial_id = project_text.editorial_store(self.root).current().release_id
        args = {"snapshot_id": before.release_id, "source_id": self.source["id"],
                "reviewer": "Withdraw manager", "reason": "Needs correction", "checked": True}
        for changes in ({"checked": False}, {"reviewer": ""}, {"reason": ""},
                        {"snapshot_id": "0"*64}, {"source_id": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                project_text.unpublish(self.root, **(args | changes))
        with patch.object(collections.CollectionStore, "publish", side_effect=ValueError("Staging failure")):
            with self.assertRaisesRegex(ValueError, "Staging failure"):
                project_text.unpublish(self.root, **args)
        self.assertEqual(registry.current().release_id, before.release_id)
        self.assertFalse(list((self.root / "runs").glob("correction-unpublication-*")))
        project_text.unpublish(self.root, **args)
        after = registry.current()
        self.assertEqual([source["id"] for source in after.snapshots["core"]._bundle["sources"]], [other["id"]])
        self.assertEqual(project_text.editorial_store(self.root).current().release_id, editorial_id)
        self.assertEqual(registry.load(before.release_id).snapshots["core"]._bundle,
                         before.snapshots["core"]._bundle)
        self.assertIsNone(project_text.publication(self.root, self.source["id"]))
        self.assertIsNotNone(project_text.publication(self.root, other["id"]))
        self.assertEqual(project_library.status(self.root)["papers"][1]["status"], "draft")
        history = project_edit.history(self.root, self.source["id"])
        self.assertEqual(history[0]["scope"], "unpublication")
        self.assertEqual(history[0]["reviewer"], "Withdraw manager")
        self.assertTrue(any(event["scope"] == "publication" for event in history))
        self.assertTrue((self.root / "papers" / (self.source["sha256"] + ".pdf")).is_file())
        self.publish()
        self.assertIsNotNone(project_text.publication(self.root, self.source["id"]))

    def test_published_tab_uses_active_audit_even_with_newer_draft(self):
        self.publish()
        event = project_text.publication(self.root, self.source["id"])["event"]
        current = project_text.editorial_store(self.root).current()
        project_edit.correct(self.root,
            snapshot_id=current.release_id, source_id=self.source["id"], unit_id=self.units[0]["id"],
            text=self.units[0]["text"] + " New draft", bibliography={k: v or "" for k, v in self.source["bibliography"].items()},
            source_url=self.source["url"], tables=[], reviewer="Draft editor", reason="Incomplete change", checked=False)
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        html = client.get("/library/paper?id=doi%3A10.0000%2Ftext&tab=publication",
                          base_url="http://localhost:8080").text
        self.assertIn('aria-current="page">Published</a>', html)
        self.assertIn("<dt>Published by</dt><dd>Synthetic admin</dd>", html)
        self.assertIn(event["at"], html)
        self.assertIn("newer unpublished draft", html)
        self.assertIn('name="action" value="publish"', html)
        self.assertIn('name="action" value="unpublish"', html)

    def test_publication_validation_and_failed_activation_leave_retrieval_unchanged(self):
        before = project_library.store(self.root).current().release_id
        for changes in [{"checked": False}, {"reviewer": ""}, {"reason": ""}, {"snapshot_id": "0"*64},
                        {"source_id": "unknown"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.publish(**changes)
        with patch.object(collections.CollectionStore, "publish", side_effect=ValueError("Staging failure")):
            with self.assertRaisesRegex(ValueError, "Staging failure"):
                self.publish()
        self.assertEqual(project_library.store(self.root).current().release_id, before)
        self.assertFalse(list((self.root / "runs").glob("correction-publication-*")))

    def test_admin_can_import_utf8_draft_then_publish_through_web(self):
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/library/paper?id=doi%3A10.0000%2Ftext&mode=edit", base_url=base).get_data(as_text=True)
        form = {name: re.search(fr'name="{name}" value="([^"]*)"', html).group(1)
                for name in ("token", "doi", "unit_id", "source_id", "snapshot_id", "scope")}
        form.update({k: v or "" for k, v in self.source["bibliography"].items()})
        form.update(source_url=self.source["url"], reviewer="Web admin", reason="Imported cleanup", tables="[]")
        edited = project_edit.document_draft(self.units).replace("Drafttoken", "Importedtoken")
        bad = client.post("/library/edit", data=form | {"clear_text_file": (io.BytesIO(b"\xff"), "draft.txt")},
                          base_url=base)
        self.assertEqual(bad.status_code, 400)
        response = client.post("/library/edit", data=form | {"clear_text_file": (io.BytesIO(edited.encode()), "draft.md")},
                               base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(project_library.store(self.root).call("search_evidence", {"query": "Importedtoken"})["result"]["items"], [])
        html = client.get(response.location + "&tab=publication", base_url=base).get_data(as_text=True)
        form = {name: re.search(fr'name="{name}" value="([^"]*)"', html).group(1)
                for name in ("token", "id", "snapshot_id")}
        form.update(action="publish", reviewer="Web admin", reason="Checked against PDF")
        self.assertEqual(client.post("/library/paper/publication", data=form, base_url=base).status_code, 400)
        self.assertEqual(client.post("/library/paper/publication", data=form | {"checked": "yes"}, base_url=base).status_code, 303)
        self.assertEqual(len(project_library.store(self.root).call("search_evidence", {"query": "Importedtoken"})["result"]["items"]), 2)

    def test_migration_withdraws_active_text_but_preserves_original_snapshot_and_pdf(self):
        self.publish()
        previous = project_library.store(self.root).current()
        result = project_text.migrate(self.root, withdraw=True)
        self.assertEqual(result["previous_snapshot"], previous.release_id)
        self.assertEqual(project_library.store(self.root).current().snapshots["core"]._bundle["units"], [])
        self.assertEqual(project_library.store(self.root).load(previous.release_id).snapshots["core"]._bundle,
                         previous.snapshots["core"]._bundle)
        self.assertEqual(project_text.editorial_store(self.root).current().snapshots["core"]._bundle, self.bundle)
        self.assertTrue(self.pdf.is_file())

    def test_purge_removes_unpublished_editorial_content(self):
        project_purge.purge(self.root, "10.0000/text", self.root.parent / "no-shared-pdfs")
        self.assertEqual(project_text.editorial_store(self.root).current().snapshots["core"]._bundle["sources"], [])
        self.assertEqual(project_library.store(self.root).current().snapshots["core"]._bundle["sources"], [])

    def test_attachment_without_extraction_accepts_manual_clear_text(self):
        project_library.register(self.root, [{"doi": "10.0000/manual", "title": "Manual article"}])
        path = self.root.parent / "manual.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.new_page()
            pdf.save(path)
        with patch.object(project_library.literature_ingest, "extract_pdf", side_effect=AssertionError("Extraction called")):
            result = project_library.attach(self.root, "10.0000/manual", path,
                                             identity_confirmed=True, extract=False)
        self.assertEqual(result["extraction"], "not_extracted")
        current = project_text.editorial_store(self.root).current()
        source = next(s for s in current.snapshots["core"]._bundle["sources"] if s["doi"] == "10.0000/manual")
        units = project_edit.document_units(current.snapshots["core"]._bundle, source["id"])
        self.assertEqual([u["text"] for u in units], ["", ""])
        with self.assertRaisesRegex(ValueError, "Provide clear text"):
            project_text.publish(self.root, snapshot_id=current.release_id, source_id=source["id"],
                                 reviewer="Admin", reason="No text", checked=True)
        units[0]["text"] = "Manually supplied transcription."
        project_edit.correct(self.root, snapshot_id=current.release_id, source_id=source["id"],
            unit_id=units[0]["id"], text=project_edit.document_draft(units), whole_document=True,
            bibliography={k: v or "" for k, v in source["bibliography"].items()},
            source_url=source["url"], tables=[], reviewer="Admin", reason="Manual draft", checked=False)
        self.assertEqual(project_library.store(self.root).call("search_evidence", {"query": "Manually"})["result"]["items"], [])
        project_text.publish(self.root, snapshot_id=project_text.editorial_store(self.root).current().release_id,
                             source_id=source["id"], reviewer="Admin", reason="Checked including blank page", checked=True)
        self.assertEqual(len(project_library.store(self.root).call("search_evidence", {"query": "Manually"})["result"]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
