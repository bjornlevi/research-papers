"""Deterministic local release export and fail-closed immutable snapshots."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import threading
from typing import Any

from jsonschema import Draft202012Validator

from . import contracts, retrieval

MANIFEST_SCHEMA = 1
PROFILES = frozenset({"synthetic", "curated", "private_context", "full_source"})
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
SECTIONS = ("sources", "claims", "units", "tables", "issues", "relationships")


class ReleaseError(ValueError):
    """An invalid or unauthorized evidence release."""


class AccessDeniedError(ReleaseError):
    """The selected rights profile does not permit this operation."""


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def memory_index(data):
    db = sqlite3.connect(":memory:")
    try:
        if hasattr(db, "deserialize"):
            db.deserialize(data)
        else:
            # Python 3.10 lacks deserialize; a private temporary file plus backup
            # provides the same detached snapshot without writing the release.
            with tempfile.TemporaryDirectory(prefix="research-library-index-") as temporary:
                path = Path(temporary) / "index.sqlite"
                path.write_bytes(data)
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
                    reader.backup(db)
    except (OSError, sqlite3.Error):
        db.close()
        raise
    return db


def read_json(data: bytes) -> Any:
    def unique(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ReleaseError(f"Duplicate JSON property: {key}")
            record[key] = value
        return record
    def invalid(value):
        raise ReleaseError(f"Non-finite JSON value: {value}")
    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)


def safe_file(root: Path, name: str) -> Path:
    """Reject traversal and every symlink component, including in-root aliases."""
    if not isinstance(name, str) or not name or "\\" in name:
        raise ReleaseError("Invalid release-relative path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ReleaseError("Unsafe release-relative path")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ReleaseError("Symlinks are not allowed in release paths")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ReleaseError("Release path escaped its root")
    if not path.is_file():
        raise ReleaseError(f"Missing release file: {name}")
    return path


def _record_schema(name):
    document = deepcopy(contracts.DOCUMENT)
    document.pop("$id", None)
    document["$ref"] = "#/$defs/" + name
    if name == "table":
        # Storage is complete; response pagination, not export, imposes 100 rows.
        document["$defs"]["table"]["properties"]["rows"].pop("maxItems")
    return document


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or set(bundle) != {"schema_version", "caveats", *SECTIONS}:
        raise ReleaseError("Evidence bundle has unexpected or missing sections")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != MANIFEST_SCHEMA:
        raise ReleaseError("Unsupported evidence bundle schema")
    if any(not isinstance(bundle[section], list) for section in SECTIONS):
        raise ReleaseError("Every evidence section must be an array")
    Draft202012Validator(contracts.DOCUMENT["$defs"]["caveats"] |
                         {"$defs": contracts.DOCUMENT["$defs"]}).validate(bundle["caveats"])
    registry = {}
    source_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["id", "doi", "sha256", "format", "url", "provenance", "rights", "omission"],
        "properties": {
            "id": contracts.ref("id"), "doi": contracts.ref("id"), "sha256": contracts.ref("hash"),
            "format": {"enum": ["pdf", "docx", "xlsx"]},
            "url": {"type": ["string", "null"], "maxLength": 2048, "pattern": "^https?://\\S+$"},
            "provenance": {"type": "string", "minLength": 1, "maxLength": 4096},
            "rights": {"enum": ["unknown", "restricted", "synthetic"]},
            "omission": {"enum": ["original_not_exported", "original_included"]},
            "asset_id": contracts.ref("id"),
            "bibliography": contracts.ref("source_bibliography"),
        }, "$defs": contracts.DOCUMENT["$defs"],
    }
    source_validator = Draft202012Validator(source_schema)
    for source in bundle["sources"]:
        source_validator.validate(source)
        if source["id"] in registry:
            raise ReleaseError("Duplicate source ID")
        registry[source["id"]] = source
    if not registry:
        raise ReleaseError("An evidence release must identify at least one source")
    ids = {}
    validators = {key: Draft202012Validator(_record_schema(kind)) for key, kind in (
        ("claims", "claim"), ("units", "context"), ("tables", "table"),
        ("issues", "issue"), ("relationships", "relationship"))}
    unit_schema = _record_schema("context")
    unit_schema["$defs"]["context"]["properties"]["text"] = {"type": "string"}
    unit_validator = Draft202012Validator(unit_schema)
    for section in SECTIONS[1:]:
        if not isinstance(bundle[section], list):
            raise ReleaseError(f"Expected array: {section}")
        for wrapped in bundle[section]:
            if not isinstance(wrapped, dict):
                raise ReleaseError("Evidence records must be objects")
            if section in {"issues", "relationships"}:
                expected = {"number", "title", "paper_ids", "images", "record"} if section == "issues" else {"from_id", "record"}
                if set(wrapped) != expected:
                    raise ReleaseError(f"Invalid {section} wrapper")
                record = wrapped["record"]
                if section == "issues" and (
                        type(wrapped["number"]) is not int or wrapped["number"] < 1 or
                        not isinstance(wrapped["title"], str) or not wrapped["title"].strip() or
                        not isinstance(wrapped["paper_ids"], list) or
                        not all(isinstance(value, str) for value in wrapped["paper_ids"]) or
                        not isinstance(wrapped["images"], list) or
                        not all(isinstance(value, str) for value in wrapped["images"])):
                    raise ReleaseError("Invalid source issue presentation metadata")
                if section == "relationships" and not isinstance(wrapped["from_id"], str):
                    raise ReleaseError("Invalid relationship origin")
            else:
                record = wrapped
            if section == "units":
                # Full text is only stored in explicitly authorized profiles.
                unit_validator.validate(record)
            else:
                validators[section].validate(record)
            if record["id"] in ids:
                raise ReleaseError("Duplicate evidence ID: " + record["id"])
            ids[record["id"]] = section
            _citations(record, registry)
            if section == "claims":
                # Use semantic CI validation as well as the record schema.
                contracts.success("get_claim", "bundle-validation", record, [])
            if section == "tables":
                width = len(record["headers"])
                if len(record["units"]) != width or any(len(row) != width for row in record["rows"]):
                    raise ReleaseError("Ragged table or missing column units")
                if record["truncated"] or record["next_offset"] is not None:
                    raise ReleaseError("Release tables must contain all stored rows")
    issue_ids = {item["record"]["id"] for item in bundle["issues"]}
    for section in ("claims", "units"):
        for record in bundle[section]:
            if not set(record["source_issue_ids"]) <= issue_ids:
                raise ReleaseError("Dangling source issue reference")
    paper_ids = {retrieval.paper_id(source["doi"]) for source in registry.values()}
    if any(not set(wrapped["paper_ids"]) <= paper_ids for wrapped in bundle["issues"]):
        raise ReleaseError("Dangling source issue paper reference")
    for wrapped in bundle["relationships"]:
        if ids.get(wrapped["from_id"]) not in {"claims", "units", "tables", "issues"} or ids.get(
                wrapped["record"]["target_id"]) not in {"claims", "units", "tables", "issues"}:
            raise ReleaseError("Dangling relationship endpoint")
    return registry


def _citations(record, sources):
    if isinstance(record, dict):
        if "source_id" in record:
            source = sources.get(record["source_id"])
            if source is None or (record["source_sha256"], record["doi"], record["source_url"],
                                  record["provenance"]) != (
                    source["sha256"], source["doi"], source["url"], source["provenance"]):
                raise ReleaseError("Citation source identity/provenance mismatch")
            kind = record["locator"]["kind"]
            if (source["format"] == "pdf") != (kind == "pdf"):
                raise ReleaseError("PDF and Office source locators cannot be interchanged")
            if source["format"] == "docx" and kind not in {"word_table", "word_paragraph", "word_package"}:
                raise ReleaseError("Invalid Word locator")
            if source["format"] == "xlsx" and kind != "worksheet":
                raise ReleaseError("Invalid spreadsheet locator")
        for value in record.values():
            _citations(value, sources)
    elif isinstance(record, list):
        for value in record:
            _citations(value, sources)


def _set_availability(value, availability):
    if isinstance(value, dict):
        if "source_id" in value:
            value["availability"] = availability[value["source_id"]]
        for item in value.values():
            _set_availability(item, availability)
    elif isinstance(value, list):
        for item in value:
            _set_availability(item, availability)


def _index(path, bundle):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE papers (paper_id TEXT PRIMARY KEY, doi TEXT);
            CREATE TABLE content_units (unit_id TEXT PRIMARY KEY, paper_id TEXT, payload_json TEXT);
            CREATE VIRTUAL TABLE content_search USING fts5(unit_id UNINDEXED,paper_id UNINDEXED,text,tokenize='unicode61 remove_diacritics 2');
            CREATE TABLE claims (claim_id TEXT PRIMARY KEY,paper_id TEXT,inventory_number INTEGER,
                                 topic TEXT,claim_kind TEXT,payload_json TEXT);
            CREATE VIRTUAL TABLE claim_search USING fts5(claim_id UNINDEXED,text,tokenize='unicode61 remove_diacritics 2');
            CREATE TABLE results (result_id TEXT PRIMARY KEY,paper_id TEXT,measure TEXT,payload_json TEXT);
        """)
        papers = {retrieval.paper_id(source["doi"]): source["doi"] for source in bundle["sources"]}
        db.executemany("INSERT INTO papers VALUES (?,?)", sorted(papers.items()))
        for unit in sorted(bundle["units"], key=lambda row: row["id"]):
            pid = retrieval.paper_id(unit["citation"]["doi"])
            db.execute("INSERT INTO content_units VALUES (?,?,?)", (unit["id"], pid, encode(unit).decode()))
            db.execute("INSERT INTO content_search VALUES (?,?,?)", (unit["id"], pid, unit["text"]))
        for claim in sorted(bundle["claims"], key=lambda row: row["id"]):
            pid = retrieval.paper_id(claim["citation"]["doi"])
            db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?)",
                       (claim["id"], pid, 0, "", claim["claim_kind"], encode(claim).decode()))
            db.execute("INSERT INTO claim_search VALUES (?,?)",
                       (claim["id"], claim["statement"] + " " + claim["supporting_quote"]))
    return path.read_bytes()


