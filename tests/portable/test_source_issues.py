from copy import deepcopy
import unittest

from jsonschema import ValidationError

from research_library import contracts
from fixtures import CLAIM


class SourceIssueContractTests(unittest.TestCase):
    def test_package_locators_and_null_context_are_explicit_contract_two_changes(self):
        self.assertEqual(contracts.SCHEMA_VERSION, "2.3.0")
        claim = deepcopy(CLAIM)
        claim["numbers"][0]["context"] = None
        claim["citation"]["locator"] = {"kind": "word_package", "package_member": "word/footnotes.xml"}
        contracts.success("get_claim", "synthetic", claim, [])
        for name in ("../../secrets", "word/../document.xml", "/etc/passwd"):
            claim["citation"]["locator"]["package_member"] = name
            with self.assertRaises(ValidationError):
                contracts.success("get_claim", "synthetic", claim, [])
