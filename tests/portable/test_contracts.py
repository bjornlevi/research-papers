from copy import deepcopy
import json
import unittest

from jsonschema import Draft202012Validator, ValidationError

from research_library import contracts
from fixtures import CLAIM, CONTEXT, HIT, ISSUE, RELATIONSHIP, STATUS, TABLE


class ContractTests(unittest.TestCase):
    def test_all_advertised_schemas_fit_http_tool_discovery_limit(self):
        from research_library.server import DESCRIPTIONS
        from mcp import types
        tools = [types.Tool(name=name, description=DESCRIPTIONS[name],
                            inputSchema=contracts.schema(name, "input"),
                            outputSchema=contracts.schema(name, "output")).model_dump(
                                mode="json", exclude_none=True) for name in contracts.TOOLS]
        self.assertLess(len(json.dumps({"tools": tools}).encode()), 256 * 1024)
        for name in ("list_publications", "get_publication", "catalog_status"):
            Draft202012Validator.check_schema(contracts.schema(name, "input"))
            Draft202012Validator.check_schema(contracts.schema(name, "output"))

    def test_all_schema_definitions_are_valid_and_successes_roundtrip(self):
        results = {
            "list_collections": {"items": []},
            "list_sources": {"items": [], "next_offset": None, "scope": contracts.CORPUS_SCOPE},
            "corpus_status": STATUS,
            "search_evidence": {"items": [HIT], "next_offset": None, "scope": contracts.CORPUS_SCOPE},
            "get_claim": CLAIM, "get_source_context": CONTEXT, "get_table": TABLE,
            "get_source_issue": ISSUE,
            "get_related_evidence": {"items": [RELATIONSHIP], "next_offset": None,
                                     "scope": contracts.CORPUS_SCOPE},
        }
        for tool, result in results.items():
            with self.subTest(tool=tool):
                for direction in ("input", "output"):
                    schema = contracts.schema(tool, direction)
                    self.assertEqual(schema["type"], "object")
                    Draft202012Validator.check_schema(schema)
                response = contracts.success(tool, "synthetic-release-1", deepcopy(result),
                                             ["Publication fixtures only; not scientific evidence."])
                self.assertEqual(json.loads(json.dumps(response, allow_nan=False)), response)

    def test_requests_and_defaults_for_all_tools(self):
        inputs = {
            "list_collections": {},
            "list_sources": {},
            "corpus_status": {}, "search_evidence": {"query": "screening"},
            "get_claim": {"claim_id": "fictional-claim"},
            "get_source_context": {"unit_id": "fictional-unit"},
            "get_table": {"table_id": "fictional-table"},
            "get_source_issue": {"issue_id": "fictional-issue"},
            "get_related_evidence": {"evidence_id": "fictional-claim"},
        }
        for tool, value in inputs.items():
            before = deepcopy(value)
            output = contracts.request(tool, value)
            self.assertEqual(value, before)
            if tool in ("search_evidence", "get_table", "get_related_evidence"):
                self.assertEqual((output["limit"], output["offset"]), (10, 0))
            if tool == "get_source_context":
                self.assertEqual(output["max_characters"], 1024)
            if tool == "search_evidence":
                self.assertEqual(output["mode"], "plain")
            with self.assertRaises(ValidationError):
                contracts.request(tool, {**value, "sql": "SELECT * FROM claims"})

    def test_input_boundaries_and_whitespace_are_rejected(self):
        for limit in (1, 100):
            contracts.request("search_evidence", {"query": "x", "limit": limit})
        contracts.request("search_evidence", {"query": "x" * 2048, "offset": 1_000_000})
        for args in ({"query": ""}, {"query": " "}, {"query": "x" * 2049},
                     {"query": "x", "limit": 0}, {"query": "x", "limit": 101},
                     {"query": "x", "limit": True}, {"query": "x", "offset": -1},
                     {"query": "x", "offset": 1_000_001}, {"query": "x", "mode": "auto"}):
            with self.subTest(args=args):
                with self.assertRaises(ValidationError):
                    contracts.request("search_evidence", args)
        for count in (1, 2048):
            contracts.request("get_source_context", {"unit_id": "unit", "max_characters": count})
        for count in (0, 2049):
            with self.assertRaises(ValidationError):
                contracts.request("get_source_context", {"unit_id": "unit", "max_characters": count})
        for identifier in ("", " ", "x" * 257):
            with self.assertRaises(ValidationError):
                contracts.request("get_claim", {"claim_id": identifier})

    def test_empty_search_and_documented_error_envelopes(self):
        contracts.success("search_evidence", "release",
                          {"items": [], "next_offset": None, "scope": contracts.CORPUS_SCOPE}, [])
        for code in ("not_found", "invalid_input", "permission_denied", "invalid_release",
                     "unavailable", "response_too_large", "internal_error"):
            response = contracts.failure("get_claim", {"code": code, "message": "Explicit failure."})
            self.assertFalse(response["ok"])
            self.assertNotIn("result", response)
            self.assertIsNone(response["release_id"])
        with self.assertRaises(ValidationError):
            contracts.success("get_claim", "", CLAIM, [])
        with self.assertRaises(ValueError):
            contracts.schema("invented_tool", "input")

    def test_numeric_null_zero_and_required_context(self):
        response = contracts.success("get_claim", "release", deepcopy(CLAIM), [])
        number = response["result"]["numbers"][0]
        self.assertEqual(number["value"], 0)
        self.assertIsNone(number["reference_interval"])
        for field in ("population", "adjustment", "unit", "measure", "denominator", "ci_95"):
            broken = deepcopy(CLAIM)
            del broken["numbers"][0][field]
            with self.assertRaises(ValidationError):
                contracts.success("get_claim", "release", broken, [])
        for value in (float("nan"), float("inf"), float("-inf")):
            broken = deepcopy(CLAIM)
            broken["numbers"][0]["value"] = value
            with self.assertRaises(ValueError):
                contracts.success("get_claim", "release", broken, [])
        for interval in ([1, 0], [2, 3]):
            broken = deepcopy(CLAIM)
            broken["numbers"][0]["ci_95"] = interval
            with self.assertRaises(ValueError):
                contracts.success("get_claim", "release", broken, [])

    def test_provenance_and_office_locators_cannot_invent_pdf_pages(self):
        for field in ("source_id", "doi", "source_sha256", "locator", "provenance", "availability"):
            broken = deepcopy(CLAIM)
            del broken["citation"][field]
            with self.assertRaises(ValidationError):
                contracts.success("get_claim", "release", broken, [])
        for locator in ({"kind": "word_table", "body_element": 2},
                        {"kind": "worksheet", "worksheet": "Data", "row": 4}):
            result = deepcopy(CONTEXT)
            result["citation"]["locator"] = locator
            contracts.success("get_source_context", "release", result, [])
            result["citation"]["locator"]["pdf_page"] = 2
            with self.assertRaises(ValidationError):
                contracts.success("get_source_context", "release", result, [])

    def test_ai_checks_are_not_presented_as_human_review(self):
        result = deepcopy(CLAIM)
        result["verification_status"] = "source_checked_by_ai"
        response = contracts.success("get_claim", "release", result, [])
        self.assertEqual(response["result"]["human_verification"], "not_reviewed")
        result["verification_status"] = "source_checked_by_human"
        with self.assertRaises(ValidationError):
            contracts.success("get_claim", "release", result, [])
        result["human_verification"] = "reviewed"
        contracts.success("get_claim", "release", result, [])
        for field in ("citation", "supporting_quote"):
            result = deepcopy(CLAIM)
            del result["numbers"][0][field]
            with self.assertRaises(ValidationError):
                contracts.success("get_claim", "release", result, [])

    def test_limits_reject_instead_of_silently_losing_qualifications(self):
        page = {"items": [HIT] * 101, "next_offset": None, "scope": contracts.CORPUS_SCOPE}
        with self.assertRaises((ValueError, ValidationError)):
            contracts.success("search_evidence", "release", page, [])
        result = deepcopy(CONTEXT)
        result["text"] = "x" * 2048
        contracts.success("get_source_context", "release", result, [])
        result["text"] += "x"
        with self.assertRaises(ValidationError):
            contracts.success("get_source_context", "release", result, [])
        for change in ({"rows": [["A"]]}, {"units": [None]}, {"truncated": True}):
            with self.assertRaises(ValueError):
                contracts.success("get_table", "release", {**deepcopy(TABLE), **change}, [])
        with self.assertRaisesRegex(ValueError, "serialized UTF-8 bytes"):
            contracts.success("corpus_status", "release", STATUS, ["x" * 4096] * 20)
        # Exercise the exact serialized-byte limit, not just the item/text limits.
        response = contracts.success("corpus_status", "release", STATUS, ["x" * 4096] * 15 + [""])
        current = len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode())
        response["caveats"][-1] = "x" * (contracts.MAX_RESPONSE_BYTES - current)
        self.assertLessEqual(len(response["caveats"][-1]), 4096)
        contracts.validate("corpus_status", "output", response)
        response["caveats"][-1] += "x"
        with self.assertRaisesRegex(ValueError, "serialized UTF-8 bytes"):
            contracts.validate("corpus_status", "output", response)

    def test_request_dependent_pagination_and_context_bounds(self):
        page = {"items": [HIT], "next_offset": 1, "scope": contracts.CORPUS_SCOPE}
        response = contracts.success("search_evidence", "release", page, [])
        contracts.validate_response("search_evidence", {"query": "x", "limit": 1}, response)
        for items, continuation in (([HIT, HIT], None), ([HIT], 2), ([], 0)):
            response = contracts.success("search_evidence", "release",
                                         {**page, "items": items, "next_offset": continuation}, [])
            with self.assertRaises(ValueError):
                contracts.validate_response("search_evidence", {"query": "x", "limit": 1}, response)
        response = contracts.success("get_source_context", "release", CONTEXT, [])
        with self.assertRaises(ValueError):
            contracts.validate_response("get_source_context", {"unit_id": "x", "max_characters": 1}, response)
        response = contracts.success("get_table", "release", TABLE, [])
        with self.assertRaises(ValueError):
            contracts.validate_response("get_table", {"table_id": "x", "limit": 1}, response)
