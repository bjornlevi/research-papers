"""Rebuildable local Library index with database-side filtering and pagination."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile

from research_library import collections, releases
from . import literature_update, project_library, project_purge


LABELS = {"awaiting_pdf": "Awaiting PDF", "extracted": "Extracted",
          "needs_visual_review": "Needs visual review",
          "extracted_with_ocr": "Extracted with OCR (not human verified)"}


def discovery_titles(graph):
    titles_by_doi = {
        collections.publication(node["record"]["identifiers"]["doi"]): node["record"]["title"]
        for node in graph["nodes"].values()
        if node["record"]["title"] and node["record"]["identifiers"].get("doi")
    }
    return {
        key: (node["record"]["title"]
              or (titles_by_doi.get(collections.publication(node["record"]["identifiers"]["doi"]), "")
                  if node["record"]["identifiers"].get("doi") else "")
              or node.get("reference", {}).get("unstructured") or "")
        for key, node in graph["nodes"].items()
    }


def _state(root):
    paths = [root / "project.json", root / "registry/active.json"]
    searches = []
    discovery = root / "discovery"
    for folder in sorted(discovery.iterdir()) if discovery.exists() else []:
        if (not folder.is_dir() or folder.is_symlink() or len(folder.name) != 32
                or any(c not in "0123456789abcdef" for c in folder.name)
                or not (folder / "job.json").is_file()):
            continue
        job = releases.read_json(releases.safe_file(folder, "job.json").read_bytes())
        searches.append((folder.name, job))
        if (folder / "neighborhood.json").is_file():
            paths.append(releases.safe_file(folder, "neighborhood.json"))
    signature = releases.encode({
        "version": 2,
        "files": [[str(path.relative_to(root)), project_purge._file_version(path)]
                  for path in paths if path.exists()],
    }).decode()
    return signature, searches


def _rebuild(root, destination, signature, searches):
    status = project_library.status(root)
    extracted = {paper["doi"]: LABELS[paper["status"]] for paper in status["discovered"]}
    extracted_titles = {paper["doi"]: paper["title"] for paper in status["discovered"]}
    with closing(sqlite3.connect(destination)) as connection:
        connection.executescript("""
            CREATE TABLE state (signature TEXT NOT NULL);
            CREATE TABLE papers (
                id TEXT PRIMARY KEY, doi TEXT NOT NULL, title TEXT NOT NULL,
                core INTEGER NOT NULL, status TEXT NOT NULL,
                untitled INTEGER NOT NULL, sort_title TEXT NOT NULL, search_text TEXT NOT NULL
            );
            CREATE TABLE connections (
                paper TEXT NOT NULL, direction TEXT NOT NULL, seed TEXT NOT NULL, run TEXT NOT NULL,
                PRIMARY KEY (paper, direction, seed, run)
            );
        """)
        for paper in status["papers"]:
            connection.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?)", (
                collections.publication(paper["doi"]), paper["doi"], paper["title"], 1,
                LABELS[paper["status"]], int(not paper["title"]), paper["title"].casefold(),
                (paper["title"] + " " + paper["doi"]).casefold()))
        for run, _ in searches:
            path = root / "discovery" / run / "neighborhood.json"
            if not path.is_file():
                continue
            graph = releases.read_json(releases.safe_file(path.parent, path.name).read_bytes())
            titles = discovery_titles(graph)
            records, connections = {}, []
            for edge in graph["edges"]:
                key = edge["target_provider_record"]
                doi = graph["nodes"][key]["record"]["identifiers"].get("doi", "")
                identity = collections.publication(doi) if doi else key
                title = extracted_titles.get(doi) or titles[key]
                if identity not in records or not records[identity][2]:
                    records[identity] = (
                        identity, doi, title, 0,
                        extracted.get(doi, "Awaiting PDF" if doi else "Metadata only — DOI unresolved"),
                        int(not title), title.casefold(), (title + " " + doi).casefold())
                connections.append((identity, edge["direction"], edge["seed_doi"], run))
            connection.executemany("""
                INSERT INTO papers VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, untitled=excluded.untitled,
                    sort_title=excluded.sort_title, search_text=excluded.search_text
                WHERE papers.title='' AND excluded.title!=''
            """, records.values())
            connection.executemany("INSERT OR IGNORE INTO connections VALUES (?,?,?,?)", connections)
            del graph, titles, records, connections
        connection.execute("CREATE INDEX paper_order ON papers(core DESC,untitled,sort_title,doi,id)")
        connection.execute("INSERT INTO state VALUES (?)", (signature,))
        connection.commit()


def library_page(root, query="", offset=0, limit=50, *, relationship="", status="", select_all=False):
    root = Path(root).absolute()
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid Library pagination")
    if relationship not in {"", "core", "background", "impact"} or status not in {
            "", "awaiting", "extracted", "review", "unresolved"}:
        raise ValueError("Invalid Library filter")
    path = root / "library-index.sqlite"
    with literature_update.writer_lock(root):
        signature, searches = _state(root)
        if path.is_symlink():
            raise ValueError("Symlinked Library index")
        current = None
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                row = connection.execute("SELECT signature FROM state").fetchone()
                current = row[0] if row else None
        if current != signature:
            descriptor, temporary = tempfile.mkstemp(prefix=".library-index-", suffix=".sqlite", dir=root)
            os.close(descriptor)
            try:
                _rebuild(root, temporary, signature, searches)
                if _state(root)[0] != signature:
                    raise ValueError("Project changed while indexing Library; reload to retry")
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            words = query.casefold().split()
            where = " AND ".join("instr(search_text,?)>0" for _ in words) or "1"
            if relationship == "core":
                where += " AND core=1"
            elif relationship:
                where += " AND EXISTS (SELECT 1 FROM connections c WHERE c.paper=papers.id AND c.direction=?)"
                words.append(relationship)
            conditions = {
                "awaiting": "status='Awaiting PDF'",
                "extracted": "status IN ('Extracted','Extracted with OCR (not human verified)')",
                "review": "status='Needs visual review'",
                "unresolved": "doi=''",
            }
            if status:
                where += " AND " + conditions[status]
            total, core_count = connection.execute("SELECT count(*),coalesce(sum(core),0) FROM papers").fetchone()
            matching = connection.execute("SELECT count(*) FROM papers WHERE " + where, words).fetchone()[0]
            pagination = "" if select_all else " LIMIT ? OFFSET ?"
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM papers WHERE " + where
                + " ORDER BY core DESC,untitled,sort_title,doi,id" + pagination,
                words if select_all else [*words, limit, offset])]
            for row in rows:
                if select_all:
                    continue
                links = connection.execute(
                    "SELECT direction,seed,run FROM connections WHERE paper=?", (row["id"],)).fetchall()
                row["relations"] = {(link["direction"], link["seed"]) for link in links}
                row["runs"] = {link["run"] for link in links}
    return {"rows": rows, "total": total, "core_count": core_count, "matching": matching,
            "searches": searches, "core": project_library.load(root)["papers"]}
