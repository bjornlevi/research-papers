"""Metadata catalogs: conservative identity reconciliation, provenance and immutable snapshots."""

from collections import Counter
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
from contextlib import closing
import hashlib
import os
import shutil
import sqlite3
import tempfile
from itertools import groupby, zip_longest

from jsonschema import Draft202012Validator, ValidationError

from . import contracts, releases, retrieval
from .collections import atomic_json
from .literature import validate_run

MAX_CATALOG_BYTES = 32 * 1024 * 1024
RELATIONS = {"corrects", "retracts", "concerns", "published_version_of", "supplement_of"}
CAVEATS = [
    "Provisional metadata catalog; not an authoritative or researcher-approved bibliography.",
    "Metadata only: no abstracts, full text, extracted passages or clinical claims.",
    "No match is not global scientific absence. Reviews may reuse the same primary studies.",
    "Provider availability and indexing do not establish redistribution permission or peer review.",
]


def alias(namespace, value):
    if not isinstance(value, str) or not value.strip():
        raise releases.ReleaseError("Invalid publication identifier")
    value = value.strip()
    if namespace == "doi":
        value = re.sub(r"^(?:https?://doi\.org/|doi:\s*)", "", value, flags=re.I).lower()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
            raise releases.ReleaseError("Invalid DOI")
    elif namespace == "pmid":
        if not value.isdigit():
            raise releases.ReleaseError("Invalid PMID")
    elif namespace == "pmcid":
        value = value.upper()
        if not re.fullmatch(r"PMC\d+", value):
            raise releases.ReleaseError("Invalid PMCID")
    else:
        raise releases.ReleaseError("Unknown publication identifier namespace")
    return namespace + ":" + value.lower()


def provider_key(record):
    return ":".join(record[key] for key in ("provider", "provider_source", "provider_id"))


def record_aliases(record):
    aliases, invalid = [], []
    for namespace, value in record["identifiers"].items():
        try:
            aliases.append(alias(namespace, value))
        except releases.ReleaseError:
            invalid.append(namespace)
    return aliases, invalid


def load_occurrences(roots, spool=None):
    occurrences, runs = {}, {}
    for root in roots:
        root = Path(root)
        state, _, _ = validate_run(root, retain_metadata=spool is None)
        run_id = releases.sha256(releases.encode(state))
        runs[run_id] = {
            "id": run_id, "provider": state["config"]["provider"], "query": state["config"]["query"],
            "policy_version": state["config"]["policy_version"],
            "policy_sha256": state["config"]["policy_sha256"],
            "from_date": state["config"]["from_date"], "through_date": state["config"]["through_date"],
            "started_at": state["started_at"], "updated_at": state["updated_at"],
            "status": state["status"], "disease": state["config"]["disease"],
        }
        for index in range(len(state["pages"])):
            page = releases.read_json(releases.safe_file(root, f"page-{index:06}.json").read_bytes())
            provenance = [{"url": request["url"], "retrieved_at": request["retrieved_at"],
                           "response_sha256": request["sha256"]} for request in page["requests"]]
            for record in page["records"]:
                occurrence = {"record": record, "run_id": run_id,
                              "page_sha256": state["pages"][index], "requests": provenance}
                if spool is None:
                    occurrences[releases.sha256(releases.encode(occurrence))] = occurrence
                else:
                    spool.add(occurrence)
    groups = spool if spool is not None else None
    if groups is None:
        groups = {}
        for key in sorted(occurrences):
            occurrence = occurrences[key]
            groups.setdefault(provider_key(occurrence["record"]), []).append(occurrence)
    return groups, [runs[key] for key in sorted(runs)]


