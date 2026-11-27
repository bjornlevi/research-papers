import asyncio
from argparse import Namespace
from copy import deepcopy
from datetime import timedelta
from html import unescape
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import re
import socket
from urllib.request import urlopen

import httpx
import pymupdf
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from werkzeug.serving import WSGIRequestHandler, make_server

from research_library import (collections, connection_check, http_auth, http_server, literature, quality,
                              passage_context, releases, server, web_lifecycle, web_server)
from research_library.build import (
    citation_neighborhood, literature_text, literature_update, project_backup, project_batch, project_chunks, project_current_store, project_edit,
    project_cutover, project_index, project_library, project_state, project_text,
)
from project_text_helpers import publish_all

sys.path.insert(0, str(Path(__file__).resolve().parent / "portable"))
from test_http import running_server


class ProjectCurrentStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        project_library.initialize(self.root, "Current state", "Synthetic test data")
        for doi in ("one", "two"):
            project_library.register(self.root, [{"doi": "10.0000/" + doi, "title": doi}])
            pdf = self.base / (doi + ".pdf")
            with pymupdf.open() as document:
                document.new_page().insert_text((50, 50), f"Fictional research paper {doi} with ample text.")
                document.save(pdf)
            project_library.attach(self.root, "10.0000/" + doi, pdf, identity_confirmed=True)
        publish_all(self.root)
        self.state = project_state.current(self.root)
        self.store = self.base / "current"
        project_current_store.initialize(self.state, self.store)
        self.source_id = self.state["editorial"]["bundle"]["sources"][0]["id"]
        self.path = self.store / "papers" / project_state._paper_filename(self.source_id)

    def paper(self):
        return releases.read_json(self.path.read_bytes())

    def test_paper_validation_cache_detects_changed_corrupt_and_missing_files(self):
        with project_current_store._VALIDATED_PAPERS_LOCK:
            project_current_store._VALIDATED_PAPERS.clear()
        with patch.object(project_current_store, "_validate_paper",
                          wraps=project_current_store._validate_paper) as validate:
            first = project_current_store.read(self.store)
            self.assertEqual(validate.call_count, 2)
            first["editorial"]["bundle"]["sources"][0]["doi"] = "mutated copy"
            second = project_current_store.read(self.store)
            self.assertEqual(validate.call_count, 2)
            self.assertNotEqual(second["editorial"]["bundle"]["sources"][0]["doi"], "mutated copy")
            data = self.path.read_bytes()
            self.path.write_bytes(data + b"\n")
            project_current_store.read(self.store)
            self.assertEqual(validate.call_count, 3)
            manifest = project_current_store._manifest(self.store)
            manifest["headers"]["editorial"]["caveats"].append("Changed header")
            project_current_store._read_papers(self.store, manifest, {})
            self.assertEqual(validate.call_count, 5)
            self.path.write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "identity"):
                project_current_store.read(self.store)
            self.path.unlink()
            with self.assertRaisesRegex(ValueError, "files do not match"):
                project_current_store.read(self.store)
            self.path.write_bytes(data)
            self.assertEqual(project_current_store.read(self.store), self.state)

    def test_compare_current_store_reports_exact_drift_and_pdf_integrity(self):
        baseline = project_state.current(self.root)
        current_before = project_current_store.read(self.store)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected snapshot export")):
            report = project_cutover.compare(self.root, self.store)
        self.assertTrue(all(report["sections_match"].values()))
        self.assertEqual(report["project_fields_differ"], [])
        self.assertEqual(report["paper_differences"], [])
        self.assertEqual(report["normal_source_count"], 2)
        self.assertEqual({pdf["status"] for pdf in report["pdfs"]}, {"verified"})
        self.assertTrue(report["quality_records_match"])
        self.assertFalse(report["cutover_authorized"])
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        revision = project_current_store.update_rag(
            self.store, source_id=self.source_id, revision=revision, form={"ranges": ""})["revision"]
        project_current_store.save_draft(
            self.store, source_id=self.source_id, revision=revision, unit_id=unit["id"],
            text=unit["text"] + "\nDraft drift.", bibliography={
                key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correction")
        pdf = self.root / "papers" / (source["sha256"] + ".pdf")
        original = pdf.read_bytes()
        pdf.write_bytes(b"corrupt")
        quality.inventory(self.store / "quality", source, [{"kind": "pdf", "pdf_page": 1}])
        report = project_cutover.compare(self.root, self.store)
        self.assertEqual(report["paper_differences"], [
            {"source_id": self.source_id, "sections": ["editorial", "rag", "audit"]}])
        self.assertFalse(report["sections_match"]["editorial"])
        self.assertFalse(report["sections_match"]["rag"])
        self.assertTrue(report["sections_match"]["published"])
        self.assertEqual(next(row["status"] for row in report["pdfs"]
                              if row["source_id"] == self.source_id), "checksum_mismatch")
        self.assertFalse(report["quality_records_match"])
        self.assertEqual(report["current_quality_records"], 1)
        project, project_revision = project_current_store.project_view(self.store)
        project["name"] = "Changed name"
        project_current_store.update_project(self.store, project=project, expected=project_revision)
        self.assertEqual(project_cutover.compare(self.root, self.store)["project_fields_differ"], ["name"])
        pdf.unlink()
        self.assertEqual(next(row["status"] for row in project_cutover.compare(self.root, self.store)["pdfs"]
                              if row["source_id"] == self.source_id), "missing")
        pdf.write_bytes(original)
        self.assertEqual(project_state.current(self.root), baseline)
        self.assertNotEqual(project_current_store.read(self.store), current_before)

    def test_compare_current_store_cli_and_invalid_paths(self):
        executable = Path(sys.executable).parent / "research-project"
        result = subprocess.run([str(executable), "--root", str(self.root),
                                 "compare-current-store", str(self.store)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"cutover_authorized": false', result.stdout)
        with self.assertRaisesRegex(ValueError, "separate"):
            project_cutover.compare(self.root, self.root)
        alias = self.base / "alias-current"
        alias.symlink_to(self.store, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            project_cutover.compare(self.root, alias)

    def test_compare_markdown_reports_changes_without_modifying_repository(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        staged = repository / "staged.txt"
        staged.write_text("Already staged\n")
        subprocess.run(["git", "-C", str(repository), "add", "--", staged.name], check=True)
        project_current_store.sync_git_markdown(store, repository)
        report = project_cutover.compare(self.root, store, markdown_repository=repository)["markdown"]
        self.assertEqual(report["counts"]["matching"], 4)
        self.assertEqual(report["differences"], [])
        sources = state["editorial"]["bundle"]["sources"]
        first = repository / "project-markdown" / literature_text.slug(sources[0])
        second = repository / "project-markdown" / literature_text.slug(sources[1])
        (first / "draft.md").write_text(project_text._MARKDOWN_GENERATED + "different generated content\n")
        (first / "published.md").write_text("User-authored content\n")
        (second / "draft.md").unlink()
        before = {path.relative_to(repository): path.read_bytes()
                  for path in (repository / "project-markdown").rglob("*") if path.is_file()}
        report = project_cutover.compare(self.root, store, markdown_repository=repository)["markdown"]
        self.assertEqual(report["counts"]["different_generated"], 1)
        self.assertEqual(report["counts"]["unmanaged"], 1)
        self.assertEqual(report["counts"]["missing"], 1)
        self.assertEqual(report["counts"]["matching"], 1)
        self.assertEqual({path.relative_to(repository): path.read_bytes()
                          for path in (repository / "project-markdown").rglob("*") if path.is_file()}, before)
        staged_names = subprocess.run(["git", "-C", str(repository), "diff", "--cached", "--name-only"],
                                      capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(staged_names, ["staged.txt"])
        (first / "draft.md").unlink()
        (first / "draft.md").symlink_to(staged)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            project_cutover.compare(self.root, store, markdown_repository=repository)

    def test_compare_markdown_detects_withdrawn_evidence_and_uninitialized_output(self):
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        report = project_cutover.compare(self.root, self.store, markdown_repository=repository)["markdown"]
        self.assertEqual(report["counts"]["missing"], 4)
        self.assertFalse((repository / "project-markdown").exists())
        executable = Path(sys.executable).parent / "research-project"
        result = subprocess.run([str(executable), "--root", str(self.root),
                                 "compare-current-store", str(self.store),
                                 "--markdown-repository", str(repository)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["markdown"]["counts"]["missing"], 4)
        self.assertFalse((repository / "project-markdown").exists())
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        project_current_store.sync_git_markdown(store, repository)
        source = project_current_store.read_paper(store, self.source_id)[0]["editorial"]["source"]
        published = repository / "project-markdown" / literature_text.slug(source) / "published.md"
        previous = published.read_bytes()
        _, revision = project_current_store.read_paper(store, self.source_id)
        project_current_store.edit_with_git_markdown(
            store, repository, action="unpublish", revision=revision, source_id=self.source_id,
            reviewer="Tester", reason="Withdraw", checked=True)
        published.write_bytes(previous)
        report = project_cutover.compare(self.root, store, markdown_repository=repository)["markdown"]
        self.assertEqual(report["counts"]["withdrawal_needed"], 1)
        self.assertTrue(any(row["source_id"] == self.source_id and row["status"] == "withdrawal_needed"
                            for row in report["differences"]))

    def test_compare_markdown_rejects_invalid_paths_and_duplicate_slugs(self):
        repository = self.base / "markdown-repository"
        repository.mkdir()
        state = project_current_store.read(self.store)
        with self.assertRaisesRegex(ValueError, "Git repository root"):
            project_cutover._markdown_check(state, repository)
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        alias = self.base / "markdown-alias"
        alias.symlink_to(repository, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked paths"):
            project_cutover._markdown_check(state, alias)
        sources = state["editorial"]["bundle"]["sources"]
        sources[1]["doi"] = sources[0]["doi"]
        sources[1]["sha256"] = sources[0]["sha256"]
        with self.assertRaisesRegex(ValueError, "same Markdown path"):
            project_cutover._markdown_check(state, repository)
        self.assertFalse((repository / "project-markdown").exists())

    def test_empty_current_store_can_attach_and_publish_without_snapshots(self):
        destination = self.base / "new-current"
        project_before = project_state.current(self.root)
        runs_before = set((self.root / "runs").iterdir())
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected snapshot export")):
            result = project_current_store.initialize_empty(
                destination, name=" New project ", authorization=" Private permission ",
                rows=[{"doi": "10.0000/new", "title": "New paper"}])
            self.assertEqual(result["papers"], 0)
            state = project_current_store.read(destination)
            self.assertEqual(state["project"]["name"], "New project")
            self.assertEqual(state["project"]["authorization"], "Private permission")
            self.assertEqual(state["editorial"]["bundle"]["sources"], [])
            self.assertEqual(state["published"]["bundle"]["sources"], [])
            endpoint = project_current_store.CurrentCollectionStore(destination, allowed_profiles={"private_context"})
            self.assertEqual(endpoint.current().call("search_evidence", {"query": "fictional"})["result"]["items"], [])
            pdf_data = self.base / "empty-pdf-data"
            pdf_data.mkdir()
            pdf = pdf_data / "new.pdf"
            with pymupdf.open() as document:
                document.new_page().insert_text((50, 50), "Fictional freshly initialized project text.")
                document.save(pdf)
            client = web_server.create_app(current_store=destination, project_root=self.root,
                                           pdf_data_root=pdf_data, profiles={"private_context"}).test_client()
            base = "http://localhost:8080"
            url = "/library/paper?id=doi%3A10.0000%2Fnew"
            html = client.get(url, base_url=base).get_data(as_text=True)
            fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                      "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                      "doi": "10.0000/new", "title": "New paper", "pdf": "new.pdf",
                      "confirm_identity": "yes", "return_to": "/library/paper"}
            self.assertEqual(client.post("/settings/attach-pdf", data=fields, base_url=base).status_code, 303)
            self.assertFalse(endpoint.current().call("search_evidence", {"query": "freshly"})["result"]["items"])
            source_id = project_current_store.read(destination)["editorial"]["bundle"]["sources"][0]["id"]
            _, revision = project_current_store.read_paper(destination, source_id)
            project_current_store.publish(destination, source_id=source_id, revision=revision,
                                          reviewer="Tester", reason="Compared", checked=True)
            self.assertTrue(endpoint.current().call("search_evidence", {"query": "freshly"})["result"]["items"])
        self.assertEqual(project_state.current(self.root), project_before)
        self.assertEqual(set((self.root / "runs").iterdir()), runs_before)

    def test_empty_current_store_rejects_invalid_input_without_creating_directory(self):
        destination = self.base / "new-current"
        for kwargs in ({"name": "", "authorization": "Private"},
                       {"name": "Test", "authorization": ""},
                       {"name": "Test", "authorization": "Private", "rows": []},
                       {"name": "Test", "authorization": "Private",
                        "rows": [{"doi": "not-a-doi", "title": "Bad"}]}):
            with self.assertRaises(ValueError):
                project_current_store.initialize_empty(destination, **kwargs)
            self.assertFalse(destination.exists())

    def test_empty_current_store_supports_later_core_registration_and_recovery(self):
        destination = self.base / "new-current"
        project_current_store.initialize_empty(destination, name="Test", authorization="Private")
        endpoint = project_current_store.CurrentCollectionStore(destination, allowed_profiles={"private_context"})
        empty_id = endpoint.current().release_id
        client = web_server.create_app(current_store=destination, project_root=self.root,
                                       profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        self.assertEqual(client.get("/library", base_url=base).status_code, 200)
        self.assertEqual(client.get("/quality", base_url=base).status_code, 200)
        _, revision = project_current_store.project_view(destination)
        project_current_store.register_core_papers(
            destination, [{"doi": "10.0000/later", "title": "Later paper"}], expected=revision)
        self.assertEqual(endpoint.current().release_id, empty_id)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.register_core_papers(
                destination, [{"doi": "10.0000/other", "title": "Other paper"}], expected=revision)
        restored = self.base / "recovered-empty"
        project_current_store.restore_project(destination, restored)
        self.assertEqual(project_state.current(restored), project_current_store.read(destination))

    def test_preserved_current_drafts_are_reused_without_publication_or_reextraction(self):
        source_id = self.source_id
        paper, revision = project_current_store.read_paper(self.store, source_id)
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        revision = project_current_store.update_rag(
            self.store, source_id=source_id, revision=revision, form={"ranges": ""})["revision"]
        project_current_store.save_draft(
            self.store, source_id=source_id, revision=revision, unit_id=unit["id"],
            text="Preserved cleaned draft text.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "",
            tables=[{"id": "", "title": "Synthetic table", "headers": ["Measure", "Count"],
                     "units": [None, "participants"], "rows": [["Example", 42]], "footnotes": ["Note"]}],
            reviewer="Tester", reason="Reviewed")
        previous = project_current_store.read(self.store)
        prior_project = project_state.current(self.root)
        destination = self.base / "new-preserved"
        selected_doi = source["doi"]
        rows = [{"doi": selected_doi, "title": "Selected"},
                {"doi": "10.0000/unavailable", "title": "Awaiting PDF"}]
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected snapshot")), \
             patch.object(project_library.literature_ingest, "extract_pdf", side_effect=AssertionError("Re-extracted")):
            result = project_current_store.initialize_from_preserved(
                self.store, self.root, destination, name="New", authorization="Private", rows=rows)
        self.assertEqual(result["restored_drafts"], 1)
        self.assertEqual(result["published"], 0)
        restored, _ = project_current_store.read_paper(destination, source_id)
        old, _ = project_current_store.read_paper(self.store, source_id)
        self.assertEqual(restored["editorial"], old["editorial"])
        self.assertEqual(restored["audit"], {"draft": old["audit"]["draft"]})
        self.assertIsNone(restored["published"])
        self.assertIsNone(restored["rag"])
        state = project_current_store.read(destination)
        self.assertEqual(set(state["project"]["papers"]), {selected_doi, "10.0000/unavailable"})
        self.assertEqual(len(state["editorial"]["bundle"]["sources"]), 1)
        endpoint = project_current_store.CurrentCollectionStore(destination, allowed_profiles={"private_context"})
        self.assertFalse(endpoint.current().call("search_evidence", {"query": "Preserved cleaned"})["result"]["items"])
        _, new_revision = project_current_store.read_paper(destination, source_id)
        project_current_store.publish(destination, source_id=source_id, revision=new_revision,
                                      reviewer="Tester", reason="Compared", checked=True)
        self.assertTrue(endpoint.current().call("search_evidence", {"query": "Preserved cleaned"})["result"]["items"])
        self.assertEqual(project_current_store.read(self.store), previous)
        self.assertEqual(project_state.current(self.root), prior_project)

    def test_preserved_draft_reuse_rejects_bad_pdf_and_changes_atomically(self):
        source = project_current_store.read_paper(self.store, self.source_id)[0]["editorial"]["source"]
        pdf = self.root / "papers" / (source["sha256"] + ".pdf")
        original = pdf.read_bytes()
        destination = self.base / "bad-preserved"
        rows = [{"doi": source["doi"], "title": "Selected"}]
        pdf.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            project_current_store.initialize_from_preserved(
                self.store, self.root, destination, name="New", authorization="Private", rows=rows)
        self.assertFalse(destination.exists())
        pdf.write_bytes(original)
        initialize = project_current_store.initialize

        def mutate(state, candidate):
            result = initialize(state, candidate)
            pdf.write_bytes(b"changed")
            return result

        with patch.object(project_current_store, "initialize", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                project_current_store.initialize_from_preserved(
                    self.store, self.root, destination, name="New", authorization="Private", rows=rows)
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".bad-preserved-preserved-") for path in self.base.iterdir()))
        pdf.write_bytes(original)
        self.assertEqual(project_current_store.read(self.store), self.state)

    def test_git_markdown_sync_uses_current_store_and_does_not_stage(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        staged = repository / "staged.txt"
        staged.write_text("Preserve staged data.\n")
        subprocess.run(["git", "-C", str(repository), "add", "--", staged.name], check=True)
        before = project_current_store.read(store)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            result = project_current_store.sync_git_markdown(store, repository)
        self.assertEqual(result["exported"], 4)
        paper, revision = project_current_store.read_paper(store, self.source_id)
        source = paper["editorial"]["source"]
        directory = repository / "project-markdown" / literature_text.slug(source)
        draft = directory / "draft.md"
        published = directory / "published.md"
        old_published = published.read_text()
        self.assertIn(paper["editorial"]["units"][0]["text"], draft.read_text())
        self.assertEqual(project_current_store.read(store), before)
        unit = paper["editorial"]["units"][0]
        revision = project_current_store.edit_with_git_markdown(
            store, repository, action="save", revision=revision, source_id=self.source_id, unit_id=unit["id"],
            text=unit["text"] + "\nRevised current draft.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correct")["revision"]
        project_current_store.sync_git_markdown(store, repository)
        self.assertIn("Revised current draft.", draft.read_text())
        self.assertEqual(published.read_text(), old_published)
        revision = project_current_store.edit_with_git_markdown(
            store, repository, action="publish", revision=revision, source_id=self.source_id,
            reviewer="Tester", reason="Publish", checked=True)["revision"]
        project_current_store.sync_git_markdown(store, repository)
        self.assertIn("Revised current draft.", published.read_text())
        project_current_store.edit_with_git_markdown(
            store, repository, action="unpublish", revision=revision, source_id=self.source_id,
            reviewer="Tester", reason="Withdraw", checked=True)
        project_current_store.sync_git_markdown(store, repository)
        self.assertIn("not currently published", published.read_text())
        self.assertIn("Revised current draft.", draft.read_text())
        staged_names = subprocess.run(
            ["git", "-C", str(repository), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(staged_names, ["staged.txt"])

    def test_git_markdown_single_paper_refresh_requires_current_revision(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        project_current_store.sync_git_markdown(store, repository)
        paper, revision = project_current_store.read_paper(store, self.source_id)
        other = next(source for source in state["editorial"]["bundle"]["sources"] if source["id"] != self.source_id)
        other_draft = repository / "project-markdown" / literature_text.slug(other) / "draft.md"
        other_draft.write_text("Unmanaged other paper.\n")
        source = paper["editorial"]["source"]
        unit = paper["editorial"]["units"][0]
        result = project_current_store.edit_with_git_markdown(
            store, repository, action="save", revision=revision, source_id=self.source_id, unit_id=unit["id"],
            text=unit["text"] + "\nTargeted refresh.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correct")
        draft = repository / "project-markdown" / literature_text.slug(source) / "draft.md"
        old = draft.read_text()
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.sync_git_markdown(store, repository, source_id=self.source_id, revision=revision)
        self.assertEqual(draft.read_text(), old)
        with self.assertRaisesRegex(ValueError, "both"):
            project_current_store.sync_git_markdown(store, repository, source_id=self.source_id)
        result = project_current_store.sync_git_markdown(
            store, repository, source_id=self.source_id, revision=result["revision"])
        self.assertEqual(result["exported"], 2)
        self.assertIn("Targeted refresh.", draft.read_text())
        self.assertEqual(other_draft.read_text(), "Unmanaged other paper.\n")

    def test_git_markdown_sync_rejects_disabled_tracking_and_unmanaged_output(self):
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        with self.assertRaisesRegex(ValueError, "not enabled"):
            project_current_store.sync_git_markdown(self.store, repository)
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        for source in state["editorial"]["bundle"]["sources"]:
            target = repository / "project-markdown" / literature_text.slug(source) / "draft.md"
            target.parent.mkdir(parents=True)
            target.write_text("Unmanaged user text.\n")
        before = project_current_store.read(store)
        with self.assertRaisesRegex(ValueError, "not generated"):
            project_current_store.sync_git_markdown(store, repository)
        self.assertEqual(project_current_store.read(store), before)
        for source in state["editorial"]["bundle"]["sources"]:
            target = repository / "project-markdown" / literature_text.slug(source) / "draft.md"
            self.assertEqual(target.read_text(), "Unmanaged user text.\n")

    def test_git_markdown_sync_fails_explicitly_and_can_be_retried(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        before = project_current_store.read(store)
        export = project_text.export_git_markdown
        calls = []

        def fail_second(*args, **kwargs):
            calls.append(kwargs["source_id"])
            if len(calls) == 2:
                raise OSError("Synthetic export failure")
            return export(*args, **kwargs)

        with patch.object(project_text, "export_git_markdown", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "Synthetic export failure"):
                project_current_store.sync_git_markdown(store, repository)
        self.assertEqual(project_current_store.read(store), before)
        self.assertEqual(project_current_store.sync_git_markdown(store, repository)["exported"], 4)
        self.assertEqual(project_current_store.read(store), before)

    def test_tracked_editor_saves_publishes_and_recovers_failed_export(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        client = web_server.create_app(
            current_store=store, project_root=self.root, profiles={"private_context"},
            experimental_markdown_repository=repository, confirm_markdown_rights=True).test_client()
        paper, revision = project_current_store.read_paper(store, self.source_id)
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        identity = collections.publication(source["doi"])
        url = "/library/paper?" + web_server.urlencode({"id": identity})
        html = client.get(url + "&mode=edit&scope=page", base_url="http://localhost:8080").get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        form = {
            "token": token, "doi": source["doi"], "unit_id": unit["id"], "source_id": self.source_id,
            "scope": "page", "snapshot_id": revision, "text": unit["text"] + "\nTracked edit.",
            "tables": "[]", "source_url": source["url"] or "", "reviewer": "Tester", "reason": "Correct",
            **{key: value or "" for key, value in source["bibliography"].items()},
        }
        before_project = project_state.current(self.root)
        with patch.object(project_text, "export_git_markdown", side_effect=OSError("Disk failure")):
            response = client.post("/library/edit", data=form, base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 503)
        self.assertIn("may already have been saved", response.get_data(as_text=True))
        with self.assertRaisesRegex(ValueError, "Interrupted Git Markdown"):
            project_current_store.read(store)
        other = self.base / "other-repository"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "original Git repository"):
            project_current_store.recover_git_markdown(store, other)
        self.assertTrue(project_current_store.recover_git_markdown(store, repository)["recovered"])
        saved, revision = project_current_store.read_paper(store, self.source_id)
        self.assertIn("Tracked edit.", saved["editorial"]["units"][0]["text"])
        directory = repository / "project-markdown" / literature_text.slug(source)
        self.assertIn("Tracked edit.", (directory / "draft.md").read_text())
        self.assertNotIn("Tracked edit.", (directory / "published.md").read_text())
        self.assertEqual(client.post("/library/paper/publication", data={
            "id": identity, "token": token, "snapshot_id": revision, "action": "publish",
            "reviewer": "Tester", "reason": "Compare source", "checked": "yes",
        }, base_url="http://localhost:8080").status_code, 303)
        self.assertIn("Tracked edit.", (directory / "published.md").read_text())
        _, revision = project_current_store.read_paper(store, self.source_id)
        self.assertEqual(client.post("/library/paper/publication", data={
            "id": identity, "token": token, "snapshot_id": revision, "action": "unpublish",
            "reviewer": "Tester", "reason": "Withdraw", "checked": "yes",
        }, base_url="http://localhost:8080").status_code, 303)
        self.assertIn("not currently published", (directory / "published.md").read_text())
        self.assertEqual(project_state.current(self.root), before_project)
        self.assertFalse(subprocess.run(["git", "-C", str(repository), "diff", "--cached", "--name-only"],
                                        capture_output=True, text=True, check=True).stdout)

    def test_markdown_journal_handles_invalid_edit_and_interrupted_paper_write(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        repository = self.base / "markdown-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        paper, revision = project_current_store.read_paper(store, self.source_id)
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        args = dict(source_id=self.source_id, revision=revision, unit_id=unit["id"], text=unit["text"] + "\nEdit.",
                    bibliography={key: value or "" for key, value in source["bibliography"].items()},
                    source_url=source["url"] or "", tables=[], reviewer="", reason="Correct")
        with self.assertRaises(ValueError):
            project_current_store.edit_with_git_markdown(store, repository, action="save", **args)
        self.assertFalse((store / "markdown-transaction.json").exists())
        self.assertEqual(project_current_store.read(store), state)
        atomic = collections.atomic_json

        def fail_paper(folder, name, value):
            if Path(folder) == store / "papers":
                raise OSError("Paper write failed")
            return atomic(folder, name, value)

        with patch.object(collections, "atomic_json", side_effect=fail_paper):
            with self.assertRaisesRegex(OSError, "Paper write failed"):
                project_current_store.edit_with_git_markdown(
                    store, repository, action="save", **(args | {"reviewer": "Tester"}))
        with self.assertRaisesRegex(ValueError, "Interrupted Git Markdown"):
            project_current_store.read(store)
        executable = Path(sys.executable).parent / "research-project"
        result = subprocess.run([str(executable), "recover-current-markdown", str(store),
                                 "--repository", str(repository)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(project_current_store.read(store), state)
        self.assertFalse(project_current_store.recover_git_markdown(store, repository)["recovered"])

    def test_tracked_editor_requires_explicit_rights_and_separate_git_repository(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        subprocess.run(["git", "init", "-q", str(self.base)], check=True)
        shared = dict(current_store=store, project_root=self.root, profiles={"private_context"})
        with self.assertRaisesRegex(ValueError, "rights confirmation"):
            web_server.create_app(**shared, experimental_markdown_repository=self.base)
        with self.assertRaisesRegex(ValueError, "separate Git repository"):
            web_server.create_app(**shared, experimental_markdown_repository=self.base,
                                  confirm_markdown_rights=True)
        separate = self.base / "separate"
        separate.mkdir()
        subprocess.run(["git", "init", "-q", str(separate)], check=True)
        nested = separate / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "repository root"):
            web_server.create_app(**shared, experimental_markdown_repository=nested,
                                  confirm_markdown_rights=True)

    def test_web_git_markdown_settings_backfill_disable_and_preserve_evidence(self):
        repository = self.base / "settings-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        with self.assertRaisesRegex(ValueError, "rights confirmation"):
            web_server.create_app(current_store=self.store, project_root=self.root,
                                  profiles={"private_context"}, experimental_markdown_repository=repository)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"}, experimental_markdown_repository=repository,
                                    confirm_markdown_rights=True)
        client = app.test_client()
        base = "http://localhost:8080"
        original = project_current_store.read(self.store)
        original_project = project_state.current(self.root)
        html = client.get("/settings", base_url=base).get_data(as_text=True)
        self.assertIn("Current paper records", html)
        self.assertIn("published text and RAG settings", html)
        self.assertIn("generated views, not the paper records", html)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "enabled": "true", "confirm_rights": "yes"}
        self.assertIn("Enable Git Markdown tracking", html)
        self.assertEqual(client.post("/settings/git-markdown", data=fields | {"confirm_rights": ""},
                                     base_url=base).status_code, 400)
        self.assertEqual(client.post("/settings/git-markdown", data=fields | {"enabled": "invalid"},
                                     base_url=base).status_code, 400)
        self.assertEqual(client.post("/settings/git-markdown", data=fields | {"project_revision": "stale"},
                                     base_url=base).status_code, 400)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            response = client.post("/settings/git-markdown", data=fields, base_url=base)
        self.assertEqual(response.status_code, 200)
        self.assertIn("4 current versions exported", response.get_data(as_text=True))
        after = project_current_store.read(self.store)
        self.assertTrue(after["project"]["git_markdown_tracking"])
        for key in ("editorial", "published", "rag", "audit"):
            self.assertEqual(after[key], original[key])
        self.assertEqual(project_state.current(self.root), original_project)
        files = {path: path.read_bytes() for path in (repository / "project-markdown").rglob("*.md")}
        self.assertEqual(len(files), 5)
        self.assertFalse(subprocess.run(["git", "-C", str(repository), "diff", "--cached", "--name-only"],
                                        capture_output=True, text=True, check=True).stdout)
        self.assertEqual(client.post("/settings/git-markdown", data=fields, base_url=base).status_code, 400)
        html = client.get("/settings", base_url=base).get_data(as_text=True)
        fields["project_revision"] = re.search(r'name="project_revision" value="([^"]+)"', html).group(1)
        fields["enabled"] = "false"
        self.assertEqual(client.post("/settings/git-markdown", data=fields, base_url=base).status_code, 200)
        self.assertFalse(project_current_store.read(self.store)["project"]["git_markdown_tracking"])
        self.assertEqual(files, {path: path.read_bytes() for path in files})
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        response = client.post("/library/edit", data={
            "token": fields["token"], "doi": source["doi"], "unit_id": unit["id"], "source_id": self.source_id,
            "scope": "page", "snapshot_id": revision, "text": unit["text"] + "\nTracking disabled.",
            "tables": "[]", "source_url": source["url"] or "", "reviewer": "Tester", "reason": "Correct",
            **{key: value or "" for key, value in source["bibliography"].items()},
        }, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(files, {path: path.read_bytes() for path in files})
        self.assertEqual(project_state.current(self.root), original_project)
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}],
            expected=project_current_store.membership_revision(self.store))
        pdf_folder = self.base / "available"
        pdf_folder.mkdir()
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "A new draft with tracking disabled.")
            document.save(pdf_folder / "new.pdf")
        second_app = web_server.create_app(current_store=self.store, project_root=self.root,
                                           pdf_data_root=pdf_folder, profiles={"private_context"},
                                           experimental_markdown_repository=repository, confirm_markdown_rights=True)
        second_client = second_app.test_client()
        html = second_client.get("/library/paper?id=doi%3A10.0000%2Fnew", base_url=base).get_data(as_text=True)
        attach = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "doi": "10.0000/new", "title": "New paper", "pdf": "new.pdf",
                  "confirm_identity": "yes", "return_to": "/library/paper", "skip_extraction": "yes"}
        self.assertEqual(second_client.post("/settings/attach-pdf", data=attach, base_url=base).status_code, 303)
        self.assertEqual(files, {path: path.read_bytes() for path in files})
        self.assertEqual(project_state.current(self.root), original_project)

    def test_git_markdown_enable_recovers_after_interrupted_manifest_or_backfill(self):
        repository = self.base / "enable-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        wrong = self.base / "wrong-repository"
        wrong.mkdir()
        initial = project_current_store.read(self.store)
        atomic = collections.atomic_json
        for failure in ("manifest", "backfill"):
            with self.subTest(failure=failure):
                store = self.base / ("enable-" + failure)
                project_current_store.initialize(initial, store)
                revision = project_current_store.membership_revision(store)
                def write(folder, name, data):
                    if failure == "manifest" and name == "manifest.json":
                        raise OSError("Manifest failure")
                    return atomic(folder, name, data)
                with patch.object(collections, "atomic_json", side_effect=write), \
                        patch.object(project_text, "sync_git_markdown_views",
                                     side_effect=OSError("Backfill failure") if failure == "backfill"
                                     else project_text.sync_git_markdown_views):
                    with self.assertRaises(OSError):
                        project_current_store.set_git_markdown_tracking(
                            store, repository, enabled=True, expected=revision, rights_confirmed=True)
                with self.assertRaisesRegex(ValueError, "Interrupted Git Markdown"):
                    project_current_store.read(store)
                with self.assertRaisesRegex(ValueError, "original Git repository"):
                    project_current_store.recover_git_markdown(store, wrong)
                recovered = project_current_store.recover_git_markdown(store, repository)
                self.assertEqual(recovered, {"recovered": True, "enabled": True, "files": 4})
                current = project_current_store.read(store)
                self.assertTrue(current["project"]["git_markdown_tracking"])
                for key in ("editorial", "published", "rag", "audit"):
                    self.assertEqual(current[key], initial[key])
                self.assertFalse((store / "markdown-transaction.json").exists())

    def test_web_git_markdown_enable_reports_interrupted_backfill(self):
        repository = self.base / "settings-recovery"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"}, experimental_markdown_repository=repository,
                                    confirm_markdown_rights=True)
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/settings", base_url=base).get_data(as_text=True)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "enabled": "true", "confirm_rights": "yes"}
        with patch.object(project_text, "sync_git_markdown_views", side_effect=OSError("Backfill failed")):
            response = client.post("/settings/git-markdown", data=fields, base_url=base)
        self.assertEqual(response.status_code, 503)
        self.assertIn("recover-current-markdown", response.get_data(as_text=True))
        self.assertIn("tracking enabled", response.get_data(as_text=True))
        self.assertTrue(project_current_store.recover_git_markdown(self.store, repository)["enabled"])
        self.assertIn("Disable Git Markdown tracking", client.get(
            "/settings", base_url=base).get_data(as_text=True))

    def test_tracked_store_blocks_uncoordinated_text_changes_but_allows_rag(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking"
        project_current_store.initialize(state, store)
        paper, revision = project_current_store.read_paper(store, self.source_id)
        changed = deepcopy(paper)
        changed["editorial"]["units"][0]["text"] += "\nUncoordinated."
        with self.assertRaisesRegex(ValueError, "tracked edit"):
            project_current_store.replace(store, changed, expected=revision)
        with self.assertRaisesRegex(ValueError, "Adding papers"):
            project_current_store.add_paper(store, editorial=paper["editorial"],
                                            expected=project_current_store.membership_revision(store))
        with self.assertRaisesRegex(ValueError, "Removing papers"):
            project_current_store.remove_paper(store, source_id=self.source_id, revision=revision,
                                               expected=project_current_store.membership_revision(store),
                                               confirmed=True)
        revision = project_current_store.update_rag(store, source_id=self.source_id, revision=revision,
                                                     form={"ranges": ""}, activate=True)["revision"]
        self.assertEqual(project_current_store.read_paper(store, self.source_id)[1], revision)

    def test_tracked_attachment_recovers_at_each_write_boundary(self):
        editorial = self.new_editorial()
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        repository = self.base / "attachment-markdown"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        other = self.base / "other-markdown"
        other.mkdir()
        original_atomic = collections.atomic_json

        for step in ("membership", "paper", "manifest", "membership_cleanup", "markdown"):
            with self.subTest(step=step):
                store = self.base / ("tracked-" + step)
                project_current_store.initialize(state, store)
                revision = project_current_store.membership_revision(store)
                source_id = editorial["source"]["id"]

                def write(folder, name, data):
                    if ((step == "membership" and name == "membership-transaction.json")
                            or (step == "paper" and Path(folder) == store / "papers")
                            or (step == "manifest" and name == "manifest.json")):
                        raise OSError("Interrupted " + step)
                    return original_atomic(folder, name, data)

                with patch.object(collections, "atomic_json", side_effect=write), \
                        patch.object(project_current_store, "_recover_membership",
                                     side_effect=OSError("Interrupted cleanup") if step == "membership_cleanup"
                                     else project_current_store._recover_membership), \
                        patch.object(project_current_store, "sync_git_markdown",
                                     side_effect=OSError("Interrupted Markdown") if step == "markdown"
                                     else project_current_store.sync_git_markdown):
                    with self.assertRaises(OSError):
                        project_current_store.add_paper_with_git_markdown(
                            store, repository, editorial=editorial, expected=revision)
                with self.assertRaisesRegex(ValueError, "Interrupted"):
                    project_current_store.read(store)
                with self.assertRaisesRegex(ValueError, "original Git repository"):
                    project_current_store.recover_git_markdown(store, other)
                result = project_current_store.recover_git_markdown(store, repository)
                committed = step in {"membership_cleanup", "markdown"}
                self.assertEqual(result, {"recovered": True, "attached": committed})
                current = project_current_store.read(store)
                self.assertEqual(current["published"], state["published"])
                self.assertEqual(current["rag"], state["rag"])
                self.assertEqual(current["project"], state["project"])
                self.assertEqual(any(s["id"] == source_id for s in current["editorial"]["bundle"]["sources"]),
                                 committed)
                if committed:
                    paper, _ = project_current_store.read_paper(store, source_id)
                    self.assertIsNone(paper["published"])
                    draft = repository / "project-markdown" / literature_text.slug(editorial["source"]) / "draft.md"
                    self.assertIn(editorial["units"][0]["text"], draft.read_text())
                self.assertFalse((store / "membership-transaction.json").exists())
                self.assertEqual(project_current_store.recover_git_markdown(store, repository), {"recovered": False})

        self.assertFalse(subprocess.run(["git", "-C", str(repository), "diff", "--cached", "--name-only"],
                                        capture_output=True, text=True, check=True).stdout)

    def test_tracked_web_attachment_refreshes_markdown_and_reports_recovery(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        state["project"]["papers"]["10.0000/new"] = {"title": "New paper"}
        state["project"]["papers"]["10.0000/second"] = {"title": "Second new paper"}
        store = self.base / "tracking-attachment"
        project_current_store.initialize(state, store)
        repository = self.base / "attachment-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        pdf_folder = self.base / "available"
        pdf_folder.mkdir()
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Tracked draft text long enough for native extraction.")
            document.save(pdf_folder / "new.pdf")
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Second tracked draft with enough native text.")
            document.save(pdf_folder / "second.pdf")
        baseline = project_state.current(self.root)
        app = web_server.create_app(current_store=store, project_root=self.root, pdf_data_root=pdf_folder,
                                    profiles={"private_context"}, experimental_markdown_repository=repository,
                                    confirm_markdown_rights=True)
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/library/paper?id=doi%3A10.0000%2Fnew", base_url=base).get_data(as_text=True)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "doi": "10.0000/new", "title": "New paper", "pdf": "new.pdf",
                  "confirm_identity": "yes", "return_to": "/library/paper"}
        with patch.object(project_text, "export_git_markdown", side_effect=OSError("Disk failure")):
            response = client.post("/settings/attach-pdf", data=fields, base_url=base)
        self.assertEqual(response.status_code, 503)
        self.assertIn("may already have been attached", response.get_data(as_text=True))
        recovered = project_current_store.recover_git_markdown(store, repository)
        self.assertTrue(recovered["attached"])
        current = project_current_store.read(store)
        source = next(s for s in current["editorial"]["bundle"]["sources"] if s["doi"] == "10.0000/new")
        paper, _ = project_current_store.read_paper(store, source["id"])
        draft = repository / "project-markdown" / literature_text.slug(source) / "draft.md"
        self.assertIn("Tracked draft text", draft.read_text())
        self.assertIsNone(paper["published"])
        self.assertFalse((draft.parent / "published.md").exists())
        self.assertEqual(current["published"], state["published"])
        self.assertEqual(project_state.current(self.root), baseline)
        self.assertEqual(client.get("/library/paper?id=doi%3A10.0000%2Fnew", base_url=base).status_code, 200)
        html = client.get("/library/paper?id=doi%3A10.0000%2Fsecond", base_url=base).get_data(as_text=True)
        fields.update(doi="10.0000/second", title="Second new paper", pdf="second.pdf",
                      project_revision=re.search(r'name="project_revision" value="([^"]+)"', html).group(1))
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            response = client.post("/settings/attach-pdf", data=fields, base_url=base)
        self.assertEqual(response.status_code, 303)
        current = project_current_store.read(store)
        source = next(s for s in current["editorial"]["bundle"]["sources"] if s["doi"] == "10.0000/second")
        draft = repository / "project-markdown" / literature_text.slug(source) / "draft.md"
        self.assertIn("Second tracked draft", draft.read_text())
        self.assertEqual(current["published"], state["published"])
        self.assertFalse((store / "markdown-transaction.json").exists())
        self.assertEqual(project_state.current(self.root), baseline)

    def test_tracked_attachment_rejects_mismatched_recovery_journals(self):
        editorial = self.new_editorial()
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking-conflict"
        project_current_store.initialize(state, store)
        repository = self.base / "conflict-markdown"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        with patch.object(project_current_store, "_recover_membership", side_effect=OSError("Interrupted")):
            with self.assertRaises(OSError):
                project_current_store.add_paper_with_git_markdown(
                    store, repository, editorial=editorial,
                    expected=project_current_store.membership_revision(store))
        membership_path = store / "membership-transaction.json"
        membership = releases.read_json(membership_path.read_bytes())
        changed = deepcopy(membership)
        changed["paper_sha256"] = "0" * 64
        collections.atomic_json(store, membership_path.name, changed)
        with self.assertRaisesRegex(ValueError, "transactions do not match"):
            project_current_store.recover_git_markdown(store, repository)
        self.assertTrue((store / "markdown-transaction.json").exists())
        self.assertEqual(releases.read_json(membership_path.read_bytes()), changed)
        collections.atomic_json(store, membership_path.name, membership)
        self.assertTrue(project_current_store.recover_git_markdown(store, repository)["attached"])

    def test_library_index_uses_current_store_without_mixing_project_state(self):
        baseline = project_index.library_page(self.root)
        with patch.object(project_library, "status", side_effect=AssertionError("Snapshot-backed status read")), \
                patch.object(project_library, "load", side_effect=AssertionError("Snapshot-backed project read")):
            store_page = project_index.library_page(self.root, current_store=self.store)
        self.assertEqual(store_page["total"], baseline["total"])
        self.assertEqual(store_page["core_count"], 2)
        old_index = (self.root / "library-index.sqlite").read_bytes()
        current_index = project_index._index_path(self.root, self.store)
        self.assertTrue(current_index.is_file())
        state = project_current_store.read(self.store)
        project = deepcopy(state["project"])
        removed = "10.0000/two"
        del project["papers"][removed]
        project["papers"]["10.0000/new"] = {"title": "New Core paper"}
        revision = project_current_store.membership_revision(self.store)
        project_current_store.update_project(self.store, project=project, expected=revision)
        listing = project_index.library_page(self.root, current_store=self.store)
        self.assertEqual(listing["total"], 3)
        self.assertEqual(listing["core_count"], 2)
        rows = {row["doi"]: row for row in listing["rows"]}
        self.assertFalse(rows[removed]["core"])
        self.assertEqual(rows[removed]["status"], "Published for retrieval")
        self.assertEqual(rows["10.0000/new"]["status"], "Awaiting PDF")
        detail = project_index.paper_detail(self.root, "doi:" + removed, current_store=self.store)
        self.assertEqual(detail["doi"], removed)
        self.assertEqual(detail["library_groups"], "")
        self.assertEqual((self.root / "library-index.sqlite").read_bytes(), old_index)
        self.assertEqual(project_index.library_page(self.root)["core_count"], 2)
        self.assertEqual(project_index.paper_detail(self.root, "doi:" + removed)["core"], 1)

        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        unit = paper["editorial"]["units"][0]
        source = paper["editorial"]["source"]
        project_current_store.save_draft(
            self.store, revision=revision, source_id=self.source_id, unit_id=unit["id"],
            text=unit["text"] + "\nChanged for Library.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Edit")
        changed = project_index.paper_detail(self.root, "doi:" + source["doi"], current_store=self.store)
        self.assertEqual(changed["status"], "Draft revision (published text remains active)")
        self.assertEqual(project_index.paper_detail(self.root, "doi:" + source["doi"])["status"],
                         "Published for retrieval")
        _, revision = project_current_store.read_paper(self.store, self.source_id)
        project_current_store.update_rag(self.store, source_id=self.source_id, revision=revision,
                                         form={"ranges": ""})
        with patch.object(project_index, "_rebuild", side_effect=AssertionError("Unnecessary index rebuild")):
            self.assertEqual(project_index.paper_detail(
                self.root, "doi:" + source["doi"], current_store=self.store)["status"],
                "Draft revision (published text remains active)")

    def test_current_library_index_uses_saved_discovery_relationships(self):
        run = "a" * 32
        folder = self.root / "discovery" / run
        collections.atomic_json(folder, "job.json", {"status": "complete", "label": "References"})
        collections.atomic_json(folder, "neighborhood.json", {
            "nodes": {"two": {"record": {"title": "Provider title", "identifiers": {"doi": "10.0000/two"},
                                          "authors": ["Example Author"], "publication_date": "2024",
                                          "journal": "Example journal"}}},
            "edges": [{"seed_doi": "10.0000/one", "direction": "background",
                       "target_provider_record": "two"}],
        })
        state = project_current_store.read(self.store)
        project = deepcopy(state["project"])
        del project["papers"]["10.0000/two"]
        project_current_store.update_project(
            self.store, project=project, expected=project_current_store.membership_revision(self.store))
        detail = project_index.paper_detail(self.root, "doi:10.0000/two", current_store=self.store)
        self.assertEqual(detail["status"], "Published for retrieval")
        self.assertEqual(detail["title"], "two")
        self.assertEqual(detail["library_groups"], "References")
        self.assertEqual(detail["citing"][0]["doi"], "10.0000/one")
        self.assertEqual(detail["records"][0]["authors"], ["Example Author"])
        self.assertEqual(project_index.library_page(
            self.root, current_store=self.store, relationship="background")["matching"], 1)

    def test_current_store_discovery_uses_selected_seeds_and_isolates_results(self):
        from tests.test_citation_neighborhood import work

        project, revision = project_current_store.project_view(self.store)
        project["papers"] = {"10.0000/paper-1": {"title": "Current Core paper"}}
        project_current_store.update_project(self.store, project=project, expected=revision)
        original = project_state.current(self.root)
        current = project_current_store.read(self.store)
        existing = self.root / "discovery" / ("a" * 32)
        collections.atomic_json(existing, "job.json", {
            "status": "failed", "error": "Provider unavailable", "label": "Find references",
            "direction": "background", "started_at": "2026-11-10T00:00:00Z"})
        collections.atomic_json(existing, "neighborhood.json", {
            "nodes": {"old": {"record": {"title": "Existing Reference",
                                          "identifiers": {"doi": "10.0000/old"}}}},
            "edges": [{"seed_doi": "10.0000/paper-1", "direction": "background",
                       "target_provider_record": "old"}],
        })
        existing_job = (existing / "job.json").read_bytes()
        existing_graph = (existing / "neighborhood.json").read_bytes()
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/discovery", base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        revision = re.search(r'name="project_revision" value="([^"]+)"', html).group(1)
        self.assertIn("Find references", html)
        self.assertIn("Find citing papers", html)
        fields = {"token": token, "project_revision": revision}
        self.assertEqual(client.post("/discovery", data=fields | {"direction": "background",
                                                                  "project_revision": "stale"},
                                     base_url=base).status_code, 400)
        self.assertEqual(client.post("/discovery", data=fields | {"direction": "impact", "token": ""},
                                     base_url=base).status_code, 403)

        def handler(request):
            if request.url.host == "api.crossref.org":
                identifier = request.url.path.removeprefix("/works/")
                return httpx.Response(200, json={"message": {
                    "DOI": identifier, "reference": [], "reference-count": 0}})
            if "/https://doi.org/" in request.url.path:
                return httpx.Response(200, json=work(int(request.url.path.rsplit("-", 1)[-1])))
            expression = request.url.params["filter"]
            values = ([work(101)] if expression == "cites:W1" else []
                      if expression.startswith("cites:") else
                      [work(int(value[1:])) for value in expression.split(":", 1)[1].split("|")])
            return httpx.Response(200, json={"meta": {"count": len(values)}, "results": values})

        client_class = literature.MetadataClient
        with patch.object(literature, "MetadataClient", side_effect=lambda: client_class(
                transport=httpx.MockTransport(handler), sleep=lambda _: None, clock=lambda: 0)), \
                patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            for direction, discovered in (("background", "10.0000/paper-100"),
                                           ("impact", "10.0000/paper-101")):
                response = client.post("/discovery", data=fields | {"direction": direction}, base_url=base)
                self.assertEqual(response.status_code, 303)
                thread = app.extensions["citation_discovery"]["thread"]
                thread.join(10)
                self.assertFalse(thread.is_alive())
                page = client.get(response.location, base_url=base)
                self.assertEqual(page.status_code, 200)
                self.assertIn("Status: <strong>complete</strong>", page.get_data(as_text=True))
                run = app.extensions["citation_discovery"]["run"]
                folder = self.store / "discovery" / run
                self.assertTrue((folder / "citation-edges.csv").is_file())
                self.assertIn("10.0000/paper-1", (folder / "seeds.csv").read_text())
                self.assertNotIn("10.0000/one", (folder / "seeds.csv").read_text())
                detail = project_index.paper_detail(self.root, "doi:" + discovered, current_store=self.store)
                self.assertIn((direction, "10.0000/paper-1"), detail["relations"])
                self.assertFalse(detail["core"])
        self.assertEqual((existing / "job.json").read_bytes(), existing_job)
        self.assertEqual((existing / "neighborhood.json").read_bytes(), existing_graph)
        self.assertEqual(project_state.current(self.root), original)
        self.assertEqual(project_current_store.read(self.store), current)
        self.assertEqual(project_index.paper_detail(
            self.root, "doi:10.0000/old", current_store=self.store)["title"], "Existing Reference")
        self.assertNotIn("10.0000/paper-100", {row["doi"] for row in project_index.library_page(self.root)["rows"]})
        pdf = self.base / "reference.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Reference awaiting clear text.")
            document.save(pdf)
        added = project_library.attach(
            self.root, "10.0000/paper-100", pdf, identity_confirmed=True, title="Fictional paper 100",
            extract=False, current_store=self.store, membership_revision=revision)
        self.assertIsNone(project_current_store.read_paper(self.store, added["source_id"])[0]["published"])
        self.assertFalse(project_index.paper_detail(
            self.root, "doi:10.0000/paper-100", current_store=self.store)["core"])
        self.assertEqual(project_state.current(self.root), original)

    def test_discovery_errors_and_duplicate_run_ids_fail_explicitly(self):
        project, revision = project_current_store.project_view(self.store)
        project["papers"] = {"10.0000/paper-1": {"title": "Current Core paper"}}
        project_current_store.update_project(self.store, project=project, expected=revision)
        before = project_current_store.read(self.store)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/discovery", base_url=base).get_data(as_text=True)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "direction": "impact"}
        with patch.object(project_library, "discover", side_effect=ValueError("Provider unavailable")):
            response = client.post("/discovery", data=fields, base_url=base)
            thread = app.extensions["citation_discovery"]["thread"]
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertIn("Provider unavailable", client.get(response.location, base_url=base).get_data(as_text=True))
        self.assertEqual(project_current_store.read(self.store), before)
        run = app.extensions["citation_discovery"]["run"]
        collections.atomic_json(self.root / "discovery" / run, "job.json", {
            "status": "failed", "error": "Old result", "direction": "impact",
            "label": "Find citing papers", "started_at": "2026-11-10T00:00:00Z"})
        with self.assertRaisesRegex(ValueError, "Duplicate discovery run"):
            project_index.library_page(self.root, current_store=self.store)
        self.assertEqual(client.get("/discovery", base_url=base).status_code, 400)

    def test_current_store_search_reader_and_api_respect_publication_and_groups(self):
        project, revision = project_current_store.project_view(self.store)
        del project["papers"]["10.0000/one"]
        project_current_store.update_project(self.store, project=project, expected=revision)
        folder = self.store / "discovery" / ("b" * 32)
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "label": "Find references", "direction": "background",
            "started_at": "2026-11-10T00:00:00Z"})
        collections.atomic_json(folder, "neighborhood.json", {
            "nodes": {"one": {"record": {"title": "one", "identifiers": {"doi": "10.0000/one"}}},
                      "two": {"record": {"title": "two", "identifiers": {"doi": "10.0000/two"}}}},
            "edges": [{"seed_doi": "10.0000/two", "direction": "background", "target_provider_record": "one"},
                      {"seed_doi": "10.0000/two", "direction": "impact", "target_provider_record": "two"}],
        })
        state = project_current_store.read(self.store)
        source = next(item for item in state["editorial"]["bundle"]["sources"] if item["doi"] == "10.0000/one")
        paper, revision = project_current_store.read_paper(self.store, source["id"])
        unit = paper["editorial"]["units"][0]
        project_current_store.save_draft(
            self.store, revision=revision, source_id=source["id"], unit_id=unit["id"],
            text=unit["text"] + "\nPrivateunpublishedword.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Edit")
        original = project_state.current(self.root)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        all_results = client.get("/search?query=fictional", base_url=base).get_data(as_text=True)
        self.assertIn("10.0000/one", all_results)
        self.assertIn("10.0000/two", all_results)
        core = client.get("/search?query=fictional&relationship=core", base_url=base).get_data(as_text=True)
        self.assertIn("10.0000/two", core)
        self.assertNotIn("10.0000/one", core)
        refs = client.get("/search?query=fictional&relationship=background", base_url=base).get_data(as_text=True)
        self.assertIn("10.0000/one", refs)
        self.assertNotIn("10.0000/two", refs)
        citing = client.get("/search?query=fictional&relationship=impact", base_url=base).get_data(as_text=True)
        self.assertIn("10.0000/two", citing)
        self.assertNotIn("10.0000/one", citing)
        self.assertIn("No matching retained passages", client.get(
            "/search?query=privateunpublishedword", base_url=base).get_data(as_text=True))
        api = client.get("/api/search?collection=core&query=fictional", base_url=base)
        self.assertEqual(api.status_code, 200)
        self.assertEqual(len(api.json["result"]["items"]), 2)
        reader = unescape(re.search(r'href="([^"]+)"[^>]*>Read source context', refs).group(1))
        read = client.get(reader, base_url=base)
        self.assertEqual(read.status_code, 200)
        text = read.get_data(as_text=True)
        self.assertIn("Fictional research paper", text)
        self.assertIn("Citation and current link", text)
        self.assertNotIn("Privateunpublishedword", text)
        document = unescape(re.search(r'href="([^"]+)"[^>]*>Read whole document', text).group(1))
        self.assertIn("Fictional research paper", client.get(document, base_url=base).get_data(as_text=True))
        self.assertEqual(client.get("/reader.js", base_url=base).status_code, 200)
        self.assertEqual(client.get("/api/search?collection=unknown&query=fictional", base_url=base).status_code, 403)
        paper, revision = project_current_store.read_paper(self.store, source["id"])
        revision = project_current_store.unpublish(
            self.store, source_id=source["id"], revision=revision,
            reviewer="Tester", reason="Withdraw", checked=True)["revision"]
        stale = client.get(reader, base_url=base)
        self.assertEqual(stale.status_code, 410)
        self.assertIn("Old reader links are not retained", stale.get_data(as_text=True))
        self.assertNotIn("restore the requested snapshot", stale.get_data(as_text=True))
        self.assertEqual(client.get("/api/search", query_string={
            "collection": "core", "query": "fictional",
            "snapshot_id": api.json["collection_context"]["snapshot_id"]}, base_url=base).status_code, 410)
        paper, revision = project_current_store.read_paper(self.store, source["id"])
        project_current_store.publish(self.store, source_id=source["id"], revision=revision,
                                      reviewer="Tester", reason="Publish", checked=True)
        published = client.get("/api/search?collection=core&query=privateunpublishedword", base_url=base)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(len(published.json["result"]["items"]), 1)
        self.assertIn("10.0000/one", client.get(
            "/search?query=privateunpublishedword&relationship=background", base_url=base).get_data(as_text=True))
        self.assertEqual(project_state.current(self.root), original)

    def test_current_store_sources_show_only_published_papers_and_pinned_reader(self):
        state = project_current_store.read(self.store)
        source = next(item for item in state["editorial"]["bundle"]["sources"] if item["doi"] == "10.0000/one")
        paper, revision = project_current_store.read_paper(self.store, source["id"])
        revision = project_current_store.unpublish(
            self.store, source_id=source["id"], revision=revision,
            reviewer="Tester", reason="Withdraw", checked=True)["revision"]
        before = project_state.current(self.root)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    pdf_data_root=self.base / "available", profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        response = client.get("/sources", base_url=base)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("10.0000/one", html)
        self.assertIn("10.0000/two", html)
        self.assertIn("Sources available to AI", html)
        self.assertIn("View and correct extracted data", html)
        link = unescape(re.search(r'href="([^"]+)"[^>]*>Read retained text', html).group(1))
        read = client.get(link, base_url=base)
        self.assertEqual(read.status_code, 200)
        self.assertIn("Fictional research paper two", read.get_data(as_text=True))
        self.assertIn("No matching sources", client.get(
            "/sources?paper=10.0000/one", base_url=base).get_data(as_text=True))
        self.assertEqual(client.get("/sources?collection=unknown", base_url=base).status_code, 403)
        self.assertEqual(client.get("/sources?snapshot_id=" + "0" * 64, base_url=base).status_code, 410)
        project_current_store.publish(self.store, source_id=source["id"], revision=revision,
                                      reviewer="Tester", reason="Restore", checked=True)
        self.assertEqual(client.get(link, base_url=base).status_code, 410)
        self.assertIn("10.0000/one", client.get("/sources", base_url=base).get_data(as_text=True))
        self.assertEqual(project_state.current(self.root), before)

    def test_current_store_openalex_settings_use_shared_private_key(self):
        baseline = project_current_store.read(self.store)
        original = project_state.current(self.root)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        settings = client.get("/settings", base_url=base).get_data(as_text=True)
        self.assertIn("shared with the normal project", settings)
        token = re.search(r'action="/settings/openalex-key">\s*<input type="hidden" name="token" value="([^"]+)"',
                          settings).group(1)
        key = "synthetic-private-key"
        self.assertEqual(client.post("/settings/openalex-key", data={"api_key": key}, base_url=base).status_code, 403)
        self.assertEqual(client.post("/settings/openalex-key", data={
            "token": token, "api_key": key}, base_url=base).status_code, 303)
        path = project_library.openalex_key_path(self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(project_library.metadata_client_options(self.root), {"openalex_api_key": key})
        self.assertNotIn(key, client.get("/settings", base_url=base).get_data(as_text=True))
        self.assertEqual(client.post("/settings/openalex-key", data={
            "token": token, "api_key": "bad key"}, base_url=base).status_code, 400)
        self.assertEqual(project_library.metadata_client_options(self.root), {"openalex_api_key": key})
        self.assertEqual(client.post("/settings/openalex-key", data={
            "token": token, "action": "remove"}, base_url=base).status_code, 303)
        self.assertFalse(path.exists())
        self.assertEqual(project_current_store.read(self.store), baseline)
        self.assertEqual(project_state.current(self.root), original)

    def test_current_store_reads_existing_catalog_without_changing_normal_project(self):
        from tests.test_citation_neighborhood import work

        project_library.register(self.root, [{"doi": "10.0000/paper-1", "title": "Synthetic seed"}])
        folder = self.root / "discovery" / ("a" * 32)

        def handler(request):
            if request.url.host == "api.crossref.org":
                return httpx.Response(200, json={"message": {
                    "DOI": request.url.path.removeprefix("/works/"), "reference": [], "reference-count": 0}})
            if "/https://doi.org/" in request.url.path:
                doi = request.url.path.removeprefix("/works/https://doi.org/")
                result = work({"10.0000/one": 1, "10.0000/two": 2, "10.0000/paper-1": 3}[doi])
                result["doi"] = "https://doi.org/" + doi
                result["referenced_works"] = [f"https://openalex.org/W{number}" for number in range(100, 107)]
                return httpx.Response(200, json=result)
            expression = request.url.params["filter"]
            values = [work(int(value[1:])) for value in expression.split(":", 1)[1].split("|")]
            return httpx.Response(200, json={"meta": {"count": len(values)}, "results": values})

        client_class = literature.MetadataClient
        with patch.object(literature, "MetadataClient", side_effect=lambda: client_class(
                transport=httpx.MockTransport(handler), sleep=lambda _: None, clock=lambda: 0)):
            project_library.discover(self.root, folder, directions=["background"])
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "direction": "background", "label": "Synthetic search",
            "started_at": "2026-11-10T12:00:00Z"})
        project_library.configure_metadata(self.root)
        baseline = project_state.current(self.root)
        active = (self.root / "registry" / "active.json").read_bytes()
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        metadata = client.get("/publications?query=Fictional", base_url=base)
        self.assertEqual(metadata.status_code, 200)
        self.assertIn("Fictional paper", metadata.get_data(as_text=True))
        self.assertIn("used read-only", metadata.get_data(as_text=True))
        self.assertIn("not included", metadata.get_data(as_text=True))
        coverage = client.get("/coverage", base_url=base)
        self.assertEqual(coverage.status_code, 200)
        self.assertIn("2 of 2 core papers", coverage.get_data(as_text=True))
        core = client.get("/publications?collection=core", base_url=base)
        self.assertEqual(core.status_code, 200)
        self.assertEqual(client.get("/publications?collection=unknown", base_url=base).status_code, 403)
        self.assertEqual(client.get("/coverage?snapshot_id=" + "0" * 64, base_url=base).status_code, 410)
        pinned = re.search(r'snapshot_id=([0-9a-f]{64})', metadata.get_data(as_text=True))
        self.assertIsNotNone(pinned)
        source = project_current_store.read(self.store)["published"]["bundle"]["sources"][0]
        _, revision = project_current_store.read_paper(self.store, source["id"])
        project_current_store.unpublish(self.store, source_id=source["id"], revision=revision,
                                        reviewer="Tester", reason="Withdraw", checked=True)
        self.assertEqual(client.get("/publications?snapshot_id=" + pinned.group(1), base_url=base).status_code, 410)
        project, revision = project_current_store.project_view(self.store)
        del project["papers"]["10.0000/two"]
        project_current_store.update_project(self.store, project=project, expected=revision)
        self.assertIn("1 of 1 core papers", client.get("/coverage", base_url=base).get_data(as_text=True))
        self.assertEqual((self.root / "registry" / "active.json").read_bytes(), active)
        self.assertEqual(project_state.current(self.root), baseline)
        self.assertEqual(client.post("/settings/configure-metadata", base_url=base).status_code, 403)
        metadata = client.get("/publications?query=Fictional", base_url=base).get_data(as_text=True)
        pinned = re.search(r'snapshot_id=([0-9a-f]{64})', metadata).group(1)
        registry = project_library.store(self.root)
        document = deepcopy(registry.current().document)
        next(entry for entry in document["collections"] if entry["id"] == "metadata")["title"] += " updated"
        registry.publish(document)
        self.assertEqual(client.get("/publications?snapshot_id=" + pinned, base_url=base).status_code, 410)

    def test_current_store_metadata_empty_state_does_not_read_old_evidence(self):
        baseline = project_state.current(self.root)
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        for path in ("/publications", "/coverage"):
            response = client.get(path, base_url=base)
            self.assertEqual(response.status_code, 200)
            self.assertIn("No metadata catalog is configured", response.get_data(as_text=True))
            self.assertIn("New current-store discovery searches are not included", response.get_data(as_text=True))
        self.assertEqual(project_state.current(self.root), baseline)

    def test_current_store_builds_metadata_from_both_search_directories(self):
        from tests.test_citation_neighborhood import work
        project, revision = project_current_store.project_view(self.store)
        project["papers"] = {"10.0000/paper-1": {"title": "Synthetic seed"}}
        revision = project_current_store.update_project(self.store, project=project, expected=revision)["revision"]
        baseline = project_state.current(self.root)
        paper_state = project_current_store.read(self.store)
        empty_catalog_endpoint = project_current_store.CurrentCollectionStore(
            self.store, allowed_profiles={"private_context"}, allow_metadata=True)
        self.assertEqual(set(empty_catalog_endpoint.current().snapshots), {"core"})

        def handler(request):
            if request.url.host == "api.crossref.org":
                return httpx.Response(200, json={"message": {
                    "DOI": request.url.path.removeprefix("/works/"), "reference": [], "reference-count": 0}})
            if "/https://doi.org/" in request.url.path:
                result = work(1)
                result["referenced_works"] = [f"https://openalex.org/W{number}" for number in range(100, 107)]
                return httpx.Response(200, json=result)
            expression = request.url.params["filter"]
            values = ([work(101)] if expression == "cites:W1" else
                      [work(int(value[1:])) for value in expression.split(":", 1)[1].split("|")])
            return httpx.Response(200, json={"meta": {"count": len(values)}, "results": values})

        client_class = literature.MetadataClient
        with patch.object(literature, "MetadataClient", side_effect=lambda: client_class(
                transport=httpx.MockTransport(handler), sleep=lambda _: None, clock=lambda: 0)):
            for root, name, direction in ((self.root, "a" * 32, "background"),
                                          (self.store, "b" * 32, "impact")):
                folder = root / "discovery" / name
                report = project_library.discover(self.root, folder, directions=[direction],
                    current_store=self.store, membership_revision=revision)
                collections.atomic_json(folder, "job.json", {
                    "status": "complete", "direction": direction, "label": "Synthetic search",
                    "started_at": "2026-11-10T12:00:00Z", "report": report})
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        page = client.get("/discovery", base_url=base).get_data(as_text=True)
        self.assertIn("Configure/update metadata catalog", page)
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            response = client.post("/settings/configure-metadata", data={"token": token}, base_url=base)
            self.assertEqual(response.status_code, 303)
            worker = app.extensions["metadata_update"]["thread"]
            worker.join(15)
            self.assertFalse(worker.is_alive())
        job = releases.read_json((self.store / "metadata-job.json").read_bytes())
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["report"]["searches"], 2)
        self.assertFalse((self.root / "metadata-job.json").exists())
        self.assertEqual(client.get("/settings/metadata-progress", base_url=base).status_code, 200)
        page = client.get("/publications?query=Fictional", base_url=base).get_data(as_text=True)
        self.assertIn("current-store catalog includes completed searches", page)
        self.assertIn("Fictional paper 100", page)
        self.assertIn("Fictional paper 101", page)
        coverage = client.get("/coverage", base_url=base)
        self.assertEqual(coverage.status_code, 200)
        self.assertIn("current-store catalog includes completed searches", coverage.get_data(as_text=True))
        self.assertEqual(project_state.current(self.root), baseline)
        endpoint = project_current_store.CurrentCollectionStore(
            self.store, allowed_profiles={"private_context"}, allow_metadata=True)
        website = web_server.create_app(current_store=self.store, project_root=self.root,
                                        profiles={"private_context"}, allow_current_metadata=True).test_client()
        inventory = website.get("/sources", base_url=base)
        self.assertEqual(inventory.status_code, 200)
        self.assertNotIn('value="metadata"', inventory.get_data(as_text=True))
        self.assertEqual(website.get("/sources?collection=metadata", base_url=base).status_code, 403)

        async def exercise(client):
            listing = await client.call_tool("list_collections", {})
            self.assertFalse(listing.isError)
            items = listing.structuredContent["result"]["items"]
            self.assertEqual({item["id"] for item in items}, {"core", "metadata"})
            self.assertEqual([item["id"] for item in items if item["default"]], ["core"])
            snapshot_id = listing.structuredContent["collection_context"]["snapshot_id"]
            search = await client.call_tool("search_evidence", {"query": "fictional"})
            self.assertEqual(search.structuredContent["collection_context"]["snapshot_id"], snapshot_id)
            self.assertTrue(search.structuredContent["result"]["items"])
            default = await client.call_tool("list_publications", {})
            self.assertEqual(default.structuredContent["result"]["items"], [])
            publications = await client.call_tool("list_publications", {
                "collections": ["metadata"], "query": "Fictional", "snapshot_id": snapshot_id})
            self.assertFalse(publications.isError)
            identifier = publications.structuredContent["result"]["items"][0]["id"]
            record = await client.call_tool("get_publication", {
                "collections": ["metadata"], "publication_id": identifier, "snapshot_id": snapshot_id})
            self.assertFalse(record.isError)
            self.assertEqual(record.structuredContent["result"]["id"], identifier)
            status = await client.call_tool("catalog_status", {
                "collections": ["metadata"], "snapshot_id": snapshot_id})
            self.assertTrue(status.structuredContent["result"]["items"])
            unit = search.structuredContent["result"]["items"][0]
            reader = website.get("/read?" + web_server.urlencode({
                "snapshot_id": snapshot_id, "collection": "core", "unit_id": unit["id"],
                "source_id": unit["citation"]["source_id"]}), base_url=base)
            self.assertEqual(reader.status_code, 200)
            return snapshot_id

        async def check_stdio():
            params = StdioServerParameters(command=sys.executable, args=[
                "-m", "research_library.server", "--current-state", str(self.store),
                "--allow-profile", "private_context", "--allow-current-metadata"])
            with tempfile.TemporaryFile(mode="w+") as diagnostics:
                async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                        await client.initialize()
                        return await exercise(client)

        self.assertEqual(asyncio.run(check_stdio()), endpoint.current().release_id)
        with running_server(self.base, None, profiles=("private_context",), current_state=self.store,
                            extra=("--allow-current-metadata",)) as (url, token, _, _, _):
            async def check_http():
                async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                    async with streamable_http_client(url, http_client=http) as (reader, writer, _):
                        async with ClientSession(reader, writer) as client:
                            await client.initialize()
                            return await exercise(client)
            self.assertEqual(asyncio.run(check_http()), endpoint.current().release_id)
        disabled = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        self.assertEqual(set(disabled.current().snapshots), {"core"})
        disabled_id = disabled.current().release_id
        with self.assertRaises(releases.AccessDeniedError):
            disabled.current().call("list_publications", {"collections": ["metadata"]})
        enabled_page = website.get("/connect", base_url=base).get_data(as_text=True)
        self.assertIn("CURRENT_METADATA=1", enabled_page)
        pointer = (self.store / "metadata-current.json").read_bytes()
        atomic = collections.atomic_json
        def fail_pointer(root, name, data):
            if name == "metadata-current.json":
                raise OSError("Pointer write failed")
            return atomic(root, name, data)
        with patch.object(collections, "atomic_json", side_effect=fail_pointer):
            with self.assertRaises(OSError):
                project_library.configure_metadata(self.root, current_store=self.store)
        self.assertEqual((self.store / "metadata-current.json").read_bytes(), pointer)
        self.assertEqual(client.get("/publications?query=Fictional", base_url=base).status_code, 200)
        publish = citation_neighborhood.publish_metadata
        def mutate_during_build(*args, **kwargs):
            report = publish(*args, **kwargs)
            job_path = self.store / "discovery" / ("b" * 32) / "job.json"
            job = releases.read_json(job_path.read_bytes())
            job["started_at"] = "2026-11-11T12:00:00Z"
            collections.atomic_json(job_path.parent, job_path.name, job)
            return report
        with patch.object(citation_neighborhood, "publish_metadata", side_effect=mutate_during_build):
            with self.assertRaisesRegex(ValueError, "Discovery data changed"):
                project_library.configure_metadata(self.root, current_store=self.store)
        self.assertEqual((self.store / "metadata-current.json").read_bytes(), pointer)
        metadata = client.get("/publications?query=Fictional", base_url=base).get_data(as_text=True)
        pinned = re.search(r'snapshot_id=([0-9a-f]{64})', metadata)
        self.assertIsNotNone(pinned)
        entry = releases.read_json(pointer)
        entry["title"] += " updated"
        collections.atomic_json(self.store, "metadata-current.json", entry)
        self.assertEqual(client.get("/publications?snapshot_id=" + pinned.group(1),
                                    base_url=base).status_code, 410)
        with self.assertRaises(collections.SnapshotUnavailable):
            endpoint.load(pinned.group(1))
        self.assertNotEqual(endpoint.current().release_id, pinned.group(1))
        self.assertEqual(disabled.current().release_id, disabled_id)
        self.assertEqual(website.get("/search?query=fictional&snapshot_id=" + pinned.group(1),
                                     base_url=base).status_code, 410)
        catalog_file = self.store / "catalogs" / (entry["release_id"] + ".sqlite")
        original_bytes = catalog_file.read_bytes()
        catalog_file.write_bytes(b"corrupt")
        with self.assertRaises(releases.ReleaseError):
            endpoint.current()
        self.assertEqual(set(disabled.current().snapshots), {"core"})
        catalog_file.write_bytes(original_bytes)
        self.assertIn("metadata", endpoint.current().snapshots)
        (self.store / "metadata-current.json").write_text("{invalid", encoding="utf-8")
        with self.assertRaises(releases.ReleaseError):
            endpoint.current()
        self.assertEqual(set(disabled.current().snapshots), {"core"})
        collections.atomic_json(self.store, "metadata-current.json", entry)
        self.assertIn("metadata", endpoint.current().snapshots)
        self.assertEqual(project_current_store.read(self.store), paper_state)
        self.assertEqual(project_state.current(self.root), baseline)

    def test_current_store_connect_page_and_make_commands_select_current_store(self):
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    profiles={"private_context"})
        page = app.test_client().get("/connect", base_url="http://localhost:8080")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("CURRENT_STATE=" + str(self.store), html)
        self.assertNotIn("COLLECTION_REGISTRY=", html)
        self.assertIn("--collection core", html)
        self.assertIn("This current-state endpoint does not authorize a metadata catalog", html)
        self.assertIn("old pinned requests become unavailable", html)
        for target in ("run", "mcp"):
            result = subprocess.run(["make", "-n", target, "CURRENT_STATE=" + str(self.store)],
                                    capture_output=True, text=True, check=True)
            self.assertIn('--current-state "' + str(self.store) + '"', result.stdout)
            self.assertNotIn("--allow-collection", result.stdout)
            self.assertNotIn('--registry "' + str(self.root / "registry") + '"', result.stdout)
            enabled = subprocess.run(["make", "-n", target, "CURRENT_STATE=" + str(self.store),
                                      "CURRENT_METADATA=1"], capture_output=True, text=True, check=True)
            self.assertIn("--allow-current-metadata", enabled.stdout)
        for target in ("web", "web-off"):
            result = subprocess.run(["make", "-n", target, "CURRENT_STATE=" + str(self.store)],
                                    capture_output=True, text=True, check=True)
            self.assertIn('--current-state "' + str(self.store) + '"', result.stdout)
            self.assertNotIn('--registry "', result.stdout)
            enabled = subprocess.run(["make", "-n", target, "CURRENT_STATE=" + str(self.store),
                                      "CURRENT_METADATA=1"], capture_output=True, text=True, check=True)
            self.assertIn("--allow-current-metadata", enabled.stdout)
        for value in ("1", "invalid"):
            result = subprocess.run(["make", "-n", "run", "CURRENT_STATE=", "CURRENT_METADATA=" + value],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CURRENT_METADATA", result.stderr)

    def test_detached_current_store_website_requires_matching_store_to_stop(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        args = Namespace(
            state_dir=self.base / "web-state", project_root=self.root,
            registry=None, current_state=self.store, pdf_data_root=self.base / "available",
            port=port, mcp_url="http://127.0.0.1:8000/mcp",
            allow_profile=["private_context"], allow_collection=["core"], default_collection=["core"],
            experimental_markdown_repository=None, confirm_markdown_rights=False, allow_current_metadata=False)
        wrong = Namespace(**vars(args))
        wrong.current_state = None
        wrong.registry = self.root / "registry"
        children = []
        spawn = web_lifecycle.subprocess.Popen

        def tracked_spawn(*popen_args, **kwargs):
            child = spawn(*popen_args, **kwargs)
            children.append(child)
            return child

        try:
            with patch.object(web_lifecycle.subprocess, "Popen", side_effect=tracked_spawn):
                self.assertEqual(web_lifecycle.start(args), 0)
            pid = web_lifecycle.read_pid(args.state_dir / "web.pid")
            with urlopen(f"http://127.0.0.1:{port}/sources", timeout=10) as response:
                self.assertIn("10.0000/one", response.read().decode())
            self.assertEqual(web_lifecycle.start(args), 0)
            with self.assertRaisesRegex(ValueError, "refusing to replace"):
                web_lifecycle.start(wrong)
            with self.assertRaisesRegex(ValueError, "refusing to stop"):
                web_lifecycle.stop(wrong)
            different_project = Namespace(**vars(args))
            different_project.project_root = self.base / "other-project"
            with self.assertRaisesRegex(ValueError, "refusing to replace"):
                web_lifecycle.start(different_project)
            different_markdown = Namespace(**vars(args))
            different_markdown.experimental_markdown_repository = self.base / "other-repo"
            different_markdown.confirm_markdown_rights = True
            with self.assertRaisesRegex(ValueError, "refusing to replace"):
                web_lifecycle.start(different_markdown)
            different_metadata = Namespace(**vars(args))
            different_metadata.allow_current_metadata = True
            with self.assertRaisesRegex(ValueError, "refusing to replace"):
                web_lifecycle.start(different_metadata)
            self.assertTrue(web_lifecycle.process_running(pid))
            self.assertEqual(web_lifecycle.read_pid(args.state_dir / "web.pid"), pid)
        finally:
            try:
                web_lifecycle.stop(args)
            finally:
                for child in children:
                    child.wait(timeout=5)

    def test_current_store_mcp_and_web_reader_agree(self):
        class QuietHandler(WSGIRequestHandler):
            def log(self, kind, message, *args):
                pass

        with running_server(self.base, None, profiles=("private_context",), current_state=self.store) as (
                url, token, _, _, _):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            app = web_server.create_app(current_store=self.store, project_root=self.root,
                                        profiles={"private_context"}, port=port, mcp_url=url)
            web = make_server("127.0.0.1", port, app, request_handler=QuietHandler, threaded=True)
            thread = threading.Thread(target=web.serve_forever, daemon=True)
            thread.start()
            try:
                result = asyncio.run(connection_check.check(
                    url, token, f"http://127.0.0.1:{port}", "fictional", collection="core"))
                self.assertTrue(result["passed"])
                self.assertTrue(result["search_context_reader_agree"])
                self.assertEqual(result["collection"], "core")
            finally:
                web.shutdown()
                web.server_close()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

    def test_current_store_mcp_and_web_reader_agree_for_activated_chunk(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        published = paper["published"]
        blocks = passage_context.document_blocks(published["units"],
                                                   title=published["source"]["bibliography"]["title"])
        project_current_store.update_rag(
            self.store, source_id=self.source_id, revision=revision, activate=True,
            form={"ranges": f"1-{len(blocks)}", "labels": "1: chunkreaderunique"})

        class QuietHandler(WSGIRequestHandler):
            def log(self, kind, message, *args):
                pass

        with running_server(self.base, None, profiles=("private_context",), current_state=self.store) as (
                url, token, _, _, _):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            app = web_server.create_app(current_store=self.store, project_root=self.root,
                                        profiles={"private_context"}, port=port, mcp_url=url)
            web = make_server("127.0.0.1", port, app, request_handler=QuietHandler, threaded=True)
            thread = threading.Thread(target=web.serve_forever, daemon=True)
            thread.start()
            try:
                result = asyncio.run(connection_check.check(
                    url, token, f"http://127.0.0.1:{port}", "chunkreaderunique", collection="core"))
                self.assertTrue(result["passed"])
                self.assertTrue(result["search_context_reader_agree"])
            finally:
                web.shutdown()
                web.server_close()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

    def test_library_index_rejects_changes_during_current_state_rebuild(self):
        project_index.library_page(self.root, current_store=self.store)
        index = project_index._index_path(self.root, self.store)
        before = index.read_bytes()
        state = project_current_store.read(self.store)
        project = deepcopy(state["project"])
        project["papers"]["10.0000/new"] = {"title": "New Core paper"}
        project_current_store.update_project(
            self.store, project=project, expected=project_current_store.membership_revision(self.store))
        rebuild = project_index._rebuild

        def mutate_after_rebuild(*args, **kwargs):
            rebuild(*args, **kwargs)
            changed = deepcopy(project)
            changed["papers"]["10.0000/later"] = {"title": "Later Core paper"}
            project_current_store.update_project(
                self.store, project=changed, expected=project_current_store.membership_revision(self.store))

        with patch.object(project_index, "_rebuild", side_effect=mutate_after_rebuild):
            with self.assertRaisesRegex(ValueError, "changed while indexing"):
                project_index.library_page(self.root, current_store=self.store)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(project_index.library_page(self.root, current_store=self.store)["core_count"], 4)

    def test_current_store_library_uses_full_filters_sorting_and_pagination(self):
        baseline = project_state.current(self.root)
        project, revision = project_current_store.project_view(self.store)
        del project["papers"]["10.0000/two"]
        project_current_store.update_project(self.store, project=project, expected=revision)
        project_current_store.register_core_papers(
            self.store, [{"doi": f"10.0000/new-{number:02}", "title": f"New paper {number:02}"}
                         for number in range(12)],
            expected=project_current_store.membership_revision(self.store))
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        with patch.object(project_library, "status", side_effect=AssertionError("Snapshot-backed status read")), \
                patch.object(project_library, "load", side_effect=AssertionError("Snapshot-backed project read")), \
                patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            response = client.get("/library?relationship=core&status=awaiting&sort=title&order=desc&limit=10",
                                  base_url=base)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("12 matching records", html)
            self.assertIn("Page 1 of 2", html)
            self.assertLess(html.index("New paper 11"), html.index("New paper 10"))
            self.assertNotIn("New paper 00", html)
            self.assertIn("#batch-pdfs", html)
            self.assertIn("column-filters", html)
            second = client.get("/library?relationship=core&status=awaiting&sort=title&order=desc&limit=10&page=2",
                                base_url=base)
            self.assertEqual(second.status_code, 200)
            self.assertIn("New paper 00", second.get_data(as_text=True))
            published = client.get("/library?query=10.0000%2Ftwo&status=published", base_url=base)
            self.assertEqual(published.status_code, 200)
            self.assertIn("Published for retrieval", published.get_data(as_text=True))
            self.assertIn("Open local PDF", published.get_data(as_text=True))
            core = client.get("/library?query=10.0000%2Ftwo&relationship=core", base_url=base)
            self.assertIn("0 matching records", core.get_data(as_text=True))
        self.assertEqual(project_state.current(self.root), baseline)

    def test_batch_web_locates_and_attaches_to_current_store_only(self):
        baseline = project_state.current(self.root)
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/batch", "title": "Batch synthetic paper"}],
            expected=project_current_store.membership_revision(self.store))
        pdf_root = self.base / "available"
        pdf_root.mkdir()
        pdf = pdf_root / "batch.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text(
                (50, 50), "Batch synthetic paper DOI 10.0000/batch. Fictional text for extraction and testing.")
            document.save(pdf)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    pdf_data_root=pdf_root, profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/discovery?query=10.0000%2Fbatch", base_url=base).get_data(as_text=True)
        self.assertIn('id="batch-pdfs"', html)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        self.assertEqual(client.get("/library.js", base_url=base).status_code, 200)
        response = client.post("/library/batch", data={
            "token": token, "action": "all", "query": "10.0000/batch", "return_to": "/discovery"
        }, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(list(project_batch.load(self.store)["items"]), ["10.0000/batch"])
        self.assertFalse((self.root / "batch-pdf.json").exists())

        def wait_for_batch():
            worker = app.extensions["batch_pdf"]["thread"]
            worker.join(15)
            self.assertFalse(worker.is_alive(), "Batch worker did not finish")
            return project_batch.load(self.store)

        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")), \
                patch.object(literature.MetadataClient, "get", side_effect=AssertionError("Unexpected network")):
            self.assertEqual(client.post("/library/batch", data={
                "token": token, "action": "locate"
            }, base_url=base).status_code, 303)
            job = wait_for_batch()
            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["items"]["10.0000/batch"]["status"], "local_found")
            self.assertEqual(client.get("/library/batch/confirm", base_url=base).status_code, 200)
            self.assertEqual(client.post("/library/batch", data={
                "token": token, "action": "accept", "doi": "10.0000/batch",
                "file:10.0000/batch": pdf.name,
            }, base_url=base).status_code, 303)
            self.assertEqual(client.post("/library/batch", data={
                "token": token, "action": "extract-accepted"
            }, base_url=base).status_code, 303)
            job = wait_for_batch()
            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["items"]["10.0000/batch"]["status"], "extracted")
        current = project_current_store.read(self.store)
        self.assertEqual(current["published"], baseline["published"])
        self.assertIn("10.0000/batch", [item["doi"] for item in current["editorial"]["bundle"]["sources"]])
        self.assertEqual(project_state.current(self.root), baseline)

    def test_batch_current_store_stops_for_membership_recovery(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/batch", "title": "Batch synthetic paper"}],
            expected=project_current_store.membership_revision(self.store))
        pdf_root = self.base / "available"
        pdf_root.mkdir()
        pdf = pdf_root / "batch.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Batch synthetic paper with sufficient fictional text.")
            document.save(pdf)
        project_batch.select(self.store, [{
            "doi": "10.0000/batch", "title": "Batch synthetic paper", "status": "Awaiting PDF"
        }], True)
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       pdf_data_root=pdf_root, profiles={"private_context"}).test_client()
        job = project_batch.prepare_extract(self.store, pdf_root, [("10.0000/batch", pdf.name)])
        with patch.object(project_current_store, "_recover_membership", side_effect=OSError("interrupted")):
            project_batch.run(self.root, pdf_root, job, threading.Event(), current_store=self.store)
        saved = project_batch.load(self.store)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["processed"], 0)
        self.assertTrue((self.store / "membership-transaction.json").exists())
        response = client.get("/library/batch", base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 503)
        self.assertIn("recover-current-store", response.get_data(as_text=True))
        self.assertTrue(project_current_store.recover_membership(self.store))
        with patch.object(project_library, "attach", side_effect=AssertionError("Duplicate attachment")):
            project_batch.run(self.root, pdf_root, job, threading.Event(), current_store=self.store)
        self.assertEqual(project_batch.load(self.store)["items"]["10.0000/batch"]["status"], "extracted")

    def test_batch_current_store_rejects_changed_confirmation_and_stale_title(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/batch", "title": "Batch synthetic paper"}],
            expected=project_current_store.membership_revision(self.store))
        pdf_root = self.base / "available"
        pdf_root.mkdir()
        pdf = pdf_root / "batch.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Batch synthetic paper with sufficient fictional text.")
            document.save(pdf)
        row = {"doi": "10.0000/batch", "title": "Batch synthetic paper", "status": "Awaiting PDF"}
        project_batch.select(self.store, [row], True)
        job = project_batch.prepare_extract(self.store, pdf_root, [(row["doi"], pdf.name)])
        pdf.write_bytes(pdf.read_bytes() + b"\n")
        project_batch.run(self.root, pdf_root, job, threading.Event(), current_store=self.store)
        self.assertIn("changed after confirmation",
                      project_batch.load(self.store)["items"][row["doi"]]["error"])
        project, revision = project_current_store.project_view(self.store)
        project["papers"][row["doi"]]["title"] = "Changed paper title"
        project_current_store.update_project(self.store, project=project, expected=revision)
        job = project_batch.prepare_extract(self.store, pdf_root, [(row["doi"], pdf.name)])
        project_batch.run(self.root, pdf_root, job, threading.Event(), current_store=self.store)
        item = project_batch.load(self.store)["items"][row["doi"]]
        self.assertEqual(item["status"], "extraction_failed")
        self.assertIn("title changed", item["error"])
        self.assertNotIn(row["doi"], [source["doi"] for source in
                                     project_current_store.read(self.store)["editorial"]["bundle"]["sources"]])

    def test_batch_current_store_recovers_tracked_markdown_attachment(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/batch", "title": "Batch synthetic paper"}],
            expected=project_current_store.membership_revision(self.store))
        repository = self.base / "batch-markdown"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        project_current_store.set_git_markdown_tracking(
            self.store, repository, enabled=True, expected=project_current_store.membership_revision(self.store),
            rights_confirmed=True)
        pdf_root = self.base / "available"
        pdf_root.mkdir()
        pdf = pdf_root / "batch.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Batch synthetic paper with sufficient fictional text.")
            document.save(pdf)
        project_batch.select(self.store, [{
            "doi": "10.0000/batch", "title": "Batch synthetic paper", "status": "Awaiting PDF"
        }], True)
        job = project_batch.prepare_extract(self.store, pdf_root, [("10.0000/batch", pdf.name)])
        with patch.object(project_text, "sync_git_markdown_views", side_effect=OSError("interrupted")):
            project_batch.run(self.root, pdf_root, job, threading.Event(),
                              current_store=self.store, markdown_repository=repository)
        self.assertEqual(project_batch.load(self.store)["status"], "failed")
        self.assertTrue((self.store / "markdown-transaction.json").exists())
        self.assertTrue(project_current_store.recover_git_markdown(self.store, repository)["recovered"])
        source = next(item for item in project_current_store.read(self.store)["editorial"]["bundle"]["sources"]
                      if item["doi"] == "10.0000/batch")
        draft = repository / "project-markdown" / literature_text.slug(source) / "draft.md"
        self.assertTrue(draft.is_file())
        with patch.object(project_library, "attach", side_effect=AssertionError("Duplicate attachment")):
            project_batch.run(self.root, pdf_root, job, threading.Event(),
                              current_store=self.store, markdown_repository=repository)
        self.assertEqual(project_batch.load(self.store)["items"]["10.0000/batch"]["status"], "extracted")

    def test_batch_current_store_attaches_reference_without_selecting_it_as_core(self):
        folder = self.store / "discovery" / ("a" * 32)
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "label": "References", "direction": "background",
            "started_at": "2026-11-10T00:00:00Z"})
        collections.atomic_json(folder, "neighborhood.json", {
            "nodes": {"ref": {"record": {"title": "Synthetic reference",
                                          "identifiers": {"doi": "10.0000/ref"}}}},
            "edges": [{"seed_doi": "10.0000/one", "direction": "background",
                       "target_provider_record": "ref"}],
        })
        pdf_root = self.base / "available"
        pdf_root.mkdir()
        pdf = pdf_root / "ref.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Synthetic reference with sufficient fictional text.")
            document.save(pdf)
        row = project_index.library_page(self.root, query="10.0000/ref", current_store=self.store)["rows"][0]
        self.assertIn("References", row["library_groups"])
        project_batch.select(self.store, [row], True)
        job = project_batch.prepare_extract(self.store, pdf_root, [("10.0000/ref", pdf.name)])
        before = project_current_store.read(self.store)
        project_batch.run(self.root, pdf_root, job, threading.Event(), current_store=self.store)
        self.assertEqual(project_batch.load(self.store)["items"]["10.0000/ref"]["status"], "extracted")
        after = project_current_store.read(self.store)
        self.assertNotIn("10.0000/ref", after["project"]["papers"])
        self.assertEqual(after["published"], before["published"])
        self.assertTrue(any(source["doi"] == "10.0000/ref" for source in after["editorial"]["bundle"]["sources"]))

    def test_library_index_does_not_serve_cached_data_if_store_is_invalid(self):
        project_index.library_page(self.root, current_store=self.store)
        self.path.write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "identity"):
            project_index.library_page(self.root, current_store=self.store)

    def test_experimental_web_editor_end_to_end_and_blocked_project_routes(self):
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}, port=8080).test_client()
        base = "http://localhost:8080"
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source = paper["editorial"]["source"]
        doi = source["doi"]
        unit = paper["editorial"]["units"][0]
        identity = collections.publication(doi)
        url = "/library/paper?" + web_server.urlencode({"id": identity})
        baseline = project_state.current(self.root)
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        old = endpoint.current()
        html = client.get(url + "&mode=edit&scope=page", base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        submitted_revision = re.search(r'name="snapshot_id" value="([^"]+)"', html).group(1)
        self.assertEqual(submitted_revision, revision)
        self.assertIn("Experimental separate current-state store", html)
        args = {
            "token": token, "doi": doi, "unit_id": unit["id"], "source_id": self.source_id,
            "scope": "page", "snapshot_id": revision, "text": unit["text"] + "\nUniqueeditorword.",
            "tables": "[]", "source_url": source["url"] or "", "reviewer": "Tester", "reason": "Correct",
            **{key: value or "" for key, value in source["bibliography"].items()},
        }
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            response = client.post("/library/edit", data=args, base_url=base)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(endpoint.current().release_id, old.release_id)
            stale = client.post("/library/edit", data=args, base_url=base)
            self.assertEqual(stale.status_code, 400)
            self.assertIn("changed", stale.get_data(as_text=True))
            pub = client.get(url + "&tab=publication", base_url=base).get_data(as_text=True)
            revision = re.search(r'name="snapshot_id" value="([^"]+)"', pub).group(1)
            response = client.post("/library/paper/publication", data={
                "id": identity, "token": token, "snapshot_id": revision, "action": "publish",
                "reviewer": "Tester", "reason": "Compare source", "checked": "yes",
            }, base_url=base)
            self.assertEqual(response.status_code, 303)
            current = endpoint.current()
            self.assertTrue(current.call("search_evidence", {"query": "uniqueeditorword"})["result"]["items"])
            with self.assertRaises(collections.SnapshotUnavailable):
                endpoint.load(old.release_id)
            rag = client.get(url + "&tab=rag", base_url=base).get_data(as_text=True)
            revision = re.search(r'name="snapshot_id" value="([^"]+)"', rag).group(1)
            saved, _ = project_current_store.read_paper(self.store, self.source_id)
            blocks = passage_context.document_blocks(saved["published"]["units"],
                                                       title=source["bibliography"]["title"])
            form = {"id": identity, "token": token, "snapshot_id": revision,
                    "ranges": f"1-{len(blocks)}", "labels": "1: uniquelabelword", "action": "save"}
            preview = client.post("/library/paper/rag",
                                  data=form | {"action": "preview", "query": "uniquelabelword"}, base_url=base)
            self.assertEqual(preview.status_code, 200)
            self.assertIn("uniquelabelword", preview.get_data(as_text=True))
            check = form | {"action": "add-check", "query": "fictional research",
                            "expected_page": "1", "expected_excerpt": "Fictional research paper"}
            response = client.post("/library/paper/rag", data=check, base_url=base)
            self.assertEqual(response.status_code, 200)
            rag = response.get_data(as_text=True)
            self.assertIn("Search check saved.", rag)
            self.assertIn("uniquelabelword", rag)
            form["snapshot_id"] = re.search(r'name="snapshot_id" value="([^"]+)"', rag).group(1)
            checked = client.post("/library/paper/rag", data=form | {"action": "run-checks"}, base_url=base)
            self.assertEqual(checked.status_code, 200)
            self.assertIn("Rank 1", checked.get_data(as_text=True))
            self.assertEqual(client.post("/library/paper/rag", data=form, base_url=base).status_code, 303)
            self.assertEqual(endpoint.current().release_id, current.release_id)
            self.assertFalse(endpoint.current().call(
                "search_evidence", {"query": "uniquelabelword"})["result"]["items"])
            rag = client.get(url + "&tab=rag", base_url=base).get_data(as_text=True)
            form["snapshot_id"] = re.search(r'name="snapshot_id" value="([^"]+)"', rag).group(1)
            form["action"] = "activate"
            self.assertEqual(client.post("/library/paper/rag", data=form, base_url=base).status_code, 303)
            self.assertTrue(endpoint.current().call(
                "search_evidence", {"query": "uniquelabelword"})["result"]["items"])
            self.assertEqual(client.post("/library/paper/rag", data=form, base_url=base).status_code, 400)
            pub = client.get(url + "&tab=publication", base_url=base).get_data(as_text=True)
            revision = re.search(r'name="snapshot_id" value="([^"]+)"', pub).group(1)
            self.assertEqual(client.post("/library/paper/publication", data={
                "id": identity, "token": token, "snapshot_id": revision, "action": "unpublish",
                "reviewer": "Tester", "reason": "Withdraw", "checked": "yes",
            }, base_url=base).status_code, 303)
            self.assertFalse(endpoint.current().call(
                "search_evidence", {"query": "uniqueeditorword"})["result"]["items"])
            withdrawn, _ = project_current_store.read_paper(self.store, self.source_id)
            self.assertIsNone(withdrawn["published"])
            self.assertEqual(len(withdrawn["rag"]["checks"]), 1)
        self.assertEqual(project_state.current(self.root), baseline)
        self.assertEqual(client.post("/settings/purge-paper", base_url=base).status_code, 404)
        self.assertEqual(client.get(url, base_url="http://example.com:8080").status_code, 421)
        self.assertEqual(client.post("/library/edit", data=args | {"token": ""}, base_url=base).status_code, 403)
        self.assertEqual(client.get("/library", base_url=base,
                                    headers={"Origin": "http://example.com"}).status_code, 403)

    def test_quality_reviews_current_drafts_and_repairs_without_publishing(self):
        baseline = project_state.current(self.root)
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        before = endpoint.current().release_id
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        listing = client.get("/quality", base_url=base)
        self.assertEqual(listing.status_code, 200)
        self.assertIn("The two sets do not synchronize", listing.get_data(as_text=True))
        current, _ = project_current_store.editorial_collection(self.store)
        evidence = current.snapshots["core"]
        findings = quality.report(evidence, self.store / "quality")["findings"]
        inventory = next(row for row in findings if row["source_id"] == self.source_id
                         and row["code"] == "inventory_missing")
        page = client.get("/quality", query_string={"collection": "core", "finding": inventory["id"]},
                          base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        common = {"token": token, "snapshot_id": current.release_id, "finding_id": inventory["id"],
                  "unit_id": "", "action": "inventory"}
        invalid = client.post("/quality?collection=core", data=common | {
            "original": (BytesIO(b"wrong original"), "wrong.pdf")}, base_url=base)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("checksum does not match", invalid.get_data(as_text=True))
        self.assertFalse((self.store / "quality" / "inventory").exists())
        response = client.post("/quality?collection=core", data=common, base_url=base)
        self.assertEqual(response.status_code, 303)
        source = next(source for source in evidence._bundle["sources"] if source["id"] == self.source_id)
        quality.inventory(self.store / "quality", source, [{"kind": "pdf", "pdf_page": 1},
                                                            {"kind": "pdf", "pdf_page": 2}])
        report = quality.report(evidence, self.store / "quality")
        coverage = next(row for row in report["findings"]
                        if row["source_id"] == self.source_id and row["code"] == "coverage_review")
        review = common | {"finding_id": coverage["id"], "action": "review", "decision": "verified",
                           "reviewer": "Tester", "note": "Compared to the original.", "checked": "yes"}
        self.assertEqual(client.post("/quality?collection=core", data=review, base_url=base).status_code, 303)
        self.assertTrue(next(row for row in quality.report(evidence, self.store / "quality")["findings"]
                             if row["id"] == coverage["id"])["resolved"])
        unit = next(unit for unit in evidence._bundle["units"]
                    if unit["citation"]["source_id"] == self.source_id)
        problem = review | {"action": "problem", "unit_id": unit["id"], "note": "Original differs."}
        self.assertEqual(client.post("/quality?collection=core", data=problem, base_url=base).status_code, 303)
        self.assertIn("reported_defect", {item["code"] for item in
                      quality.report(evidence, self.store / "quality")["findings"]})
        missing = next(row for row in report["findings"]
                       if row["source_id"] == self.source_id and row["code"] == "missing_unit")
        repair = review | {"finding_id": missing["id"], "action": "repair", "text": "Missing page transcription."}
        result = client.post("/quality?collection=core", data=repair, base_url=base)
        self.assertEqual(result.status_code, 303)
        self.assertIn("draft", client.get(result.location, base_url=base).get_data(as_text=True))
        self.assertEqual(endpoint.current().release_id, before)
        paper, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertIn("Missing page transcription.", [unit["text"] for unit in paper["editorial"]["units"]])
        self.assertNotIn("Missing page transcription.", [unit["text"] for unit in paper["published"]["units"]])
        changed, _ = project_current_store.editorial_collection(self.store)
        changed_findings = quality.report(changed.snapshots["core"], self.store / "quality")["findings"]
        self.assertFalse(next(row for row in changed_findings
                              if row["source_id"] == self.source_id and row["code"] == "coverage_review")["resolved"])
        self.assertEqual(client.get("/quality", query_string={"snapshot_id": current.release_id},
                                    base_url=base).status_code, 410)
        stale = client.post("/quality?collection=core", data=repair, base_url=base)
        self.assertEqual(stale.status_code, 400)
        self.assertIn("Evidence changed", stale.get_data(as_text=True))
        self.assertEqual(project_state.current(self.root), baseline)
        self.assertFalse((self.root / "releases/data-quality").exists())

    def test_quality_missing_page_refreshes_tracked_draft_only(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        store = self.base / "tracking-quality"
        project_current_store.initialize(state, store)
        repository = self.base / "quality-markdown"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        project_current_store.sync_git_markdown(store, repository)
        paper, _ = project_current_store.read_paper(store, self.source_id)
        source = paper["editorial"]["source"]
        folder = repository / "project-markdown" / literature_text.slug(source)
        published = (folder / "published.md").read_text()
        current, _ = project_current_store.editorial_collection(store)
        evidence = current.snapshots["core"]
        quality.inventory(store / "quality", source, [{"kind": "pdf", "pdf_page": 1},
                                                      {"kind": "pdf", "pdf_page": 2}])
        finding = next(row for row in quality.report(evidence, store / "quality")["findings"]
                       if row["source_id"] == self.source_id and row["code"] == "missing_unit")
        client = web_server.create_app(current_store=store, project_root=self.root, profiles={"private_context"},
            experimental_markdown_repository=repository, confirm_markdown_rights=True).test_client()
        base = "http://localhost:8080"
        page = client.get("/quality", query_string={"collection": "core", "finding": finding["id"]},
                          base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        result = client.post("/quality?collection=core", data={
            "token": token, "snapshot_id": current.release_id, "finding_id": finding["id"],
            "action": "repair", "reviewer": "Tester", "note": "Compared with original",
            "checked": "yes", "text": "Tracked missing page."}, base_url=base)
        self.assertEqual(result.status_code, 303)
        self.assertIn("Tracked missing page.", (folder / "draft.md").read_text())
        self.assertEqual((folder / "published.md").read_text(), published)
        self.assertFalse((store / "markdown-transaction.json").exists())

    def test_quality_includes_unpublished_drafts(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        project_current_store.unpublish(self.store, revision=revision, source_id=self.source_id,
                                        reviewer="Tester", reason="Withdraw", checked=True)
        published = project_current_store.published_snapshot(self.store)
        self.assertNotIn(self.source_id, {source["id"] for source in published._bundle["sources"]})
        current, _ = project_current_store.editorial_collection(self.store)
        findings = quality.report(current.snapshots["core"], self.store / "quality")["findings"]
        self.assertIn(self.source_id, {finding["source_id"] for finding in findings})
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}).test_client()
        response = client.get("/quality", base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 200)
        self.assertIn(paper["editorial"]["source"]["bibliography"]["title"], response.get_data(as_text=True))

    def test_experimental_web_editor_rejects_git_tracking_and_bad_configuration(self):
        state = project_current_store.read(self.store)
        state["project"]["git_markdown_tracking"] = True
        tracking = self.base / "tracking"
        project_current_store.initialize(state, tracking)
        with self.assertRaisesRegex(ValueError, "Git Markdown"):
            web_server.create_app(current_store=tracking, project_root=self.root, profiles={"private_context"})
        with self.assertRaisesRegex(ValueError, "separate"):
            web_server.create_app(current_store=self.store, profiles={"private_context"})
        with self.assertRaisesRegex(ValueError, "private core"):
            web_server.create_app(current_store=self.store, project_root=self.root, profiles={"public"})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            web_server.create_app(current_store=self.store, registry=self.root / "registry",
                                  project_root=self.root, profiles={"private_context"})

    def test_experimental_web_failed_write_preserves_both_stores(self):
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}).test_client()
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source = paper["editorial"]["source"]
        unit = paper["editorial"]["units"][0]
        url = "/library/paper?" + web_server.urlencode(
            {"id": collections.publication(source["doi"]), "mode": "edit", "scope": "page"})
        html = client.get(url, base_url="http://localhost:8080").get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        form = {
            "token": token, "doi": source["doi"], "unit_id": unit["id"], "source_id": self.source_id,
            "scope": "page", "snapshot_id": revision, "text": unit["text"] + "\nUnwritten.",
            "tables": "[]", "source_url": source["url"] or "", "reviewer": "Tester", "reason": "Correct",
            **{key: value or "" for key, value in source["bibliography"].items()},
        }
        before = self.path.read_bytes()
        baseline = project_state.current(self.root)
        with patch.object(collections, "atomic_json", side_effect=OSError("Write failed")):
            response = client.post("/library/edit", data=form, base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(project_state.current(self.root), baseline)
        other = next(source for source in baseline["editorial"]["bundle"]["sources"] if source["id"] != self.source_id)
        response = client.post("/library/edit", data=form | {"source_id": other["id"]},
                               base_url="http://localhost:8080")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_current_library_index_accepts_source_without_optional_bibliography(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        doi = paper["editorial"]["source"]["doi"]
        del paper["editorial"]["source"]["bibliography"]
        project_current_store.replace(self.store, paper, expected=revision)
        detail = project_index.paper_detail(self.root, "doi:" + doi, current_store=self.store)
        self.assertEqual(detail["title"], project_current_store.read(self.store)["project"]["papers"][doi]["title"])
        self.assertEqual(detail["status"], "Draft revision (published text remains active)")

    def test_experimental_settings_register_core_papers_without_touching_evidence(self):
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        original = project_current_store.read(self.store)
        original_project = project_state.current(self.root)
        paths = {path.name: path.read_bytes() for path in (self.store / "papers").iterdir()}
        html = client.get("/settings", base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        revision = re.search(r'name="project_revision" value="([^"]+)"', html).group(1)
        fields = {"token": token, "project_revision": revision}
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            response = client.post("/settings/add-papers", data=fields | {
                "doi": "https://doi.org/10.0000/new", "title": "New Core paper"}, base_url=base)
            self.assertEqual(response.status_code, 303)
            stale = client.post("/settings/add-papers", data=fields | {
                "doi": "10.0000/later", "title": "Later"}, base_url=base)
            self.assertEqual(stale.status_code, 400)
            self.assertIn("changed", stale.get_data(as_text=True))
            html = client.get("/settings", base_url=base).get_data(as_text=True)
            revision = re.search(r'name="project_revision" value="([^"]+)"', html).group(1)
            fields["project_revision"] = revision
            duplicate = client.post("/settings/import-papers", data=fields | {
                "papers": (BytesIO(b"doi,title\n10.0000/other,Other\n10.0000/new,Duplicate\n"), "papers.csv")
            }, base_url=base)
            self.assertEqual(duplicate.status_code, 400)
            self.assertNotIn("10.0000/other", project_current_store.read(self.store)["project"]["papers"])
            imported = client.post("/settings/import-papers", data=fields | {
                "papers": (BytesIO(b"doi,title\n10.0000/other,Other\n10.0000/another,Another\n"), "papers.csv")
            }, base_url=base)
            self.assertEqual(imported.status_code, 303)
            html = client.get("/settings", base_url=base).get_data(as_text=True)
            fields["project_revision"] = re.search(r'name="project_revision" value="([^"]+)"', html).group(1)
            imported = client.post("/settings/import-papers", data=fields | {
                "papers": (BytesIO(b'{"schema_version":1,"papers":[{"doi":"10.0000/json","title":"JSON"}]}'),
                           "papers.json")
            }, base_url=base)
            self.assertEqual(imported.status_code, 303)
        current = project_current_store.read(self.store)
        self.assertEqual(set(current["project"]["papers"]) - set(original["project"]["papers"]),
                         {"10.0000/new", "10.0000/other", "10.0000/another", "10.0000/json"})
        for key in ("editorial", "published", "rag", "audit"):
            self.assertEqual(current[key], original[key])
        self.assertEqual(paths, {path.name: path.read_bytes() for path in (self.store / "papers").iterdir()})
        self.assertEqual(project_state.current(self.root), original_project)
        detail = project_index.paper_detail(self.root, "doi:10.0000/new", current_store=self.store)
        self.assertTrue(detail["core"])
        self.assertEqual(detail["status"], "Awaiting PDF")
        self.assertEqual(client.post("/settings/add-papers", data=fields | {
            "token": "", "doi": "10.0000/x", "title": "X"}, base_url=base).status_code, 403)
        self.assertEqual(client.post("/settings/git-markdown", data=fields | {"enabled": "true"},
                                     base_url=base).status_code, 400)

    def test_current_store_web_attachment_extracts_only_the_selected_draft(self):
        _, revision = project_current_store.project_view(self.store)
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}], expected=revision)
        pdf_folder = self.base / "available"
        pdf_folder.mkdir()
        pdf = pdf_folder / "new.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Unique newly attached research draft text.")
            document.save(pdf)
        original = project_current_store.read(self.store)
        original_project = project_state.current(self.root)
        runs = set((self.root / "runs").iterdir())
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       pdf_data_root=pdf_folder, profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        url = "/library/paper?id=doi%3A10.0000%2Fnew"
        html = client.get(url, base_url=base).get_data(as_text=True)
        self.assertIn("new.pdf", html)
        self.assertIn('action="/settings/find-pdf"', html)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "doi": "10.0000/new", "title": "New paper", "pdf": "new.pdf",
                  "confirm_identity": "yes", "return_to": "/library/paper"}
        self.assertEqual(client.post("/settings/attach-pdf", data=fields | {"confirm_identity": ""},
                                     base_url=base).status_code, 400)
        self.assertEqual(client.post("/settings/attach-pdf", data=fields | {"token": ""},
                                     base_url=base).status_code, 403)
        self.assertEqual(client.post("/settings/attach-pdf", data=fields | {"title": "Wrong"},
                                     base_url=base).status_code, 400)
        self.assertEqual(client.post("/settings/attach-pdf", data=fields | {"project_revision": "stale"},
                                     base_url=base).status_code, 400)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            response = client.post("/settings/attach-pdf", data=fields, base_url=base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, url)
        current = project_current_store.read(self.store)
        source = next(s for s in current["editorial"]["bundle"]["sources"] if s["doi"] == "10.0000/new")
        paper, _ = project_current_store.read_paper(self.store, source["id"])
        self.assertIn("Unique newly attached research draft text", paper["editorial"]["units"][0]["text"])
        self.assertIsNone(paper["published"])
        self.assertIsNone(paper["rag"])
        self.assertEqual((self.root / "papers" / (source["sha256"] + ".pdf")).read_bytes(), pdf.read_bytes())
        for key in ("published", "rag", "audit"):
            self.assertEqual(current[key], original[key])
        self.assertEqual(project_state.current(self.root), original_project)
        self.assertEqual(set((self.root / "runs").iterdir()), runs)
        self.assertEqual(client.post("/settings/attach-pdf", data=fields, base_url=base).status_code, 400)
        self.assertIn("Unique newly attached", client.get(url, base_url=base).get_data(as_text=True))

    def test_find_local_pdf_in_current_store_requires_separate_confirmation(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}],
            expected=project_current_store.membership_revision(self.store))
        pdf_folder = self.base / "available"
        pdf_folder.mkdir()
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "New paper DOI: 10.0000/new")
            document.save(pdf_folder / "new-paper.pdf")
        collections.atomic_json(self.root, "pdf-job.json", {"status": "existing normal app job"})
        original_job = (self.root / "pdf-job.json").read_bytes()
        before = project_current_store.read(self.store)
        original = project_state.current(self.root)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    pdf_data_root=pdf_folder, profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/library/paper?id=doi%3A10.0000%2Fnew", base_url=base).get_data(as_text=True)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "doi": "10.0000/new", "title": "New paper", "return_to": "/library/paper"}
        self.assertEqual(client.post("/settings/find-pdf", data=fields | {"token": ""},
                                     base_url=base).status_code, 403)
        self.assertEqual(client.post("/settings/find-pdf", data=fields | {"project_revision": "stale"},
                                     base_url=base).status_code, 400)
        with patch.object(literature.MetadataClient, "get", side_effect=AssertionError("Unexpected network")):
            response = client.post("/settings/find-pdf", data=fields, base_url=base)
            self.assertEqual(response.status_code, 303)
            app.extensions["find_pdf"]["thread"].join(10)
            self.assertFalse(app.extensions["find_pdf"]["thread"].is_alive())
        progress = client.get(response.location, base_url=base).get_data(as_text=True)
        self.assertIn("Found local PDF", progress)
        self.assertIn("Not attached or extracted", progress)
        self.assertIn('new-paper.pdf" selected', progress)
        self.assertIn('name="confirm_identity"', progress)
        self.assertEqual(project_current_store.read(self.store), before)
        self.assertEqual(project_state.current(self.root), original)
        self.assertEqual((self.root / "pdf-job.json").read_bytes(), original_job)
        attach = fields | {"pdf": "new-paper.pdf", "confirm_identity": "yes"}
        self.assertEqual(client.post("/settings/attach-pdf", data=attach, base_url=base).status_code, 303)
        state = project_current_store.read(self.store)
        source = next(source for source in state["editorial"]["bundle"]["sources"]
                      if source["doi"] == "10.0000/new")
        paper, _ = project_current_store.read_paper(self.store, source["id"])
        self.assertIsNone(paper["published"])

    def test_find_pdf_reports_lookup_failure_without_attaching(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}],
            expected=project_current_store.membership_revision(self.store))
        before = project_current_store.read(self.store)
        app = web_server.create_app(current_store=self.store, project_root=self.root,
                                    pdf_data_root=self.base / "available",
                                    profiles={"private_context"})
        client = app.test_client()
        base = "http://localhost:8080"
        html = client.get("/library/paper?id=doi%3A10.0000%2Fnew", base_url=base).get_data(as_text=True)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "doi": "10.0000/new", "title": "New paper", "return_to": "/library/paper"}
        with patch.object(literature.MetadataClient, "get", side_effect=ValueError("Metadata unavailable")):
            response = client.post("/settings/find-pdf", data=fields, base_url=base)
            app.extensions["find_pdf"]["thread"].join(10)
            self.assertFalse(app.extensions["find_pdf"]["thread"].is_alive())
        self.assertEqual(response.status_code, 303)
        progress = client.get(response.location, base_url=base).get_data(as_text=True)
        self.assertIn("Metadata unavailable", progress)
        self.assertEqual(project_current_store.read(self.store), before)

    def test_current_store_online_find_rejects_stale_membership_before_writing(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}],
            expected=project_current_store.membership_revision(self.store))
        revision = project_current_store.membership_revision(self.store)
        before = project_current_store.read(self.store)
        pdf_folder = self.base / "available"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Synthetic downloaded test PDF.")
            data = document.tobytes()
        work = {"id": "https://openalex.org/W123", "doi": "https://doi.org/10.0000/new",
                "publication_date": "2020-01-01", "locations": [{
                    "license": "cc-by", "pdf_url": "https://example.org/paper.pdf",
                    "version": "publishedVersion", "landing_page_url": "https://example.org/paper"}]}
        source = {"url": "https://api.openalex.org/works/W123",
                  "retrieved_at": "2026-11-10T00:00:00Z", "sha256": "a" * 64}

        def download(*args, **kwargs):
            project_current_store.register_core_papers(
                self.store, [{"doi": "10.0000/other", "title": "Other"}], expected=revision)
            return data, {"final_url": "https://example.org/paper.pdf", "pdf_pages": 1}

        with patch.object(literature.MetadataClient, "get", return_value=(work, source)), \
                patch.object(citation_neighborhood, "download", side_effect=download):
            with self.assertRaisesRegex(ValueError, "membership changed"):
                project_library.find_pdf(self.root, "10.0000/new", pdf_folder, title="New paper",
                                         current_store=self.store, membership_revision=revision)
        self.assertFalse(pdf_folder.exists())
        after = project_current_store.read(self.store)
        for key in ("editorial", "published", "rag", "audit"):
            self.assertEqual(after[key], before[key])

    def test_current_store_online_find_preserves_download_provenance(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}],
            expected=project_current_store.membership_revision(self.store))
        before = project_current_store.read(self.store)
        pdf_folder = self.base / "available"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Synthetic downloaded test PDF.")
            data = document.tobytes()
        work = {"id": "https://openalex.org/W123", "doi": "https://doi.org/10.0000/new",
                "publication_date": "2020-01-01", "locations": [{
                    "license": "cc-by", "pdf_url": "https://example.org/paper.pdf",
                    "version": "publishedVersion", "landing_page_url": "https://example.org/paper"}]}
        source = {"url": "https://api.openalex.org/works/W123",
                  "retrieved_at": "2026-11-10T00:00:00Z", "sha256": "a" * 64}
        with patch.object(literature.MetadataClient, "get", return_value=(work, source)), \
                patch.object(citation_neighborhood, "download", return_value=(
                    data, {"final_url": "https://example.org/paper.pdf", "pdf_pages": 1})):
            result = project_library.find_pdf(
                self.root, "10.0000/new", pdf_folder, title="New paper", current_store=self.store,
                membership_revision=project_current_store.membership_revision(self.store))
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual((pdf_folder / result["filename"]).read_bytes(), data)
        self.assertEqual(project_library.downloaded_pdf_metadata(pdf_folder)[
            ("doi:10.0000/new", releases.sha256(data))]["final_url"], "https://example.org/paper.pdf")
        self.assertEqual(project_current_store.read(self.store), before)

    def test_current_store_attachment_without_extraction_and_failed_membership_write(self):
        _, revision = project_current_store.project_view(self.store)
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}], expected=revision)
        pdf = self.base / "new.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "This text must not be extracted.")
            document.save(pdf)
        revision = project_current_store.membership_revision(self.store)
        before = project_current_store.read(self.store)
        with patch.object(project_current_store, "add_paper", side_effect=OSError("Write failed")):
            with self.assertRaisesRegex(OSError, "Write failed"):
                project_library.attach(self.root, "10.0000/new", pdf, identity_confirmed=True,
                                       title="New paper", extract=False, current_store=self.store,
                                       membership_revision=revision)
        self.assertEqual(project_current_store.read(self.store), before)
        result = project_library.attach(self.root, "10.0000/new", pdf, identity_confirmed=True,
                                        title="New paper", extract=False, current_store=self.store,
                                        membership_revision=revision)
        paper, _ = project_current_store.read_paper(self.store, result["source_id"])
        self.assertEqual(result["extraction"], "not_extracted")
        self.assertEqual(paper["editorial"]["units"][0]["text"], "")
        self.assertIsNone(paper["published"])

    def test_attachment_of_reference_does_not_make_it_core(self):
        folder = self.root / "discovery" / ("a" * 32)
        collections.atomic_json(folder, "job.json", {
            "status": "complete", "label": "References", "started_at": "2026-11-10T00:00:00Z"})
        collections.atomic_json(folder, "neighborhood.json", {
            "nodes": {"ref": {"record": {"title": "Reference paper",
                                         "identifiers": {"doi": "10.0000/ref"}}}},
            "edges": [{"seed_doi": "10.0000/one", "direction": "background",
                       "target_provider_record": "ref"}],
        })
        pdf = self.base / "reference.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Reference paper draft.")
            document.save(pdf)
        before = project_current_store.read(self.store)
        result = project_library.attach(
            self.root, "10.0000/ref", pdf, identity_confirmed=True, title="Reference paper",
            current_store=self.store, membership_revision=project_current_store.membership_revision(self.store))
        after = project_current_store.read(self.store)
        self.assertEqual(after["project"], before["project"])
        self.assertNotIn("10.0000/ref", after["project"]["papers"])
        self.assertIsNone(project_current_store.read_paper(self.store, result["source_id"])[0]["published"])
        detail = project_index.paper_detail(self.root, "doi:10.0000/ref", current_store=self.store)
        self.assertFalse(detail["core"])

    def test_attachment_interruption_reports_recovery_and_preserves_committed_paper(self):
        project_current_store.register_core_papers(
            self.store, [{"doi": "10.0000/new", "title": "New paper"}],
            expected=project_current_store.membership_revision(self.store))
        pdf_folder = self.base / "available"
        pdf_folder.mkdir()
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Recovered draft.")
            document.save(pdf_folder / "new.pdf")
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       pdf_data_root=pdf_folder, profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        html = client.get("/library/paper?id=doi%3A10.0000%2Fnew", base_url=base).get_data(as_text=True)
        fields = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
                  "project_revision": re.search(r'name="project_revision" value="([^"]+)"', html).group(1),
                  "doi": "10.0000/new", "title": "New paper", "pdf": "new.pdf",
                  "confirm_identity": "yes", "return_to": "/library/paper"}
        with patch.object(project_current_store, "_recover_membership", side_effect=OSError("Interrupted")):
            response = client.post("/settings/attach-pdf", data=fields, base_url=base)
        self.assertEqual(response.status_code, 503)
        self.assertIn("recover-current-store", response.get_data(as_text=True))
        self.assertTrue(project_current_store.recover_membership(self.store))
        state = project_current_store.read(self.store)
        self.assertTrue(any(source["doi"] == "10.0000/new" for source in state["editorial"]["bundle"]["sources"]))
        self.assertFalse(any(source["doi"] == "10.0000/new" for source in state["published"]["bundle"]["sources"]))

    def test_register_existing_attached_paper_as_core_preserves_retrieval(self):
        state = project_current_store.read(self.store)
        project = deepcopy(state["project"])
        doi = "10.0000/one"
        del project["papers"][doi]
        revision = project_current_store.membership_revision(self.store)
        revision = project_current_store.update_project(self.store, project=project, expected=revision)["revision"]
        before = project_current_store.read(self.store)
        result = project_current_store.register_core_papers(
            self.store, [{"doi": doi, "title": "Selected title"}], expected=revision)
        self.assertEqual(result["added"], [doi])
        after = project_current_store.read(self.store)
        for key in ("editorial", "published", "rag", "audit"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(project_index.paper_detail(
            self.root, "doi:" + doi, current_store=self.store)["status"], "Published for retrieval")
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.register_core_papers(
                self.store, [{"doi": "10.0000/extra", "title": "Extra"}], expected=revision)

    def test_core_registration_failed_write_and_membership_race_are_explicit(self):
        project, revision = project_current_store.project_view(self.store)
        before = project_current_store.read(self.store)
        args = [{"doi": "10.0000/extra", "title": "Extra"}]
        with patch.object(collections, "atomic_json", side_effect=OSError("Write failed")):
            with self.assertRaisesRegex(OSError, "Write failed"):
                project_current_store.register_core_papers(self.store, args, expected=revision)
        self.assertEqual(project_current_store.read(self.store), before)
        self.assertEqual(project_current_store.project_view(self.store), (project, revision))
        replacement = deepcopy(project)
        replacement["name"] = "Concurrent update"
        project_current_store.update_project(self.store, project=replacement, expected=revision)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.register_core_papers(self.store, args, expected=revision)
        self.assertNotIn("10.0000/extra", project_current_store.read(self.store)["project"]["papers"])

    def test_project_configuration_update_preserves_evidence_and_checks_stale_changes(self):
        state = project_current_store.read(self.store)
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        old = endpoint.current()
        revision = project_current_store.membership_revision(self.store)
        project = deepcopy(state["project"])
        project["name"] = "New project name"
        project["papers"]["10.0000/new"] = {"title": "New Core paper"}
        before = {path.name: path.read_bytes() for path in (self.store / "papers").iterdir()}
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            result = project_current_store.update_project(self.store, project=project, expected=revision)
        after = project_current_store.read(self.store)
        self.assertEqual(after["project"], project)
        for key in ("editorial", "published", "rag", "audit"):
            self.assertEqual(after[key], state[key])
        self.assertEqual(before, {path.name: path.read_bytes() for path in (self.store / "papers").iterdir()})
        current = endpoint.current()
        self.assertEqual(current.snapshots["core"].release_id, old.snapshots["core"].release_id)
        self.assertNotEqual(current.release_id, old.release_id)
        with self.assertRaises(collections.SnapshotUnavailable):
            endpoint.load(old.release_id)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.update_project(self.store, project=state["project"], expected=revision)
        restored = self.base / "metadata-restored"
        project_current_store.restore_project(self.store, restored)
        self.assertEqual(project_state.current(restored), after)
        self.assertEqual(result["revision"], project_current_store.membership_revision(self.store))

    def test_project_configuration_rejects_bad_inputs_and_unapproved_tracking(self):
        state = project_current_store.read(self.store)
        revision = project_current_store.membership_revision(self.store)
        for change in ({"name": ""}, {"authorization": ""}, {"papers": {"bad doi": {"title": "Bad"}}},
                       {"papers": {"10.0000/new": {"title": ""}}}, {"git_markdown_tracking": True}):
            with self.subTest(change=change):
                project = deepcopy(state["project"])
                project.update(change)
                with self.assertRaises(ValueError):
                    project_current_store.update_project(self.store, project=project, expected=revision)
                self.assertEqual(project_current_store.read(self.store), state)
        valid = deepcopy(state["project"])
        valid["name"] = "Changed name"
        with patch.object(collections, "atomic_json", side_effect=OSError("Write failed")):
            with self.assertRaisesRegex(OSError, "Write failed"):
                project_current_store.update_project(self.store, project=valid, expected=revision)
        self.assertEqual(project_current_store.read(self.store), state)

    def new_editorial(self):
        project_library.register(self.root, [{"doi": "10.0000/three", "title": "third"}])
        pdf = self.base / "three.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "Third fictional paper with enough extracted text.")
            document.save(pdf)
        project_library.attach(self.root, "10.0000/three", pdf, identity_confirmed=True)
        state = project_state.current(self.root)
        source = next(item for item in state["editorial"]["bundle"]["sources"] if item["doi"] == "10.0000/three")
        return project_current_store._paper_snapshot(state["editorial"], source["id"])

    def test_add_and_remove_paper_are_explicit_and_keep_other_papers(self):
        editorial = self.new_editorial()
        source_id = editorial["source"]["id"]
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        old = endpoint.current()
        before = project_current_store.read(self.store)
        membership = project_current_store.membership_revision(self.store)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            result = project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        added, revision = project_current_store.read_paper(self.store, source_id)
        self.assertEqual(added["editorial"], editorial)
        self.assertIsNone(added["published"])
        self.assertIsNone(added["rag"])
        self.assertNotIn("10.0000/three", project_current_store.read(self.store)["project"]["papers"])
        self.assertEqual(project_current_store.read(self.store)["published"], before["published"])
        self.assertEqual(endpoint.current().release_id, old.release_id)
        with self.assertRaisesRegex(ValueError, "membership changed"):
            project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        with self.assertRaisesRegex(ValueError, "already attached"):
            project_current_store.add_paper(self.store, editorial=editorial, expected=result["membership_revision"])
        with self.assertRaisesRegex(ValueError, "Confirm"):
            project_current_store.remove_paper(self.store, source_id=source_id, revision=revision,
                                               expected=result["membership_revision"])
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            project_current_store.remove_paper(self.store, source_id=source_id, revision=revision,
                                               expected=result["membership_revision"], confirmed=True)
        self.assertEqual(project_current_store.read(self.store), before)
        self.assertEqual(endpoint.current().release_id, old.release_id)
        self.assertEqual(project_current_store.membership_revision(self.store), membership)

    def test_membership_interruption_recovers_without_partial_reads(self):
        editorial = self.new_editorial()
        source_id = editorial["source"]["id"]
        before = project_current_store.read(self.store)
        membership = project_current_store.membership_revision(self.store)
        original = collections.atomic_json

        def fail_manifest(root, name, data):
            if name == "manifest.json":
                raise OSError("Manifest write failed")
            return original(root, name, data)

        with patch.object(collections, "atomic_json", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "Manifest write failed"):
                project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        with self.assertRaisesRegex(ValueError, "Interrupted"):
            project_current_store.read(self.store)
        self.assertTrue(project_current_store.recover_membership(self.store))
        self.assertFalse(project_current_store.recover_membership(self.store))
        self.assertEqual(project_current_store.read(self.store), before)

        result = project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        _, revision = project_current_store.read_paper(self.store, source_id)
        target = self.store / "papers" / project_state._paper_filename(source_id)
        original_unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path == target:
                raise OSError("Removal failed")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_unlink):
            with self.assertRaisesRegex(OSError, "Removal failed"):
                project_current_store.remove_paper(self.store, source_id=source_id, revision=revision,
                                                   expected=result["membership_revision"], confirmed=True)
        with self.assertRaisesRegex(ValueError, "Interrupted"):
            project_current_store.read(self.store)
        self.assertTrue(project_current_store.recover_membership(self.store))
        self.assertEqual(project_current_store.read(self.store), before)

    def test_membership_recovery_rejects_changed_paper_and_stale_removal(self):
        editorial = self.new_editorial()
        membership = project_current_store.membership_revision(self.store)
        result = project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        source_id = editorial["source"]["id"]
        paper, revision = project_current_store.read_paper(self.store, source_id)
        paper["editorial"]["units"][0]["text"] += " New draft."
        project_current_store.replace(self.store, paper, expected=revision)
        with self.assertRaisesRegex(ValueError, "Paper changed"):
            project_current_store.remove_paper(self.store, source_id=source_id, revision=revision,
                                               expected=result["membership_revision"], confirmed=True)
        _, revision = project_current_store.read_paper(self.store, source_id)
        target = self.store / "papers" / project_state._paper_filename(source_id)
        original_unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path == target:
                raise OSError("Removal failed")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_unlink):
            with self.assertRaises(OSError):
                project_current_store.remove_paper(self.store, source_id=source_id, revision=revision,
                                                   expected=result["membership_revision"], confirmed=True)
        original = target.read_bytes()
        target.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "changed during"):
            project_current_store.recover_membership(self.store)
        target.write_bytes(original)
        self.assertTrue(project_current_store.recover_membership(self.store))

    def test_committed_add_recovery_and_published_removal(self):
        editorial = self.new_editorial()
        source_id = editorial["source"]["id"]
        membership = project_current_store.membership_revision(self.store)
        with patch.object(project_current_store, "_recover_membership", side_effect=OSError("Interrupted")):
            with self.assertRaisesRegex(OSError, "Interrupted"):
                project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        with self.assertRaises(releases.ReleaseError):
            endpoint.current()
        executable = Path(sys.executable).parent / "research-project"
        result = subprocess.run([str(executable), "recover-current-store", str(self.store)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        paper, revision = project_current_store.read_paper(self.store, source_id)
        self.assertIsNone(paper["published"])
        revision = project_current_store.publish(self.store, source_id=source_id, revision=revision,
                                                 reviewer="Tester", reason="Publish", checked=True)["revision"]
        previous = endpoint.current()
        query = {"query": "Third fictional paper", "paper": "10.0000/three"}
        self.assertTrue(previous.call("search_evidence", query)["result"]["items"])
        membership = project_current_store.membership_revision(self.store)
        project_current_store.remove_paper(self.store, source_id=source_id, revision=revision,
                                           expected=membership, confirmed=True)
        with self.assertRaises(collections.SnapshotUnavailable):
            endpoint.load(previous.release_id)
        self.assertEqual(endpoint.current().call("search_evidence", query)["result"]["items"], [])
        self.assertTrue(previous.call("search_evidence", query)["result"]["items"])

    def test_membership_recovery_before_paper_or_manifest_write(self):
        editorial = self.new_editorial()
        source_id = editorial["source"]["id"]
        before = project_current_store.read(self.store)
        membership = project_current_store.membership_revision(self.store)
        filename = project_state._paper_filename(source_id)
        original = collections.atomic_json

        def fail_paper(root, name, data):
            if name == filename:
                raise OSError("Paper write failed")
            return original(root, name, data)

        with patch.object(collections, "atomic_json", side_effect=fail_paper):
            with self.assertRaisesRegex(OSError, "Paper write failed"):
                project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        self.assertTrue(project_current_store.recover_membership(self.store))
        self.assertEqual(project_current_store.read(self.store), before)
        result = project_current_store.add_paper(self.store, editorial=editorial, expected=membership)
        added = project_current_store.read(self.store)
        _, revision = project_current_store.read_paper(self.store, source_id)

        def fail_manifest(root, name, data):
            if name == "manifest.json":
                raise OSError("Manifest write failed")
            return original(root, name, data)

        with patch.object(collections, "atomic_json", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "Manifest write failed"):
                project_current_store.remove_paper(self.store, source_id=source_id, revision=revision,
                                                   expected=result["membership_revision"], confirmed=True)
        self.assertTrue(project_current_store.recover_membership(self.store))
        self.assertEqual(project_current_store.read(self.store), added)

    def test_round_trip_and_single_paper_atomic_update(self):
        self.assertEqual(project_current_store.read(self.store), self.state)
        paper = self.paper()
        old = releases.sha256(releases.encode(paper))
        revised = deepcopy(paper)
        revised["editorial"]["units"][0]["text"] += "\nSaved draft revision."
        before = {path.name: path.read_bytes() for path in (self.store / "papers").iterdir()}
        revision = project_current_store.replace(self.store, revised, expected=old)
        self.assertEqual(revision, releases.sha256(releases.encode(revised)))
        after = {path.name: path.read_bytes() for path in (self.store / "papers").iterdir()}
        self.assertEqual([name for name in before if before[name] != after[name]], [self.path.name])
        current = project_current_store.read(self.store)
        self.assertEqual(current["published"], self.state["published"])
        self.assertEqual(current["rag"], self.state["rag"])
        self.assertIn("Saved draft revision.", current["editorial"]["bundle"]["units"][0]["text"])
        self.assertEqual(project_state.current(self.root), self.state)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.replace(self.store, paper, expected=old)

    def test_invalid_or_failed_write_preserves_previous_paper(self):
        paper = self.paper()
        previous = self.path.read_bytes()
        revision = releases.sha256(releases.encode(paper))
        invalid = deepcopy(paper)
        invalid["editorial"]["units"][0]["citation"]["source_id"] = "wrong"
        with self.assertRaises(ValueError):
            project_current_store.replace(self.store, invalid, expected=revision)
        self.assertEqual(self.path.read_bytes(), previous)
        with patch.object(collections, "atomic_json", side_effect=OSError("Write failed")):
            with self.assertRaisesRegex(OSError, "Write failed"):
                project_current_store.replace(self.store, paper, expected=revision)
        self.assertEqual(self.path.read_bytes(), previous)
        with patch.object(collections.os, "replace", side_effect=OSError("Replace failed")):
            with self.assertRaisesRegex(OSError, "Replace failed"):
                project_current_store.replace(self.store, paper, expected=revision)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(len(list((self.store / "papers").iterdir())), 2)

    def test_save_draft_uses_editor_validation_without_a_library_snapshot(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source = paper["editorial"]["source"]
        units = paper["editorial"]["units"]
        args = {
            "source_id": self.source_id, "unit_id": units[0]["id"],
            "text": project_edit.document_draft(units) + "\nUpdated draft.",
            "bibliography": {key: value or "" for key, value in source["bibliography"].items()},
            "source_url": source["url"] or "", "tables": [{
                "id": "", "unit_id": units[0]["id"], "title": "Counts", "headers": ["Value"],
                "units": [None], "rows": [["3"]], "footnotes": ["Checked note."],
            }],
            "reviewer": "Tester", "reason": "Corrected draft", "whole_document": True,
        }
        registry = project_text.editorial_store(self.root)
        existing_releases = {path.name for path in (self.root / "editorial" / "releases").iterdir()}
        before = {path.name: path.read_bytes() for path in (self.store / "papers").iterdir()}
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            result = project_current_store.save_draft(self.store, revision=revision, **args)
        current, current_revision = project_current_store.read_paper(self.store, self.source_id)
        self.assertEqual(result["revision"], current_revision)
        self.assertEqual(project_current_store.editor_view(self.store, self.source_id)["publication_state"],
                         "draft_revision")
        self.assertEqual(current["published"], paper["published"])
        self.assertEqual(current["rag"], paper["rag"])
        self.assertEqual(current["audit"]["publication"], paper["audit"]["publication"])
        self.assertEqual(current["audit"]["draft"]["reviewer"], "Tester")
        self.assertEqual({name for name, old in before.items()
                          if (self.store / "papers" / name).read_bytes() != old}, {self.path.name})
        self.assertEqual({path.name for path in (self.root / "editorial" / "releases").iterdir()},
                         existing_releases)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.save_draft(self.store, revision=revision, **args)
        snapshot_id = registry.current().release_id
        project_edit.correct(self.root, snapshot_id=snapshot_id, checked=False, **args)
        snapshot = registry.current().snapshots["core"]
        expected = project_current_store._paper_snapshot({
            "bundle": snapshot._bundle, "authorization": snapshot._manifest["authorization"],
            "chunk_plans": snapshot.chunk_plans}, self.source_id)
        self.assertEqual(current["editorial"], expected)

    def test_git_tracks_draft_publication_and_rag_in_only_the_changed_paper(self):
        subprocess.run(["git", "init", "-q", str(self.base)], check=True)
        store_path = self.store.relative_to(self.base).as_posix()
        subprocess.run(["git", "-C", str(self.base), "add", "--", store_path], check=True)
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        original_published = deepcopy(paper["published"])
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            revision = project_current_store.save_draft(
                self.store, source_id=self.source_id, revision=revision, unit_id=unit["id"],
                text=unit["text"] + "\nCorrected text.",
                bibliography={key: value or "" for key, value in source["bibliography"].items()},
                source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correction")["revision"]
            draft, _ = project_current_store.read_paper(self.store, self.source_id)
            self.assertEqual(draft["published"], original_published)
            revision = project_current_store.publish(
                self.store, source_id=self.source_id, revision=revision,
                reviewer="Tester", reason="Checked", checked=True)["revision"]
            published, _ = project_current_store.read_paper(self.store, self.source_id)
            self.assertNotEqual(published["published"], original_published)
            revision = project_current_store.update_rag(
                self.store, source_id=self.source_id, revision=revision, form={"ranges": ""})["revision"]
            result, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertIsNotNone(result["rag"]["draft"])
        self.assertEqual(result["published"], published["published"])
        changed = subprocess.run(["git", "-C", str(self.base), "diff", "--name-only"],
                                 capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(changed, [self.path.relative_to(self.base).as_posix()])

    def test_web_edits_publication_and_live_rag_survive_git_checkout(self):
        subprocess.run(["git", "init", "-q", str(self.base)], check=True)
        store_path = self.store.relative_to(self.base).as_posix()
        git = ["git", "-C", str(self.base)]
        commit = git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-q"]
        subprocess.run(git + ["add", "--", store_path], check=True)
        subprocess.run(commit + ["-m", "Initial paper state", "-m",
                                 "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"],
                       check=True)
        client = web_server.create_app(current_store=self.store, project_root=self.root,
                                       profiles={"private_context"}, port=8080).test_client()
        base = "http://localhost:8080"
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        identity = collections.publication(source["doi"])
        url = "/library/paper?" + web_server.urlencode({"id": identity})
        html = client.get(url + "&mode=edit&scope=page", base_url=base).get_data(as_text=True)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        edit = {
            "token": token, "doi": source["doi"], "unit_id": unit["id"], "source_id": self.source_id,
            "scope": "page", "snapshot_id": revision, "text": unit["text"] + "\nGitcheckoutword.",
            "tables": "[]", "source_url": source["url"] or "", "reviewer": "Tester", "reason": "Correct",
            **{key: value or "" for key, value in source["bibliography"].items()},
        }
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            self.assertEqual(client.post("/library/edit", data=edit, base_url=base).status_code, 303)
            saved, revision = project_current_store.read_paper(self.store, self.source_id)
            self.assertEqual(saved["published"], paper["published"])
            self.assertEqual(client.post("/library/paper/publication", data={
                "id": identity, "token": token, "snapshot_id": revision, "action": "publish",
                "reviewer": "Tester", "reason": "Checked", "checked": "yes",
            }, base_url=base).status_code, 303)
            published, revision = project_current_store.read_paper(self.store, self.source_id)
            blocks = passage_context.document_blocks(published["published"]["units"],
                                                       title=source["bibliography"]["title"])
            form = {"id": identity, "token": token, "snapshot_id": revision,
                    "ranges": f"1-{len(blocks)}", "labels": "1: gitcheckoutlabel", "action": "activate"}
            self.assertEqual(client.post("/library/paper/rag", data=form, base_url=base).status_code, 303)
            before = project_current_store.read(self.store)
            endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
            self.assertTrue(endpoint.current().call(
                "search_evidence", {"query": "gitcheckoutlabel"})["result"]["items"])
        changed = subprocess.run(git + ["diff", "--name-only"], capture_output=True,
                                 text=True, check=True).stdout.splitlines()
        self.assertEqual(changed, [self.path.relative_to(self.base).as_posix()])
        subprocess.run(git + ["add", "--", store_path], check=True)
        subprocess.run(commit + ["-m", "Save paper changes", "-m",
                                 "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"],
                       check=True)
        clone = self.base / "git-recovered"
        subprocess.run(["git", "clone", "-q", "--no-local", str(self.base), str(clone)], check=True)
        recovered = clone / store_path
        self.assertEqual(project_current_store.read(recovered), before)
        restored_endpoint = project_current_store.CurrentCollectionStore(
            recovered, allowed_profiles={"private_context"})
        self.assertTrue(restored_endpoint.current().call(
            "search_evidence", {"query": "gitcheckoutlabel"})["result"]["items"])

    def test_paragraph_repair_uses_current_paper_revision_and_original_pdf(self):
        project_library.register(self.root, [{"doi": "10.0000/layout", "title": "Layout test"}])
        pdf = self.base / "layout.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 60), "Synthetic heading and first paragraph with sufficient native text.")
            page.insert_text((50, 100), "A second paragraph contains more words for the extraction fixture.")
            page.insert_text((50, 140), "The final paragraph completes a sufficiently detailed fictional paper.")
            document.save(pdf)
        project_library.attach(self.root, "10.0000/layout", pdf, identity_confirmed=True)
        state = project_state.current(self.root)
        source = next(item for item in state["editorial"]["bundle"]["sources"] if item["doi"] == "10.0000/layout")
        source_id = source["id"]
        editorial = project_current_store._paper_snapshot(state["editorial"], source_id)
        project_current_store.add_paper(
            self.store, editorial=editorial, expected=project_current_store.membership_revision(self.store))
        paper, revision = project_current_store.read_paper(self.store, source_id)
        unit_id = paper["editorial"]["units"][0]["id"]
        expected = project_edit.paragraph_draft(
            self.root, snapshot_id=project_text.editorial_store(self.root).current().release_id,
            source_id=source_id, unit_id=unit_id)
        before = project_current_store.read(self.store)
        actual = project_current_store.paragraph_draft(
            self.store, self.root, source_id=source_id, unit_id=unit_id, revision=revision)
        self.assertEqual(actual, expected)
        self.assertIn("Synthetic heading", actual)
        self.assertEqual(project_current_store.read(self.store), before)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.paragraph_draft(
                self.store, self.root, source_id=source_id, unit_id=unit_id, revision="stale")
        with self.assertRaisesRegex(ValueError, "Select an extracted PDF page"):
            project_current_store.paragraph_draft(
                self.store, self.root, source_id=source_id, unit_id="invalid", revision=revision)
        (self.root / "papers" / (source["sha256"] + ".pdf")).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum differs"):
            project_current_store.paragraph_draft(
                self.store, self.root, source_id=source_id, unit_id=unit_id, revision=revision)
        self.assertEqual(project_current_store.read(self.store), before)

    def test_publication_is_explicit_and_never_applies_rag_draft(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        published_units = paper["published"]["units"]
        title = paper["published"]["source"]["bibliography"]["title"]
        blocks = passage_context.document_blocks(published_units, title=title)
        paper["published"]["chunk_plans"] = {
            published_units[0]["id"]: {"ranges": [[1, len(blocks)]]}}
        paper["rag"] = {"draft": {"content_identity": "older", "plans": {},
                                  "term_anchors": {}, "unassigned_terms": []}, "checks": []}
        revision = project_current_store.replace(self.store, paper, expected=revision)
        source = paper["editorial"]["source"]
        unit = paper["editorial"]["units"][0]
        args = {"source_id": self.source_id, "unit_id": unit["id"], "text": unit["text"] + "\nRevision.",
                "bibliography": {key: value or "" for key, value in source["bibliography"].items()},
                "source_url": source["url"] or "", "tables": [], "reviewer": "Tester", "reason": "Corrected"}
        revision = project_current_store.save_draft(self.store, revision=revision, **args)["revision"]
        draft, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertEqual(draft["published"], paper["published"])
        self.assertEqual(draft["rag"], paper["rag"])
        with self.assertRaisesRegex(ValueError, "Confirm"):
            project_current_store.publish(self.store, revision=revision, source_id=self.source_id,
                                          reviewer="Tester", reason="Publish", checked=False)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            result = project_current_store.publish(self.store, revision=revision, source_id=self.source_id,
                                                   reviewer="Tester", reason="Publish", checked=True)
        self.assertTrue(result["chunks_stale"])
        live, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertEqual(live["rag"], paper["rag"])
        self.assertEqual(live["published"]["chunk_plans"], {})
        self.assertIn("Revision.", live["published"]["units"][0]["text"])
        self.assertEqual(live["published"]["units"][0]["human_verification"], "reviewed")
        self.assertEqual(live["editorial"], draft["editorial"])
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.publish(self.store, revision=revision, source_id=self.source_id,
                                          reviewer="Tester", reason="Publish", checked=True)

    def test_missing_and_modified_files_are_rejected(self):
        other = self.store / "papers" / "unknown.json"
        other.write_text("{}")
        with self.assertRaisesRegex(ValueError, "do not match"):
            project_current_store.read(self.store)
        other.unlink()
        paper = self.paper()
        paper["source_id"] = "wrong"
        self.path.write_bytes(releases.encode(paper))
        with self.assertRaisesRegex(ValueError, "identity"):
            project_current_store.read(self.store)

    def test_rag_save_checks_and_activation_match_existing_workflow(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        units = paper["published"]["units"]
        blocks = passage_context.document_blocks(units, title=paper["published"]["source"]["bibliography"]["title"])
        form = {"ranges": f"1-{len(blocks)}", "labels": "1: Fictional evidence"}
        check = {"query": "fictional research", "expected_page": "1",
                 "expected_excerpt": "Fictional research paper"}
        snapshot_id = project_library.store(self.root).current().release_id
        project_chunks.update(self.root, source_id=self.source_id, snapshot_id=snapshot_id,
                              form=form, activate=False)
        project_chunks.update_check(self.root, source_id=self.source_id, snapshot_id=snapshot_id, form=check)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            result = project_current_store.update_rag(self.store, source_id=self.source_id,
                                                      revision=revision, form=form)
            saved, revision = project_current_store.read_paper(self.store, self.source_id)
            self.assertFalse(result["activated"])
            self.assertEqual(saved["published"], paper["published"])
            self.assertEqual(saved["rag"]["draft"], project_chunks.load_draft(self.root, self.source_id))
            result = project_current_store.update_check(self.store, source_id=self.source_id,
                                                        revision=revision, form=check)
            saved, revision = project_current_store.read_paper(self.store, self.source_id)
            self.assertEqual(saved["rag"]["checks"], project_chunks.load_checks(self.root, self.source_id))
            self.assertEqual(saved["published"], paper["published"])
            result = project_current_store.update_rag(self.store, source_id=self.source_id,
                                                      revision=revision, form=form, activate=True)
            live, revision = project_current_store.read_paper(self.store, self.source_id)
            self.assertTrue(result["activated"])
            self.assertEqual(live["published"]["chunk_plans"], live["rag"]["draft"]["plans"])
            self.assertEqual(live["editorial"], paper["editorial"])
            project_current_store.update_check(
                self.store, source_id=self.source_id, revision=revision,
                form={"check_id": live["rag"]["checks"][0]["id"]}, remove=True)
        project_chunks.update(self.root, source_id=self.source_id, snapshot_id=snapshot_id,
                              form=form, activate=True)
        snapshot = project_library.store(self.root).current().snapshots["core"]
        self.assertEqual(live["published"]["chunk_plans"], snapshot.chunk_plans)

    def test_editor_view_and_rag_preview_are_revision_bound_and_read_only(self):
        view = project_current_store.editor_view(self.store, self.source_id)
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        self.assertEqual(view["revision"], revision)
        self.assertEqual(view["editorial"], paper["editorial"])
        self.assertEqual(view["published"], paper["published"])
        self.assertEqual(view["rag"], paper["rag"])
        self.assertEqual(view["publication_state"], "published")
        live = project_library.store(self.root).current().snapshots["core"]
        self.assertEqual(view["published_content_identity"],
                         project_text.content_identity(live._bundle, self.source_id))
        units = live.document_units(self.source_id)
        source = live._lookup("sources", self.source_id)
        blocks = passage_context.document_blocks(units, title=source["bibliography"]["title"])
        form = {"ranges": f"1-{len(blocks)}", "labels": "1: Fictional evidence"}
        check = {"query": "fictional research", "expected_page": "1",
                 "expected_excerpt": "Fictional research paper"}
        revision = project_current_store.update_check(
            self.store, source_id=self.source_id, revision=revision, form=check)["revision"]
        saved, _ = project_current_store.read_paper(self.store, self.source_id)
        expected_preview = project_chunks.preview(
            live, source, units, project_chunks.parse(units, form), check["query"])
        expected_checks = project_chunks.run_checks(
            live, source, units, project_chunks.parse(units, form), saved["rag"]["checks"])
        before = self.path.read_bytes()
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            actual_preview = project_current_store.preview_rag(
                self.store, source_id=self.source_id, revision=revision, form=form, query=check["query"])
            actual_checks = project_current_store.check_rag(
                self.store, source_id=self.source_id, revision=revision, form=form)
        self.assertTrue(actual_preview)
        self.assertEqual(actual_preview, expected_preview)
        self.assertEqual(actual_checks, expected_checks)
        self.assertEqual(len(actual_checks), 1)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.preview_rag(
                self.store, source_id=self.source_id, revision=view["revision"], form=form, query="fictional")
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.check_rag(
                self.store, source_id=self.source_id, revision=view["revision"], form=form)
        with self.assertRaisesRegex(ValueError, "query"):
            project_current_store.preview_rag(
                self.store, source_id=self.source_id, revision=revision, form=form, query=" ")
        revision = project_current_store.unpublish(
            self.store, source_id=self.source_id, revision=revision,
            reviewer="Tester", reason="Withdraw", checked=True)["revision"]
        withdrawn = project_current_store.editor_view(self.store, self.source_id)
        self.assertIsNone(withdrawn["published"])
        self.assertIsNone(withdrawn["published_content_identity"])
        self.assertEqual(withdrawn["publication_state"], "draft")
        self.assertEqual(withdrawn["rag"], saved["rag"])
        with self.assertRaisesRegex(ValueError, "Publish"):
            project_current_store.check_rag(self.store, source_id=self.source_id, revision=revision, form=form)

    def test_rag_checks_use_one_detached_read_when_paper_changes_during_preview(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        units = paper["published"]["units"]
        blocks = passage_context.document_blocks(units, title=paper["published"]["source"]["bibliography"]["title"])
        form = {"ranges": f"1-{len(blocks)}", "labels": ""}
        check = {"query": "fictional research", "expected_page": "1",
                 "expected_excerpt": "Fictional research paper"}
        build_snapshot = project_current_store._published_snapshot
        updated = []

        def change_after_read(state):
            updated.append(project_current_store.update_check(
                self.store, source_id=self.source_id, revision=revision, form=check)["revision"])
            return build_snapshot(state)

        with patch.object(project_current_store, "_published_snapshot", side_effect=change_after_read):
            results = project_current_store.check_rag(
                self.store, source_id=self.source_id, revision=revision, form=form)
        self.assertEqual(results, [])
        self.assertEqual(len(updated), 1)
        self.assertEqual(len(project_current_store.check_rag(
            self.store, source_id=self.source_id, revision=updated[0], form=form)), 1)
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.check_rag(self.store, source_id=self.source_id, revision=revision, form=form)

    def test_rag_rejects_stale_invalid_and_failed_writes(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            project_current_store.update_rag(self.store, source_id=self.source_id,
                                             revision=revision, form={"ranges": "999"})
        with self.assertRaisesRegex(ValueError, "excerpt"):
            project_current_store.update_check(
                self.store, source_id=self.source_id, revision=revision,
                form={"query": "test", "expected_page": "1", "expected_excerpt": "Not on this page"})
        with patch.object(collections, "atomic_json", side_effect=OSError("Write failed")):
            with self.assertRaisesRegex(OSError, "Write failed"):
                project_current_store.update_rag(self.store, source_id=self.source_id,
                                                 revision=revision, form={"ranges": ""}, activate=True)
        self.assertEqual(self.path.read_bytes(), before)
        project_current_store.update_rag(self.store, source_id=self.source_id,
                                        revision=revision, form={"ranges": ""})
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.update_check(
                self.store, source_id=self.source_id, revision=revision,
                form={"query": "test", "expected_page": "1", "expected_excerpt": "Fictional research paper"})
        current, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertEqual(current["published"], paper["published"])

    def test_detached_retrieval_reads_only_published_and_live_state(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        old = project_current_store.published_snapshot(self.store)
        old_hits = old.call("search_evidence", {"query": "Fictional research"})["result"]["items"]
        self.assertTrue(old_hits)
        source = paper["editorial"]["source"]
        unit = paper["editorial"]["units"][0]
        revision = project_current_store.save_draft(
            self.store, revision=revision, source_id=self.source_id, unit_id=unit["id"],
            text=unit["text"] + "\nNovelpublishedtoken.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correction")["revision"]
        draft_view = project_current_store.published_snapshot(self.store)
        self.assertEqual(old.release_id, draft_view.release_id)
        self.assertEqual(draft_view.call("search_evidence", {"query": "Novelpublishedtoken"})["result"]["items"], [])
        revision = project_current_store.publish(self.store, revision=revision, source_id=self.source_id,
                                                 reviewer="Tester", reason="Publish", checked=True)["revision"]
        published = project_current_store.published_snapshot(self.store)
        self.assertNotEqual(old.release_id, published.release_id)
        self.assertTrue(published.call("search_evidence", {"query": "Novelpublishedtoken"})["result"]["items"])
        self.assertEqual(old.call("search_evidence", {"query": "Novelpublishedtoken"})["result"]["items"], [])
        self.assertEqual(old.call("search_evidence", {"query": "Fictional research"})["result"]["items"], old_hits)
        live, _ = project_current_store.read_paper(self.store, self.source_id)
        blocks = passage_context.document_blocks(live["published"]["units"],
                                                  title=source["bibliography"]["title"])
        form = {"ranges": f"1-{len(blocks)}", "labels": "1: uniquelabeltoken"}
        revision = project_current_store.update_rag(self.store, source_id=self.source_id,
                                                     revision=revision, form=form)["revision"]
        saved = project_current_store.published_snapshot(self.store)
        self.assertEqual(saved.release_id, published.release_id)
        self.assertEqual(saved.call("search_evidence", {"query": "uniquelabeltoken"})["result"]["items"], [])
        project_current_store.update_rag(self.store, source_id=self.source_id, revision=revision,
                                         form=form, activate=True)
        applied = project_current_store.published_snapshot(self.store)
        self.assertNotEqual(applied.release_id, published.release_id)
        self.assertTrue(applied.call("search_evidence", {"query": "uniquelabeltoken"})["result"]["items"])
        self.assertEqual(published.call("search_evidence", {"query": "uniquelabeltoken"})["result"]["items"], [])

    def test_rag_terms_survive_republication_for_review(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source = paper["editorial"]["source"]
        unit = paper["editorial"]["units"][0]
        blocks = passage_context.document_blocks(paper["published"]["units"],
                                                  title=source["bibliography"]["title"])
        revision = project_current_store.update_rag(
            self.store, source_id=self.source_id, revision=revision,
            form={"ranges": f"1-{len(blocks)}", "labels": "1: Important term"}, activate=True)["revision"]
        configured, _ = project_current_store.read_paper(self.store, self.source_id)
        anchor = configured["rag"]["draft"]["term_anchors"]["1"]
        revision = project_current_store.save_draft(
            self.store, revision=revision, source_id=self.source_id, unit_id=unit["id"],
            text=unit["text"] + "\nChanged text.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correction")["revision"]
        revision = project_current_store.publish(self.store, revision=revision, source_id=self.source_id,
                                                 reviewer="Tester", reason="Publish", checked=True)["revision"]
        view = project_current_store.editor_view(self.store, self.source_id)
        self.assertEqual(view["pending_terms"], [{"terms": "Important term", **anchor}])
        revision = project_current_store.update_rag(self.store, source_id=self.source_id,
                                                     revision=revision, form={"ranges": ""})["revision"]
        updated, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertEqual(updated["published"]["chunk_plans"], {})
        self.assertEqual(updated["rag"]["draft"]["unassigned_terms"], [{"terms": "Important term", **anchor}])

    def test_unpublish_preserves_draft_and_saved_rag(self):
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        revision = project_current_store.update_rag(self.store, source_id=self.source_id, revision=revision,
                                                    form={"ranges": ""})["revision"]
        before, _ = project_current_store.read_paper(self.store, self.source_id)
        old = project_current_store.published_snapshot(self.store)
        doi = paper["published"]["source"]["doi"]
        query = {"query": "Fictional research", "paper": doi}
        self.assertTrue(old.call("search_evidence", query)["result"]["items"])
        with self.assertRaisesRegex(ValueError, "Confirm"):
            project_current_store.unpublish(self.store, revision=revision, source_id=self.source_id,
                                            reviewer="Tester", reason="Withdraw", checked=False)
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected release export")):
            result = project_current_store.unpublish(self.store, revision=revision, source_id=self.source_id,
                                                     reviewer="Tester", reason="Withdraw", checked=True)
        withdrawn, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertIsNone(withdrawn["published"])
        self.assertEqual(withdrawn["editorial"], before["editorial"])
        self.assertEqual(withdrawn["rag"], before["rag"])
        self.assertEqual(withdrawn["audit"]["unpublication"]["scope"], "unpublication")
        self.assertEqual(project_current_store.published_snapshot(self.store).call(
            "search_evidence", query)["result"]["items"], [])
        self.assertTrue(old.call("search_evidence", query)["result"]["items"])
        with self.assertRaisesRegex(ValueError, "changed"):
            project_current_store.unpublish(self.store, revision=revision, source_id=self.source_id,
                                            reviewer="Tester", reason="Withdraw", checked=True)
        result = project_current_store.publish(self.store, revision=result["revision"], source_id=self.source_id,
                                               reviewer="Tester", reason="Republish", checked=True)
        republished, _ = project_current_store.read_paper(self.store, self.source_id)
        self.assertEqual(republished["rag"], before["rag"])
        self.assertTrue(project_current_store.published_snapshot(self.store).call(
            "search_evidence", query)["result"]["items"])

    def test_collection_endpoint_mcp_pins_current_evidence(self):
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        with self.assertRaises(releases.AccessDeniedError):
            project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"curated"})

        async def exercise():
            async with create_connected_server_and_client_session(server.create_server(None, endpoint)) as client:
                initial = await client.call_tool("search_evidence", {"query": "Fictional research"})
                self.assertFalse(initial.isError)
                response = initial.structuredContent
                snapshot_id = response["collection_context"]["snapshot_id"]
                unit_id = response["result"]["items"][0]["id"]
                context = await client.call_tool("get_source_context", {
                    "unit_id": unit_id, "collections": ["core"], "snapshot_id": snapshot_id})
                self.assertFalse(context.isError)
                unpinned = await client.call_tool("get_source_context", {
                    "unit_id": unit_id, "collections": ["core"]})
                self.assertEqual(unpinned.structuredContent["error"]["code"], "invalid_input")
                paper, revision = project_current_store.read_paper(self.store, self.source_id)
                source = paper["editorial"]["source"]
                unit = paper["editorial"]["units"][0]
                revision = project_current_store.save_draft(
                    self.store, revision=revision, source_id=self.source_id, unit_id=unit["id"],
                    text=unit["text"] + "\nMcpnovelword.",
                    bibliography={key: value or "" for key, value in source["bibliography"].items()},
                    source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correction")["revision"]
                unchanged = await client.call_tool("get_source_context", {
                    "unit_id": unit_id, "collections": ["core"], "snapshot_id": snapshot_id})
                self.assertFalse(unchanged.isError)
                project_current_store.publish(self.store, revision=revision, source_id=self.source_id,
                                              reviewer="Tester", reason="Publish", checked=True)
                stale = await client.call_tool("get_source_context", {
                    "unit_id": unit_id, "collections": ["core"], "snapshot_id": snapshot_id})
                self.assertTrue(stale.isError)
                self.assertEqual(stale.structuredContent["error"]["code"], "unavailable")
                updated = await client.call_tool("search_evidence", {"query": "Mcpnovelword"})
                self.assertTrue(updated.structuredContent["result"]["items"])
                self.assertNotEqual(snapshot_id, updated.structuredContent["collection_context"]["snapshot_id"])
        asyncio.run(exercise())

    def test_current_state_stdio_flag(self):
        async def exercise():
            with tempfile.TemporaryFile(mode="w+") as diagnostics:
                params = StdioServerParameters(command=sys.executable, args=[
                    "-m", "research_library.server", "--current-state", str(self.store),
                    "--allow-profile", "private_context"])
                async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=20)) as client:
                        await client.initialize()
                        response = await client.call_tool("search_evidence", {"query": "Fictional research"})
                        self.assertFalse(response.isError)
                        self.assertTrue(response.structuredContent["result"]["items"])
        asyncio.run(exercise())

    def test_current_state_http_authentication_and_pinning(self):
        credentials = self.base / "configuration-credentials.json"
        http_auth.issue(credentials, self.base / "configuration-token", "http://127.0.0.1:8000/mcp",
                        ["private_context"])
        options = {"registry": credentials, "base_url": "http://127.0.0.1:8000/mcp"}
        with self.assertRaises(releases.AccessDeniedError):
            http_server.create_app(current_state=self.store, profiles={"curated"}, **options)
        with self.assertRaises(ValueError):
            http_server.create_app(current_state=self.store, collection_registry=self.root / "registry",
                                   allowed_collections=["core"], profiles={"private_context"}, **options)
        with self.assertRaises(ValueError):
            http_server.create_app(current_state=self.store, allowed_collections=["core"],
                                   profiles={"private_context"}, **options)
        with running_server(self.base, None, profiles=("private_context",), current_state=self.store) as (
                url, token, _, _, _):
            self.assertEqual(httpx.post(url, json={}).status_code, 401)

            async def exercise():
                async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
                    async with streamable_http_client(url, http_client=http) as (reader, writer, _):
                        async with ClientSession(reader, writer) as client:
                            await client.initialize()
                            response = await client.call_tool("search_evidence", {"query": "Fictional research"})
                            self.assertFalse(response.isError)
                            initial = response.structuredContent
                            snapshot_id = initial["collection_context"]["snapshot_id"]
                            unit_id = initial["result"]["items"][0]["id"]
                            paper, revision = project_current_store.read_paper(self.store, self.source_id)
                            project_current_store.unpublish(
                                self.store, revision=revision, source_id=self.source_id,
                                reviewer="Tester", reason="Withdraw", checked=True)
                            stale = await client.call_tool("get_source_context", {
                                "unit_id": unit_id, "collections": ["core"], "snapshot_id": snapshot_id})
                            self.assertTrue(stale.isError)
                            self.assertEqual(stale.structuredContent["error"]["code"], "unavailable")
                            current = await client.call_tool("search_evidence", {
                                "query": "Fictional research", "paper": paper["published"]["source"]["doi"]})
                            self.assertEqual(current.structuredContent["result"]["items"], [])
            asyncio.run(exercise())
            original = self.path.read_bytes()
            self.path.write_bytes(b"invalid")
            self.assertEqual(httpx.get(url.replace("/mcp", "/readyz")).status_code, 503)
            self.path.write_bytes(original)
            self.assertEqual(httpx.get(url.replace("/mcp", "/readyz")).status_code, 200)

    def test_collection_snapshot_tracks_live_rag_not_saved_drafts(self):
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        original = endpoint.current()
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        blocks = passage_context.document_blocks(paper["published"]["units"],
                                                  title=paper["published"]["source"]["bibliography"]["title"])
        form = {"ranges": f"1-{len(blocks)}", "labels": "1: uniquelabeltoken"}
        revision = project_current_store.update_rag(self.store, source_id=self.source_id,
                                                    revision=revision, form=form)["revision"]
        self.assertEqual(endpoint.current().release_id, original.release_id)
        self.assertEqual(endpoint.load(original.release_id).call(
            "search_evidence", {"query": "uniquelabeltoken"})["result"]["items"], [])
        project_current_store.update_rag(self.store, source_id=self.source_id,
                                         revision=revision, form=form, activate=True)
        with self.assertRaises(collections.SnapshotUnavailable):
            endpoint.load(original.release_id)
        active = endpoint.current()
        self.assertTrue(active.call("search_evidence", {"query": "uniquelabeltoken"})["result"]["items"])
        self.assertEqual(original.call("search_evidence", {"query": "uniquelabeltoken"})["result"]["items"], [])

    def test_collection_endpoint_fails_closed_on_invalid_current_state(self):
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        original = self.path.read_bytes()
        self.path.write_bytes(b'{"source_id":"wrong"}')
        with self.assertRaises(releases.ReleaseError):
            endpoint.current()
        self.path.write_bytes(original)
        self.assertTrue(endpoint.call("search_evidence", {"query": "Fictional research"})["result"]["items"])

    def test_collection_cache_validates_changed_bytes_and_rebuilds_only_live_changes(self):
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        with patch.object(releases, "_index", wraps=releases._index) as build:
            original = endpoint.current()
            self.assertEqual(build.call_count, 1)
            self.assertIs(endpoint.current(), original)
            self.assertEqual(build.call_count, 1)
            paper, revision = project_current_store.read_paper(self.store, self.source_id)
            source = paper["editorial"]["source"]
            unit = paper["editorial"]["units"][0]
            revision = project_current_store.save_draft(
                self.store, revision=revision, source_id=self.source_id, unit_id=unit["id"],
                text=unit["text"] + "\nCachechangedtoken.",
                bibliography={key: value or "" for key, value in source["bibliography"].items()},
                source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correction")["revision"]
            self.assertIs(endpoint.current(), original)
            self.assertEqual(build.call_count, 1)
            project_current_store.publish(self.store, revision=revision, source_id=self.source_id,
                                          reviewer="Tester", reason="Publish", checked=True)
            current = endpoint.current()
            self.assertNotEqual(current.release_id, original.release_id)
            self.assertEqual(build.call_count, 2)
            self.assertTrue(current.call("search_evidence", {"query": "Cachechangedtoken"})["result"]["items"])
            self.assertIs(endpoint.current(), current)
            self.assertEqual(build.call_count, 2)

        data = self.path.read_bytes()
        stat = self.path.stat()
        self.path.write_bytes(b"!" + data[1:])
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(releases.ReleaseError):
            endpoint.current()
        self.path.write_bytes(data)
        self.assertEqual(endpoint.current().release_id, current.release_id)

    def test_collection_cache_retries_after_index_failure(self):
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        old = endpoint.current()
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        project_current_store.unpublish(self.store, revision=revision, source_id=self.source_id,
                                        reviewer="Tester", reason="Withdraw", checked=True)
        with patch.object(releases, "_index", side_effect=OSError("Index failed")):
            with self.assertRaisesRegex(OSError, "Index failed"):
                endpoint.current()
        with self.assertRaises(collections.SnapshotUnavailable):
            endpoint.load(old.release_id)
        new = endpoint.current()
        self.assertNotEqual(new.release_id, old.release_id)
        self.assertEqual(new.call("search_evidence", {
            "query": "Fictional research", "paper": paper["published"]["source"]["doi"]})["result"]["items"], [])

    def test_collection_reads_remain_coherent_during_publication(self):
        endpoint = project_current_store.CurrentCollectionStore(self.store, allowed_profiles={"private_context"})
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        doi = paper["published"]["source"]["doi"]
        started = threading.Event()
        resume = threading.Event()
        snapshots, errors = [], []
        original_index = releases._index

        def delayed_index(*args, **kwargs):
            started.set()
            if not resume.wait(10):
                raise TimeoutError("Index build did not resume")
            return original_index(*args, **kwargs)

        def read_snapshot():
            try:
                snapshots.append(endpoint.current())
            except Exception as error:
                errors.append(error)

        with patch.object(releases, "_index", side_effect=delayed_index):
            thread = threading.Thread(target=read_snapshot)
            thread.start()
            try:
                self.assertTrue(started.wait(10))
                project_current_store.unpublish(
                    self.store, revision=revision, source_id=self.source_id,
                    reviewer="Tester", reason="Withdraw", checked=True)
            finally:
                resume.set()
                thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(snapshots), 1)
        query = {"query": "Fictional research", "paper": doi}
        self.assertTrue(snapshots[0].call("search_evidence", query)["result"]["items"])
        self.assertEqual(endpoint.current().call("search_evidence", query)["result"]["items"], [])
        with self.assertRaises(collections.SnapshotUnavailable):
            endpoint.load(snapshots[0].release_id)

    def test_read_locks_allow_readers_and_exclude_writer(self):
        entered = threading.Event()
        leave = threading.Event()
        errors = []

        def reader():
            try:
                with literature_update.reader_lock(self.store):
                    entered.set()
                    if not leave.wait(10):
                        raise TimeoutError("Reader did not complete")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual(project_current_store.read(self.store), self.state)
            paper, revision = project_current_store.read_paper(self.store, self.source_id)
            with self.assertRaisesRegex(ValueError, "writer holds"):
                project_current_store.replace(self.store, paper, expected=revision)
        finally:
            leave.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(project_current_store.replace(self.store, paper, expected=revision), revision)

    def test_refuses_existing_destination_and_cross_paper_dependencies(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_current_store.initialize(self.state, self.store)
        invalid = deepcopy(self.state)
        invalid["editorial"]["bundle"]["claims"] = [{"id": "unexpected"}]
        with self.assertRaisesRegex(ValueError, "dependencies"):
            project_current_store.initialize(invalid, self.base / "invalid")
        self.assertFalse((self.base / "invalid").exists())

    def test_prepare_and_recover_exact_current_state_without_touching_project(self):
        source_id = self.source_id
        published = project_library.store(self.root).current()
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": "1", "labels": "1: Meaningful term"}, activate=True)
        published = project_library.store(self.root).current()
        project_chunks.update(self.root, source_id=source_id, snapshot_id=published.release_id,
                              form={"ranges": ""}, activate=False)
        project_chunks.update_check(self.root, source_id=source_id, snapshot_id=published.release_id,
                                    form={"query": "fictional", "expected_page": "1",
                                          "expected_excerpt": "Fictional research paper"})
        expected = project_state.current(self.root)
        destination = self.base / "prepared"
        result = project_current_store.prepare(self.root, destination)
        self.assertTrue(result["recovery_verified"])
        self.assertFalse(result["connected_to_project"])
        self.assertEqual(project_current_store.read(destination), expected)
        self.assertEqual(project_state.current(self.root), expected)
        recovered = self.base / "recovered"
        result = project_current_store.restore_project(destination, recovered)
        self.assertFalse(result["pdfs_restored"])
        self.assertFalse(result["discovery_restored"])
        self.assertEqual(project_state.current(recovered), expected)
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_current_store.prepare(self.root, destination)
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_current_store.restore_project(destination, recovered)
        self.assertEqual(project_state.current(self.root), expected)

    def test_full_backup_and_recovery_preserve_current_and_auxiliary_data(self):
        pdf_data = self.base / "pdf-data"
        pdf_data.mkdir()
        (pdf_data / "local.pdf").write_bytes(b"synthetic extra PDF")
        (self.root / "discovery").mkdir(exist_ok=True)
        (self.root / "discovery" / "saved.txt").write_text("Saved discovery")
        (self.store / "batch-pdf.json").write_text('{"status":"synthetic"}')
        (self.store / "discovery").mkdir()
        (self.store / "discovery" / "saved.txt").write_text("New discovery")
        paper, revision = project_current_store.read_paper(self.store, self.source_id)
        source = paper["editorial"]["source"]
        unit = paper["editorial"]["units"][0]
        revision = project_current_store.save_draft(
            self.store, revision=revision, source_id=self.source_id, unit_id=unit["id"],
            text=unit["text"] + "\nUnpublished draft.", bibliography={
                key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correct text")["revision"]
        project_current_store.update_rag(self.store, source_id=self.source_id, revision=revision,
                                         form={"ranges": ""})
        current, _ = project_current_store.editorial_collection(self.store)
        source = next(item for item in current.snapshots["core"]._bundle["sources"] if item["id"] == self.source_id)
        quality.inventory(self.store / "quality", source, [{"kind": "pdf", "pdf_page": 1}])
        before_project, before_current = project_state.current(self.root), project_current_store.read(self.store)
        backup, recovered = self.base / "full-backup", self.base / "recovered-full"
        with self.assertRaisesRegex(ValueError, "Stop the website"):
            project_backup.create(self.root, pdf_data, backup, current_state=self.store)
        result = project_backup.create(self.root, pdf_data, backup, current_state=self.store, writers_stopped=True)
        self.assertTrue(result["verified"])
        self.assertEqual(project_backup.verify(backup)["trees"], ["current-state", "pdf-data", "project"])
        self.assertTrue(project_backup.restore(backup, recovered)["verified"])
        self.assertEqual(project_state.current(recovered / "project"), before_project)
        self.assertEqual(project_current_store.read(recovered / "current-state"), before_current)
        self.assertEqual((recovered / "pdf-data/local.pdf").read_bytes(), b"synthetic extra PDF")
        self.assertEqual((recovered / "project/discovery/saved.txt").read_text(), "Saved discovery")
        self.assertEqual((recovered / "current-state/discovery/saved.txt").read_text(), "New discovery")
        self.assertEqual(project_current_store._quality_files(recovered / "current-state/quality"),
                         project_current_store._quality_files(self.store / "quality"))
        self.assertEqual(project_state.current(self.root), before_project)
        self.assertEqual(project_current_store.read(self.store), before_current)

    def test_full_restore_aborts_if_backup_changes_mid_copy(self):
        pdf_data = self.base / "pdf-data"
        pdf_data.mkdir()
        (pdf_data / "source.pdf").write_bytes(b"synthetic")
        backup, destination = self.base / "backup", self.base / "recovered"
        project_backup.create(self.root, pdf_data, backup, writers_stopped=True)
        original_copy = project_backup._copy

        def mutate(root, target, entries):
            original_copy(root, target, entries)
            if root.name == "pdf-data":
                (root / "source.pdf").write_bytes(b"changed")

        with patch.object(project_backup, "_copy", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "differs from manifest"):
                project_backup.restore(backup, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".recovered-restore-") for path in self.base.iterdir()))

    def test_full_backup_rejects_changes_corruption_and_unsafe_paths(self):
        pdf_data = self.base / "pdf-data"
        pdf_data.mkdir()
        pdf = pdf_data / "sample.pdf"
        pdf.write_bytes(b"PDF")
        destination = self.base / "full-backup"
        with self.assertRaisesRegex(ValueError, "non-overlapping"):
            project_backup.create(self.root, pdf_data, self.root / "backup", writers_stopped=True)
        link = pdf_data / "link"
        link.symlink_to(pdf)
        with self.assertRaisesRegex(ValueError, "symlink"):
            project_backup.create(self.root, pdf_data, destination, writers_stopped=True)
        link.unlink()
        original_copy = project_backup._copy

        def mutate(root, target, entries):
            original_copy(root, target, entries)
            if root == pdf_data:
                pdf.write_bytes(b"changed")

        with patch.object(project_backup, "_copy", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed during backup"):
                project_backup.create(self.root, pdf_data, destination, writers_stopped=True)
        self.assertFalse(destination.exists())
        project_backup.create(self.root, pdf_data, destination, writers_stopped=True)
        (destination / "pdf-data" / "sample.pdf").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "differs from manifest"):
            project_backup.restore(destination, self.base / "bad-restore")
        self.assertFalse((self.base / "bad-restore").exists())

    def test_full_backup_cli_verifies_and_restores(self):
        pdf_data = self.base / "pdf-data"
        pdf_data.mkdir()
        (pdf_data / "source.pdf").write_bytes(b"synthetic")
        backup = self.base / "cli-backup"
        restore = self.base / "cli-recovered"
        executable = Path(sys.executable).parent / "research-project"
        arguments = [str(executable), "--root", str(self.root)]
        for command in (
                ["backup-project", str(backup), "--pdf-data-root", str(pdf_data),
                 "--current-state", str(self.store), "--confirm-writers-stopped"],
                ["verify-project-backup", str(backup)],
                ["restore-project-backup", str(backup), "--destination", str(restore)]):
            result = subprocess.run(arguments + command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"verified": true', result.stdout)
        self.assertEqual(project_state.current(restore / "project"), self.state)
        self.assertEqual(project_current_store.read(restore / "current-state"), self.state)
        self.assertEqual((restore / "pdf-data/source.pdf").read_bytes(), b"synthetic")

    def test_start_from_preserved_backup_builds_isolated_usable_project(self):
        source_id = self.source_id
        paper, revision = project_current_store.read_paper(self.store, source_id)
        source, unit = paper["editorial"]["source"], paper["editorial"]["units"][0]
        project_current_store.save_draft(
            self.store, source_id=source_id, revision=revision, unit_id=unit["id"],
            text=unit["text"] + "\nFreshly preserved correction.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Compared")
        pdf_data = self.base / "pdf-data"
        pdf_data.mkdir()
        backup = self.base / "archive"
        project_backup.create(self.root, pdf_data, backup, current_state=self.store, writers_stopped=True)
        original = project_current_store.read(self.store)
        rows = [{"doi": source["doi"], "title": "Selected paper"},
                {"doi": "10.0000/new", "title": "New paper"}]
        destination = self.base / "restart"
        with patch.object(releases, "export", side_effect=AssertionError("Unexpected snapshot")), \
             patch.object(project_library.literature_ingest, "extract_pdf", side_effect=AssertionError("Re-extracted")):
            result = project_backup.start_from_preserved(
                backup, destination, name="New project", authorization="Private", rows=rows)
        self.assertEqual(result["restored_drafts"], 1)
        self.assertEqual(result["published"], 0)
        store, project = destination / "current-state", destination / "project"
        state = project_current_store.read(store)
        self.assertEqual(len(state["editorial"]["bundle"]["sources"]), 1)
        self.assertEqual(state["published"]["bundle"]["sources"], [])
        self.assertFalse((project / "discovery").exists())
        self.assertFalse((project / "registry").exists())
        copied_pdf = project / "papers" / (source["sha256"] + ".pdf")
        self.assertEqual(releases.sha256(copied_pdf.read_bytes()), source["sha256"])
        client = web_server.create_app(current_store=store, project_root=project,
                                       pdf_data_root=pdf_data, profiles={"private_context"}).test_client()
        base = "http://localhost:8080"
        identity = collections.publication(source["doi"])
        response = client.get("/library/paper", query_string={"id": identity}, base_url=base)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Freshly preserved correction.", response.get_data(as_text=True))
        self.assertEqual(client.get("/library/paper/pdf", query_string={"id": identity}, base_url=base).status_code, 200)
        self.assertEqual(client.get("/search?query=Freshly", base_url=base).status_code, 200)
        endpoint = project_current_store.CurrentCollectionStore(store, allowed_profiles={"private_context"})
        self.assertFalse(endpoint.current().call("search_evidence", {"query": "Freshly"})["result"]["items"])
        restored, revision = project_current_store.read_paper(store, source_id)
        self.assertIsNone(restored["rag"])
        self.assertIsNone(restored["published"])
        project_current_store.publish(store, source_id=source_id, revision=revision,
                                      reviewer="Tester", reason="Compared", checked=True)
        self.assertTrue(endpoint.current().call("search_evidence", {"query": "Freshly"})["result"]["items"])
        self.assertEqual(project_current_store.read(self.store), original)
        self.assertTrue(project_backup.verify(backup)["verified"])

    def test_start_from_preserved_cli_and_failure_preserve_archive(self):
        pdf_data = self.base / "pdf-data"
        pdf_data.mkdir()
        backup = self.base / "archive"
        project_backup.create(self.root, pdf_data, backup, current_state=self.store, writers_stopped=True)
        source = project_current_store.read_paper(self.store, self.source_id)[0]["editorial"]["source"]
        rows = self.base / "papers.csv"
        rows.write_text(f'doi,title\n{source["doi"]},Selected\n', encoding="utf-8")
        executable = Path(sys.executable).parent / "research-project"
        destination = self.base / "new-project"
        command = [str(executable), "start-from-preserved", str(backup), "--destination", str(destination),
                   "--name", "New", "--authorization", "Private", "--papers", str(rows)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"restored_drafts": 1', result.stdout)
        retry = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(retry.returncode, 0)
        bad = backup / "project" / "papers" / (source["sha256"] + ".pdf")
        bad.write_bytes(b"corrupted")
        failed = self.base / "failed-project"
        with self.assertRaisesRegex(ValueError, "differs from manifest"):
            project_backup.start_from_preserved(backup, failed, name="New", authorization="Private",
                                                rows=[{"doi": source["doi"], "title": "Selected"}])
        self.assertFalse(failed.exists())
        self.assertEqual(project_current_store.read(self.store), self.state)

    def test_start_from_preserved_fails_without_current_state_or_on_copy_error(self):
        pdf_data = self.base / "pdf-data"
        pdf_data.mkdir()
        backup = self.base / "archive"
        rows = [{"doi": "10.0000/one", "title": "Selected"}]
        project_backup.create(self.root, pdf_data, backup, writers_stopped=True)
        destination = self.base / "restart"
        with self.assertRaisesRegex(ValueError, "must include a current-state store"):
            project_backup.start_from_preserved(backup, destination, name="New",
                                                authorization="Private", rows=rows)
        self.assertFalse(destination.exists())
        backup = self.base / "archive-current"
        project_backup.create(self.root, pdf_data, backup, current_state=self.store, writers_stopped=True)
        with patch.object(project_backup.shutil, "copyfileobj", side_effect=OSError("Disk failure")):
            with self.assertRaisesRegex(OSError, "Disk failure"):
                project_backup.start_from_preserved(backup, destination, name="New",
                                                    authorization="Private", rows=rows)
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".restart-restart-") for path in self.base.iterdir()))
        self.assertTrue(project_backup.verify(backup)["verified"])

    def test_prepare_copies_valid_quality_records_with_confirmation(self):
        source_root = self.root / "releases/data-quality"
        evidence = project_text.editorial_store(self.root).current().snapshots["core"]
        source = next(item for item in evidence._bundle["sources"] if item["id"] == self.source_id)
        quality.inventory(source_root, source, [{"kind": "pdf", "pdf_page": 1}])
        report = quality.report(evidence, source_root)
        finding = next(item for item in report["findings"]
                       if item["source_id"] == self.source_id and item["code"] == "coverage_review")
        quality.review(evidence, source_root, finding["id"], "verified", "Tester", "Compared to the original.")
        unit = next(item for item in evidence._bundle["units"]
                    if item["citation"]["source_id"] == self.source_id)
        quality.problem(evidence, source_root, unit["id"], "Tester", "Known extraction problem.")
        expected = project_current_store._quality_files(source_root)
        destination = self.base / "prepared-quality"
        with self.assertRaisesRegex(ValueError, "Stop all quality"):
            project_current_store.prepare(self.root, destination)
        self.assertFalse(destination.exists())
        result = project_current_store.prepare(self.root, destination, confirm_quality_writers_stopped=True)
        self.assertEqual(result["quality_records_copied"], 3)
        self.assertEqual(project_current_store._quality_files(destination / "quality"), expected)
        current, _ = project_current_store.editorial_collection(destination)
        after = quality.report(current.snapshots["core"], destination / "quality")
        self.assertEqual(after["findings"], quality.report(evidence, source_root)["findings"])
        self.assertTrue(next(item for item in after["findings"] if item["id"] == finding["id"])["resolved"])
        self.assertIn("reported_defect", {item["code"] for item in after["findings"]})
        self.assertEqual(project_current_store._quality_files(source_root), expected)

    def test_prepare_quality_fails_closed_for_corruption_and_concurrent_changes(self):
        source_root = self.root / "releases/data-quality"
        evidence = project_text.editorial_store(self.root).current().snapshots["core"]
        source = next(item for item in evidence._bundle["sources"] if item["id"] == self.source_id)
        quality.inventory(source_root, source, [{"kind": "pdf", "pdf_page": 1}])
        destination = self.base / "prepared-quality"
        real_read = project_current_store._quality_files
        calls = 0

        def changing(root):
            nonlocal calls
            calls += 1
            result = real_read(root)
            if calls == 3:
                return {}
            return result

        with patch.object(project_current_store, "_quality_files", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "changed during preparation"):
                project_current_store.prepare(self.root, destination, confirm_quality_writers_stopped=True)
        self.assertFalse(destination.exists())
        inventory = next((source_root / "inventory").iterdir())
        inventory.write_text("{}")
        with self.assertRaises(ValueError):
            project_current_store.prepare(self.root, destination, confirm_quality_writers_stopped=True)
        self.assertFalse(destination.exists())
        self.assertEqual(project_state.current(self.root), self.state)

    def test_prepare_cli_custom_quality_directory_and_rejects_symlinks(self):
        source_root = self.base / "custom-quality"
        evidence = project_text.editorial_store(self.root).current().snapshots["core"]
        source = next(item for item in evidence._bundle["sources"] if item["id"] == self.source_id)
        quality.inventory(source_root, source, [{"kind": "pdf", "pdf_page": 1}])
        executable = Path(sys.executable).parent / "research-project"
        destination = self.base / "prepared-custom-quality"
        args = [str(executable), "--root", str(self.root), "prepare-current-store", str(destination),
                "--quality-root", str(source_root)]
        refused = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(destination.exists())
        result = subprocess.run(args + ["--confirm-quality-writers-stopped"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"quality_records_copied": 1', result.stdout)
        self.assertEqual(project_current_store._quality_files(source_root),
                         project_current_store._quality_files(destination / "quality"))
        alias = self.base / "quality-alias"
        alias.symlink_to(source_root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            project_current_store.prepare(self.root, self.base / "symlink-quality", quality_root=alias,
                                          confirm_quality_writers_stopped=True)

    def test_restore_current_store_preserves_quality_and_leaves_source_unchanged(self):
        state = project_current_store.read(self.store)
        current, _ = project_current_store.editorial_collection(self.store)
        evidence = current.snapshots["core"]
        source = next(item for item in evidence._bundle["sources"] if item["id"] == self.source_id)
        quality_root = self.store / "quality"
        quality.inventory(quality_root, source, [{"kind": "pdf", "pdf_page": 1}])
        report = quality.report(evidence, quality_root)
        finding = next(item for item in report["findings"]
                       if item["source_id"] == self.source_id and item["code"] == "coverage_review")
        quality.review(evidence, quality_root, finding["id"], "verified", "Tester", "Compared original.")
        expected = project_current_store._quality_files(quality_root)
        destination = self.base / "restored-with-quality"
        result = project_current_store.restore_project(self.store, destination)
        self.assertEqual(result["project"], str(destination))
        self.assertEqual(result["quality_records_restored"], 2)
        self.assertFalse(result["pdfs_restored"])
        self.assertFalse(result["discovery_restored"])
        self.assertEqual(project_state.current(destination), state)
        copied = destination / "releases/data-quality"
        self.assertEqual(project_current_store._quality_files(copied), expected)
        restored = project_text.editorial_store(destination).current().snapshots["core"]
        self.assertEqual(quality.report(restored, copied)["findings"], quality.report(evidence, quality_root)["findings"])
        self.assertEqual(project_current_store.read(self.store), state)
        self.assertEqual(project_current_store._quality_files(quality_root), expected)
        with self.assertRaisesRegex(ValueError, "already exists"):
            project_current_store.restore_project(self.store, destination)

    def test_restore_current_store_quality_failure_leaves_no_destination(self):
        current, _ = project_current_store.editorial_collection(self.store)
        source = next(item for item in current.snapshots["core"]._bundle["sources"] if item["id"] == self.source_id)
        quality.inventory(self.store / "quality", source, [{"kind": "pdf", "pdf_page": 1}])
        destination = self.base / "failed-restore"
        with patch.object(project_current_store, "_write_quality_files", side_effect=OSError("Disk failure")):
            with self.assertRaisesRegex(OSError, "Disk failure"):
                project_current_store.restore_project(self.store, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".failed-restore-current-restore-") for path in self.base.iterdir()))
        inventory = next((self.store / "quality/inventory").iterdir())
        inventory.write_text("{}")
        with self.assertRaises(ValueError):
            project_current_store.restore_project(self.store, destination)
        self.assertFalse(destination.exists())
        with self.assertRaisesRegex(ValueError, "separate"):
            project_current_store.restore_project(self.store, self.store / "nested")

    def test_restore_refuses_active_current_store_writer(self):
        entered, leave = threading.Event(), threading.Event()
        errors = []

        def writer():
            try:
                with literature_update.writer_lock(self.store):
                    entered.set()
                    if not leave.wait(10):
                        raise TimeoutError("Writer did not finish")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            destination = self.base / "busy-restore"
            with self.assertRaisesRegex(ValueError, "writer holds"):
                project_current_store.restore_project(self.store, destination)
            self.assertFalse(destination.exists())
        finally:
            leave.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_prepare_fails_closed_and_rejects_project_paths(self):
        destination = self.base / "prepared"
        with self.assertRaisesRegex(ValueError, "outside"):
            project_current_store.prepare(self.root, self.root / "current")
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            project_current_store.prepare(self.root, alias / "current")
        empty = self.base / "empty-project"
        project_library.initialize(empty, "Empty", "Private permission")
        with self.assertRaisesRegex(ValueError, "Attach a paper"):
            project_current_store.prepare(empty, self.base / "empty-store")
        self.assertFalse((self.base / "empty-store").exists())
        with patch.object(project_state, "restore_state", side_effect=ValueError("Invalid recovery")):
            with self.assertRaisesRegex(ValueError, "Invalid recovery"):
                project_current_store.prepare(self.root, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(project_state.current(self.root), self.state)
        self.assertFalse(any(path.name.startswith(".prepared-prepare-") for path in self.base.iterdir()))
        with patch.object(project_state, "current", side_effect=[self.state, {"unexpected": True}]):
            with self.assertRaisesRegex(ValueError, "verification failed"):
                project_current_store.prepare(self.root, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(project_state.current(self.root), self.state)

    def test_prepare_can_disable_git_markdown_only_in_experimental_copy(self):
        project = project_library.load(self.root)
        project["git_markdown_tracking"] = True
        collections.atomic_json(self.root, "project.json", project)
        original = project_state.current(self.root)
        destination = self.base / "without-git-markdown"
        result = project_current_store.prepare(self.root, destination, without_git_markdown=True)
        copied = project_current_store.read(destination)
        expected = deepcopy(original)
        expected["project"]["git_markdown_tracking"] = False
        self.assertEqual(copied, expected)
        self.assertFalse(result["git_markdown_tracking"])
        self.assertEqual(project_state.current(self.root), original)
        self.assertEqual(web_server.create_app(
            current_store=destination, project_root=self.root, profiles={"private_context"}
        ).test_client().get("/library", base_url="http://localhost:8080").status_code, 200)

    def test_recover_store_after_edit_publish_rag_and_withdrawal(self):
        source_id = self.source_id
        paper, revision = project_current_store.read_paper(self.store, source_id)
        source = paper["editorial"]["source"]
        unit = paper["editorial"]["units"][0]
        revision = project_current_store.save_draft(
            self.store, revision=revision, source_id=source_id, unit_id=unit["id"],
            text=unit["text"] + "\nRecovered published edit.",
            bibliography={key: value or "" for key, value in source["bibliography"].items()},
            source_url=source["url"] or "", tables=[], reviewer="Tester", reason="Correction")["revision"]
        revision = project_current_store.publish(self.store, revision=revision, source_id=source_id,
                                                 reviewer="Tester", reason="Publish", checked=True)["revision"]
        revised, _ = project_current_store.read_paper(self.store, source_id)
        blocks = passage_context.document_blocks(revised["published"]["units"],
                                                  title=source["bibliography"]["title"])
        revision = project_current_store.update_rag(
            self.store, source_id=source_id, revision=revision,
            form={"ranges": f"1-{len(blocks)}", "labels": "1: Test recovery term"}, activate=True)["revision"]
        revision = project_current_store.update_check(
            self.store, source_id=source_id, revision=revision,
            form={"query": "fictional", "expected_page": "1",
                  "expected_excerpt": "Fictional research paper"})["revision"]
        project_current_store.update_rag(self.store, source_id=source_id, revision=revision,
                                         form={"ranges": ""})
        restored = self.base / "after-edit"
        project_current_store.restore_project(self.store, restored)
        self.assertEqual(project_state.current(restored), project_current_store.read(self.store))
        self.assertTrue(project_library.store(restored).call(
            "search_evidence", {"query": "Test recovery term"})["result"]["items"])

        _, revision = project_current_store.read_paper(self.store, source_id)
        project_current_store.unpublish(self.store, revision=revision, source_id=source_id,
                                        reviewer="Tester", reason="Withdraw", checked=True)
        withdrawn = self.base / "after-withdrawal"
        project_current_store.restore_project(self.store, withdrawn)
        self.assertEqual(project_state.current(withdrawn), project_current_store.read(self.store))
        self.assertFalse(project_library.store(withdrawn).call(
            "search_evidence", {"query": "Recovered published edit"})["result"]["items"])

    def test_prepare_and_restore_commands(self):
        destination = self.base / "prepared-cli"
        executable = Path(sys.executable).parent / "research-project"
        prepare = subprocess.run(
            [str(executable), "--root", str(self.root), "prepare-current-store", str(destination)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        current, _ = project_current_store.editorial_collection(destination)
        source = next(item for item in current.snapshots["core"]._bundle["sources"] if item["id"] == self.source_id)
        quality.inventory(destination / "quality", source, [{"kind": "pdf", "pdf_page": 1}])
        recovered = self.base / "recovered-cli"
        restore = subprocess.run(
            [str(executable), "--root", str(recovered), "restore-current-store", str(destination)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(restore.returncode, 0, restore.stderr)
        self.assertIn('"quality_records_restored": 1', restore.stdout)
        self.assertEqual(project_current_store._quality_files(destination / "quality"),
                         project_current_store._quality_files(recovered / "releases/data-quality"))
        self.assertEqual(project_state.current(recovered), self.state)
        project = project_library.load(self.root)
        project["git_markdown_tracking"] = True
        collections.atomic_json(self.root, "project.json", project)
        destination = self.base / "prepared-without-markdown"
        prepare = subprocess.run(
            [str(executable), "--root", str(self.root), "prepare-current-store", str(destination),
             "--without-git-markdown"], capture_output=True, text=True, timeout=30)
        self.assertEqual(prepare.returncode, 0, prepare.stderr)
        self.assertFalse(project_current_store.read(destination)["project"]["git_markdown_tracking"])
        self.assertTrue(project_library.load(self.root)["git_markdown_tracking"])


if __name__ == "__main__":
    unittest.main()
