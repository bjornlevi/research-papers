"""Small read-only HTTP adapter over validated evidence snapshots."""

from flask import jsonify

from . import collections, contracts, releases, retrieval


def json_response(value):
    response = jsonify(value)
    if len(response.get_data()) > contracts.MAX_RESPONSE_BYTES:
        raise releases.ReleaseError("Library response exceeds byte limit; request a smaller page")
    return response


def respond(current, request):
    endpoint = request.view_args["endpoint"]
    allowed = {
        "collections": set(),
        "publications": {"limit", "offset"},
        "sources": {"paper", "limit", "offset"},
        "units": {"source_id", "limit", "offset"},
        "text": {"unit_id", "offset", "limit"},
        "search": {"query", "paper", "limit", "offset"},
    }
    if endpoint not in allowed:
        raise retrieval.NotFoundError("Unknown library endpoint")
    params = request.args
    if len(list(params.items(multi=True))) != len(params) or set(params) - allowed[endpoint] - {"collection", "snapshot_id"}:
        raise retrieval.RetrievalError("Unknown or repeated library parameter")
    if endpoint == "collections":
        return json_response(current.call("list_collections", {}))
    cid = params.get("collection")
    if isinstance(current, collections.CollectionSnapshot):
        if not cid:
            raise retrieval.RetrievalError("Select one evidence collection")
        if cid not in current.allowed:
            raise releases.AccessDeniedError("Unknown or unauthorized collection")
        evidence = current.snapshots[cid]
        if collections.is_catalog(evidence):
            raise retrieval.RetrievalError("Metadata catalogs do not contain evidence text")
        scope = {"collections": [cid], "snapshot_id": current.release_id}
    else:
        if cid:
            raise retrieval.RetrievalError("Single release has no named collections")
        evidence, scope = current, {}
    offset = int(params.get("offset", "0"))
    limit = int(params.get("limit", "2048" if endpoint == "text" else "20"))
    if offset and isinstance(current, collections.CollectionSnapshot) and not params.get("snapshot_id"):
        raise retrieval.RetrievalError("Pin snapshot_id for pagination")
    if endpoint == "text":
        unit_id = params.get("unit_id", "")
        contracts.request("get_source_context", {"unit_id": unit_id, "max_characters": limit})
        result = evidence.source_context(unit_id, offset=offset, limit=limit)
    else:
        paging = {"offset": offset, "limit": limit}
        contracts.request("list_sources", paging)
        if endpoint == "search":
            return json_response(current.call("search_evidence", {
                **scope, **paging, "query": params.get("query", ""),
                **({"paper": params["paper"]} if "paper" in params else {})}))
        if endpoint == "units":
            source_id = params.get("source_id", "")
            contracts.request("get_source_context", {"unit_id": source_id})
            rows = evidence.source_units(source_id)
        else:
            paper = params.get("paper")
            if paper is not None:
                contracts.request("list_sources", {"paper": paper})
            rows = evidence.source_inventory(paper)
            for row in rows:
                document = row["source_id"].rsplit(":", 1)[-1]
                row["document_role"] = (document if document == "main" or document.startswith("supplement-")
                                        else "source_version")
                if isinstance(current, collections.CollectionSnapshot):
                    row["publication_id"] = current._aliases[cid, row["doi"]]
            if endpoint == "publications":
                papers = {}
                for row in rows:
                    publication = papers.setdefault(row["publication_id"], {
                        "publication_id": row["publication_id"], "title": None, "source_count": 0})
                    publication["source_count"] += 1
                    if row["bibliography"]["title"]:
                        publication["title"] = row["bibliography"]["title"]
                if isinstance(current, collections.CollectionSnapshot):
                    for metadata_cid, snapshot in current.snapshots.items():
                        if not collections.is_catalog(snapshot):
                            continue
                        for key in collections.publication_keys(snapshot):
                            identity = current._aliases[metadata_cid, key]
                            if identity in papers and not papers[identity]["title"]:
                                papers[identity]["title"] = snapshot.get(key)["title"] or None
                rows = [papers[key] for key in sorted(papers)]
        items, continuation = releases.Snapshot._page(rows, paging)
        result = {"items": items, "next_offset": continuation}
    return json_response({
        "ok": True, "api_version": "1", "snapshot_id": current.release_id,
        "collection_id": cid, "release_id": evidence.release_id,
        "result": result, "caveats": evidence._bundle["caveats"],
    })