def export(bundle, destination, *, profile="curated", authorization=None, asset_paths=None):
    """Export to a new directory only; original workspaces are never modified."""
    validate_bundle(bundle)
    if profile not in PROFILES:
        raise ReleaseError("Unknown rights profile")
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ReleaseError("Export destination must not already exist")
    grants = authorization or {}
    if not isinstance(grants, dict):
        raise ReleaseError("Authorization must be a source-ID mapping")
    if profile == "synthetic" and grants:
        raise ReleaseError("Synthetic export must not claim external authorization")
    selected = deepcopy(bundle)
    # Never infer rights from OA hosting, DOI, possession, or private repository status.
    for source in selected["sources"]:
        if profile == "synthetic":
            if source["rights"] != "synthetic":
                raise AccessDeniedError("Synthetic profile requires authored fictional sources")
        else:
            grant = grants.get(source["id"], {})
            required = {"curated"}
            if profile in {"private_context", "full_source"}:
                required.add("context")
            if profile == "full_source":
                required.add("original")
            if (not isinstance(grant, dict) or set(grant) != {"scopes", "basis"}
                    or not isinstance(grant.get("scopes"), list)
                    or not all(isinstance(scope, str) for scope in grant["scopes"])
                    or not required <= set(grant["scopes"]) or not isinstance(grant.get("basis"), str)
                    or not grant["basis"].strip()):
                raise AccessDeniedError(f"Explicit authorization and basis required for {source['id']}")
        source["omission"] = "original_not_exported"
        source.pop("asset_id", None)
    if profile == "curated":
        # Curated claims/issues/tables remain; unbounded extracted context is excluded entirely.
        selected["units"] = []
        selected["tables"] = [record for record in selected["tables"]
                              if record["verification_status"] in {"source_checked_by_ai", "source_checked_by_human"}]
        retained = {row["id"] for section in ("claims", "units", "tables") for row in selected[section]}
        retained.update(item["record"]["id"] for item in selected["issues"])
        selected["relationships"] = [edge for edge in selected["relationships"]
                                     if edge["from_id"] in retained and edge["record"]["target_id"] in retained]
    for item in selected["issues"]:
        # Rendered review images stay in the build workspace, never as dangling release asset links.
        item["images"] = []
    payloads = {}
    assets = {}
    if profile == "full_source" or (profile == "synthetic" and asset_paths):
        for source in selected["sources"]:
            path = (asset_paths or {}).get(source["id"])
            if path is None:
                raise ReleaseError("Original asset required by selected export profile")
            path = Path(path)
            data = path.read_bytes()
            if sha256(data) != source["sha256"]:
                raise ReleaseError("Original asset checksum mismatch")
            name = "assets/" + source["sha256"] + "." + source["format"]
            payloads[name] = data
            source["asset_id"] = source["id"]
            source["omission"] = "original_included"
            assets[source["id"]] = name
    availability = {source["id"]: ("authorized" if source["id"] in assets else "metadata_only")
                    for source in selected["sources"]}
    _set_availability(selected, availability)
    for section in SECTIONS:
        selected[section].sort(key=lambda row: row["record"]["id"] if "record" in row else row["id"])
    validate_bundle(selected)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".release-build-") as temporary:
        staging = Path(temporary) / "release"
        staging.mkdir()
        (staging / "evidence").mkdir()
        payloads["records.json"] = encode(selected)
        payloads["evidence/index.sqlite"] = _index(staging / "evidence/index.sqlite", selected)
        entries = {name: {"sha256": sha256(data), "bytes": len(data)}
                   for name, data in sorted(payloads.items())}
        manifest = {"schema_version": MANIFEST_SCHEMA, "tool_schema_version": contracts.SCHEMA_VERSION,
                    "profile": profile, "files": entries, "assets": assets,
                    "authorization": {} if profile == "synthetic" else {
                        source["id"]: grants[source["id"]] for source in selected["sources"]}}
        manifest["release_id"] = sha256(encode(manifest))
        for name, data in payloads.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (staging / "manifest.json").write_bytes(encode(manifest))
        validate(staging, allowed_profiles={profile})
        os.rename(staging, destination)
    return manifest["release_id"]