def build(roots, *, as_of, rationale, authorization_basis, istopmm_dois=(), relationships=(),
          sqlite_output=None):
    parsed_date = date.fromisoformat(as_of)
    if (as_of != parsed_date.isoformat() or as_of > date.today().isoformat()
            or not rationale.strip() or not authorization_basis.strip()):
        raise releases.ReleaseError("Supply an as-of date, selection rationale and metadata-use basis")
    if sqlite_output is not None:
        with tempfile.TemporaryDirectory(prefix="research-library-catalog-") as temporary:
            with closing(sqlite3.connect(Path(temporary) / "occurrences.sqlite")) as connection:
                groups, runs = load_occurrences(roots, Occurrences(connection))
                return write_sqlite(sqlite_output, groups, runs, as_of=as_of, rationale=rationale,
                                    authorization_basis=authorization_basis, istopmm_dois=istopmm_dois,
                                    relationships=relationships)
    groups, runs = load_occurrences(roots)
    issues = []
    publications = list(reconcile(groups, rationale, istopmm_dois, issues))
    document = header(runs, as_of, rationale, authorization_basis)
    document.update(issues=issues, publications=sorted(publications, key=lambda record: record["id"]))
    finish_relationships(document, relationships)
    validate(document)
    return document


def header(runs, as_of, rationale, authorization_basis):
    if not runs:
        raise releases.ReleaseError("Catalog requires at least one discovery run")
    if any(run["through_date"] > as_of for run in runs):
        raise releases.ReleaseError("Catalog as-of date precedes an imported discovery cutoff")
    return {
        "schema_version": 1, "kind": "literature_catalog", "as_of": as_of,
        "selection_rationale": rationale, "metadata_authorization_basis": authorization_basis,
        "istopmm_matching": "Exact DOI only; absent DOI is not proof of no overlap.",
        "runs": runs, "caveats": CAVEATS,
    }


def reconcile(groups, rationale, istopmm_dois, issues):
    # Provider identities join repeated imports; strong aliases join providers.
    keys = sorted(groups)
    parents = {key: key for key in keys}

    def find(key):
        while parents[key] != key:
            key = parents[key]
        return key

    owners, invalid_keys = {}, set()
    for key in keys:
        for item in groups[key]:
            identifiers, invalid = record_aliases(item["record"])
            if invalid and key not in invalid_keys:
                invalid_keys.add(key)
                issues.append({"kind": "invalid_identifier", "records": [key],
                               "detail": "Malformed provider identifier excluded from alias matching; original value "
                                         "retained in provenance. Researcher resolution required."})
            for identifier in identifiers:
                if identifier in owners:
                    first, second = sorted((find(key), find(owners[identifier])))
                    parents[second] = first
                owners[identifier] = key
    components = {}
    for key in keys:
        components.setdefault(find(key), []).append(key)
    partitions = []
    for members in components.values():
        ids = {}
        for key in members:
            for occurrence in groups[key]:
                for identifier in record_aliases(occurrence["record"])[0]:
                    ids.setdefault(identifier.split(":", 1)[0], set()).add(identifier)
        if any(len(values) > 1 for values in ids.values()):
            issues.append({"kind": "identifier_collision", "records": members,
                           "detail": "Conflicting strong identifiers; component not merged. Researcher resolution required."})
            partitions.extend(([key], True) for key in members)
        else:
            partitions.append((members, False))
    istopmm = {alias("doi", value) for value in istopmm_dois}
    for members, ambiguous in partitions:
        observations = [item for key in members for item in groups[key]]
        records = [item["record"] for item in observations]
        aliases = sorted({identifier for record in records for identifier in record_aliases(record)[0]})
        identity_review = ambiguous or bool(invalid_keys.intersection(members))
        canonical = (next((a for ns in ("doi:", "pmid:", "pmcid:") for a in aliases if a.startswith(ns)), None)
                     if not ambiguous else None)
        canonical = canonical or "record:" + releases.sha256(releases.encode(members))
        variants = {}
        for key in ("title", "authors", "publication_date", "journal", "eligibility", "publication_state"):
            values = {releases.encode(record.get(key)): record.get(key) for record in records}
            if len(values) > 1:
                variants[key] = [values[value] for value in sorted(values)]
        if variants:
            issues.append({"kind": "metadata_conflict", "records": members,
                           "detail": "Provider variants retained; selected display is deterministic, not scientific adjudication."})
        # PubMed is preferred for display only. Every original value remains below.
        representative = sorted(records, key=lambda record: (
            record["provider"] != "pubmed", record["provider"] == "crossref",
            provider_key(record), releases.encode(record)))[0]
        statuses = {record["publication_state"] for record in records}
        state = next((s for s in ("retracted", "notice", "preprint") if s in statuses), "unknown")
        decisions = {record["eligibility"]["decision"] for record in records}
        decision = ("excluded" if "excluded" in decisions else
                    "needs_review" if identity_review or decisions != {"included"} else "included")
        provenance = [{
            "provider_record": provider_key(item["record"]), "run_id": item["run_id"],
            "page_sha256": item["page_sha256"], "requests": item["requests"],
            "metadata": {key: item["record"].get(key) for key in (
                "identifiers", "title", "authors", "journal", "publication_date", "publication_types",
                "source_url", "eligibility", "publication_state")},
        } for item in observations]
        relations = {}
        for record in records:
            for relation in record.get("relations", []):
                relations[releases.encode(relation)] = relation
        yield {
            "id": canonical, "aliases": aliases, "title": representative["title"],
            "authors": representative["authors"], "journal": representative.get("journal"),
            "publication_date": representative["publication_date"],
            "citation_url": representative["source_url"],
            "diseases": sorted({topic for record in records for topic in record["query_topics"]}),
            "publication_types": sorted({t for record in records for t in record["publication_types"]}),
            "languages": sorted({lang for record in records for lang in record["languages"]}),
            "eligibility": decision, "publication_state": state,
            "study_design": ["unknown"], "peer_review_status": "not_verified",
            "researcher_review": "not_reviewed", "identity_status": "needs_review" if identity_review else "resolved",
            "istopmm_overlap": "needs_review" if identity_review and istopmm.intersection(aliases) else
                               "matched_doi" if istopmm.intersection(aliases) else "not_matched",
            "availability": {
                "metadata": True, "abstract_reported": any(r["availability"]["abstract_reported"] for r in records),
                "full_text_reported": any(r["availability"]["full_text_reported"] for r in records),
                "full_text_acquired": False, "redistribution_authorized": False,
            },
            "extraction_status": "not_extracted", "claim_review_status": "not_reviewed",
            "selection_rationale": rationale, "metadata_conflicts": variants,
            "relations": [relations[key] for key in sorted(relations)],
            "provenance": provenance,
        }


