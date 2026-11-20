"""Derive display metadata from explicit markup, never from inferred findings."""

import re

from . import chunks


_HEADING = re.compile(r"^[ ]{0,3}(#{1,6})[ \t]+(.+?)\s*$")
_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_LABEL = re.compile(
    r"^[ ]{0,3}(?P<bold>\*\*|__)?(?P<label>(?:Figure|Table)[ \t]+[A-Za-z]?\d+[A-Za-z]?"
    r"(?:[ \t]+\([A-Za-z0-9]+\))?)"
    r"(?:[ \t]+(?P<description>description))?"
    r"(?(bold):?(?P=bold)|(?=[:.\-]|\s*$))(?=\W|$)", re.I)


def _unique(values, limit=32):
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
            if len(seen) == limit:
                break
    return seen


def _title_key(value):
    return " ".join((value or "").casefold().split()).rstrip(".")


def blocks(units, *, title=None, spans=None):
    """Return one context descriptor per numbered block in PDF reading order."""
    headings = {}
    pages = {}
    fence = None
    title_key = _title_key(title)
    for unit in units:
        text = unit["text"]
        details = []
        inherited = [list(headings.values())] if headings else []
        standalone = None
        for start, end in spans[unit["id"]] if spans is not None else chunks.paragraphs(text):
            paths, labels = [], []
            for line in text[start:end].splitlines():
                match = _FENCE.match(line)
                if match:
                    marker, tail = match.groups()
                    if fence is None:
                        fence = marker
                        continue
                    if marker[0] == fence[0] and len(marker) >= len(fence) and not tail.strip():
                        fence = None
                        continue
                if not line.strip():
                    continue
                heading = _HEADING.match(line) if fence is None else None
                label_line = line
                if heading:
                    marker, name = heading.groups()
                    label_line = re.sub(r"[ \t]+#+$", "", name).strip()
                    if not _LABEL.match(label_line) and label_line and _title_key(label_line) != title_key:
                        depth = len(marker)
                        headings = {level: name for level, name in headings.items() if level < depth}
                        headings[depth] = label_line[:256]
                if headings:
                    path = list(headings.values())
                    if len(paths) < 32 and path not in paths:
                        paths.append(path)
                label = _LABEL.match(label_line) if fence is None else None
                if label and len(labels) < 32:
                    name = " ".join(label["label"].split())
                    kind = "figure" if name.lower().startswith("figure") else "table"
                    role = "description" if label["description"] else "label"
                    element = {"kind": kind, "label": name[:64], "role": role}
                    if element not in labels:
                        labels.append(element)
            if not details and paths == inherited:
                standalone = next((item["label"] for item in labels if item["role"] == "label"), None)
            if standalone and paths == inherited:
                paths = [[standalone]]
            else:
                standalone = None
            details.append((paths, labels))
        pages[unit["id"]] = details
    return pages


def document_blocks(units, *, title=None):
    """Number blocks in reading order, splitting at explicit section changes."""
    spans = {}
    fence = None
    title_key = _title_key(title)
    for unit in units:
        text = unit["text"]
        starts = {start for start, _ in chunks.paragraphs(text)}
        offset = 0
        headings = set()
        for line in text.splitlines(keepends=True):
            marker = _FENCE.match(line)
            if fence:
                starts.discard(offset)
            if marker:
                token, tail = marker.groups()
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence) and not tail.strip():
                    fence = None
            elif not fence and (match := _HEADING.match(line)):
                name = re.sub(r"[ \t]+#+$", "", match[2]).strip()
                if name and _title_key(name) != title_key and not _LABEL.match(name):
                    headings.add(offset)
            offset += len(line)
        starts = sorted(starts | headings | {0})
        starts = [start for previous, start in zip([-1, *starts], starts)
                  if previous not in headings or start in headings
                  or text[previous:start].strip().count("\n") > 0]
        spans[unit["id"]] = list(zip(starts, starts[1:] + [len(text)]))
    details = blocks(units, title=title, spans=spans)
    return [(unit, start, end, context) for unit in units
            for (start, end), context in zip(spans[unit["id"]], details[unit["id"]])]


def describe(title, details, first=None, last=None):
    selected = details if first is None else details[first - 1:last]
    context = {
        "section_paths": _unique(path for paths, _ in selected for path in paths),
        "elements": _unique(label for _, labels in selected for label in labels),
    }
    if title and title.strip():
        context["paper_title"] = title.strip()[:4096]
    if first is not None:
        context["block_range"] = {"first": first, "last": last}
    return context