def validate(root, *, allowed_profiles):
    """Validate every byte and reference before returning a detached request snapshot."""
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError("Release root must be a real directory")
    manifest_path = safe_file(root, "manifest.json")
    if manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise ReleaseError("Manifest exceeds byte limit")
    manifest_bytes = manifest_path.read_bytes()
    manifest = read_json(manifest_bytes)
    expected_keys = {"schema_version", "tool_schema_version", "profile", "files", "assets",
                     "authorization", "release_id"}
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise ReleaseError("Invalid release manifest")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != MANIFEST_SCHEMA
            or manifest["tool_schema_version"] not in {"2.0.0", "2.1.0", "2.2.0", contracts.SCHEMA_VERSION}):
        raise ReleaseError("Unsupported release/tool schema")
    if not isinstance(manifest["profile"], str) or manifest["profile"] not in PROFILES or manifest["profile"] not in allowed_profiles:
        raise AccessDeniedError("Release rights profile is not allowed by this operator")
    identity = {key: value for key, value in manifest.items() if key != "release_id"}
    if sha256(encode(identity)) != manifest["release_id"]:
        raise ReleaseError("Manifest content identity mismatch")
    if not isinstance(manifest["files"], dict) or not {"records.json", "evidence/index.sqlite"} <= manifest["files"].keys():
        raise ReleaseError("Release payload inventory is incomplete")
    payloads = {}
    total = 0
    for name, entry in manifest["files"].items():
        if not isinstance(entry, dict) or set(entry) != {"sha256", "bytes"} or type(entry["bytes"]) is not int or entry["bytes"] < 0:
            raise ReleaseError("Invalid file inventory record")
        total += entry["bytes"]
        if total > MAX_PAYLOAD_BYTES:
            raise ReleaseError("Release payload exceeds byte limit")
        path = safe_file(root, name)
        if path.stat().st_size != entry["bytes"]:
            raise ReleaseError("Release payload size mismatch: " + name)
        data = path.read_bytes()
        if sha256(data) != entry["sha256"]:
            raise ReleaseError("Release payload checksum mismatch: " + name)
        payloads[name] = data
    actual_files = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseError("Symlink in release directory")
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != {"manifest.json", *payloads}:
        raise ReleaseError("Unlisted or missing release files")
    bundle = read_json(payloads["records.json"])
    sources = validate_bundle(bundle)
    if manifest["profile"] == "synthetic" and any(s["rights"] != "synthetic" for s in sources.values()):
        raise AccessDeniedError("Synthetic release contains sources with non-synthetic rights")
    if manifest["profile"] == "curated" and bundle["units"]:
        raise AccessDeniedError("Curated profile must not contain extracted context")
    if manifest["profile"] == "curated" and any(
            record["verification_status"] not in {"source_checked_by_ai", "source_checked_by_human"}
            for record in bundle["tables"]):
        raise AccessDeniedError("Curated profile cannot contain machine-extracted tables")
    if not isinstance(manifest["assets"], dict):
        raise ReleaseError("Invalid asset registry")
    if manifest["profile"] not in {"synthetic", "full_source"} and manifest["assets"]:
        raise AccessDeniedError("Profile forbids originals")
    asset_files = set()
    for source_id, name in manifest["assets"].items():
        source = sources.get(source_id)
        if source is None or name != "assets/" + source["sha256"] + "." + source["format"]:
            raise ReleaseError("Invalid controlled asset reference")
        if name not in payloads or sha256(payloads[name]) != source["sha256"]:
            raise ReleaseError("Missing or invalid original asset")
        asset_files.add(name)
    if set(payloads) != {"records.json", "evidence/index.sqlite", *asset_files}:
        raise ReleaseError("Unexpected profile payloads")
    for source_id, source in sources.items():
        included = source_id in manifest["assets"]
        if (source["omission"] == "original_included") != included or ("asset_id" in source) != included:
            raise ReleaseError("Source asset availability mismatch")
        if included and source["asset_id"] != source_id:
            raise ReleaseError("Source asset ID mismatch")
        if manifest["profile"] == "full_source" and not included:
            raise ReleaseError("Full-source release is missing an original")
    expected_availability = {sid: "authorized" if sid in manifest["assets"] else "metadata_only" for sid in sources}
    checked = deepcopy(bundle)
    _set_availability(checked, expected_availability)
    if checked != bundle:
        raise ReleaseError("Citation availability disagrees with release permissions")
    if manifest["profile"] != "synthetic":
        required = {"curated"}
        if manifest["profile"] in {"private_context", "full_source"}:
            required.add("context")
        if manifest["profile"] == "full_source":
            required.add("original")
        grants = manifest["authorization"]
        if not isinstance(grants, dict) or set(grants) != set(sources):
            raise AccessDeniedError("Incomplete source authorization inventory")
        for grant in grants.values():
            if (not isinstance(grant, dict) or set(grant) != {"scopes", "basis"} or
                    not isinstance(grant["scopes"], list) or
                    not all(isinstance(scope, str) for scope in grant["scopes"]) or
                    not required <= set(grant["scopes"]) or
                    not isinstance(grant["basis"], str) or not grant["basis"].strip()):
                raise AccessDeniedError("Invalid source authorization")
    elif manifest["authorization"] != {}:
        raise ReleaseError("Synthetic release must not claim external authorization")
    # Check SQLite semantics against the checksummed canonical payload, not only file integrity.
    with closing(memory_index(payloads["evidence/index.sqlite"])) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ReleaseError("Release search index integrity failure")
        for table in ("content_search", "claim_search"):
            db.execute(f"INSERT INTO {table}({table},rank) VALUES('integrity-check',1)")
        units = sorted((r["id"], retrieval.paper_id(r["citation"]["doi"]), encode(r).decode()) for r in bundle["units"])
        claims = sorted((r["id"], retrieval.paper_id(r["citation"]["doi"]), 0, "", r["claim_kind"], encode(r).decode())
                        for r in bundle["claims"])
        comparisons = [
            ("SELECT * FROM content_units ORDER BY unit_id", units),
            ("SELECT * FROM claims ORDER BY claim_id", claims),
            ("SELECT * FROM content_search ORDER BY unit_id",
             sorted((r["id"], retrieval.paper_id(r["citation"]["doi"]), r["text"]) for r in bundle["units"])),
            ("SELECT * FROM claim_search ORDER BY claim_id",
             sorted((r["id"], r["statement"] + " " + r["supporting_quote"]) for r in bundle["claims"])),
            ("SELECT * FROM papers ORDER BY paper_id",
             sorted({(retrieval.paper_id(s["doi"]), s["doi"]) for s in bundle["sources"]})),
            ("SELECT * FROM results", []),
        ]
        for sql, expected in comparisons:
            if db.execute(sql).fetchall() != expected:
                raise ReleaseError("Search index differs from canonical records")
    return Snapshot(manifest, bundle, payloads)