def finish_relationships(document, relationships):
    publications = document["publications"]
    by_id = {record["id"]: record for record in document["publications"]}
    for link in relationships:
        if not isinstance(link, dict) or set(link) != {"source", "kind", "target", "provenance"}:
            raise releases.ReleaseError("Relationship import requires source, kind, target and provenance")
        record = by_id.get(link["source"])
        if record is None:
            raise releases.ReleaseError("Relationship source must be an exact catalog record ID")
        relation = {key: link[key] for key in ("kind", "target", "provenance")}
        if relation not in record["relations"]:
            record["relations"].append(relation)
            record["relations"].sort(key=releases.encode)
    all_ids = {identifier for record in publications for identifier in [record["id"], *record["aliases"]]}
    for record in publications:
        if record["publication_state"] in {"notice", "retracted"} and not record["relations"]:
            document["issues"].append({"kind": "unresolved_notice", "records": [record["id"]],
                                      "detail": "Provider notice state has no interpreted relationship; review source metadata."})
        if any(link["target"] not in all_ids for link in record["relations"]):
            document["issues"].append({"kind": "unresolved_relationship", "records": [record["id"]],
                                      "detail": "Typed relationship target is outside this bounded catalog; not evidence of absence."})
    document["issues"].sort(key=releases.encode)


