"""Retrieve a cited passage over HTTP using only the Python standard library."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen


def retrieve(base_url, query, collection=None):
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTP(S) service origin without credentials or a path.")
    base_url = base_url.rstrip("/")

    def get(endpoint, **params):
        with urlopen(base_url + "/api/" + endpoint + "?" + urlencode(params), timeout=60) as response:
            data = json.load(response)
        if not data.get("ok"):
            raise ValueError("API rejected the evidence request.")
        return data

    listed = get("collections")
    registry = listed.get("collection_context")
    scope = {}
    if registry:
        evidence = [item["id"] for item in listed["result"]["items"] if item["kind"] == "evidence"]
        selected = collection or next(iter(evidence), None)
        if selected not in evidence:
            raise ValueError("Choose an authorized evidence collection, not a metadata catalog.")
        scope = {"snapshot_id": registry["snapshot_id"], "collection": selected}
    elif collection:
        raise ValueError("Single-release endpoints have no named collections; omit --collection.")

    hits = get("search", **scope, query=query, limit=1)
    if not hits["result"]["items"]:
        raise ValueError("No matching retained passage; try a known word. This is not proof of no evidence.")
    hit = hits["result"]["items"][0]
    release_id = (hits["collection_context"]["evidence_refs"][0]["release_id"]
                  if registry else hits["release_id"])
    slices = []
    offset = 0
    for _ in range(2):
        data = get("text", **scope, unit_id=hit["id"], offset=offset, limit=2048)
        record = data["result"]["record"]
        if (data["release_id"] != release_id or record["citation"] != hit["citation"]
                or record["id"] != hit["id"]
                or registry and data["snapshot_id"] != scope["snapshot_id"]):
            raise ValueError("Evidence identity changed between requests; do not combine versions.")
        slices.append(data["result"])
        offset = data["result"]["next_offset"]
        if offset is None:
            break
    pin = scope if registry else {"release_id": release_id}
    reader_url = base_url + "/read?" + urlencode({
        **pin, "source_id": hit["citation"]["source_id"], "unit_id": hit["id"],
    })
    return {
        "collection": scope.get("collection"), "snapshot_id": scope.get("snapshot_id"),
        "release_id": release_id, "unit_id": hit["id"], "citation": hit["citation"],
        "text_slices": slices, "next_text_offset": offset,
        "next_search_offset": hits["result"]["next_offset"], "reader_url": reader_url,
        "caveats": hits["caveats"], "scientific_approval": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--query", default="myeloma")
    parser.add_argument("--collection")
    args = parser.parse_args(argv)
    try:
        result = retrieve(args.base_url, args.query, args.collection)
    except HTTPError as error:
        message = {
            400: "Invalid request; check parameters and query.",
            403: "Access denied; check collection and text permissions.",
            404: "Source or unit not found; use an ID returned by search.",
            410: "Snapshot unavailable; restore it or explicitly start a new search.",
            503: "Evidence unavailable; ask the operator to check readiness.",
        }.get(error.code, "HTTP request rejected; check the service configuration.")
        print(f"HTTP {error.code}: {message}", file=sys.stderr)
        return 1
    except (OSError, URLError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "Cannot reach the service; check its URL and startup."
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