class Snapshot:
    """A pinned, detached view; later activation cannot change an in-flight request."""

    def __init__(self, manifest, bundle, payloads):
        self.release_id = manifest["release_id"]
        self.profile = manifest["profile"]
        self._manifest = deepcopy(manifest)
        self._bundle = deepcopy(bundle)
        self._payloads = dict(payloads)

    def verify_backing(self, root):
        """Verify backing bytes against this already fully validated snapshot."""
        root = Path(root).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ReleaseError("Release root must be a real directory")
        manifest_path = safe_file(root, "manifest.json")
        if manifest_path.stat().st_size > 4 * 1024 * 1024:
            raise ReleaseError("Manifest exceeds byte limit")
        if read_json(manifest_path.read_bytes()) != self._manifest:
            raise ReleaseError("Backing manifest differs from validated snapshot")
        for name, entry in self._manifest["files"].items():
            path = safe_file(root, name)
            if path.stat().st_size != entry["bytes"] or sha256(path.read_bytes()) != entry["sha256"]:
                raise ReleaseError("Backing payload differs from validated snapshot")
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ReleaseError("Symlink in release directory")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != {"manifest.json", *self._manifest["files"]}:
            raise ReleaseError("Unlisted or missing release files")

    def _lookup(self, section, identifier):
        for wrapped in self._bundle[section]:
            record = wrapped["record"] if section in {"issues", "relationships"} else wrapped
            if record["id"] == identifier:
                return deepcopy(record)
        raise retrieval.NotFoundError(f"Unknown {section} ID")

    def asset(self, asset_id):
        if not any(source["id"] == asset_id for source in self._bundle["sources"]):
            raise retrieval.NotFoundError("Unknown source asset ID")
        name = self._manifest["assets"].get(asset_id)
        if name is None:
            raise AccessDeniedError("Original not included/authorized; citation metadata remains available")
        return self._payloads[name]

    def call(self, tool: contracts.ToolName, arguments: dict):
        selected = contracts.request(tool, arguments)
        if selected.get("collections") or selected.get("snapshot_id"):
            raise AccessDeniedError("Collection selection requires a collection registry endpoint")
        if tool == "list_collections":
            return contracts.success(tool, self.release_id, {"items": []},
                                     ["Legacy single-release endpoint; no collection registry configured."])
        if tool in ("list_publications", "catalog_status"):
            result = ({"items": [], "next_offset": None, "scope": contracts.CORPUS_SCOPE}
                      if tool == "list_publications" else
                      {"items": [], "scope": "No metadata catalog configured on this evidence endpoint."})
            return contracts.success(tool, self.release_id, result, ["Evidence release only; no literature catalog."])
        if tool == "get_publication":
            raise retrieval.NotFoundError("No metadata catalog configured")
        if tool == "corpus_status":
            result = {
                "paper_count": len({source["doi"] for source in self._bundle["sources"]}),
                "claim_count": len(self._bundle["claims"]), "content_unit_count": len(self._bundle["units"]),
                "coverage": "Published evidence only; not exhaustive and no participant-level data.",
                "retrieval": "lexical_fts5", "rights_profile": self.profile,
                "source_assets_available": bool(self._manifest["assets"]),
            }
        elif tool == "list_sources":
            rows = self.source_inventory(selected.get("paper"))
            items, continuation = self._page(rows, selected)
            result = {"items": items, "next_offset": continuation, "scope": contracts.CORPUS_SCOPE}
        elif tool == "get_claim":
            result = self._lookup("claims", selected["claim_id"])
        elif tool == "get_source_issue":
            result = self._lookup("issues", selected["issue_id"])
        elif tool == "get_source_context":
            result = self.source_context(selected["unit_id"], limit=selected["max_characters"])["record"]
        elif tool == "get_table":
            result = self._lookup("tables", selected["table_id"])
            rows, continuation = self._page(result["rows"], selected)
            result.update(rows=rows, next_offset=continuation, truncated=continuation is not None)
        elif tool == "get_related_evidence":
            identifier = selected["evidence_id"]
            if not (any(record["id"] == identifier for section in ("claims", "units", "tables")
                        for record in self._bundle[section]) or any(
                            item["record"]["id"] == identifier for item in self._bundle["issues"])):
                raise retrieval.NotFoundError("Unknown relationship origin ID")
            rows = [deepcopy(wrapped["record"]) for wrapped in self._bundle["relationships"]
                    if wrapped["from_id"] == identifier]
            items, continuation = self._page(rows, selected)
            result = {"items": items, "next_offset": continuation, "scope": contracts.CORPUS_SCOPE}
        else:
            with closing(memory_index(self._payloads["evidence/index.sqlite"])) as db:
                db.execute("PRAGMA query_only=ON")
                # Existing shared retrieval owns FTS syntax, parameterization and ordering.
                rows = retrieval.search(db, selected["query"], paper=selected.get("paper"),
                                        limit=selected["limit"], offset=selected["offset"],
                                        mode=selected["mode"])
                count_args = {**selected, "offset": selected["offset"] + len(rows)}
                if rows and count_args["offset"] > retrieval.MAX_OFFSET:
                    raise ReleaseError("Search pagination offset ceiling exceeded")
                more = retrieval.search(db, selected["query"], paper=selected.get("paper"),
                                        limit=1, offset=count_args["offset"], mode=selected["mode"])
            items = []
            for row in rows:
                row.pop("doi")
                row.pop("truncated")
                row["snippet"] = row["snippet"][:2048]
                items.append(row)
            continuation = selected["offset"] + len(items) if more else None
            result = {"items": items, "next_offset": continuation, "scope": contracts.CORPUS_SCOPE}
        response = contracts.success(tool, self.release_id, result, deepcopy(self._bundle["caveats"]))
        contracts.validate_response(tool, arguments, response)
        return response

    def source_context(self, unit_id, *, offset=0, limit=2048):
        """Read a bounded slice of canonical text without changing its locator."""
        if self.profile == "curated":
            raise AccessDeniedError("Extracted context is not authorized by this release profile")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 2048:
            raise retrieval.RetrievalError("Invalid text pagination")
        result = self._lookup("units", unit_id)
        text = result["text"]
        if offset > len(text):
            raise retrieval.RetrievalError("Text offset exceeds retained unit length")
        result["text"] = text[offset:offset + limit]
        result["truncated"] = result["truncated"] or offset > 0 or offset + limit < len(text)
        return {"record": result, "offset": offset, "total_characters": len(text),
                "next_offset": offset + limit if offset + limit < len(text) else None}

    def source_units(self, source_id):
        """List original locators, not text, for a known source document."""
        if not any(source["id"] == source_id for source in self._bundle["sources"]):
            raise retrieval.NotFoundError("Unknown source document")
        return [{key: deepcopy(unit[key]) for key in (
            "id", "citation", "verification_status", "human_verification", "caveats", "source_issue_ids")}
                for unit in self._bundle["units"] if unit["citation"]["source_id"] == source_id]

    def source_inventory(self, paper=None):
        """Inventory the exact source versions behind this release, without original bytes."""
        usage = {source["id"]: set() for source in self._bundle["sources"]}

        def collect(value, section):
            if isinstance(value, dict):
                if "source_id" in value:
                    usage[value["source_id"]].add(section)
                for child in value.values():
                    collect(child, section)
            elif isinstance(value, list):
                for child in value:
                    collect(child, section)

        for section in SECTIONS[1:]:
            collect(self._bundle[section], section)
        rows = []
        for source in sorted(self._bundle["sources"], key=lambda source: (source["doi"], source["id"])):
            if paper is not None and retrieval.paper_id(source["doi"]) != retrieval.paper_id(paper):
                continue
            bibliography = deepcopy(source.get("bibliography", {
                "title": None, "year": None, "acquired_from": None,
                "acquisition_method": None, "acquired_at": None,
            }))
            rows.append({
                "source_id": source["id"], "publication_id": source["doi"], "doi": source["doi"],
                "source_sha256": source["sha256"], "format": source["format"],
                "source_url": source["url"], "provenance": source["provenance"],
                "bibliography": bibliography, "used_in": sorted(usage[source["id"]]),
                "original_served": False,
                "acquisition_details_recorded": any(bibliography[key] is not None for key in (
                    "acquired_from", "acquisition_method", "acquired_at")),
            })
        return rows

    @staticmethod
    def _page(rows, selected):
        offset, limit = selected["offset"], selected["limit"]
        items = rows[offset:offset + limit]
        continuation = offset + len(items) if offset + len(items) < len(rows) else None
        if continuation is not None and continuation > retrieval.MAX_OFFSET:
            raise ReleaseError("Pagination offset ceiling exceeded")
        return deepcopy(items), continuation