def validate(document):
    Draft202012Validator({"$defs": contracts.DOCUMENT["$defs"],
                          **contracts.ref("catalog_document")}).validate(document)
    if len(releases.encode(document)) > MAX_CATALOG_BYTES:
        raise releases.ReleaseError("Catalog exceeds supported byte limit; split the collection")
    ids = [item["id"] for item in document["publications"]]
    if ids != sorted(set(ids)):
        raise releases.ReleaseError("Catalog IDs must be unique and sorted")
    run_ids = {run["id"] for run in document["runs"]}
    for record in document["publications"]:
        if any(provenance["run_id"] not in run_ids for provenance in record["provenance"]):
            raise releases.ReleaseError("Dangling catalog provenance")
        for relation in record["relations"]:
            if relation["kind"] not in RELATIONS:
                raise releases.ReleaseError("Unsupported catalog relationship")
    return document


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CATALOG_BYTES:
        raise releases.ReleaseError("Invalid catalog file")
    try:
        return validate(releases.read_json(path.read_bytes()))
    except ValidationError as error:
        raise releases.ReleaseError("Catalog schema validation failed") from error


def coverage(document, *, catalog_id=None):
    counters = {key: Counter() for key in ("diseases", "publication_types", "eligibility", "istopmm_overlap")}
    providers = set()
    publications = observations = unavailable = 0
    for record in document["publications"]:
        publications += 1
        observations += len(record["provenance"])
        providers.update(p["provider_record"] for p in record["provenance"])
        unavailable += not record["availability"]["full_text_reported"]
        for key in counters:
            counters[key].update(record[key] if isinstance(record[key], list) else [record[key]])
    issue_counts = Counter(issue["kind"] for issue in document["issues"])
    return {
        "catalog_id": catalog_id or releases.sha256(releases.encode(document)), "as_of": document["as_of"],
        "publication_count": publications, "observation_count": observations,
        "provider_record_count": len(providers), "duplicates_collapsed": len(providers) - publications,
        **{key: dict(sorted(value.items())) for key, value in counters.items()},
        "identity_issues": issue_counts["identifier_collision"] + issue_counts["invalid_identifier"],
        "metadata_conflicts": issue_counts["metadata_conflict"],
        "unresolved_links": issue_counts["unresolved_notice"] + issue_counts["unresolved_relationship"],
        "metadata_only_count": publications, "full_text_acquired_count": 0,
        "full_text_not_reported_count": unavailable,
        "unreviewed_count": publications, "complete_discovery": all(r["status"] == "complete" for r in document["runs"]),
        "runs": document["runs"], "scope": document["selection_rationale"],
    }


class CatalogSnapshot:
    profile = "metadata_only"

    def __init__(self, document):
        self.document = deepcopy(validate(document))
        self.release_id = releases.sha256(releases.encode(document))
        self.caveats = document["caveats"]

    def verify_backing(self, path):
        if releases.sha256(releases.encode(read(path))) != self.release_id:
            raise releases.ReleaseError("Catalog checksum mismatch")

    def publication_keys(self):
        return [record["id"] for record in self.document["publications"]]

    def identities(self):
        return ((record["id"], record["aliases"] if record["identity_status"] == "resolved" else [])
                for record in self.document["publications"])

    def coverage(self):
        return coverage(self.document)

    def matching_ids(self, arguments, *, limit=None, offset=0):
        ids = [record["id"] for record in self.matches(arguments)]
        return ids[offset:offset + limit] if limit is not None else ids[offset:]

    def matches(self, arguments):
        words = re.findall(r"\w+", arguments.get("query", "").casefold())
        if arguments.get("query") and not words:
            raise retrieval.RetrievalError("Catalog query requires words or numbers")
        records = []
        for record in self.document["publications"]:
            if any(arguments.get(key) and arguments[key] not in record[field]
                   for key, field in (("disease", "diseases"), ("publication_type", "publication_types"))):
                continue
            if arguments.get("eligibility") and record["eligibility"] != arguments["eligibility"]:
                continue
            text = releases.encode({key: record[key] for key in (
                "id", "aliases", "title", "authors", "journal", "diseases", "publication_types")}).decode().casefold()
            tokens = set(re.findall(r"\w+", text))
            if all(word in tokens for word in words):
                records.append(deepcopy(record))
        return records

    def get(self, identifier):
        exact = [r for r in self.document["publications"] if r["id"] == identifier]
        matches = exact or [r for r in self.document["publications"] if identifier.lower() in r["aliases"]]
        if len(matches) > 1:
            raise retrieval.RetrievalError("Ambiguous publication alias; use the catalog record ID")
        if not matches:
            raise retrieval.NotFoundError("Unknown catalog publication")
        return deepcopy(matches[0])

    def status(self):
        return {"paper_count": len(self.document["publications"]), "claim_count": 0, "content_unit_count": 0,
                "coverage": self.document["selection_rationale"], "retrieval": "lexical_fts5",
                "rights_profile": self.profile, "source_assets_available": False}


