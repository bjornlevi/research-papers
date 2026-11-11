"""Independent evidence collections, immutable registry snapshots and scoped search."""

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading

from jsonschema import ValidationError

from . import contracts, releases, retrieval

MAX_COLLECTIONS = 100
MAX_REGISTRY_BYTES = 4 * 1024 * 1024


def publication_keys(snapshot):
    return (snapshot.publication_keys() if hasattr(snapshot, "publication_keys") else
            [source["doi"] for source in snapshot._bundle["sources"]])


def is_catalog(snapshot):
    return hasattr(snapshot, "document")


def snapshot_caveats(snapshot):
    return snapshot.caveats if is_catalog(snapshot) else snapshot._bundle["caveats"]


class SnapshotUnavailable(releases.ReleaseError):
    """A requested retained registry snapshot is unavailable."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value):
        raise releases.ReleaseError("Invalid collection ID")
    return value


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise releases.ReleaseError("Invalid registry or release digest")
    return value


def publication(value):
    value = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return "doi:" + value if value.startswith("10.") else value


def read_document(path):
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise releases.ReleaseError("Collection registry exceeds byte limit")
    return releases.read_json(path.read_bytes())


def atomic_json(root, name, data):
    if root.is_symlink() or (root / name).is_symlink():
        raise releases.ReleaseError("Symlinked collection registry path")
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix=".registry-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(releases.encode(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / name)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class CollectionSnapshot:
    """Detached collection set pinned to one content-addressed registry."""

    def __init__(self, document, snapshots, allowed, defaults):
        self.document = deepcopy(document)
        self.release_id = releases.sha256(releases.encode(document))
        self.snapshots = snapshots
        self.allowed = frozenset(allowed)
        self.defaults = tuple(defaults)
        self.entries = {entry["id"]: entry for entry in document["collections"]
                        if entry["id"] in self.allowed}
        if not self.allowed <= self.entries.keys():
            raise releases.AccessDeniedError("Configured collection is absent from this registry")
        self._aliases = {}
        owners = {}
        for cid, entry in self.entries.items():
            snapshot = snapshots[cid]
            if not entry["publications"].keys() <= set(publication_keys(snapshot)):
                raise releases.ReleaseError("Publication mapping refers to an absent source")
            identities = (snapshot.identities() if is_catalog(snapshot) else
                          ((key, []) for key in publication_keys(snapshot)))
            for key, catalog_aliases in identities:
                canonical = publication(key)
                mapping = entry["publications"].get(key)
                aliases = [canonical]
                if mapping:
                    canonical = mapping["id"]
                    aliases += mapping["aliases"]
                aliases += catalog_aliases
                for alias in aliases:
                    alias = publication(alias)
                    if alias in owners and owners[alias] != canonical:
                        raise releases.ReleaseError("Conflicting publication identities; resolve aliases before publishing")
                    owners[alias] = canonical
                self._aliases[cid, key] = canonical

    def selected(self, arguments):
        selected = tuple(sorted(arguments.get("collections", self.defaults)))
        if not set(selected) <= self.allowed:
            raise releases.AccessDeniedError("Unknown or unauthorized collection selection")
        return selected

    def info(self, cid):
        entry = self.entries[cid]
        snapshot = self.snapshots[cid]
        status = snapshot.status() if is_catalog(snapshot) else snapshot.call("corpus_status", {})["result"]
        return {key: entry[key] for key in ("id", "title", "version", "updated_at", "coverage")} | {
            "release_id": snapshot.release_id, "rights_profile": snapshot.profile,
            **{key: status[key] for key in ("paper_count", "claim_count", "content_unit_count")},
            "default": cid in self.defaults,
            "kind": "catalog" if is_catalog(snapshot) else "evidence",
        }

    def reference(self, cid, record, index):
        return {"record_id": record["id"], "collection_id": cid,
                "release_id": self.snapshots[cid].release_id, "result_index": index,
                "publication_id": self._aliases[cid, record["citation"]["doi"]]}

    def _search(self, selected, arguments, *, papers=None):
        selected = [cid for cid in selected if not is_catalog(self.snapshots[cid])]
        if not selected:
            return {"items": [], "next_offset": None, "scope": contracts.CORPUS_SCOPE}, []
        if len(selected) == 1 and papers is None:
            cid, = selected
            response = self.snapshots[cid].call("search_evidence", arguments)
            result = response["result"]
            refs = [self.reference(cid, record, index) for index, record in enumerate(result["items"])]
            return result, refs
        # One derived index per query means filtering precedes BM25 and pagination.
        # Only immutable canonical units are indexed; no release files are changed.
        documents = {}
        for cid in selected:
            snapshot = self.snapshots[cid]
            for unit in snapshot._bundle["units"]:
                citation = unit["citation"]
                if papers is not None and publication(citation["doi"]) not in papers:
                    continue
                identity = releases.sha256(releases.encode({
                    "publication": self._aliases[cid, citation["doi"]],
                    "sha256": citation["source_sha256"], "locator": citation["locator"],
                    "text": unit["text"], "verification_status": unit["verification_status"],
                    "human_verification": unit["human_verification"], "caveats": unit["caveats"],
                }))
                if arguments.get("paper") is not None and retrieval.paper_id(citation["doi"]) != retrieval.paper_id(arguments["paper"]):
                    continue
                item = documents.setdefault(identity, {"unit": unit, "refs": []})
                item["refs"].append((cid, unit))
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE VIRTUAL TABLE search USING fts5(identity UNINDEXED, text, "
                       "tokenize='unicode61 remove_diacritics 2')")
            db.executemany("INSERT INTO search VALUES (?, ?)",
                           [(key, item["unit"]["text"]) for key, item in sorted(documents.items())])
            rows = db.execute(
                "SELECT identity, snippet(search,1,'[',']','...',35) FROM search "
                "WHERE search MATCH ? ORDER BY bm25(search),identity LIMIT ? OFFSET ?",
                (retrieval.search_expression(arguments["query"], arguments["mode"]),
                 arguments["limit"] + 1, arguments["offset"]),
            ).fetchall()
        items, refs = [], []
        for identity, snippet in rows[:arguments["limit"]]:
            item = documents[identity]
            unit = deepcopy(item["unit"])
            unit.pop("text")
            unit.pop("truncated")
            unit["snippet"] = snippet[:2048]
            refs += [self.reference(cid, record, len(items)) for cid, record in item["refs"]]
            items.append(unit)
        continuation = arguments["offset"] + len(items) if len(rows) > arguments["limit"] else None
        if continuation is not None and continuation > retrieval.MAX_OFFSET:
            raise releases.ReleaseError("Search pagination offset ceiling exceeded")
        return {"items": items, "next_offset": continuation, "scope": contracts.CORPUS_SCOPE}, refs

    def call(self, tool, arguments):
        request = contracts.request(tool, arguments)
        selected = self.selected(request)
        plain = {key: value for key, value in request.items() if key not in ("collections", "snapshot_id")}
        refs = []
        if tool == "list_collections":
            visible = sorted(self.allowed) if "collections" not in request else selected
            result = {"items": [self.info(cid) for cid in visible]}
            selected = visible
        elif tool == "corpus_status":
            statuses = [self.snapshots[cid].status() if is_catalog(self.snapshots[cid]) else
                        self.snapshots[cid].call(tool, {})["result"] for cid in selected]
            papers = {self._aliases[cid, key] for cid in selected
                      for key in publication_keys(self.snapshots[cid])}
            result = {
                "paper_count": len(papers), "claim_count": sum(s["claim_count"] for s in statuses),
                "content_unit_count": sum(s["content_unit_count"] for s in statuses),
                "coverage": "Selected evidence and metadata catalogs only; paper_count includes metadata-only "
                            "publications, not just acquired sources. Publication counts deduplicate identities, "
                            "claim/unit counts retain independent collection records. Not exhaustive.",
                "retrieval": "lexical_fts5", "rights_profile": ",".join(sorted({
                    self.snapshots[cid].profile for cid in selected})),
                "source_assets_available": False,
            }
        elif tool == "search_evidence":
            result, refs = self._search(selected, plain)
        elif tool == "catalog_status":
            result = {"items": [self.snapshots[cid].coverage() for cid in selected
                                if is_catalog(self.snapshots[cid])],
                      "scope": "Selected metadata catalogs only; not full-text evidence or authoritative coverage."}
        elif tool in ("list_publications", "get_publication"):
            documents = {}
            catalog_ids = {self.snapshots[cid].release_id for cid in selected if is_catalog(self.snapshots[cid])}
            paged = tool == "list_publications" and len(catalog_ids) == 1
            paging = {"limit": plain.get("limit", 10) + 1, "offset": plain.get("offset", 0)} if paged else {}
            if tool == "get_publication" and len(selected) != 1:
                raise retrieval.RetrievalError("Publication retrieval requires exactly one collection")
            for cid in selected:
                snapshot = self.snapshots[cid]
                if not is_catalog(snapshot):
                    continue
                ids = ([snapshot.get(plain["publication_id"])["id"]] if tool == "get_publication" else
                       snapshot.matching_ids(plain, **paging))
                for identifier in ids:
                    key = (identifier, snapshot.release_id)
                    documents.setdefault(key, {"id": identifier, "collections": []})["collections"].append(cid)
            values = [documents[key] for key in sorted(documents)]
            if tool == "get_publication":
                if not values:
                    raise retrieval.NotFoundError("Unknown catalog publication")
                values, continuation = values[:1], None
                value = values[0]
                value["record"] = self.snapshots[value["collections"][0]].get(value["id"])
                result = value["record"]
            else:
                values, continuation = releases.Snapshot._page(values, {**plain, "offset": 0} if paged else plain)
                if paged and continuation is not None:
                    continuation += plain.get("offset", 0)
                    if continuation > retrieval.MAX_OFFSET:
                        raise releases.ReleaseError("Pagination offset ceiling exceeded")
                for value in values:
                    value["record"] = self.snapshots[value["collections"][0]].get(value["id"])
                result = {"items": [value["record"] for value in values], "next_offset": continuation,
                          "scope": contracts.CORPUS_SCOPE}
            refs = [{"record_id": value["record"]["id"], "collection_id": cid,
                     "release_id": self.snapshots[cid].release_id, "result_index": index,
                     "publication_id": self._aliases[cid, value["record"]["id"]]}
                    for index, value in enumerate(values) for cid in value["collections"]]
        elif tool == "list_sources":
            sources = []
            for cid in selected:
                if is_catalog(self.snapshots[cid]):
                    continue
                for source in self.snapshots[cid].source_inventory(plain.get("paper")):
                    source["publication_id"] = self._aliases[cid, source["doi"]]
                    sources.append((cid, source))
            sources.sort(key=lambda pair: (pair[1]["publication_id"], pair[0], pair[1]["source_id"]))
            page, continuation = releases.Snapshot._page(sources, plain)
            result = {"items": [source for cid, source in page], "next_offset": continuation,
                      "scope": contracts.CORPUS_SCOPE}
            refs = [{"record_id": source["source_id"], "collection_id": cid,
                     "release_id": self.snapshots[cid].release_id, "result_index": index,
                     "publication_id": source["publication_id"]}
                    for index, (cid, source) in enumerate(page)]
        else:
            if len(selected) != 1:
                raise retrieval.RetrievalError("Record retrieval requires exactly one collection")
            cid, = selected
            if is_catalog(self.snapshots[cid]):
                raise retrieval.NotFoundError("Metadata catalog has no extracted evidence records")
            response = self.snapshots[cid].call(tool, plain)
            result = response["result"]
            records = result["items"] if tool == "get_related_evidence" else [result]
            refs = [self.reference(cid, record, index) for index, record in enumerate(records)]
        caveats = list(dict.fromkeys(caveat for cid in selected
                                    for caveat in snapshot_caveats(self.snapshots[cid])))
        response = dict(contracts.success(tool, self.release_id, result, caveats))
        response["collection_context"] = {
            "snapshot_id": self.release_id, "selected": [self.info(cid) for cid in selected],
            "evidence_refs": refs,
        }
        contracts.validate_response(tool, arguments, response)
        return response


class CollectionStore:
    """One operator writer; immutable registries plus an atomic active pointer."""

    def __init__(self, root, *, allowed_profiles, allowed_collections, defaults=None, public_quality_root=None):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise releases.ReleaseError("Collection store cannot be a symlink")
        self.profiles = frozenset(allowed_profiles)
        self.allowed = frozenset(identifier(cid) for cid in allowed_collections)
        self.defaults = tuple(defaults if defaults is not None else sorted(self.allowed))
        if (not self.profiles or not self.profiles <= releases.PROFILES or not self.allowed
                or len(self.allowed) > MAX_COLLECTIONS or not self.defaults
                or len(set(self.defaults)) != len(self.defaults) or not set(self.defaults) <= self.allowed):
            raise releases.AccessDeniedError("Explicit authorized collections and default scope are required")
        from .quality import public_policy
        self.public_quality_root = public_policy(self.root, public_quality_root)
        self.releases = releases.ReleaseStore(self.root, allowed_profiles=self.profiles)
        self._lock = threading.Lock()
        self._cached = None

    @staticmethod
    def validate_document(document):
        if len(releases.encode(document)) > MAX_REGISTRY_BYTES:
            raise releases.ReleaseError("Collection registry exceeds byte limit")
        if (not isinstance(document, dict) or set(document) != {"schema_version", "collections"}
                or type(document["schema_version"]) is not int or document["schema_version"] != 1):
            raise releases.ReleaseError("Invalid collection registry schema")
        entries = document["collections"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_COLLECTIONS:
            raise releases.ReleaseError("Registry requires 1-100 collections")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"kind"} != {
                    "id", "title", "version", "updated_at", "coverage", "release_id", "publications"}:
                raise releases.ReleaseError("Invalid collection metadata")
            if entry.get("kind", "evidence") not in ("evidence", "catalog"):
                raise releases.ReleaseError("Unknown collection kind")
            cid = identifier(entry["id"])
            if cid in seen:
                raise releases.ReleaseError("Duplicate collection ID")
            seen.add(cid)
            digest(entry["release_id"])
            for name in ("title", "version", "updated_at", "coverage"):
                value = entry[name]
                if not isinstance(value, str) or not value.strip() or len(value) > (4096 if name == "coverage" else 256):
                    raise releases.ReleaseError("Invalid collection metadata field")
            date.fromisoformat(entry["updated_at"])
            if not isinstance(entry["publications"], dict):
                raise releases.ReleaseError("Publication identities must be a mapping")
            for key, mapping in entry["publications"].items():
                if not key or not isinstance(mapping, dict) or set(mapping) != {"id", "aliases"}:
                    raise releases.ReleaseError("Invalid publication identity mapping")
                values = [mapping["id"], *mapping["aliases"]] if isinstance(mapping["aliases"], list) else []
                if not values or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
                    raise releases.ReleaseError("Invalid publication aliases")

    def _state(self):
        state = read_document(releases.safe_file(self.root, "active.json"))
        if not isinstance(state, dict) or set(state) != {"active", "previous"}:
            raise releases.ReleaseError("Invalid registry pointer")
        digest(state["active"])
        if state["previous"] is not None:
            digest(state["previous"])
        return state

    def load(self, snapshot_id):
        digest(snapshot_id)
        registries = self.root / "registries"
        if any(path.is_symlink() for path in (self.root, registries, self.root / "releases")):
            raise releases.ReleaseError("Symlinked collection store directory")
        try:
            path = releases.safe_file(registries, snapshot_id + ".json")
        except releases.ReleaseError as error:
            if not registries.is_symlink() and not (registries / (snapshot_id + ".json")).exists():
                raise SnapshotUnavailable("Requested registry snapshot is not retained") from error
            raise
        document = read_document(path)
        if releases.sha256(releases.encode(document)) != snapshot_id:
            raise releases.ReleaseError("Registry identity mismatch")
        self.validate_document(document)
        with self._lock:
            if self._cached is not None and self._cached.release_id == snapshot_id:
                unique = {(is_catalog(snapshot), snapshot.release_id): snapshot
                          for snapshot in self._cached.snapshots.values()}
                for snapshot in unique.values():
                    if is_catalog(snapshot):
                        from .catalog import stored_path
                        path = stored_path(self.root, snapshot.release_id)
                    else:
                        path = self.root / "releases" / snapshot.release_id
                    snapshot.verify_backing(path)
                self._public_check(document)
                return self._cached
            snapshots = self._load_releases([entry for entry in document["collections"] if entry["id"] in self.allowed])
            result = CollectionSnapshot(document, snapshots, self.allowed, self.defaults)
            self._public_check(document)
            self._cached = result
            return result

    def _public_check(self, document):
        if self.public_quality_root is not None:
            from .quality import require_ready
            snapshots = self._load_releases(document["collections"])
            for snapshot in snapshots.values():
                if not is_catalog(snapshot):
                    require_ready(snapshot, self.public_quality_root)

    def _load_releases(self, entries):
        if any((self.root / kind).is_symlink() for kind in ("releases", "catalogs")):
            raise releases.ReleaseError("Symlinked collection release directory")
        previous = ({(is_catalog(snapshot), snapshot.release_id): snapshot for snapshot in self._cached.snapshots.values()}
                    if self._cached else {})
        loaded = {}
        for entry in entries:
            release_id = entry["release_id"]
            catalog = entry.get("kind") == "catalog"
            key = (catalog, release_id)
            if catalog:
                from .catalog import stored_path
                path = stored_path(self.root, release_id)
            else:
                path = self.root / "releases" / release_id
            if key not in loaded:
                if key in previous:
                    previous[key].verify_backing(path)
                    loaded[key] = previous[key]
                elif catalog:
                    from .catalog import open_snapshot
                    snapshot = open_snapshot(path)
                    if snapshot.release_id != release_id:
                        raise releases.ReleaseError("Catalog checksum mismatch")
                    loaded[key] = snapshot
                else:
                    loaded[key] = self.releases._release(release_id)
        return {entry["id"]: loaded[(entry.get("kind") == "catalog", entry["release_id"])] for entry in entries}

    def current(self):
        return self.load(self._state()["active"])

    def clear_cache(self):
        with self._lock:
            self._cached = None

    def call(self, tool, arguments):
        selected = contracts.request(tool, arguments)
        snapshot = self.load(selected["snapshot_id"]) if "snapshot_id" in selected else self.current()
        if (selected.get("offset", 0) > 0 or tool not in
                ("search_evidence", "list_sources", "list_publications", "catalog_status",
                 "corpus_status", "list_collections")) and "snapshot_id" not in selected:
            raise retrieval.RetrievalError("Pin snapshot_id for pagination and record retrieval")
        return snapshot.call(tool, arguments)

    def publish(self, document):
        self.validate_document(document)
        # Operator publication validates every entry, not just this endpoint's view.
        snapshots = self._load_releases(document["collections"])
        CollectionSnapshot(document, snapshots, set(snapshots), sorted(snapshots))
        if not self.allowed <= snapshots.keys():
            raise releases.AccessDeniedError("Registry omits configured authorized collection")
        self._public_check(document)
        registries = self.root / "registries"
        if registries.is_symlink():
            raise releases.ReleaseError("Symlinked registry directory")
        identifier_ = releases.sha256(releases.encode(document))
        if not (registries / (identifier_ + ".json")).exists():
            atomic_json(registries, identifier_ + ".json", document)
        return self.activate(identifier_)

    def activate(self, snapshot_id):
        self.load(snapshot_id)
        previous = None
        if (self.root / "active.json").exists() or (self.root / "active.json").is_symlink():
            previous = self._state()["active"]
        if previous != snapshot_id:
            atomic_json(self.root, "active.json", {"active": snapshot_id, "previous": previous})
        return snapshot_id

    def rollback(self):
        previous = self._state()["previous"]
        if previous is None:
            raise releases.ReleaseError("No previous registry snapshot")
        return self.activate(previous)

    def replace(self, cid, release, *, version, updated_at):
        cid = identifier(cid)
        document = deepcopy(self.current().document)
        entry = next((item for item in document["collections"] if item["id"] == cid), None)
        if entry is None or cid not in self.allowed:
            raise releases.AccessDeniedError("Unknown or unauthorized collection")
        if entry.get("kind") == "catalog":
            from .catalog import install
            release_id = install(self.root, release)
        else:
            release_id = self.releases.install(release)
        entry.update(release_id=release_id, version=version, updated_at=updated_at)
        return self.publish(document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--allow-profile", required=True, action="append", choices=sorted(releases.PROFILES))
    parser.add_argument("--allow-collection", required=True, action="append")
    parser.add_argument("--default-collection", action="append")
    parser.add_argument("--public-quality-root", type=Path,
                        help="Create a separate, persistently quality-gated public store")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish", help="Install releases and activate a complete registry config")
    publish.add_argument("config", type=Path)
    update = commands.add_parser("update", help="Independently replace one collection release")
    update.add_argument("collection")
    update.add_argument("release", type=Path)
    update.add_argument("--version", required=True)
    update.add_argument("--updated-at", required=True, help="ISO date YYYY-MM-DD")
    activate = commands.add_parser("activate")
    activate.add_argument("snapshot_id")
    commands.add_parser("rollback")
    commands.add_parser("status")
    args = parser.parse_args()
    try:
        store = CollectionStore(args.registry, allowed_profiles=args.allow_profile,
                                allowed_collections=args.allow_collection, defaults=args.default_collection,
                                public_quality_root=args.public_quality_root)
        if args.command == "publish":
            config = read_document(args.config)
            # Config uses explicit release paths; stored manifests contain only immutable IDs.
            for entry in config["collections"]:
                path = Path(entry.pop("release"))
                if not path.is_absolute():
                    path = args.config.absolute().parent / path
                if entry.get("kind") == "catalog":
                    from .catalog import install
                    entry["release_id"] = install(store.root, path)
                else:
                    entry["release_id"] = store.releases.install(path)
                entry.setdefault("publications", {})
            snapshot_id = store.publish(config)
        elif args.command == "update":
            snapshot_id = store.replace(args.collection, args.release, version=args.version, updated_at=args.updated_at)
        elif args.command == "activate":
            snapshot_id = store.activate(args.snapshot_id)
        elif args.command == "rollback":
            snapshot_id = store.rollback()
        else:
            snapshot_id = store.current().release_id
        print(json.dumps({"snapshot_id": snapshot_id}))
    except releases.ReleaseError as error:
        print(f"Collection operation failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error, ValidationError, KeyError, TypeError) as error:
        print(f"Collection operation failed ({type(error).__name__}); validate config, permissions and releases.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
