"""Rebuildable local Library index with database-side filtering and pagination."""

from contextlib import closing
import os
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from research_library import collections, releases
from . import literature_update, project_library, project_purge


LABELS = {"awaiting_pdf": "Awaiting PDF", "extracted": "Extracted",
          "draft": "Clear-text draft", "draft_revision": "Draft revision (published text remains active)",
          "published": "Published for retrieval",
          "needs_visual_review": "Needs visual review",
          "extracted_with_ocr": "Extracted with OCR (not human verified)"}
COLUMN_FILTERS = ("title", "doi", "first_author", "year", "journal")
VIEW_FIELDS = ("query", "relationship", "status", *(name + "_filter" for name in COLUMN_FILTERS), "sort", "order")
SORT_COLUMNS = {"title": "sort_title", "doi": "doi", "relationship": "library_groups",
                "status": "status", "first_author": "first_author", "year": "year", "journal": "journal"}


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
    paths = [root / "project.json", root / "registry/active.json", root / "editorial/active.json"]
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
        "version": 5,
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
                untitled INTEGER NOT NULL, sort_title TEXT NOT NULL, search_text TEXT NOT NULL,
                first_author TEXT NOT NULL DEFAULT '', year TEXT NOT NULL DEFAULT '',
                journal TEXT NOT NULL DEFAULT '', library_groups TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE connections (
                paper TEXT NOT NULL, direction TEXT NOT NULL, seed TEXT NOT NULL, run TEXT NOT NULL,
                PRIMARY KEY (paper, direction, seed, run)
            );
            CREATE TABLE metadata (paper TEXT NOT NULL, run TEXT NOT NULL, record TEXT NOT NULL);
            CREATE INDEX metadata_paper ON metadata(paper);
            CREATE INDEX connections_seed ON connections(seed);
        """)
        for paper in status["papers"]:
            connection.execute("INSERT INTO papers (id,doi,title,core,status,untitled,sort_title,search_text) "
                               "VALUES (?,?,?,?,?,?,?,?)", (
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
                INSERT INTO papers (id,doi,title,core,status,untitled,sort_title,search_text)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, untitled=excluded.untitled,
                    sort_title=excluded.sort_title, search_text=excluded.search_text
                WHERE papers.title='' AND excluded.title!=''
            """, records.values())
            connection.executemany("INSERT OR IGNORE INTO connections VALUES (?,?,?,?)", connections)
            metadata = []
            for key, node in graph["nodes"].items():
                record = node["record"]
                doi = record["identifiers"].get("doi", "")
                identity = collections.publication(doi) if doi else key
                authors = record.get("authors") or []
                year = re.match(r"^\d{4}(?:-|$)", record.get("publication_date") or "")
                metadata.append((authors[0] if authors else "", year[0][:4] if year else "",
                                 record.get("journal") or "", identity))
                connection.execute("INSERT INTO metadata VALUES (?,?,?)",
                                   (identity, run, json.dumps(record, ensure_ascii=False)))
            connection.executemany("""
                UPDATE papers SET
                    first_author=CASE WHEN first_author='' THEN ?1 ELSE first_author END,
                    year=CASE WHEN year='' THEN ?2 ELSE year END,
                    journal=CASE WHEN journal='' THEN ?3 ELSE journal END
                WHERE id=?4
            """, metadata)
            del graph, titles, records, connections
        connection.execute("""
            UPDATE papers SET library_groups=rtrim(
                CASE WHEN core=1 THEN 'Core papers; ' ELSE '' END ||
                CASE WHEN EXISTS (SELECT 1 FROM connections c WHERE c.paper=papers.id AND c.direction='background')
                    THEN 'References; ' ELSE '' END ||
                CASE WHEN EXISTS (SELECT 1 FROM connections c WHERE c.paper=papers.id AND c.direction='impact')
                    THEN 'Citing papers; ' ELSE '' END, '; ')
        """)
        connection.execute("""
            UPDATE papers SET search_text=search_text || ' ' || first_author || ' ' || year || ' ' ||
                journal || ' ' || status || ' ' || library_groups
        """)
        connection.execute("CREATE INDEX paper_order ON papers(core DESC,untitled,sort_title,doi,id)")
        connection.execute("INSERT INTO state VALUES (?)", (signature,))
        connection.commit()