def install(root, path):
    root = Path(root)
    path = Path(path)
    if path.suffix == ".sqlite":
        snapshot = open_snapshot(path, validate_records=True)
        destination = root / "catalogs"
        if destination.is_symlink():
            raise releases.ReleaseError("Symlinked catalog store")
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / (snapshot.release_id + ".sqlite")
        if target.exists():
            snapshot.verify_backing(target)
        else:
            descriptor, temporary = tempfile.mkstemp(dir=destination, prefix=".catalog-")
            try:
                with os.fdopen(descriptor, "wb") as output, path.open("rb") as source:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                snapshot.verify_backing(Path(temporary))
                os.replace(temporary, target)
                sync_directory(destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return snapshot.release_id
    document = read(path)
    identifier = releases.sha256(releases.encode(document))
    destination = root / "catalogs"
    if destination.is_symlink():
        raise releases.ReleaseError("Symlinked catalog store")
    target = destination / (identifier + ".json")
    if target.exists():
        if read(target) != document:
            raise releases.ReleaseError("Stored catalog differs")
    else:
        atomic_json(destination, identifier + ".json", document)
    return identifier


MAX_SQLITE_BYTES = 8 * 1024 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024


def file_digest(path):
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SQLITE_BYTES
            or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal"))):
        raise releases.ReleaseError("Invalid SQLite catalog file")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def stored_path(root, identifier):
    directory = Path(root) / "catalogs"
    paths = [directory / (identifier + suffix) for suffix in (".json", ".sqlite")]
    found = [path for path in paths if path.exists() or path.is_symlink()]
    if len(found) != 1:
        raise releases.ReleaseError("Catalog backing file missing or ambiguous")
    return found[0]


def open_snapshot(path, *, validate_records=False):
    path = Path(path)
    try:
        return (SQLiteCatalogSnapshot(path, validate_records=validate_records) if path.suffix == ".sqlite"
                else CatalogSnapshot(read(path)))
    except (sqlite3.Error, ValidationError, KeyError) as error:
        raise releases.ReleaseError("Invalid catalog storage, schema or metadata") from error


class Occurrences:
    """Private build spool; only identity keys and one component occupy memory."""

    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE occurrences (digest TEXT PRIMARY KEY, provider TEXT, data TEXT)")
        connection.execute("CREATE INDEX occurrence_provider ON occurrences(provider, digest)")

    def add(self, occurrence):
        data = releases.encode(occurrence)
        self.connection.execute("INSERT OR IGNORE INTO occurrences VALUES (?, ?, ?)",
                                (releases.sha256(data), provider_key(occurrence["record"]), data.decode()))

    def __iter__(self):
        return (row[0] for row in self.connection.execute("SELECT DISTINCT provider FROM occurrences ORDER BY provider"))

    def __getitem__(self, key):
        return [releases.read_json(row[0].encode()) for row in self.connection.execute(
            "SELECT data FROM occurrences WHERE provider=? ORDER BY digest", (key,))]


def search_tokens(record):
    text = releases.encode({key: record[key] for key in (
        "id", "aliases", "title", "authors", "journal", "diseases", "publication_types")}).decode().casefold()
    # ASCII encoding makes FTS token semantics exactly match the JSON catalog's Python word tokens.
    return " ".join(sorted(word.encode().hex() for word in set(re.findall(r"\w+", text))))


def memberships(record):
    return sorted({(kind, value) for kind, values in (
        ("alias", record["aliases"]), ("disease", record["diseases"]),
        ("publication_type", record["publication_types"])) for value in values})


def write_sqlite(path, groups, runs, *, as_of, rationale, authorization_basis, istopmm_dois, relationships):
    path = Path(path)
    if path.exists() or path.is_symlink() or path.parent.is_symlink():
        raise releases.ReleaseError("Catalog output already exists or is symlinked")
    metadata = header(runs, as_of, rationale, authorization_basis)
    path.parent.mkdir(parents=True, exist_ok=True)
    issues = []
    validator = Draft202012Validator({"$defs": contracts.DOCUMENT["$defs"], **contracts.ref("catalog_publication")})
    links = {}
    for link in relationships:
        if not isinstance(link, dict) or set(link) != {"source", "kind", "target", "provenance"}:
            raise releases.ReleaseError("Relationship import requires source, kind, target and provenance")
        links.setdefault(link["source"], []).append({key: link[key] for key in ("kind", "target", "provenance")})
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".catalog-")
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.executescript("""
                PRAGMA application_id=1230196557;
                PRAGMA user_version=1;
                CREATE TABLE metadata (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE publications (id TEXT PRIMARY KEY, data TEXT NOT NULL,
                                           search TEXT NOT NULL, eligibility TEXT NOT NULL);
                CREATE TABLE memberships (kind TEXT NOT NULL, value TEXT NOT NULL, id TEXT NOT NULL,
                                          PRIMARY KEY(kind, value, id)) WITHOUT ROWID;
                CREATE INDEX memberships_id ON memberships(id);
                CREATE VIRTUAL TABLE publication_search USING fts5(search, content='publications', tokenize='ascii');
            """)
            for record in reconcile(groups, rationale, istopmm_dois, issues):
                for link in links.pop(record["id"], []):
                    if link not in record["relations"]:
                        record["relations"].append(link)
                record["relations"].sort(key=releases.encode)
                validator.validate(record)
                data = releases.encode(record)
                if len(data) > MAX_RECORD_BYTES:
                    raise releases.ReleaseError("Catalog publication exceeds storage record limit")
                tokens = search_tokens(record)
                row = connection.execute("INSERT INTO publications VALUES (?, ?, ?, ?)",
                                         (record["id"], data.decode(), tokens, record["eligibility"]))
                connection.execute("INSERT INTO publication_search(rowid, search) VALUES (?, ?)",
                                   (row.lastrowid, tokens))
                connection.executemany("INSERT INTO memberships VALUES (?, ?, ?)",
                                       [(kind, value, record["id"]) for kind, value in memberships(record)])
            if links:
                raise releases.ReleaseError("Relationship source must be an exact catalog record ID")
            for (data,) in connection.execute("SELECT data FROM publications ORDER BY id"):
                record = releases.read_json(data.encode())
                if record["publication_state"] in {"notice", "retracted"} and not record["relations"]:
                    issues.append({"kind": "unresolved_notice", "records": [record["id"]],
                                   "detail": "Provider notice state has no interpreted relationship; review source metadata."})
                if any(connection.execute(
                    "SELECT 1 FROM publications WHERE id=? UNION ALL "
                    "SELECT 1 FROM memberships WHERE kind='alias' AND value=? LIMIT 1",
                    (link["target"], link["target"])).fetchone() is None for link in record["relations"]):
                    issues.append({"kind": "unresolved_relationship", "records": [record["id"]],
                                   "detail": "Typed relationship target is outside this bounded catalog; not evidence of absence."})
            # Validate the small header with the existing contract; records are checked individually.
            validate({**metadata, "publications": [], "issues": []})
            records = (releases.read_json(row[0].encode()) for row in connection.execute(
                "SELECT data FROM publications ORDER BY id"))
            report = coverage({**metadata, "publications": records, "issues": issues}, catalog_id="pending")
            report.pop("catalog_id")
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("header", releases.encode(metadata).decode()), ("coverage", releases.encode(report).decode())])
            connection.execute("CREATE TABLE issues (data TEXT NOT NULL)")
            connection.executemany("INSERT INTO issues VALUES (?)",
                                   [(releases.encode(issue).decode(),) for issue in sorted(issues, key=releases.encode)])
            connection.execute("INSERT INTO publication_search(publication_search, rank) VALUES ('integrity-check', 1)")
            connection.commit()
            connection.execute("VACUUM")
        identifier = file_digest(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # Link is atomic and refuses to overwrite an existing version.
        os.link(temporary, path)
        sync_directory(path.parent)
        return {**report, "catalog_id": identifier}
    finally:
        temporary.unlink(missing_ok=True)


class SQLiteCatalogSnapshot:
    profile = "metadata_only"

    def __init__(self, path, *, validate_records=False):
        self.path = Path(path).absolute()
        self.release_id = file_digest(self.path)
        with self.connect() as connection:
            if (connection.execute("PRAGMA application_id").fetchone()[0] != 1230196557
                    or connection.execute("PRAGMA user_version").fetchone()[0] != 1
                    or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"):
                raise releases.ReleaseError("Invalid SQLite catalog format or integrity")
            metadata = dict(connection.execute("SELECT key, data FROM metadata"))
            self.document = releases.read_json(metadata["header"].encode())
            validate({**self.document, "publications": [], "issues": []})
            self._coverage = releases.read_json(metadata["coverage"].encode())
            Draft202012Validator({"$defs": contracts.DOCUMENT["$defs"],
                                  **contracts.ref("catalog_coverage")}).validate(self.coverage())
            self.caveats = self.document["caveats"]
            count = connection.execute("SELECT count(*) FROM publications").fetchone()[0]
            if count != self._coverage["publication_count"]:
                raise releases.ReleaseError("SQLite catalog count differs from coverage")
            if validate_records:
                self.validate_records(connection)

    def connect(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        return closing(connection)

    def validate_records(self, connection):
        validator = Draft202012Validator({"$defs": contracts.DOCUMENT["$defs"], **contracts.ref("catalog_publication")})
        run_ids = {run["id"] for run in self.document["runs"]}
        if connection.execute("SELECT 1 FROM publications WHERE length(CAST(data AS BLOB))>? LIMIT 1",
                              (MAX_RECORD_BYTES,)).fetchone():
            raise releases.ReleaseError("Catalog publication exceeds storage record limit")
        for identifier, data, tokens, eligibility in connection.execute(
                "SELECT id, data, search, eligibility FROM publications ORDER BY id"):
            if len(data.encode()) > MAX_RECORD_BYTES:
                raise releases.ReleaseError("Catalog publication exceeds storage record limit")
            record = releases.read_json(data.encode())
            validator.validate(record)
            if (identifier != record["id"] or tokens != search_tokens(record)
                    or eligibility != record["eligibility"]
                    or any(p["run_id"] not in run_ids for p in record["provenance"])
                    or memberships(record) != sorted(connection.execute(
                        "SELECT kind, value FROM memberships WHERE id=?", (identifier,)))):
                raise releases.ReleaseError("Invalid SQLite catalog record, index or provenance")
        if connection.execute(
                "SELECT 1 FROM memberships WHERE id NOT IN (SELECT id FROM publications) LIMIT 1").fetchone():
            raise releases.ReleaseError("Dangling SQLite catalog membership")
        # Only the temporary schema is writable; the main database is opened mode=ro.
        connection.execute("PRAGMA query_only=OFF")
        connection.execute(
            "CREATE VIRTUAL TABLE temp.catalog_search_vocabulary USING fts5vocab(main, publication_search, instance)")
        connection.execute("PRAGMA query_only=ON")
        vocabulary = connection.execute("SELECT doc, term FROM temp.catalog_search_vocabulary ORDER BY doc, offset")
        indexed = ((doc, " ".join(row[1] for row in group))
                   for doc, group in groupby(vocabulary, key=lambda row: row[0]))
        expected_tokens = connection.execute("SELECT rowid, search FROM publications ORDER BY rowid")
        if any(actual != expected for actual, expected in zip_longest(indexed, expected_tokens)):
            raise releases.ReleaseError("SQLite catalog full-text index differs from publication tokens")
        issue_validator = Draft202012Validator({
            "$defs": contracts.DOCUMENT["$defs"],
            **contracts.DOCUMENT["$defs"]["catalog_document"]["properties"]["issues"]["items"]})
        def issues():
            for (data,) in connection.execute("SELECT data FROM issues"):
                issue = releases.read_json(data.encode())
                issue_validator.validate(issue)
                yield issue
        records = (releases.read_json(row[0].encode()) for row in connection.execute(
            "SELECT data FROM publications ORDER BY id"))
        expected = coverage({**self.document, "publications": records, "issues": issues()},
                            catalog_id=self.release_id)
        if expected != self.coverage():
            raise releases.ReleaseError("SQLite catalog coverage differs from records")

    def verify_backing(self, path):
        if file_digest(path) != self.release_id:
            raise releases.ReleaseError("Catalog checksum mismatch")

    def publication_keys(self):
        with self.connect() as connection:
            return [row[0] for row in connection.execute("SELECT id FROM publications ORDER BY id")]

    def identities(self):
        with self.connect() as connection:
            rows = connection.execute(
                    "SELECT p.id, m.value FROM publications p LEFT JOIN memberships m "
                    "ON p.id=m.id AND m.kind='alias' AND json_extract(p.data, '$.identity_status')='resolved' "
                    "ORDER BY p.id, m.value")
            for identifier, group in groupby(rows, key=lambda row: row[0]):
                yield identifier, [row[1] for row in group if row[1] is not None]

    def coverage(self):
        return {**deepcopy(self._coverage), "catalog_id": self.release_id}

    def matching_ids(self, arguments, *, limit=None, offset=0):
        words = re.findall(r"\w+", arguments.get("query", "").casefold())
        if arguments.get("query") and not words:
            raise retrieval.RetrievalError("Catalog query requires words or numbers")
        clauses, values = [], []
        if words:
            clauses.append("p.rowid IN (SELECT rowid FROM publication_search WHERE publication_search MATCH ?)")
            values.append(" AND ".join('"' + word.encode().hex() + '"' for word in words))
        for key in ("disease", "publication_type"):
            if arguments.get(key):
                clauses.append("p.id IN (SELECT id FROM memberships WHERE kind=? AND value=?)")
                values.extend((key, arguments[key]))
        if arguments.get("eligibility"):
            clauses.append("p.eligibility=?")
            values.append(arguments["eligibility"])
        with self.connect() as connection:
            pagination = " LIMIT ? OFFSET ?" if limit is not None or offset else ""
            if pagination:
                values.extend((limit if limit is not None else -1, offset))
            return [row[0] for row in connection.execute(
                "SELECT p.id FROM publications p" + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY p.id" + pagination, values)]

    def get(self, identifier):
        with self.connect() as connection:
            rows = list(connection.execute("SELECT data FROM publications WHERE id=?", (identifier,)))
            if not rows:
                rows = list(connection.execute(
                    "SELECT data FROM publications WHERE id IN "
                    "(SELECT id FROM memberships WHERE kind='alias' AND value=?) LIMIT 2", (identifier.lower(),)))
        if len(rows) > 1:
            raise retrieval.RetrievalError("Ambiguous publication alias; use the catalog record ID")
        if not rows:
            raise retrieval.NotFoundError("Unknown catalog publication")
        return releases.read_json(rows[0][0].encode())

    def status(self):
        return {"paper_count": self._coverage["publication_count"], "claim_count": 0, "content_unit_count": 0,
                "coverage": self.document["selection_rationale"], "retrieval": "lexical_fts5",
                "rights_profile": self.profile, "source_assets_available": False}


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