class ReleaseStore:
    """Local operator-controlled store with one atomic pointer and explicit rollback."""

    def __init__(self, root, *, allowed_profiles, public_quality_root=None):
        self.root = Path(root).absolute()
        self.allowed_profiles = frozenset(allowed_profiles)
        if self.root.is_symlink():
            raise ReleaseError("Release store cannot be a symlink")
        from .quality import public_policy
        self.public_quality_root = public_policy(self.root, public_quality_root)
        self._snapshot = None
        self._snapshot_lock = threading.Lock()

    def _release(self, identifier):
        if not isinstance(identifier, str) or len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
            raise ReleaseError("Invalid content-addressed release ID")
        path = self.root / "releases" / identifier
        if path.parent.is_symlink() or path.is_symlink():
            raise ReleaseError("Symlinked release store path")
        snapshot = validate(path, allowed_profiles=self.allowed_profiles)
        if snapshot.release_id != identifier:
            raise ReleaseError("Stored release ID differs from directory identity")
        return snapshot

    def current(self):
        state = self._state()
        identifier = state["active"]
        with self._snapshot_lock:
            if self._snapshot is not None and self._snapshot.release_id == identifier:
                parent = self.root / "releases"
                if parent.is_symlink():
                    raise ReleaseError("Symlinked release store path")
                self._snapshot.verify_backing(parent / identifier)
            else:
                self._snapshot = self._release(identifier)
            self._public_check(self._snapshot)
            return self._snapshot

    def _public_check(self, snapshot):
        if self.public_quality_root is not None:
            from .quality import require_ready
            require_ready(snapshot, self.public_quality_root)

    def _state(self):
        state = read_json(safe_file(self.root, "active.json").read_bytes())
        if not isinstance(state, dict) or set(state) != {"active", "previous"}:
            raise ReleaseError("Invalid activation pointer")
        for identifier in (state["active"], state["previous"]):
            if identifier is not None and (not isinstance(identifier, str) or len(identifier) != 64
                                           or any(c not in "0123456789abcdef" for c in identifier)):
                raise ReleaseError("Invalid activation pointer identity")
        if state["active"] is None:
            raise ReleaseError("Active release ID must not be null")
        return state

    def install(self, release):
        snapshot = validate(release, allowed_profiles=self.allowed_profiles)
        parent = self.root / "releases"
        if parent.is_symlink():
            raise ReleaseError("Symlinked release store")
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / snapshot.release_id
        if target.exists():
            self._release(snapshot.release_id)
            return snapshot.release_id
        with tempfile.TemporaryDirectory(dir=parent, prefix=".install-") as temporary:
            staging = Path(temporary) / "release"
            shutil.copytree(release, staging, symlinks=True)
            staged = validate(staging, allowed_profiles=self.allowed_profiles)
            if staged.release_id != snapshot.release_id:
                raise ReleaseError("Release changed while being installed")
            os.rename(staging, target)
        return snapshot.release_id

    def activate(self, identifier):
        # Validation completes before any activation-state write.
        snapshot = self._release(identifier)
        self._public_check(snapshot)
        self.root.mkdir(parents=True, exist_ok=True)
        previous = None
        pointer = self.root / "active.json"
        if pointer.exists() or pointer.is_symlink():
            previous = self._state()["active"]
        if previous == identifier:
            return snapshot
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix=".active-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encode({"active": identifier, "previous": previous}))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, pointer)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
        return snapshot

    def rollback(self):
        state = self._state()
        if not state.get("previous"):
            raise ReleaseError("No previous release to roll back to")
        return self.activate(state["previous"])