def library_page(root, query="", offset=0, limit=50, *, relationship="", status="", title_filter="", doi_filter="",
                 first_author_filter="", year_filter="", journal_filter="", sort="", order="asc", select_all=False,
                 paper_id=None):
    root = Path(root).absolute()
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid Library pagination")
    if relationship not in {"", "core", "background", "impact"} or status not in {
            "", "awaiting", "extracted", "review", "unresolved", "published", "draft"}:
        raise ValueError("Invalid Library filter")
    if sort not in {"", *SORT_COLUMNS} or order not in {"asc", "desc"}:
        raise ValueError("Invalid Library sort")
    column_filters = {"title": title_filter, "doi": doi_filter, "first_author": first_author_filter,
                      "year": year_filter, "journal": journal_filter}
    if year_filter and not re.fullmatch(r"\d{4}", year_filter):
        raise ValueError("Publication year must be four digits")
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
            connection.create_function("casefold", 1, str.casefold, deterministic=True)
            words = query.casefold().split()
            where = " AND ".join("instr(casefold(search_text),?)>0" for _ in words) or "1"
            if paper_id is not None:
                where += " AND id=?"
                words.append(paper_id)
            for column, value in column_filters.items():
                if value:
                    where += f" AND instr(casefold({column}),?)>0"
                    words.append(value.casefold())
            if relationship == "core":
                where += " AND core=1"
            elif relationship:
                where += " AND EXISTS (SELECT 1 FROM connections c WHERE c.paper=papers.id AND c.direction=?)"
                words.append(relationship)
            conditions = {
                "awaiting": "status='Awaiting PDF'",
                "extracted": "status!='Awaiting PDF' AND doi!=''",
                "review": "status IN ('Needs visual review','Clear-text draft','Draft revision (published text remains active)')",
                "draft": "status IN ('Clear-text draft','Draft revision (published text remains active)')",
                "published": "status IN ('Published for retrieval','Draft revision (published text remains active)')",
                "unresolved": "doi=''",
            }
            if status:
                where += " AND " + conditions[status]
            total, core_count = connection.execute("SELECT count(*),coalesce(sum(core),0) FROM papers").fetchone()
            matching = connection.execute("SELECT count(*) FROM papers WHERE " + where, words).fetchone()[0]
            pagination = "" if select_all else " LIMIT ? OFFSET ?"
            ordering = "core DESC,untitled,sort_title,doi,id"
            if sort:
                column = SORT_COLUMNS[sort]
                ordering = f"({column}=''),casefold({column}) {order},sort_title,doi,id"
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM papers WHERE " + where
                + " ORDER BY " + ordering + pagination,
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


def paper_detail(root, identity, *, include_metadata=True):
    """Return one exact Library record and its saved, one-hop citation relationships."""
    root = Path(root).absolute()
    result = library_page(root, paper_id=identity)
    if not result["rows"]:
        raise ValueError("Paper no longer exists in Library")
    paper = result["rows"][0]
    related = {"references": {}, "citing": {}}
    runs = set(paper["runs"])
    records = []
    with closing(sqlite3.connect((root / "library-index.sqlite").as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        links = connection.execute("""
            SELECT c.paper,c.seed,c.direction,p.title,p.doi
            FROM connections c JOIN papers p ON p.id=c.paper
            WHERE c.paper=? OR (?!='' AND c.seed=?)
        """, (identity, paper["doi"], paper["doi"]))
        for link in links:
            seed_id = collections.publication(link["seed"])
            source, target = ((seed_id, link["paper"]) if link["direction"] == "background"
                              else (link["paper"], seed_id))
            for name, endpoint, other in (("references", source, target), ("citing", target, source)):
                if endpoint != identity:
                    continue
                if other == link["paper"]:
                    row = {"id": other, "doi": link["doi"], "title": link["title"]}
                else:
                    core = result["core"].get(link["seed"])
                    if core is None:
                        continue
                    row = {"id": other, "doi": link["seed"], "title": core["title"]}
                related[name][other] = row
        if paper["doi"]:
            runs.update(row[0] for row in connection.execute(
                "SELECT DISTINCT run FROM connections WHERE seed=?", (paper["doi"],)))
        if include_metadata:
            records = [json.loads(row[0]) for row in connection.execute(
                "SELECT record FROM metadata WHERE paper=? ORDER BY run", (identity,))]
    return {**paper, **{name: sorted(rows.values(), key=lambda row: (row["title"].casefold(), row["id"]))
                       for name, rows in related.items()},
            "records": records, "searches": [(run, job) for run, job in result["searches"] if run in runs]}
