"""Conservative readable Markdown views; original extraction remains authoritative."""

from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

if __package__:
    from .workspace import workspace_root
else:
    from workspace import workspace_root

ROOT = workspace_root()
HEADINGS = {
    "abstract", "background", "objective", "objectives", "introduction",
    "methods", "materials and methods", "patients and methods",
    "results", "discussion", "conclusion", "conclusions", "references",
    "acknowledgments", "acknowledgements", "funding", "funding sources",
    "conflicts of interest", "conflict of interest", "disclosures",
    "author contributions", "data availability", "data availability statement",
    "ethics approval", "supplementary information", "supplementary material",
    "supplemental material", "supporting information", "statistical analysis",
    "study population", "study design", "limitations", "primary funding source",
}
WORD = r"[^\W\d_]+"
HYPHENS = re.escape("-\u2010\u2011")


def word_key(text):
    return unicodedata.normalize("NFKC", text).casefold().replace("\u2010", "-").replace("\u2011", "-")


@lru_cache(maxsize=1)
def word_evidence():
    """Use intact words/compounds in local raw extraction, not guessed spellings."""
    words, compounds = set(), set()
    base = ROOT / "evidence/papers"
    paths = sorted(base.rglob("pages.jsonl")) + sorted(base.rglob("units.jsonl"))
    for path in paths:
        for record in load_jsonl(path):
            text = word_key(record["text"])
            words.update(re.findall(rf"(?<![\w-]){WORD}(?![\w-])", text))
            compounds.update(re.findall(rf"(?<![\w-]){WORD}(?:-{WORD})+(?![\w-])", text))
    return words, compounds


def repair_line_hyphens(text, evidence=None):
    words, compounds = word_evidence() if evidence is None else evidence

    def repair(match):
        left, hyphen, right = match.group(1, 2, 3)
        if hyphen == "\u00ad":
            return left + right
        joined, compound = word_key(left + right), word_key(left + "-" + right)
        if compound in compounds:
            return left + hyphen + right
        if joined in words:
            return left + right
        return match.group(0)

    return re.sub(
        rf"(?<![\w{HYPHENS}])({WORD})([{HYPHENS}\u00ad])[ \t]*\n[ \t]*"
        rf"({WORD})(?![\w{HYPHENS}])", repair, text)


def heading(line):
    return line.strip().rstrip(":").casefold() in HEADINGS


def escape_markdown(text):
    text = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "<", ">", "#", "|"):
        text = text.replace(character, "\\" + character)
    return re.sub(r"(?m)^(\s*\d+)([.)])(?=\s)", r"\1\\\2", text)


def literal(text):
    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", text)), default=3))
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def is_prose(lines):
    if len(lines) <= 1:
        return True
    if any("\t" in line or re.search(r"\S\s{3,}\S", line) for line in lines):
        return False
    # PDF superscripts can split citations and percentages out of a prose line.
    # Ignore those fragments only in a block dominated by substantial prose.
    citation = r"\d+(?:\s*[,–-]\s*\d+)*"
    fragments = [
        bool(re.fullmatch(rf"(?:\({citation}\)|\d+(?:\s*[,–-]\s*\d+)+)[.;]?", line))
        or bool(re.fullmatch(r"\(\d+(?:\.\d+)?%\)[.;]?", line))
        for line in lines
    ]
    substantive = [line for line, fragment in zip(lines, fragments) if not fragment]
    prose_context = (
        len(substantive) >= 2
        and sum(len(line) >= 40 and bool(re.search(r"[A-Za-z]{3}", line))
                for line in substantive) >= len(lines) / 2
    )
    if prose_context:
        considered = substantive
    elif re.fullmatch(rf"\({citation}\)\.", lines[-1]):
        considered = lines[:-1]
    else:
        considered = lines
    # Short labels and numeric runs often encode tables, graphs, formulas or author lists.
    short = sum(
        len(line) < 25 and not (
            prose_context and re.match(rf"^{citation}\s+[A-Za-z]{{3}}", line))
        for line in considered[:-1]
    )
    numeric = sum(not re.search(r"[A-Za-z]{3}", line) for line in considered)
    if short > len(considered) / 3 or numeric:
        return False
    return bool(considered) and sum(len(line) for line in considered) / len(considered) >= 40