def reading_parts(text, metadata):
    """Offsets into the unchanged retrieval text for explicitly marked content."""
    parts = []

    def add(kind, label, start, end):
        if start < end:
            parts.append({"kind": kind, "label": label[:256], "start": start, "end": end})

    if any(item["kind"] == "table" for item in metadata["elements"]):
        rows = list(re.finditer(r"(?m)^[ \t]*\|[^\n]+\|[ \t]*$", text))
        if (len(rows) >= 3 and re.fullmatch(r"[\s|:\-]+", rows[1][0])
                and all(not text[left.end():right.start()].strip()
                        and text[left.end():right.start()].count("\n") == 1
                        for left, right in zip(rows, rows[1:]))):
            add("table_header", "Table header", rows[0].start(), rows[1].end())
            for row in rows[2:]:
                add("table_row", row[0].split("|")[1].strip() or "Table row", row.start(), row.end())
            end = rows[-1].end()
            notes = re.match(r"(?:[ \t\r\n]*\*\*(?:Note:\*\*|Abbreviations:\*\*|[a-z]\*\*)[^\n]*)+",
                             text[end:], re.I)
            if notes:
                add("table_notes", "Table notes and footnotes", end, end + notes.end())
    if any(item["kind"] == "figure" and item["role"] == "label" for item in metadata["elements"]):
        caption = next((match for match in re.finditer(r"(?m)^[ \t]*\*\*Figure[ \t]+\d+[A-Za-z]?\*\*[^\n]*",
                                                       text, re.I)), None)
        if caption:
            add("figure_caption", "Figure caption", caption.start(), caption.end())
        panels = list(re.compile(r"\*\*\(([A-Z])\)\*\*").finditer(text, caption.start(), caption.end())) if caption else []
        if panels and [m[1] for m in panels] == [chr(65 + i) for i in range(len(panels))]:
            add("figure_intro", "Figure caption introduction", caption.start(), panels[0].start())
            for panel, end in zip(panels, [m.start() for m in panels[1:]] + [caption.end()]):
                add("figure_panel", f"Panel {panel[1]}", panel.start(), end)
    if any(path[-1].casefold() == "references" for path in metadata["section_paths"]):
        refs = list(re.finditer(r"(?m)^[-*] \*\*([^*\n]+)\*\*[^\n]*", text))
        for ref in refs:
            add("reference", ref[1], ref.start(), ref.end())
    return parts[:100]


_EXPLICIT_TARGET = re.compile(
    r"\b(?:table\s+(?P<table>\d+[a-z]?)\b|(?:figure|fig\.?)\s+(?P<figure>\d+)"
    r"(?:(?P<panel>[a-z])\b|\s*\((?P<parenthesized>[a-z])\)|\b))", re.I)


def explicit_target(query, mode):
    if mode != "plain":
        return None
    matches = list(_EXPLICIT_TARGET.finditer(query))
    if len(matches) != 1:
        return None
    match, = matches
    if match["table"]:
        return "table", match["table"].casefold(), None, _words(query[:match.start()] + " " + query[match.end():])
    panel = match["panel"] or match["parenthesized"]
    return "figure", match["figure"], panel.upper() if panel else None, []


def _words(text):
    return re.findall(r"[^\W_]+", text.casefold())


def exact_evidence(text, metadata, target):
    kind, number, panel, words = target
    elements = [element for element in metadata["elements"] if element["kind"] == kind and element["role"] == "label"]
    if len(elements) != 1 or _words(elements[0]["label"]) != [kind, number]:
        return []
    parts = reading_parts(text, metadata)

    def excerpt(part):
        return text[part["start"]:part["end"]].strip()

    if kind == "figure":
        if panel is None:
            return [(0, {"kind": "figure_caption", "label": f"Figure {number}", "text": excerpt(p)})
                    for p in parts if p["kind"] == "figure_caption"]
        intro = next((excerpt(p) for p in parts if p["kind"] == "figure_intro"), None)
        return [(0, {"kind": "figure_panel", "label": f"Figure {number}{panel}",
                     "text": intro + "\n\n" + excerpt(p)})
                for p in parts if intro and p["kind"] == "figure_panel" and p["label"] == f"Panel {panel}"]
    headers = [excerpt(p) for p in parts if p["kind"] == "table_header"]
    notes = [excerpt(p) for p in parts if p["kind"] == "table_notes"]
    matches = []
    for part in parts:
        if part["kind"] != "table_row" or not headers:
            continue
        labels = [_words(part["label"])]
        if "(" in part["label"]:
            labels.append(_words(part["label"].split("(", 1)[0]))
        score = max((len(label) for label in labels if label and
                     any(words[i:i + len(label)] == label for i in range(len(words)))), default=0)
        if not score:
            continue
        row_text = "\n".join([*headers, excerpt(part)])
        matches.append((score, {"kind": "table_row", "label": f"Table {number}: {part['label']}",
                                     "text": "\n\n".join([row_text, *notes])}))
    return matches
