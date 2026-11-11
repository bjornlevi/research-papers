"""Version 2 tool contracts; transports reuse these bounded, source-linked responses."""

from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
import json
from typing import Any, Literal, TypedDict

from jsonschema import Draft202012Validator

from .retrieval import MAX_QUERY_LENGTH

SCHEMA_VERSION = "2.3.0"
MAX_RESPONSE_BYTES = 65_536
CORPUS_SCOPE = "Matches are limited to this evidence release; no match is not global scientific absence."
DOCUMENT = json.loads(files("research_library").joinpath("schemas/contract-v2.json").read_text())

ToolName = Literal["corpus_status", "list_collections", "list_sources", "search_evidence", "get_claim", "get_source_context",
                   "get_table", "get_source_issue", "get_related_evidence",
                   "list_publications", "get_publication", "catalog_status"]


class Citation(TypedDict):
    source_id: str
    doi: str
    source_sha256: str
    locator: dict[str, Any]
    source_url: str | None
    provenance: str
    availability: Literal["authorized", "metadata_only", "unavailable"]


class ToolError(TypedDict):
    code: Literal["invalid_input", "not_found", "unavailable", "permission_denied",
                  "invalid_release", "response_too_large", "internal_error"]
    message: str


class Success(TypedDict):
    ok: Literal[True]
    schema_version: str
    release_id: str
    tool: ToolName
    result: dict[str, Any]
    caveats: list[str]


class Failure(TypedDict):
    ok: Literal[False]
    schema_version: str
    release_id: str | None
    tool: ToolName
    error: ToolError


@dataclass(frozen=True)
class ToolSpec:
    properties: dict[str, Any]
    required: tuple[str, ...]
    result: str
    paginated: bool = False


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


PAGING = {"limit": ref("limit"), "offset": ref("offset")}
COLLECTION_INPUT = {
    "collections": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"},
                    "description": "Explicit authorized collection IDs. Omit to use the endpoint default."},
    "snapshot_id": {**ref("hash"),
                    "description": "Pin the registry snapshot from collection_context for pagination "
                                   "and follow-up retrieval. Never reuse offsets across snapshots."},
}
TOOLS: dict[ToolName, ToolSpec] = {
    "list_publications": ToolSpec({
        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_LENGTH, "pattern": "\\S"},
        "disease": {"enum": ["mgus", "smm", "mm"]}, "publication_type": ref("id"),
        "eligibility": {"enum": ["included", "excluded", "needs_review"]}, **PAGING,
    }, (), "catalog_publication", True),
    "get_publication": ToolSpec({"publication_id": ref("id")}, ("publication_id",), "catalog_publication"),
    "catalog_status": ToolSpec({}, (), "catalog_status"),
    "corpus_status": ToolSpec({}, (), "status"),
    "list_collections": ToolSpec({}, (), "collection_list"),
    "list_sources": ToolSpec({"paper": ref("id"), **PAGING}, (), "source_inventory", True),
    "search_evidence": ToolSpec({
        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_LENGTH, "pattern": "\\S",
                  "description": "Plain text by default: punctuation separates words and all words must "
                                 "match within a source unit. Use short queries; no semantic expansion. "
                                 "FTS5 expressions require mode='fts5'."},
        "mode": {"type": "string", "enum": ["plain", "fts5"], "default": "plain",
                 "description": "plain treats operators, quotes and hyphens as ordinary text. "
                                "fts5 enables phrases, OR, NOT and prefixes; quote hyphenated terms "
                                '(e.g. "LC-MGUS").'},
        "paper": ref("id"), **PAGING,
    }, ("query",), "hit", True),
    "get_claim": ToolSpec({"claim_id": ref("id")}, ("claim_id",), "claim"),
    "get_source_context": ToolSpec({
        "unit_id": ref("id"),
        "max_characters": {"type": "integer", "minimum": 1, "maximum": 2048, "default": 1024},
    }, ("unit_id",), "context"),
    "get_table": ToolSpec({"table_id": ref("id"), **PAGING}, ("table_id",), "table"),
    "get_source_issue": ToolSpec({"issue_id": ref("id")}, ("issue_id",), "issue"),
    "get_related_evidence": ToolSpec({"evidence_id": ref("id"), **PAGING},
                                    ("evidence_id",), "relationship", True),
}
for _spec in TOOLS.values():
    _spec.properties.update(deepcopy(COLLECTION_INPUT))

