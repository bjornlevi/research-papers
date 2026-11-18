"""Persistent, sequential PDF location and operator-confirmed extraction queue."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

import httpx

from research_library import collections, literature, releases
from research_library.server import diagnostic
from . import citation_neighborhood, literature_update, project_library


NAME = "batch-pdf.json"
LOCATE_SKIP_STATES = {"downloaded", "local_found", "extraction_failed", "extracted"}
CHECKPOINT_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, releases.ReleaseError)


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


def needs_location(item):
    return item["selected"] and not item.get("accepted_sha256") and item["status"] not in LOCATE_SKIP_STATES


def activity(phase, *, worker_id=None, doi=None):
    return {"phase": phase, "at": datetime.now(timezone.utc).isoformat(),
            "worker_id": worker_id, "doi": doi}


def start_pass(job, operation, tasks):
    job.update(status="running", operation=operation, tasks=tasks, processed=0, activity=activity("queued"))
    for key in ("message", "stop", "previous_stop"):
        job.pop(key, None)


def prepare_locate(root, *, dois=None):
    job = load(root)
    tasks = [doi for doi, item in job["items"].items()
             if (dois is None or doi in dois) and needs_location(item)]
    if not tasks:
        raise ValueError("Turn on Locate PDF for at least one paper without a downloaded PDF")
    start_pass(job, "locate", tasks)
    save(root, job)
    return job


def can_resume_locate(job):
    if job["operation"] != "locate" or not job["tasks"]:
        return False
    status = job["status"]
    return (status == "running"
            or (status == "paused" and not job.get("message"))
            or (status == "failed" and job.get("stop", {}).get("reason") == "worker_error"))


def stop_details(job, worker_id):
    saved = job.get("activity", {})
    stop = job.get("stop")
    if not stop:
        if job["status"] != "running":
            return None
        last_worker = saved.get("worker_id")
        reason = "different_server" if last_worker and last_worker != worker_id else "unknown"
        stop = {"reason": reason}
    details = {**saved, **stop, "operation": job["operation"],
               "processed": job.get("processed", 0), "total": len(job["tasks"])}
    if not details.get("doi") and details.get("phase") != "queued":
        position = details["processed"]
        if type(position) is int and 0 <= position < len(job["tasks"]):
            doi = job["tasks"][position]
            if job["items"].get(doi, {}).get("status") in {"locating", "extracting"}:
                details["doi"] = doi
    return details


def resume_locate(root, *, worker_id=None):
    with literature_update.writer_lock(root):
        job = load(root)
        if not can_resume_locate(job):
            raise ValueError("No saved PDF location pass to resume")
        processed = job.get("processed", 0)
        if type(processed) is not int or not 0 <= processed <= len(job["tasks"]):
            raise ValueError("Invalid PDF location checkpoint")
        job.update(status="running", processed=processed, previous_stop=stop_details(job, worker_id),
                   activity=activity("queued"))
        job.pop("stop", None)
        save(root, job)
        return job


def accept_pdf(root, pdf_root, doi, filename):
    with literature_update.writer_lock(root):
        job = load(root)
        item = job["items"].get(doi)
        if not item or item["status"] == "extracted":
            raise ValueError("Only unextracted papers in the review queue can be accepted")
        if Path(filename).suffix.casefold() != ".pdf":
            raise ValueError("Select a PDF before accepting")
        path = releases.safe_file(Path(pdf_root), filename)
        item.update(filename=filename, accepted_sha256=releases.sha256(path.read_bytes()))
        if item["status"] == "extraction_failed":
            item["status"] = "local_found"
            item.pop("error", None)
        save(root, job)


def reject_suggestion(root, doi, filename):
    with literature_update.writer_lock(root):
        job = load(root)
        item = job["items"].get(doi)
        if (not filename or not item or item["status"] == "extracted"
                or item.get("accepted_sha256") or item.get("filename") != filename):
            raise ValueError("This PDF suggestion is no longer pending; reload the review page")
        for key in ("filename", "result", "error", "confirmed_sha256"):
            item.pop(key, None)
        item.update(status="suggestion_rejected", selected=False)
        save(root, job)


def prepare_accepted(root, pdf_root):
    with literature_update.writer_lock(root):
        job = load(root)
        choices = []
        for doi, item in job["items"].items():
            if item.get("accepted_sha256") and item["status"] not in {"extracted", "extraction_failed"}:
                path = releases.safe_file(Path(pdf_root), item["filename"])
                if releases.sha256(path.read_bytes()) != item["accepted_sha256"]:
                    raise ValueError("Accepted PDF changed; inspect and accept it again: " + item["filename"])
                choices.append((doi, item["filename"]))
        return prepare_extract(root, pdf_root, choices)


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
    start_pass(job, "extract", tasks)
    save(root, job)
    return job


def record_worker_error(root, error, phase, doi):
    fields = {"exception_type": type(error).__name__, "phase": phase}
    stopped_at = datetime.now(timezone.utc).isoformat()
    diagnostic("batch_pdf_worker_stopped", at=stopped_at, **fields)
    try:
        with literature_update.writer_lock(root):
            saved = load(root)
            saved.update(status="failed", stop={"reason": "worker_error", "error_phase": phase,
                                                 "exception_type": fields["exception_type"],
                                                 "doi": doi, "stopped_at": stopped_at})
            save(root, saved)
    except CHECKPOINT_ERRORS as save_error:
        diagnostic("batch_pdf_diagnostics_not_saved", exception_type=type(save_error).__name__,
                   phase=phase, worker_exception_type=fields["exception_type"])


def run(root, pdf_root, job, pause, *, on_error=None, worker_id=None):
    """Finish the current paper before pausing; persist every item independently."""
    phase, doi, finished = "starting", None, False

    def checkpoint(stage, paper=None):
        nonlocal phase
        phase = "saving_checkpoint"
        job["activity"] = activity(stage, worker_id=worker_id, doi=paper)
        save(root, job)
        phase = stage

    try:
        checkpoint("starting")
        pdf_root = Path(pdf_root)
        pace = citation_neighborhood.RequestPacer()
        # A previous process may have activated an extraction before saving its job status.
        status = project_library.status(root)
        extracted = {paper["doi"] for paper in [*status["papers"], *status["discovered"]]
                     if paper["status"] != "awaiting_pdf"}
        for doi in job["tasks"][job.get("processed", 0):]:
            if pause.is_set():
                job.update(status="paused", stop={"reason": "manual_pause"})
                break
            item = job["items"][doi]
            if doi in extracted:
                item.update(status="extracted", selected=False)
                job["processed"] += 1
                checkpoint("between_papers")
                continue
            if job["operation"] == "locate" and not needs_location(item):
                job["processed"] += 1
                checkpoint("between_papers")
                continue
            item["status"] = "locating" if job["operation"] == "locate" else "extracting"
            item.pop("error", None)
            checkpoint(item["status"], doi)
            try:
                if job["operation"] == "locate":
                    pace()
                    result = project_library.find_pdf(root, doi, pdf_root, title=item["title"])
                    item["result"] = result
                    if result["status"] in {"downloaded", "local_found"}:
                        item.update(status=result["status"], filename=result["filename"])
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
                item.pop("accepted_sha256", None)
                # Stop the queue on provider errors rather than spending the next day's quota
                # repeatedly retrying every remaining paper.
                if isinstance(error, literature.DiscoveryError):
                    job.update(status="paused", message=str(error),
                               stop={"reason": "provider_pause", "exception_type": type(error).__name__})
                    job["processed"] += 1
                    checkpoint(phase, doi)
                    break
            job["processed"] += 1
            checkpoint("between_papers")
        else:
            job["status"] = "complete"
        if job.get("stop", {}).get("reason") != "provider_pause":
            checkpoint("finished" if job["status"] == "complete" else "between_papers")
        finished = True
    except CHECKPOINT_ERRORS as error:
        finished = True
        record_worker_error(root, error, phase, doi)
        if on_error:
            on_error(error)
    finally:
        # Record unexpected exceptions without swallowing them or advancing an unsaved cursor.
        if not finished and isinstance(error := sys.exc_info()[1], Exception):
            record_worker_error(root, error, phase, doi)
