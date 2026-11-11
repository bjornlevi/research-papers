"""Installed entry point for the optional offline build tools."""

import argparse
import importlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path,
                        help="Populated workspace containing papers/ and evidence/")
    parser.add_argument("tool", choices=("evidence", "supplements", "literature-ingest", "citation-neighborhood",
                                        "literature-update", "literature-ocr"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if importlib.util.find_spec("pymupdf") is None:
        parser.error("Build tools require: pip install 'research-library[build]'")
    root = args.data_root.expanduser().resolve()
    if not root.is_dir() or (
        args.tool not in {"literature-ingest", "citation-neighborhood", "literature-update", "literature-ocr"}
        and not (root / "papers/manifest.csv").is_file()
    ):
        parser.error("data root must contain the authorized local papers/manifest.csv; no corpus is bundled")
    os.environ["RESEARCH_LIBRARY_DATA_ROOT"] = str(root)
    module_name = {"literature-ingest": "literature_ingest",
                   "citation-neighborhood": "citation_neighborhood",
                   "literature-update": "literature_update", "literature-ocr": "literature_ocr"}.get(args.tool, args.tool)
    module = importlib.import_module(
        ".build." + module_name,
        package="research_library")
    sys.argv = [f"research-build {args.tool}", *args.arguments]
    import pymupdf
    try:
        return module.main()
    except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError, ET.ParseError, zipfile.BadZipFile,
            pymupdf.FileDataError) as error:
        print(f"Build error: {error}", file=sys.stderr)
        return 1