DOCUMENT["$defs"].update({
    "source_bibliography": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "year", "acquired_from", "acquisition_method", "acquired_at"],
        "properties": {
            "title": {"type": ["string", "null"], "maxLength": 4096},
            "year": {"type": ["string", "null"], "maxLength": 256},
            "acquired_from": {"type": ["string", "null"], "maxLength": 2048,
                              "pattern": "^https?://\\S+$"},
            "acquisition_method": {"type": ["string", "null"], "maxLength": 4096},
            "acquired_at": {"type": ["string", "null"], "maxLength": 256},
        },
    },
    "source_inventory": {
        "type": "object", "additionalProperties": False,
        "required": ["source_id", "publication_id", "doi", "source_sha256", "format",
                     "source_url", "provenance", "bibliography", "used_in",
                     "original_served", "acquisition_details_recorded"],
        "properties": {
            **{name: ref("id") for name in ("source_id", "publication_id", "doi", "format")},
            "source_sha256": ref("hash"),
            "source_url": {"type": ["string", "null"], "maxLength": 2048, "pattern": "^https?://\\S+$"},
            "provenance": ref("text"), "bibliography": ref("source_bibliography"),
            "used_in": {"type": "array", "maxItems": 5,
                        "items": {"enum": ["claims", "units", "tables", "issues", "relationships"]}},
            "original_served": {"const": False},
            "acquisition_details_recorded": {"type": "boolean"},
        },
    },
    "collection_info": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "title", "version", "updated_at", "coverage", "release_id",
                     "rights_profile", "paper_count", "claim_count", "content_unit_count", "default"],
        "properties": {
            **{name: ref("id") for name in ("id", "title", "version", "updated_at", "rights_profile")},
            "coverage": ref("text"), "release_id": ref("hash"), "default": {"type": "boolean"},
            **{name: {"type": "integer", "minimum": 0}
               for name in ("paper_count", "claim_count", "content_unit_count")},
        },
    },
    "collection_list": {
        "type": "object", "additionalProperties": False, "required": ["items"],
        "properties": {"items": {"type": "array", "maxItems": 100, "items": ref("collection_info")}},
    },
    "collection_context": {
        "type": "object", "additionalProperties": False,
        "required": ["snapshot_id", "selected", "evidence_refs"],
        "properties": {
            "snapshot_id": ref("hash"),
            "selected": {"type": "array", "maxItems": 100, "items": ref("collection_info")},
            "evidence_refs": {
                "type": "array", "maxItems": 10000,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["record_id", "collection_id", "release_id", "result_index", "publication_id"],
                    "properties": {
                        **{name: ref("id") for name in ("record_id", "collection_id", "publication_id")},
                        "release_id": ref("hash"),
                        "result_index": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
    },
})


def object_schema(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


def array_schema(items, maximum=10000):
    return {"type": "array", "maxItems": maximum, "items": items}


_string = {"type": "string", "maxLength": 4096}
_strings = array_schema(_string, 1000)
_nullable = {"type": ["string", "null"], "maxLength": 4096}
_counts = {"type": "object", "maxProperties": 1000,
           "additionalProperties": {"type": "integer", "minimum": 0}}
_decision = {"enum": ["included", "excluded", "needs_review"]}
DOCUMENT["$defs"].update({
    "catalog_request": object_schema({
        "url": _string, "retrieved_at": ref("id"), "response_sha256": ref("hash"),
    }),
    "catalog_run": object_schema({
        **{key: _string for key in ("provider", "query", "policy_version", "from_date", "through_date",
                                    "started_at", "updated_at", "disease")},
        "id": ref("hash"), "policy_sha256": ref("hash"),
        "status": {"enum": ["complete", "incomplete"]},
    }),
    "catalog_relation": object_schema({
        "kind": {"enum": ["corrects", "retracts", "concerns", "published_version_of", "supplement_of"]},
        "target": ref("id"), "provenance": _string,
    }),
    "catalog_variant": object_schema({
        "identifiers": {"type": "object", "additionalProperties": False,
                        "properties": {key: ref("id") for key in ("doi", "pmid", "pmcid")}},
        "title": _string, "authors": _strings, "journal": _nullable,
        "publication_date": _string, "publication_types": _strings, "source_url": _string,
        "eligibility": object_schema({"decision": _decision, "reason": _string}),
        "publication_state": {"enum": ["unknown", "retracted", "notice", "preprint"]},
    }),
    "catalog_publication": object_schema({
        "id": ref("id"), "aliases": array_schema(ref("id"), 1000),
        "title": _string, "authors": _strings, "journal": _nullable,
        "publication_date": _string, "citation_url": _string,
        "diseases": array_schema({"enum": ["mgus", "smm", "mm"]}, 3),
        "publication_types": _strings, "languages": _strings, "eligibility": _decision,
        "publication_state": {"enum": ["unknown", "retracted", "notice", "preprint"]},
        "study_design": {"const": ["unknown"]}, "peer_review_status": {"const": "not_verified"},
        "researcher_review": {"const": "not_reviewed"},
        "identity_status": {"enum": ["resolved", "needs_review"]},
        "istopmm_overlap": {"enum": ["matched_doi", "not_matched", "needs_review"]},
        "availability": object_schema({
            "metadata": {"const": True}, "abstract_reported": {"type": "boolean"},
            "full_text_reported": {"type": "boolean"}, "full_text_acquired": {"const": False},
            "redistribution_authorized": {"const": False},
        }),
        "extraction_status": {"const": "not_extracted"}, "claim_review_status": {"const": "not_reviewed"},
        "selection_rationale": _string,
        "metadata_conflicts": {"type": "object", "maxProperties": 6,
                               "additionalProperties": {"type": "array", "maxItems": 100}},
        "relations": array_schema(ref("catalog_relation"), 100),
        "provenance": array_schema(object_schema({
            "provider_record": ref("id"), "run_id": ref("hash"), "page_sha256": ref("hash"),
            "requests": array_schema(ref("catalog_request"), 20), "metadata": ref("catalog_variant"),
        }), 1000),
    }),
    "catalog_document": object_schema({
        "schema_version": {"const": 1}, "kind": {"const": "literature_catalog"},
        **{key: _string for key in ("as_of", "selection_rationale", "metadata_authorization_basis", "istopmm_matching")},
        "runs": array_schema(ref("catalog_run"), 1000),
        "issues": array_schema(object_schema({
            "kind": {"enum": ["identifier_collision", "invalid_identifier", "metadata_conflict", "unresolved_notice",
                              "unresolved_relationship"]},
            "records": array_schema(ref("id"), 10000), "detail": _string,
        })),
        "publications": array_schema(ref("catalog_publication"), 100000), "caveats": _strings,
    }),
    "catalog_coverage": object_schema({
        "catalog_id": ref("hash"), "as_of": _string,
        **{key: {"type": "integer", "minimum": 0} for key in (
            "publication_count", "observation_count", "provider_record_count", "duplicates_collapsed", "identity_issues",
            "metadata_conflicts", "unresolved_links", "metadata_only_count", "full_text_acquired_count",
            "full_text_not_reported_count", "unreviewed_count")},
        **{key: _counts for key in ("diseases", "publication_types", "eligibility", "istopmm_overlap")},
        "complete_discovery": {"type": "boolean"}, "runs": array_schema(ref("catalog_run"), 1000),
        "scope": _string,
    }),
    "catalog_status": object_schema({
        "items": array_schema(ref("catalog_coverage"), 100), "scope": _string,
    }),
})
DOCUMENT["$defs"]["collection_info"]["properties"]["kind"] = {"enum": ["evidence", "catalog"]}


def schema(tool: ToolName, direction: Literal["input", "output"]) -> dict[str, Any]:
    if tool not in TOOLS:
        raise ValueError(f"Unknown tool: {tool}")
    spec = TOOLS[tool]
    document = {"$schema": DOCUMENT["$schema"], "$defs": deepcopy(DOCUMENT["$defs"])}
    if direction == "input":
        document.update(type="object", additionalProperties=False,
                        properties=deepcopy(spec.properties), required=list(spec.required))
    elif direction == "output":
        document["type"] = "object"
        result: dict[str, Any] = ref(spec.result)
        if spec.paginated:
            result = {
                "type": "object", "additionalProperties": False,
                "required": ["items", "next_offset", "scope"],
                "properties": {
                    "items": {"type": "array", "maxItems": 100, "items": result},
                    "next_offset": {"anyOf": [ref("offset"), {"type": "null"}]},
                    "scope": {"const": CORPUS_SCOPE},
                },
            }
        common = {"schema_version": {"const": SCHEMA_VERSION}, "tool": {"const": tool}}
        document["oneOf"] = [
            {
                "type": "object", "additionalProperties": False,
                "required": ["ok", "schema_version", "release_id", "tool", "result", "caveats"],
                "properties": {**common, "ok": {"const": True}, "release_id": ref("id"),
                               "result": result, "caveats": ref("caveats"),
                               "collection_context": ref("collection_context")},
            },
            {
                "type": "object", "additionalProperties": False,
                "required": ["ok", "schema_version", "release_id", "tool", "error"],
                "properties": {**common, "ok": {"const": False},
                               "release_id": {"anyOf": [ref("id"), {"type": "null"}]},
                               "error": ref("error")},
            },
        ]
    else:
        raise ValueError(f"Unknown schema direction: {direction}")
    # MCP repeats schemas per tool; include only reachable definitions so adding
    # a catalog does not inflate tool discovery beyond the transport byte cap.
    needed = set()
    def collect(value):
        if isinstance(value, dict):
            target = value.get("$ref", "")
            if target.startswith("#/$defs/"):
                name = target.rsplit("/", 1)[1]
                if name not in needed:
                    needed.add(name)
                    collect(DOCUMENT["$defs"][name])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect({key: value for key, value in document.items() if key != "$defs"})
    document["$defs"] = {key: value for key, value in document["$defs"].items() if key in needed}
    return document


def validate(tool: ToolName, direction: Literal["input", "output"], value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError(f"{direction} exceeds {MAX_RESPONSE_BYTES} serialized UTF-8 bytes")
    Draft202012Validator(schema(tool, direction)).validate(value)
    if direction == "output" and value["ok"]:
        result = value["result"]
        if tool == "get_table":
            width = len(result["headers"])
            if len(result["units"]) != width or any(len(row) != width for row in result["rows"]):
                raise ValueError("Table rows and units must match header width")
            if result["truncated"] != (result["next_offset"] is not None):
                raise ValueError("Truncated table must supply next_offset")
        if tool == "get_claim":
            for number in result["numbers"]:
                for name in ("ci_95", "reference_interval"):
                    bounds = number[name]
                    if bounds is not None and bounds[0] > bounds[1]:
                        raise ValueError(f"Reversed {name}")
                bounds = number["ci_95"]
                if bounds is not None and number["value"] is not None and not bounds[0] <= number["value"] <= bounds[1]:
                    raise ValueError("Estimate outside confidence interval")


def request(tool: ToolName, value: dict[str, Any]) -> dict[str, Any]:
    """Validate and apply documented defaults without mutating caller input."""
    validate(tool, "input", value)
    result = deepcopy(value)
    for key, prop in TOOLS[tool].properties.items():
        resolved = DOCUMENT["$defs"][prop["$ref"].split("/")[-1]] if "$ref" in prop else prop
        if key not in result and "default" in resolved:
            result[key] = resolved["default"]
    return result


def validate_response(tool: ToolName, arguments: dict[str, Any], response: Success | Failure) -> None:
    """Check the output schema and bounds that depend on the original request."""
    selected = request(tool, arguments)
    validate(tool, "output", response)
    if not response["ok"]:
        return
    result = response["result"]
    spec = TOOLS[tool]
    if spec.paginated or tool == "get_table":
        items = result["items"] if spec.paginated else result["rows"]
        if len(items) > selected["limit"]:
            raise ValueError("Response exceeds requested limit")
        continuation = result["next_offset"]
        if continuation is not None and (not items or continuation != selected["offset"] + len(items)):
            raise ValueError("next_offset must advance by the number of returned records")
    if tool == "get_source_context" and len(result["text"]) > selected["max_characters"]:
        raise ValueError("Context exceeds requested character limit")


def success(tool: ToolName, release_id: str, result: dict[str, Any],
            caveats: list[str]) -> Success:
    response: Success = {"ok": True, "schema_version": SCHEMA_VERSION, "release_id": release_id,
                         "tool": tool, "result": result, "caveats": caveats}
    validate(tool, "output", response)
    return response


def failure(tool: ToolName, code: ToolError, release_id: str | None = None) -> Failure:
    response: Failure = {"ok": False, "schema_version": SCHEMA_VERSION, "release_id": release_id,
                         "tool": tool, "error": code}
    validate(tool, "output", response)
    return response
