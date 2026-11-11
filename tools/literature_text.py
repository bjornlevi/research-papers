"""Readable text.md views of the active literature collection; the release units remain authoritative."""

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import shutil
import sys

from . import literature_update as update
from .text_formatting import WORD, escape_markdown, format_block, word_key

NOTICE = ("Formatting-only view of the active library text, not a summary or verified transcription. "
          "Each section is one PDF page; cite the page and source SHA-256 shown here. Page text is the exact "
          "release text (native extraction, or separate local OCR where marked); prose line-wrap hyphens are "
          "repaired only when intact words elsewhere in the library support the spelling. Figures are not "
          "extracted; tables and figures may require the original page.\n\n")


def output_of(data_root):
    return Path(data_root).absolute() / "releases/literature-text"


def slug(source):
    base = re.sub(r"[^a-z0-9]+", "-", (source.get("doi") or "").lower()).strip("-")[:80] or "source"
    return f"{base}-{source['sha256'][:12]}"


def native_blocks(data_root, digest):
    """Native block layout for one exact PDF, if its extraction document is still on disk."""
    releases_root = Path(data_root).absolute() / "releases"
    candidates = [releases_root / "m9-ingestion-001/documents" / (digest + ".json")]
    for root in ("source-updates", "provided-sources", "m10-updates-001"):
        candidates.extend(sorted((releases_root / root / "runs").glob(f"*/export-{digest}/documents/{digest}.json")))
    for path in candidates:
        if path.is_file():
            document = json.loads(path.read_bytes())
            if document.get("source_sha256", digest) == digest:
                return {page["pdf_page"]: page.get("blocks", []) for page in document["pages"]}
    return {}


def evidence(units):
    words, compounds = set(), set()
    for unit in units:
        text = word_key(unit["text"])
        words.update(re.findall(rf"(?<![\w-]){WORD}(?![\w-])", text))
        compounds.update(re.findall(rf"(?<![\w-]){WORD}(?:-{WORD})+(?![\w-])", text))
    return words, compounds


def page_markdown(unit, blocks, known):
    compact = lambda text: re.sub(r"\s+", "", text)
    lines = [f"## PDF page {unit['citation']['locator']['pdf_page']}\n\n"]
    if any(caveat.startswith("Separate local OCR") for caveat in unit["caveats"]):
        lines.append("*OCR text, not human verified; check the original page for exact wording and numbers.*\n\n")
    if blocks and compact("".join(block["text"] for block in blocks)) == compact(unit["text"]):
        lines.append("\n\n".join(filter(None, (format_block(block["text"], known) for block in blocks))))
    elif unit["text"].strip():
        lines.append(format_block(unit["text"], known))
    else:
        lines.append("*No text detected; inspect the original page.*")
    lines.append("\n\n")
    return "".join(lines)


def render(source, units, blocks, known):
    bibliography = source.get("bibliography") or {}
    title = bibliography.get("title") or source.get("doi") or source["id"]
    head = [f"# {escape_markdown(title)}\n\n",
            f"- DOI: {escape_markdown(source.get('doi') or 'not recorded')}\n",
            f"- Source SHA-256: `{source['sha256']}`\n",
            f"- Provenance: {escape_markdown(source.get('provenance') or 'not recorded')}\n\n", NOTICE]
    pages = sorted(units, key=lambda unit: unit["citation"]["locator"]["pdf_page"])
    return "".join(head + [page_markdown(unit, blocks.get(unit["citation"]["locator"]["pdf_page"]), known)
                           for unit in pages])


def build(data_root, registry=None):
    """Regenerate text.md for every source in the active literature snapshot (replaces the folder)."""
    data_root = Path(data_root).absolute()
    registry = Path(registry or data_root / "releases/m9-platform")
    snapshot = update.store_at(registry).current().snapshots["literature"]
    bundle = snapshot._bundle
    by_source = defaultdict(list)
    for unit in bundle["units"]:
        if unit["citation"].get("locator", {}).get("kind") == "pdf":
            by_source[unit["citation"]["source_id"]].append(unit)
    known = evidence(bundle["units"])
    output = output_of(data_root)
    staging = output.with_name(".literature-text-staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    index = ["# Literature text\n\n",
             f"Readable views of the active literature snapshot `{snapshot.release_id}`. Generated; do not edit.\n\n"]
    for source in sorted(bundle["sources"], key=lambda item: (item.get("doi") or "", item["sha256"])):
        units = by_source.get(source["id"])
        if not units:
            continue
        name = slug(source)
        (staging / name).mkdir()
        (staging / name / "text.md").write_text(render(source, units, native_blocks(data_root, source["sha256"]), known))
        title = (source.get("bibliography") or {}).get("title") or source.get("doi") or source["id"]
        index.append(f"- [{escape_markdown(title)}]({name}/text.md) ({len(units)} pages)\n")
    (staging / "index.md").write_text("".join(index))
    previous = output.with_name(".literature-text-previous")
    shutil.rmtree(previous, ignore_errors=True)
    if output.exists():
        os.replace(output, previous)
    os.replace(staging, output)
    shutil.rmtree(previous, ignore_errors=True)
    return {"output": str(output), "sources": len(index) - 2, "snapshot": snapshot.release_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("RESEARCH_LIBRARY_DATA_ROOT", ".")))
    parser.add_argument("--registry", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.data_root, args.registry), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
