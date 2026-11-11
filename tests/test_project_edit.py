from copy import deepcopy
from collections import Counter
from html import unescape
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from jsonschema import ValidationError
import pymupdf

from research_library import collections, quality, releases, web_server
from research_library.build import project_edit, project_index, project_library, project_purge


class ProjectEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.pdfs = self.root.parent / "pdfs"
        self.pdfs.mkdir()
        project_library.initialize(self.root, "Correction test", "Authorized private synthetic research")
        project_library.register(self.root, [{"doi": "10.0000/example", "title": "Original title"},
                                             {"doi": "10.0000/keep", "title": "Keep title"}])
        for key in ["example", "keep"]:
            path = self.pdfs / (key + ".pdf")
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((50, 50),
                    f"Original {key} evidence contains a transcripton mistake and plenty of searchable native text.")
                pdf.new_page().insert_text((50, 50), f"Second {key} page must retain its original text without change.")
                pdf.save(path)
            project_library.attach(self.root, "10.0000/" + key, path, identity_confirmed=True)
        self.current = project_library.store(self.root).current()
        self.original = self.current.snapshots["core"]
        self.source = next(source for source in self.original._bundle["sources"] if source["doi"] == "10.0000/example")
        self.unit = next(unit for unit in self.original._bundle["units"]
                         if unit["citation"]["source_id"] == self.source["id"])
        self.args = {
            "snapshot_id": self.current.release_id, "source_id": self.source["id"], "unit_id": self.unit["id"],
            "text": self.unit["text"].replace("transcripton", "correctedtranscription"),
            "bibliography": {**self.source["bibliography"], "title": "Corrected title", "year": "2021",
                             "acquired_from": "https://example.org/original.pdf",
                             "acquisition_method": "Authorized repository download",
                             "acquired_at": "2026-11-10T00:00:00+00:00"},
            "source_url": "https://doi.org/10.0000/example",
            "tables": [{"id": "", "title": "Corrected table", "headers": ["Measure", "Count"],
                        "units": [None, "participants"], "rows": [["Example", 42]], "footnotes": ["Operator transcription"]}],
            "reviewer": "Synthetic reviewer", "reason": "Checked original page text and table", "checked": True,
        }

    def test_web_quality_inventory_review_problem_and_safety_guards(self):
        quality_root = self.root / "quality"
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdfs, quality_root=quality_root, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        finding = next(item for item in quality.report(self.original, quality_root)["findings"]
                       if item["source_id"] == self.source["id"])
        url = "/quality?" + urlencode({"collection": "core", "finding": finding["id"]})
        html = client.get(url, base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        form = {"token": token, "snapshot_id": self.current.release_id,
                "finding_id": finding["id"], "action": "inventory"}
        self.assertNotIn("research_library", html)
        self.assertNotIn("quality_cli", client.get("/quality", base_url=base).get_data(as_text=True))
        self.assertEqual(client.post(url, data=form | {"token": "wrong"}, base_url=base).status_code, 403)
        self.assertEqual(client.post(url, data=form | {"snapshot_id": "0"*64}, base_url=base).status_code, 400)
        bad = client.post(url, data=form | {"original": (io.BytesIO(b"wrong original"), "original.pdf")},
                          content_type="multipart/form-data", base_url=base)
        self.assertEqual(bad.status_code, 400)
        self.assertIn(b"checksum does not match", bad.data)
        self.assertFalse((quality_root / "inventory").exists())
        response = client.post(url, data=form, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertIn(b"Location inventory saved", client.get(response.location, base_url=base).data)
        finding = next(item for item in quality.report(self.original, quality_root)["findings"]
                       if item["source_id"] == self.source["id"])
        self.assertEqual(finding["code"], "coverage_review")
        url = "/quality?" + urlencode({"collection": "core", "finding": finding["id"], "unit_id": self.unit["id"]})
        html = client.get(url, base_url=base).get_data(as_text=True)
        self.assertIn("Edit extracted data", html)
        self.assertIn('name="reviewer"', html)
        form |= {"finding_id": finding["id"], "unit_id": self.unit["id"], "action": "review",
                 "decision": "verified", "reviewer": "Quality tester", "note": "Compared both pages with the original."}
        self.assertEqual(client.post(url, data=form, base_url=base).status_code, 400)
        self.assertFalse((quality_root / "reviews").exists())
        self.assertEqual(client.post(url, data=form | {"checked": "yes", "decision": "verified_blank"},
                                     base_url=base).status_code, 400)
        response = client.post(url, data=form | {"checked": "yes"}, base_url=base)
        self.assertEqual(response.status_code, 303)
        review = next((quality_root / "reviews").glob("*.json"))
        self.assertEqual(json.loads(review.read_text())["reviewer"], "Quality tester")
        unrelated = next(unit["id"] for unit in self.original._bundle["units"]
                         if unit["citation"]["source_id"] != self.source["id"])
        self.assertEqual(client.post(url, data=form | {"action": "problem", "unit_id": unrelated},
                                     base_url=base).status_code, 400)
        self.assertFalse((quality_root / "problems").exists())
        self.assertEqual(client.post(url, data=form | {"action": "problem",
            "note": "Original says 20 but retained text says 2O."}, base_url=base).status_code, 303)
        report = quality.report(self.original, quality_root)
        self.assertIn("reported_defect", {item["code"] for item in report["findings"] if not item["resolved"]})
        self.assertEqual(project_library.store(self.root).current().release_id, self.current.release_id)
        self.assertEqual(self.original._bundle, project_library.store(self.root).current().snapshots["core"]._bundle)
        other = next(item for item in report["findings"] if item["code"] == "inventory_missing")
        other_url = "/quality?" + urlencode({"collection": "core", "finding": other["id"]})
        uploaded = client.post(other_url, data={
            "token": token, "snapshot_id": self.current.release_id, "finding_id": other["id"],
            "action": "inventory", "original": (io.BytesIO((self.pdfs / "keep.pdf").read_bytes()), "keep.pdf"),
        }, content_type="multipart/form-data", base_url=base)
        self.assertEqual(uploaded.status_code, 303)
        self.assertNotIn("inventory_missing",
                         {item["code"] for item in quality.report(self.original, quality_root)["findings"]})
        second = next(unit for unit in self.original._bundle["units"]
                      if unit["citation"]["source_id"] == self.source["id"] and unit["id"] != self.unit["id"])
        project_edit.correct(self.root, **(self.args | {
            "unit_id": second["id"], "text": second["text"] + " Corrected second page.", "tables": []}))
        updated = project_library.store(self.root).current()
        self.assertIn("reported_defect", {item["code"] for item in
                      quality.report(updated.snapshots["core"], quality_root)["findings"]})
        project_edit.correct(self.root, **(self.args | {"snapshot_id": updated.release_id, "tables": []}))
        updated = project_library.store(self.root).current()
        self.assertNotIn("reported_defect", {item["code"] for item in
                         quality.report(updated.snapshots["core"], quality_root)["findings"]})
        self.assertEqual(len(list((quality_root / "problems").glob("*.json"))), 1)

    def test_correcting_truncated_page_clears_truncation_only_in_new_version(self):
        bundle = deepcopy(self.original._bundle)
        next(unit for unit in bundle["units"] if unit["id"] == self.unit["id"])["truncated"] = True
        path = self.root.parent / "truncated"
        releases.export(bundle, path, profile="private_context",
                        authorization=self.original._manifest["authorization"])
        registry = project_library.store(self.root)
        document = deepcopy(self.current.document)
        document["collections"][0]["release_id"] = registry.releases.install(path)
        registry.publish(document)
        current = registry.current()
        project_edit.correct(self.root, **(self.args | {"snapshot_id": current.release_id,
                                                       "text": self.unit["text"], "tables": []}))
        after = registry.current().snapshots["core"]
        self.assertFalse(next(unit for unit in after._bundle["units"] if unit["id"] == self.unit["id"])["truncated"])
        self.assertTrue(next(unit for unit in registry.load(current.release_id).snapshots["core"]._bundle["units"]
                             if unit["id"] == self.unit["id"])["truncated"])

    def test_web_quality_adds_missing_page_in_new_version_and_preserves_audit(self):
        bundle = deepcopy(self.original._bundle)
        omitted = next(unit for unit in bundle["units"] if unit["citation"]["source_id"] == self.source["id"]
                       and unit["citation"]["locator"]["pdf_page"] == 2)
        bundle["units"].remove(omitted)
        path = self.root.parent / "missing-page"
        releases.export(bundle, path, profile="private_context",
                        authorization=self.original._manifest["authorization"])
        registry = project_library.store(self.root)
        release_id = registry.releases.install(path)
        document = deepcopy(self.current.document)
        document["collections"][0]["release_id"] = release_id
        registry.publish(document)
        current = registry.current()
        quality_root = self.root / "quality"
        quality.inventory(quality_root, self.source, [{"kind": "pdf", "pdf_page": page} for page in [1, 2]])
        finding = next(item for item in quality.report(current.snapshots["core"], quality_root)["findings"]
                       if item["code"] == "missing_unit")
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdfs, quality_root=quality_root, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        url = "/quality?" + urlencode({"collection": "core", "finding": finding["id"]})
        html = client.get(url, base_url=base).get_data(as_text=True)
        self.assertIn("Add missing page text", html)
        form = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                "snapshot_id": current.release_id, "finding_id": finding["id"], "action": "repair",
                "reviewer": "Page tester", "note": "Compared the original missing second page.",
                "text": "Missingpageword full original second page transcription."}
        self.assertEqual(client.post(url, data=form, base_url=base).status_code, 400)
        self.assertEqual(registry.current().release_id, current.release_id)
        response = client.post(url, data=form | {"checked": "yes"}, base_url=base)
        self.assertEqual(response.status_code, 303)
        updated = registry.current()
        self.assertNotEqual(updated.release_id, current.release_id)
        self.assertNotIn("missing_unit", {item["code"] for item in
                         quality.report(updated.snapshots["core"], quality_root)["findings"]})
        self.assertTrue(updated.call("search_evidence", {"query": "Missingpageword"})["result"]["items"])
        self.assertEqual(registry.load(current.release_id).snapshots["core"]._bundle, bundle)
        event = project_edit.history(self.root, self.source["id"])[0]
        self.assertIsNone(event["before"]["text"])
        self.assertEqual(event["after"]["text"], form["text"])
        self.assertEqual(event["reviewer"], "Page tester")
        self.assertEqual(client.post(url, data=form | {"checked": "yes"}, base_url=base).status_code, 400)

    def test_corrected_version_search_context_metadata_tables_and_immutable_history(self):
        before = deepcopy(self.original._bundle)
        result = project_edit.correct(self.root, **self.args)
        current = project_library.store(self.root).current()
        self.assertNotEqual(current.release_id, self.current.release_id)
        evidence = current.snapshots["core"]
        self.assertEqual(self.original._bundle, before)
        old = project_library.store(self.root).load(self.current.release_id).snapshots["core"]
        self.assertEqual(old._bundle, before)
        new_source = next(source for source in evidence._bundle["sources"] if source["id"] == self.source["id"])
        self.assertEqual(new_source["sha256"], self.source["sha256"])
        self.assertEqual(new_source["bibliography"]["title"], "Corrected title")
        self.assertIn("Operator-corrected", new_source["provenance"])
        untouched = [unit for unit in before["units"] if unit["id"] != self.unit["id"]]
        after = {unit["id"]: unit for unit in evidence._bundle["units"]}
        # Metadata provenance/URLs are propagated, but text and source identities stay unchanged elsewhere.
        for unit in untouched:
            self.assertEqual(after[unit["id"]]["text"], unit["text"])
            self.assertEqual(after[unit["id"]]["citation"]["source_sha256"], unit["citation"]["source_sha256"])
        hits = current.call("search_evidence", {"query": "correctedtranscription"})["result"]["items"]
        self.assertEqual(len(hits), 1)
        context = current.call("get_source_context", {"unit_id": self.unit["id"]})
        self.assertIn("correctedtranscription", str(context))
        table = evidence._bundle["tables"][0]
        self.assertEqual(current.call("get_table", {"table_id": table["id"]})["result"]["rows"], [["Example", 42]])
        self.assertEqual(table["citation"]["locator"], self.unit["citation"]["locator"])
        events = project_edit.history(self.root, self.source["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["before"]["text"], self.unit["text"])
        self.assertEqual(events[0]["after"]["text"], self.args["text"])
        self.assertEqual(events[0]["reviewer"], "Synthetic reviewer")
        self.assertEqual(events[0]["release"], result["release"])
        indexed = project_index.library_page(self.root, "Corrected title")
        self.assertEqual(indexed["matching"], 1)
        self.assertEqual(project_library.load(self.root)["papers"]["10.0000/example"]["title"], "Original title")

    def test_invalid_stale_unconfirmed_and_failed_publication_preserve_active_evidence(self):
        cases = [
            {"checked": False}, {"reviewer": ""}, {"reason": ""},
            {"snapshot_id": "0" * 64}, {"unit_id": "missing"},
            {"bibliography": {**self.args["bibliography"], "year": "yesterday"}},
            {"bibliography": {**self.args["bibliography"], "acquired_from": "javascript:alert(1)"}},
            {"tables": [{"id": "unrelated", **{key: self.args["tables"][0][key]
                                             for key in project_edit.TABLE_FIELDS}}]},
            {"tables": [{**self.args["tables"][0], "id": []}]},
            {"tables": [{**self.args["tables"][0], "rows": [["ragged"]]}]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises((ValueError, releases.ReleaseError, ValidationError)):
                    project_edit.correct(self.root, **(self.args | changes))
                self.assertEqual(project_library.store(self.root).current().release_id, self.current.release_id)
                self.assertEqual(project_edit.history(self.root, self.source["id"]), [])
        with patch.object(collections.CollectionStore, "publish", side_effect=ValueError("Staging failure")):
            with self.assertRaisesRegex(ValueError, "Staging failure"):
                project_edit.correct(self.root, **self.args)
        self.assertEqual(project_library.store(self.root).current().release_id, self.current.release_id)
        self.assertFalse(list((self.root / "runs").glob("correction-*")))

    def test_table_edit_and_removal_and_noop_corrections(self):
        project_edit.correct(self.root, **self.args)
        current = project_library.store(self.root).current()
        table = current.snapshots["core"]._bundle["tables"][0]
        editable = {"id": table["id"], **{key: table[key] for key in project_edit.TABLE_FIELDS}}
        args = self.args | {"snapshot_id": current.release_id, "tables": [editable]}
        with self.assertRaisesRegex(ValueError, "No data changed"):
            project_edit.correct(self.root, **args)
        editable["rows"] = [["Example", 43]]
        project_edit.correct(self.root, **args)
        current = project_library.store(self.root).current()
        self.assertEqual(current.snapshots["core"]._bundle["tables"][0]["rows"], [["Example", 43]])
        project_edit.correct(self.root, **(args | {"snapshot_id": current.release_id, "tables": []}))
        self.assertEqual(project_library.store(self.root).current().snapshots["core"]._bundle["tables"], [])
        self.assertEqual(len(project_edit.history(self.root, self.source["id"])), 3)

    def test_purge_removes_correction_history_and_tables_for_removed_source_only(self):
        project_edit.correct(self.root, **self.args)
        project_purge.purge(self.root, "10.0000/keep", self.pdfs)
        current = project_library.store(self.root).current()
        self.assertEqual(len(current.snapshots["core"]._bundle["tables"]), 1)
        self.assertEqual(len(project_edit.history(self.root, self.source["id"])), 1)
        project_purge.purge(self.root, "10.0000/example", self.pdfs)
        self.assertIsNone(project_library.status(self.root)["snapshot"])

    def test_subsequent_attachment_preserves_corrections_tables_and_audit(self):
        project_edit.correct(self.root, **self.args)
        project_library.register(self.root, [{"doi": "10.0000/extra", "title": "Extra paper"}])
        path = self.pdfs / "extra.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((50, 50), "Extra synthetic evidence with unique content for subsequent attachment.")
            pdf.save(path)
        project_library.attach(self.root, "10.0000/extra", path, identity_confirmed=True)
        current = project_library.store(self.root).current()
        unit = next(unit for unit in current.snapshots["core"]._bundle["units"] if unit["id"] == self.unit["id"])
        self.assertEqual(unit["text"], self.args["text"])
        self.assertEqual(len(current.snapshots["core"]._bundle["tables"]), 1)
        self.assertEqual(len(project_edit.history(self.root, self.source["id"])), 1)

    def test_web_editor_full_text_validation_save_and_history_links(self):
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdfs, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        url = "/library/edit?doi=10.0000/example"
        response = client.get(url, base_url=base)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.unit["text"].encode(), response.data)
        self.assertIn(b"Full retained text", response.data)
        token = re.search(r'name="token" value="([^"]+)"', response.get_data(as_text=True)).group(1)
        form = {"doi": "10.0000/example", "source_id": self.source["id"], "unit_id": self.unit["id"],
                "snapshot_id": self.current.release_id, "text": self.args["text"], "tables": json.dumps(self.args["tables"]),
                "reviewer": "Test reviewer", "reason": "Typo and table correction", "checked": "yes",
                "source_url": self.args["source_url"], **self.args["bibliography"]}
        self.assertEqual(client.post("/library/edit", data=form, base_url=base).status_code, 403)
        form["token"] = token
        invalid = client.post("/library/edit", data=form | {"tables": "invalid"}, base_url=base)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b"valid JSON", invalid.data)
        self.assertIn(self.args["text"].encode(), invalid.data)
        invalid_table = client.post("/library/edit", data=form | {
            "tables": json.dumps([{**self.args["tables"][0], "id": []}])}, base_url=base)
        self.assertEqual(invalid_table.status_code, 400)
        self.assertIn(b"Table ID must be a string", invalid_table.data)
        response = client.post("/library/edit", data=form, base_url=base)
        self.assertEqual(response.status_code, 303)
        response = client.get(response.location, base_url=base)
        self.assertIn(b"Correction history (1)", response.data)
        self.assertIn(b"Test reviewer", response.data)
        self.assertIn(b"<th>Measure</th>", response.data)
        sources = client.get("/sources", base_url=base)
        self.assertIn(b"Corrected title", sources.data)
        self.assertIn(b"2021", sources.data)
        self.assertIn(b"example.org", sources.data)
        listing = client.get("/library", base_url=base)
        self.assertIn(b"View and correct extracted data", listing.data)
        stale = client.post("/library/edit", data=form, base_url=base)
        self.assertEqual(stale.status_code, 400)
        self.assertIn(b"Evidence changed while editing", stale.data)

    def test_editor_loads_full_page_not_the_reader_slice_and_escapes_text(self):
        long_text = ("Long transcription with retained details. " * 120) + "</textarea><script>not executable</script>"
        project_edit.correct(self.root, **(self.args | {"text": long_text, "tables": []}))
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdfs, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        response = client.get("/library/edit?doi=10.0000/example", base_url="http://localhost:8080")
        self.assertIn(b"&lt;/textarea&gt;&lt;script&gt;not executable&lt;/script&gt;", response.data)
        self.assertGreater(response.data.count(b"Long transcription with retained details."), 120)
        self.assertNotIn(b"<script>not executable</script>", response.data)

    def test_page_links_load_each_exact_retained_page(self):
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdfs, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        response = client.get("/library/edit?doi=10.0000/example", base_url="http://localhost:8080")
        html = response.get_data(as_text=True)
        self.assertNotIn('id="edit-page"', html)
        nav = re.search(r'<nav class="pager" aria-label="Retained pages">(.*?)</nav>', html).group(1)
        links = re.findall(r'href="([^"]+)"', nav)
        units = sorted((unit for unit in self.original._bundle["units"]
                        if unit["citation"]["source_id"] == self.source["id"]), key=web_server.quality_order)
        self.assertEqual(len(links), len(units))
        texts = []
        for link, unit in zip(links, units):
            page = client.get(unescape(link), base_url="http://localhost:8080")
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            text = unescape(re.search(r'<textarea id="edit-text"[^>]*>(.*?)</textarea>', html, re.S).group(1))
            self.assertEqual(text, unit["text"])
            self.assertIn(f'<h4 id="page-editor">{web_server.quality_location(unit["citation"]["locator"])} — Page text / OCR</h4>', html)
            self.assertIn(f'name="unit_id" value="{unit["id"]}"', html)
            self.assertIn(unescape(link).replace("&", "&amp;") + '" aria-current="page"', html)
            texts.append(text)
        self.assertNotEqual(texts[0], texts[1])

    def test_project_search_relationship_scopes_filter_before_pagination(self):
        folder = self.root / "discovery" / ("a" * 32)
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "label": "Synthetic discovery", "started_at": "2026-11-11T00:00:00Z"})
        categories = {"referenced": ["background"], "citing": ["impact"],
                      "both": ["background", "impact"], "metadata": ["background"]}
        graph = {"nodes": {key: {"record": {"title": key.title(), "identifiers": {"doi": "10.0000/" + key}}}
                           for key in categories},
                 "edges": [{"target_provider_record": key, "direction": direction, "seed_doi": "10.0000/example"}
                           for key, directions in categories.items() for direction in directions]}
        collections.atomic_json(folder, "neighborhood.json", graph)
        for key in ("referenced", "citing", "both"):
            path = self.pdfs / (key + ".pdf")
            with pymupdf.open() as pdf:
                for number in range(6):
                    pdf.new_page().insert_text((50, 50),
                        f"Scopeprobe {key} evidence on unique page {number} for relationship search validation.")
                pdf.save(path)
            project_library.attach(self.root, "10.0000/" + key, path, identity_confirmed=True)
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdfs, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        initial = client.get("/search", base_url=base).get_data(as_text=True)
        self.assertIn('name="relationship"', initial)
        self.assertNotIn('name="collection"', initial)
        for label in ("All papers", "Core papers", "References", "Citing papers"):
            self.assertIn(label, initial)
        for relationship, expected in (("", {"referenced", "citing", "both"}),
                                       ("background", {"referenced", "both"}),
                                       ("impact", {"citing", "both"}), ("core", set())):
            url = "/search?query=scopeprobe&relationship=" + relationship
            found, pages = set(), 0
            while url:
                response = client.get(url, base_url=base)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                found.update(re.findall(r'<h3 class="hit-title">([^<]+)</h3>', html))
                pages += 1
                links = re.findall(r'href="([^"]+)">Next results</a>', html)
                url = unescape(links[0]) if links else None
                if url:
                    self.assertIn("relationship=" + relationship, url)
                    self.assertIn("snapshot_id=", url)
                self.assertLess(pages, 6)
            self.assertEqual(found, {key.title() for key in expected})
            if expected:
                self.assertGreater(pages, 1)
        core = client.get("/search?query=original&relationship=core", base_url=base).get_data(as_text=True)
        self.assertIn("Original title", core)
        self.assertIn("Keep title", core)
        for path in ("/search?relationship=invalid", "/search?query=scopeprobe&relationship=impact&offset=5",
                     "/search?query=scopeprobe&collection=secret&relationship=core"):
            self.assertIn(client.get(path, base_url=base).status_code, {400, 403})
        # Discovery changes invalidate the same index used by Library and search scopes.
        graph["edges"] = [edge for edge in graph["edges"] if edge["direction"] != "impact"]
        collections.atomic_json(folder, "neighborhood.json", graph)
        self.assertNotIn('class="hit-title"', client.get(
            "/search?query=scopeprobe&relationship=impact", base_url=base).get_data(as_text=True))

    def test_paragraph_draft_column_order_and_reviewed_version_only_on_save(self):
        project_library.register(self.root, [{"doi": "10.0000/columns", "title": "Column fixture"}])
        path = self.pdfs / "columns.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=800)
            page.insert_text((40, 35), "Heading spanning both columns with enough text to cover the page width.")
            # Insert in row order deliberately: the draft must use geometry, not content-stream order.
            page.insert_text((40, 100), "Left first paragraph\ncontinues on its second line.")
            page.insert_text((330, 100), "Right first paragraph\ncontinues in the right column.")
            page.insert_text((40, 160), "Left second paragraph\nends the left column.")
            page.insert_text((330, 160), "Right second paragraph\nends the right column.")
            page.insert_text((40, 750), "Footer spanning both columns with enough text to cover the page width.")
            pdf.save(path)
        project_library.attach(self.root, "10.0000/columns", path, identity_confirmed=True)
        current = project_library.store(self.root).current()
        unit = next(unit for unit in current.snapshots["core"]._bundle["units"]
                    if unit["citation"]["doi"] == "10.0000/columns")
        args = {"snapshot_id": current.release_id, "source_id": unit["citation"]["source_id"], "unit_id": unit["id"]}
        draft = project_edit.paragraph_draft(self.root, **args)
        self.assertEqual(unit["text"], draft)
        self.assertEqual(unit["verification_status"], "machine_extracted")
        self.assertEqual(unit["human_verification"], "not_reviewed")
        self.assertIn("Left first paragraph continues on its second line.", draft)
        self.assertLess(draft.index("Left second"), draft.index("Right first"))
        self.assertTrue(draft.startswith("Heading"))
        self.assertTrue(draft.endswith("page width."))
        self.assertIn("\n\n", draft)
        self.assertEqual(Counter(character for character in draft if not character.isspace()),
                         Counter(character for character in unit["text"] if not character.isspace()))
        self.assertEqual(project_library.store(self.root).current().release_id, current.release_id)
        app = web_server.create_app(registry=self.root / "registry", project_root=self.root,
            pdf_data_root=self.pdfs, profiles={"private_context"},
            allowed_collections={"core"}, default_collections=["core"])
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/library/edit?doi=10.0000/columns", base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        form = self.args["bibliography"] | args | {
            "doi": "10.0000/columns", "action": "repair-layout", "token": token,
            "text": "Unsaved draft", "tables": "[]", "reviewer": "Preserve reviewer", "reason": "Layout correction"}
        response = client.post("/library/edit", data=form, base_url=base)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("nothing has been saved", html)
        self.assertIn("Preserve reviewer", html)
        self.assertEqual(unescape(re.search(r'<textarea id="edit-text"[^>]*>(.*?)</textarea>', html, re.S).group(1)), draft)
        self.assertEqual(project_library.store(self.root).current().release_id, current.release_id)
        self.assertEqual(project_edit.history(self.root, args["source_id"]), [])
        self.assertEqual(client.post("/library/edit", data=form | {"token": ""}, base_url=base).status_code, 403)
        with self.assertRaisesRegex(ValueError, "Evidence changed"):
            project_edit.paragraph_draft(self.root, **(args | {"snapshot_id": self.current.release_id}))
        with self.assertRaisesRegex(ValueError, "Select an extracted PDF page"):
            project_edit.paragraph_draft(self.root, **(args | {"source_id": self.source["id"]}))
        with patch.object(pymupdf.Page, "get_text", side_effect=lambda mode, **kwargs: [] if mode == "blocks" else ""):
            with self.assertRaisesRegex(ValueError, "Too little native PDF text"):
                project_edit.paragraph_draft(self.root, **args)
        stored = self.root / "papers" / (unit["citation"]["source_sha256"] + ".pdf")
        stored.write_bytes(b"%PDF-corrupted")
        with self.assertRaisesRegex(ValueError, "checksum differs"):
            project_edit.paragraph_draft(self.root, **args)


if __name__ == "__main__":
    unittest.main()
