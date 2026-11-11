"""Persistent, sequential PDF location and operator-confirmed extraction queue."""

from pathlib import Path
import sqlite3

import httpx

from research_library import collections, literature, releases
from . import citation_neighborhood, literature_update, project_library


NAME = "batch-pdf.json"


def load(root):
    root = Path(root)
    if not (root / NAME).exists():
        return {"schema_version": 1, "status": "idle", "operation": None, "items": {}, "tasks": []}
    job = releases.read_json(releases.safe_file(root, NAME).read_bytes())
    if job.get("schema_version") != 1 or not isinstance(job.get("items"), dict):
        raise ValueError("Invalid batch PDF queue")
    return job


def save(root, job):
    with literature_update.writer_lock(root):
        collections.atomic_json(Path(root), NAME, job)


def select(root, rows, selected):
    job = load(root)
    for row in rows:
        if not row["doi"] or not row["title"] or row["status"] != "Awaiting PDF":
            continue
        item = job["items"].setdefault(row["doi"], {
            "doi": row["doi"], "title": row["title"], "status": "queued", "selected": False,
        })
        item["selected"] = selected
    save(root, job)
    return job


def prepare_locate(root):
    job = load(root)
    tasks = [doi for doi, item in job["items"].items()
             if item["selected"] and item["status"] not in {"downloaded", "extraction_failed", "extracted"}]
    if not tasks:
        raise ValueError("Turn on Locate PDF for at least one paper without a downloaded PDF")
    job.update(status="running", operation="locate", tasks=tasks, processed=0)
    job.pop("message", None)
    save(root, job)
    return job


def prepare_extract(root, pdf_root, choices):
    job = load(root)
    tasks = []
    for doi, filename in choices:
        item = job["items"].get(doi)
        if not item or item["status"] == "extracted":
            raise ValueError("Only unextracted papers in the review queue can be confirmed")
        if not filename or Path(filename).suffix.casefold() != ".pdf":
            raise ValueError("Select a PDF for every confirmed paper")
        try:
            path = releases.safe_file(Path(pdf_root), filename)
        except releases.ReleaseError as error:
            raise ValueError("Selected PDF is not available in the data folder; reload the review queue") from error
        item.update(filename=filename, confirmed_sha256=releases.sha256(path.read_bytes()))
        tasks.append(doi)
    if not tasks:
        raise ValueError("Confirm the identity and private-use permission for at least one PDF")
    if len(set(tasks)) != len(tasks):
        raise ValueError("A paper was confirmed more than once")
    job.update(status="running", operation="extract", tasks=tasks, processed=0)
    job.pop("message", None)
    save(root, job)
    return job


def run(root, pdf_root, job, pause, *, on_error=None):
    """Finish the current paper before pausing; persist every item independently."""
    pdf_root = Path(pdf_root)
    pace = citation_neighborhood.RequestPacer()
    try:
        # A previous process may have activated an extraction before saving its job status.
        status = project_library.status(root)
        extracted = {paper["doi"] for paper in [*status["papers"], *status["discovered"]]
                     if paper["status"] != "awaiting_pdf"}
        for doi in job["tasks"]:
            if pause.is_set():
                job["status"] = "paused"
                break
            item = job["items"][doi]
            if doi in extracted:
                item.update(status="extracted", selected=False)
                job["processed"] += 1
                save(root, job)
                continue
            item["status"] = "locating" if job["operation"] == "locate" else "extracting"
            item.pop("error", None)
            save(root, job)
            try:
                if job["operation"] == "locate":
                    pace()
                    result = project_library.find_pdf(root, doi, pdf_root, title=item["title"])
                    item["result"] = result
                    if result["status"] == "downloaded":
                        item.update(status="downloaded", filename=result["filename"])
                    else:
                        item.update(status="not_found", error=result["status"])
                else:
                    path = releases.safe_file(pdf_root, item["filename"])
                    result = project_library.attach(root, doi, path, title=item["title"],
                        identity_confirmed=True, expected_sha256=item["confirmed_sha256"])
                    item.update(status="extracted", extraction=result, selected=False)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error,
                    httpx.HTTPError, literature.DiscoveryError, releases.ReleaseError) as error:
                if on_error:
                    on_error(error)
                item.update(status="failed" if job["operation"] == "locate" else "extraction_failed",
                            error=str(error))
                # Stop the queue on provider errors rather than spending the next day's quota
                # repeatedly retrying every remaining paper.
                if isinstance(error, literature.DiscoveryError):
                    job.update(status="paused", message=str(error))
                    job["processed"] += 1
                    save(root, job)
                    break
            job["processed"] += 1
            save(root, job)
        else:
            job["status"] = "complete"
        save(root, job)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, releases.ReleaseError) as error:
        if on_error:
            on_error(error)
        job.update(status="failed", message=str(error))
        save(root, job)
