"""Fictional evidence only; no source publication text or patient data."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from research_library.build import evidence, knowledge

DOI = "10.0000/fictional"
PID = "10-0000-fictional"
QUOTE = "Fictional screening follow-up, not clinical evidence."
HASH = hashlib.sha256(QUOTE.encode()).hexdigest()
CITATION = {
    "source_id": PID + ":main", "doi": DOI, "source_sha256": HASH,
    "locator": {"kind": "pdf", "pdf_page": 1},
    "source_url": None, "provenance": "Synthetic test document, authored for these tests.",
    "availability": "authorized",
}
NUMBER = {
    "id": "fictional-n1", "label": "Fictional estimate", "value": 0,
    "reported_value": "0 (0-1)", "measure": "subdistribution_hazard_ratio",
    "unit": "dimensionless", "numerator": 0, "denominator": 20, "ci_95": [0, 1],
    "reference_interval": None, "population": "Fictional participants", "adjustment": None,
    "context": "A software fixture, not a valid clinical estimate.",
    "citation": CITATION, "supporting_quote": QUOTE,
}
CLAIM = {
    "id": "fictional-claim", "citation": CITATION, "verification_status": "not_reviewed",
    "human_verification": "not_reviewed",
    "caveats": ["Not clinical evidence."], "statement": QUOTE, "claim_kind": "observed_result",
    "population": "Fictional participants", "study_design": "Synthetic fixture",
    "cohort_family": "Fictional shared cohort", "comparator": None, "follow_up": None,
    "adjustment": None, "numbers": [NUMBER], "supporting_quote": QUOTE,
    "supporting_citations": [], "source_issue_ids": ["fictional-issue"],
}
HIT = {key: deepcopy(CLAIM[key]) for key in ("id", "citation", "verification_status", "human_verification", "caveats")}
HIT.update(snippet=QUOTE, source_issue_ids=["fictional-issue"])
CONTEXT = {key: deepcopy(HIT[key]) for key in ("id", "citation", "verification_status", "human_verification", "caveats")}
CONTEXT.update(text=QUOTE, truncated=False, source_issue_ids=["fictional-issue"])
TABLE = {key: deepcopy(HIT[key]) for key in ("id", "citation", "verification_status", "human_verification", "caveats")}
TABLE.update(title="Fictional table", headers=["Group", "Count"], units=[None, "participants"],
             rows=[["A", 0], ["B", None]], footnotes=["Null means not reported."],
             truncated=False, next_offset=None)
ISSUE = {key: deepcopy(HIT[key]) for key in ("id", "citation", "verification_status", "human_verification", "caveats")}
ISSUE.update(status="clarification", category="reuse_clarification",
             context="Fictional reused cohort.", explanation="Repeated evidence is not replication.",
             evidence=[{"quote": QUOTE, "citation": deepcopy(CITATION)}])
RELATIONSHIP = {key: deepcopy(HIT[key]) for key in ("id", "citation", "verification_status", "human_verification", "caveats")}
RELATIONSHIP.update(target_id="fictional-review", relationship="review_reuse",
                    explanation="Same fictional finding, not an independent cohort.")
STATUS = {"paper_count": 1, "claim_count": 2, "content_unit_count": 3,
          "coverage": "Synthetic publications only, no participant-level data.",
          "retrieval": "lexical_fts5", "rights_profile": "synthetic", "source_assets_available": True}


def snapshots(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@contextmanager
def corpus():
    with tempfile.TemporaryDirectory(prefix="istopmm-fixture #? ") as temporary:
        root = Path(temporary)
        (root / "papers").mkdir()
        (root / "papers/manifest.csv").write_text("number,doi\n1,10.0000/fictional\n")
        folder = root / "evidence"
        folder.mkdir()
        catalog = [{"paper_id": PID, "doi": DOI, "title": "Fictional screening",
                    "year": 2026, "extraction_status": "machine_extracted"}]
        common = {"paper_id": PID, "document_id": "main", "source_path": "papers/fictional.pdf",
                  "source_sha256": HASH, "source_metadata": "evidence/fictional.json",
                  "verification_status": "machine_extracted"}
        units = [
            {**common, "unit_id": f"{PID}:main:p1", "text": QUOTE, "location": {"pdf_page": 1}},
            {**common, "unit_id": f"{PID}:word:t1", "document_id": "word",
             "text": "Fictional screening Word table and footnote.",
             "location": {"body_element": 2, "kind": "table"}, "table_id": "table-0002"},
            {**common, "unit_id": f"{PID}:sheet:r4", "document_id": "sheet",
             "text": "Fictional screening worksheet row.", "location": {"worksheet": "Data", "row": 4}},
        ]
        claims = []
        for index, kind in enumerate(("observed_result", "secondary_evidence"), 1):
            claim = {
                "claim_id": f"fictional-claim-{index}", "paper_id": PID, "doi": DOI,
                "inventory_number": 1, "title": "Fictional screening",
                "topic": "Fictional", "claim_kind": kind, "statement": QUOTE,
                "supporting_quote": QUOTE, "study_design": "Synthetic fixture",
                "population": "Fictional participants", "cohort_family": "shared fixture",
                "caveats": ["Not clinical evidence; review reuse is not replication."],
                "source_issues": [{"id": "fictional-issue", "status": "clarification"}],
                "source_sha256": HASH, "pdf_page": 1, "numbers": [deepcopy(NUMBER)],
                "adjustment": "Fictional age adjustment",
            }
            claims.append(claim)
        finding = {"result_id": "fictional-result", "paper_id": PID, "measure": "prevalence_ratio",
                   "value": 1.05, "verification_status": "not_reviewed", "outcome": "Fictional",
                   "population": "Fixture only", "reported_value": "1.05", "supporting_quote": QUOTE}
        with patch.object(evidence, "EVIDENCE", folder):
            evidence.build_index(catalog, [], [finding], units)
        with sqlite3.connect(folder / "index.sqlite") as db:
            knowledge.index_claims(db, claims)
        yield root
