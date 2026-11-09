"""Explicitly select portable tests or the private collection regression suite."""

import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "papers/manifest.csv", "papers/supplements/inventory.json",
    "evidence/index.sqlite", "evidence/catalog.jsonl", "evidence/supplements.jsonl",
    "evidence/knowledge/claims.jsonl", "evidence/qa/audit.json",
    "evidence/qa/source-issue-dossier.md", "evidence/qa/highlights/manifest.json",
    "evidence/qa/source-issues.json",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("portable", "collection"))
    args = parser.parse_args()
    if args.suite == "collection":
        missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
        if missing:
            print("Collection tests require the authorized local corpus, extraction and QA outputs.\n"
                  "Provision papers/ and evidence/ as documented; these are not bundled in Git.\n"
                  "Missing prerequisites: " + ", ".join(missing), file=sys.stderr)
            return 2
    folder = ROOT / "tests" / ("portable" if args.suite == "portable" else "")
    suite = unittest.defaultTestLoader.discover(str(folder))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