def format_block(text, evidence=None):
    sections = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        if heading(lines[0]):
            sections.append("### " + escape_markdown(lines.pop(0)))
        if not lines:
            continue
        if is_prose(lines):
            repaired = repair_line_hyphens("\n".join(lines), evidence)
            sections.append(escape_markdown(" ".join(repaired.splitlines())))
        else:
            sections.append(literal("\n".join(lines)))
    return "\n\n".join(sections)


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pdf_text(folder, pages, blocks):
    raw = ["# Extracted source text\n\n",
           "Machine-extracted text. PDF page numbering is authoritative; use page images for layout, tables and figures.\n\n"]
    readable = ["# Readable source text\n\n",
                "Formatting-only view of machine extraction, not a summary or verified transcription. "
                "[Raw extracted text](text.raw.md). Page/block order, headers, footers and watermarks "
                "are retained. Prose line-wrap hyphens are repaired when intact words/compounds "
                "elsewhere in the raw collection support the spelling; ambiguous cases remain unchanged. "
                "Tables/formulas may require the source image.\n\n"]
    by_page = defaultdict(list)
    for block in blocks:
        by_page[block["pdf_page"]].append(block)
    for page in pages:
        header = f"## PDF page {page['pdf_page']}\n\n[Source image]({page['page_image']})\n\n"
        raw.extend([header, page["text"] + "\n\n"])
        readable.append(header)
        source_blocks = by_page[page["pdf_page"]]
        # OCR has no native blocks. Preserve every source character, including any text
        # that the block extractor might omit, by falling back to full page text.
        block_text = "".join(block["text"] for block in source_blocks)
        compact = lambda text: re.sub(r"\s+", "", text)
        if source_blocks and compact(block_text) == compact(page["text"]):
            readable.append("\n\n".join(format_block(block["text"]) for block in source_blocks))
        elif page["text"].strip():
            readable.append(format_block(page["text"]))
        else:
            readable.append("*No text detected after OCR; inspect the source image.*")
        readable.append("\n\n")
    (folder / "text.raw.md").write_text("".join(raw))
    (folder / "text.md").write_text("".join(readable))


def office_text(folder, units):
    raw = ["# Machine-extracted supplementary content\n\n"]
    readable = ["# Readable supplementary content\n\n",
                "Formatting-only view, not curated findings. [Raw extracted text](text.raw.md). "
                "Source body-element and worksheet/row coordinates are preserved; "
                "tables and spreadsheet rows are shown literally. Prose line-wrap hyphens are "
                "repaired using intact word/compound evidence in the raw collection; "
                "ambiguous cases remain unchanged.\n\n"]
    for number, unit in enumerate(units, 1):
        locator = json.dumps(unit["location"], ensure_ascii=False)
        raw.append(f"## Unit {number}: {locator}\n\n{unit['text']}\n\n")
        readable.append(f"## Unit {number}\n\nSource location:\n\n{literal(locator)}\n\n")
        table_like = "worksheet" in unit["location"] or unit["location"].get("kind") == "table"
        readable.append((literal(unit["text"]) if table_like else format_block(unit["text"])) + "\n\n")
    (folder / "text.raw.md").write_text("".join(raw))
    (folder / "text.md").write_text("".join(readable))


def format_all():
    word_evidence.cache_clear()
    base = ROOT / "evidence/papers"
    pdfs = sorted(base.glob("*/pages.jsonl")) + sorted(base.glob("*/supplements/*/pages.jsonl"))
    offices = sorted(base.glob("*/supplements/*/units.jsonl"))
    for path in pdfs:
        pdf_text(path.parent, load_jsonl(path), load_jsonl(path.parent / "blocks.jsonl"))
    for path in offices:
        office_text(path.parent, load_jsonl(path))
    print(f"Formatted {len(pdfs)} PDF documents and {len(offices)} Office documents; raw JSON and index unchanged.")
