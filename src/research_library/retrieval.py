"""Transport-independent, read-only retrieval from an explicit evidence workspace."""

import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

CLAIM_KINDS = frozenset({
    "observed_result", "secondary_evidence", "recommendation",
    "mechanistic_inference", "correction", "study_design",
})
MAX_LIMIT = 100
MAX_QUERY_LENGTH = 2048
MAX_OFFSET = 1_000_000


class RetrievalError(ValueError):
    """Invalid input or unusable local evidence."""


class NotFoundError(RetrievalError):
    """A requested stable record ID does not exist."""


def paper_id(doi: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", doi.lower()).strip("-")


def data_root(value: Path | str | None = None) -> Path:
    configured = value if value is not None else os.environ.get("RESEARCH_LIBRARY_DATA_ROOT")
    if not configured:
        raise RetrievalError("Specify --data-root or RESEARCH_LIBRARY_DATA_ROOT; no evidence corpus is bundled")
    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        raise RetrievalError(f"Evidence data root is not a directory: {root}")
    return root


def open_index(root: Path | str | None = None) -> sqlite3.Connection:
    path = data_root(root) / "evidence/index.sqlite"
    if not path.is_file():
        raise RetrievalError(f"Missing evidence index: {path}; provision the authorized local corpus first")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RetrievalError("Evidence index integrity check failed")
        required = {"papers", "content_units", "content_search", "results", "claims", "claim_search"}
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise RetrievalError("Evidence index is incomplete; missing tables: " + ", ".join(sorted(required - tables)))
    except (sqlite3.Error, RetrievalError):
        connection.close()
        raise
    return connection


def pagination(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise RetrievalError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise RetrievalError(f"offset must be an integer from 0 to {MAX_OFFSET}")


def query_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_QUERY_LENGTH:
        raise RetrievalError(f"query must contain 1-{MAX_QUERY_LENGTH} characters, not only whitespace")


def payload(record: str) -> dict[str, Any]:
    result = json.loads(record)
    if not isinstance(result, dict):
        raise RetrievalError("Invalid indexed record: expected a JSON object")
    return result


def search_expression(text: str, mode: str) -> str:
    query_text(text)
    if mode == "plain":
        terms = re.findall(r"\w+", text)
        if not terms:
            raise RetrievalError("Plain-text search requires at least one word or number")
        text = " AND ".join('"' + term + '"' for term in terms)
    elif mode != "fts5":
        raise RetrievalError("Search mode must be plain or fts5")
    return text


def search(db: sqlite3.Connection, text: str, *, paper: str | None = None,
           limit: int = 10, offset: int = 0,
           mode: str = "fts5") -> list[dict[str, Any]]:
    pagination(limit, offset)
    text = search_expression(text, mode)
    sql = """SELECT content_units.payload_json,
                    snippet(content_search,2,'[',']','...',35),papers.doi
             FROM content_search JOIN content_units USING(unit_id)
             JOIN papers ON papers.paper_id=content_units.paper_id
             WHERE content_search MATCH ?"""
    params: list[Any] = [text]
    if paper is not None:
        sql += " AND content_search.paper_id=?"
        params.append(paper_id(paper))
    sql += " ORDER BY bm25(content_search),content_units.unit_id LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    matches = []
    for record, snippet, doi in db.execute(sql, params):
        unit = payload(record)
        unit.pop("text")
        matches.append({**unit, "snippet": snippet, "doi": doi})
    return matches


def claims(db: sqlite3.Connection, *, text: str | None = None, paper: str | None = None,
           topic: str | None = None, kind: str | None = None, measure: str | None = None,
           limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    pagination(limit, offset)
    if kind is not None and kind not in CLAIM_KINDS:
        raise RetrievalError(f"Unknown claim kind: {kind}")
    sql = "SELECT claims.payload_json FROM claims"
    params: list[Any] = []
    if text is not None:
        query_text(text)
        sql += " JOIN claim_search USING(claim_id) WHERE claim_search MATCH ?"
        params.append(text)
    else:
        sql += " WHERE 1=1"
    for column, value in (("paper_id", paper_id(paper) if paper is not None else None),
                          ("topic", topic), ("claim_kind", kind)):
        if value is not None:
            sql += f" AND claims.{column}=?"
            params.append(value)
    if measure is not None:
        sql += """ AND EXISTS (
            SELECT 1 FROM json_each(claims.payload_json, '$.numbers') AS n
            WHERE json_extract(n.value, '$.measure')=?)"""
        params.append(measure)
    sql += " ORDER BY " + ("bm25(claim_search), " if text is not None else "")
    sql += "claims.inventory_number,claims.claim_id LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    return [payload(row[0]) for row in db.execute(sql, params)]


def get_claim(db: sqlite3.Connection, claim_id: str) -> dict[str, Any]:
    if not isinstance(claim_id, str) or not claim_id.strip() or len(claim_id) > 256:
        raise RetrievalError("claim_id must contain 1-256 characters")
    row = db.execute("SELECT payload_json FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown claim ID: {claim_id}")
    return payload(row[0])


def results(db: sqlite3.Connection, *, paper: str | None = None, measure: str | None = None,
            limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    pagination(limit, offset)
    sql = "SELECT payload_json FROM results WHERE 1=1"
    params: list[Any] = []
    for column, value in (("paper_id", paper_id(paper) if paper is not None else None), ("measure", measure)):
        if value is not None:
            sql += f" AND {column}=?"
            params.append(value)
    sql += " ORDER BY paper_id,result_id LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    return [payload(row[0]) for row in db.execute(sql, params)]
