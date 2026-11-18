import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from html import unescape

import pymupdf

from research_library import collections, web_server
from research_library.build import project_edit, project_library, project_purge, project_text


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

    def test_visual_editor_assets_fallback_and_safe_markdown_reading(self):
        markdown = ("## Checked heading\n\n**Bold** and *italic* [source](https://example.org/paper)\n\n"
                    "- First item\n- Second item\n\n<script>alert('unsafe')</script>\n\n"
                    "[unsafe](javascript:alert(1))\n\n![private](https://example.org/private.png)")
        current = project_text.editorial_store(self.root).current()
        project_edit.correct(self.root,
            snapshot_id=current.release_id, source_id=self.source["id"], unit_id=self.units[0]["id"],
            text=markdown, bibliography={k: v or "" for k, v in self.source["bibliography"].items()},
            source_url=self.source["url"], tables=[], reviewer="Manager", reason="Formatting correction", checked=False)
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        editor = client.get("/library/edit?doi=10.0000/text&mode=edit", base_url=base)
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
                         'href="https://example.org/paper"', "<li>First item</li>", "&lt;script&gt;"):
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
                         'href="/library/edit?'):
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
        active_url = unescape(re.search(r'href="([^"]+)">Read the active RAG/MCP version', html).group(1))
        active = client.get(active_url, base_url=base)
        self.assertEqual(active.status_code, 200)
        self.assertIn(b"Drafttoken original article page 1", active.data)
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

    def test_tab_editor_saves_and_publishes_back_to_paper_content(self):
        client = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            profiles={"private_context"}, allowed_collections={"core"}, default_collections=["core"]).test_client()
        base = "http://localhost:8080"
        url = "/library/paper?id=doi%3A10.0000%2Ftext"
        html = client.get(url, base_url=base).text
        editor_url = unescape(re.search(r'href="([^"]+)">Edit</a>', html).group(1))
        editor = client.get(editor_url, base_url=base).text
        form = {name: unescape(re.search(fr'name="{name}" value="([^"]*)"', editor).group(1))
                for name in ("token", "doi", "unit_id", "source_id", "snapshot_id", "scope", "return_to")}
        form.update({k: v or "" for k, v in self.source["bibliography"].items()})
        form.update(text=project_edit.document_draft(self.units).replace("Drafttoken", "Tabedited"),
                    source_url=self.source["url"], tables="[]", reviewer="Manager", reason="Compared with original")
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
        html = client.get("/library/edit?doi=10.0000/text&mode=edit", base_url=base).get_data(as_text=True)
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
        html = client.get(response.location, base_url=base).get_data(as_text=True)
        form = {name: re.search(fr'name="{name}" value="([^"]*)"', html).group(1)
                for name in ("token", "doi", "unit_id", "source_id", "snapshot_id")}
        form.update(action="publish", reviewer="Web admin", reason="Checked against PDF")
        self.assertEqual(client.post("/library/edit", data=form, base_url=base).status_code, 400)
        self.assertEqual(client.post("/library/edit", data=form | {"checked": "yes"}, base_url=base).status_code, 303)
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
