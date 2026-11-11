#!/usr/bin/env python3
"""Build, validate and query selected source-linked research claims offline."""

import argparse
from contextlib import closing
import csv
import hashlib
import html
import json
import math
import re
import sqlite3
import sys
import unicodedata

from research_library.retrieval import CLAIM_KINDS, claims as query

if __package__:
    from .evidence import ROOT, EVIDENCE, load_jsonl, open_index, paper_id, positive_limit, write_jsonl
else:
    from evidence import ROOT, EVIDENCE, load_jsonl, open_index, paper_id, positive_limit, write_jsonl

KINDS = CLAIM_KINDS
NORMALIZATION = "NFKC and collapsed whitespace only; punctuation/signs/digits retained"
GLOBAL_CAVEATS = [
    "Selected principal claims, not an exhaustive extraction or systematic review.",
    "AI checked selected extracted source passages; not human-reviewed and not a full-paper visual audit.",
    "Recurring cohort families may overlap; claims are not independent studies.",
    "Null means not reported or not curated, never zero.",
    "Observed associations and no-detected-effect results do not establish causation or equivalence.",
]
CSV_FIELDS = [
    "number_id", "claim_id", "paper_id", "doi", "inventory_number", "topic",
    "claim_kind", "statement", "study_design", "population", "cohort_family",
    "comparator", "adjustment", "follow_up", "label", "value", "reported_value",
    "measure", "unit", "ci_95", "reference_interval", "numerator", "denominator",
    "context", "supporting_quote", "pdf_page", "source_path", "source_sha256",
    "source_page_image", "supporting_sources", "caveats", "verification_status", "human_verification",
]


def normalized(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def anchor(text, quote, identifier):
    text, quote = normalized(text), normalized(quote)
    if not quote or quote not in text:
        raise ValueError(f"Source quote not found for {identifier}: {quote!r}")
    start = text.index(quote)
    return {"text": quote, "normalized_start": start,
            "normalized_end": start + len(quote), "normalization": NORMALIZATION}


def check_number(number, identifier):
    value = number["value"]
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value)):
        raise ValueError(f"Invalid numeric estimate: {identifier}")
    for key in ("ci_95", "reference_interval"):
        bounds = number[key]
        if bounds is not None:
            if len(bounds) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in bounds):
                raise ValueError(f"Invalid bounds: {identifier}/{key}")
            if bounds[0] > bounds[1]:
                raise ValueError(f"Reversed bounds: {identifier}/{key}")
            if key == "ci_95" and value is not None and not bounds[0] <= value <= bounds[1]:
                raise ValueError(f"Estimate outside confidence interval: {identifier}")
    if number["measure"] == "reference_interval" and (
            value is not None or number["reference_interval"] is None or number["ci_95"] is not None):
        raise ValueError(f"Reference range misrepresented as point estimate/CI: {identifier}")
    for key in ("numerator", "denominator"):
        count = number[key]
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise ValueError(f"Invalid count: {identifier}/{key}")
    numerator, denominator = number["numerator"], number["denominator"]
    if denominator is not None and (denominator == 0 or (numerator is not None and numerator > denominator)):
        raise ValueError(f"Invalid count denominator: {identifier}")
    if not number["label"] or not number["measure"] or not number["unit"]:
        raise ValueError(f"Number context missing: {identifier}")
    # Check the transcription, not just the presence of an unrelated page quote.
    tokens = {float(t.replace(",", "").replace(" ", "").replace("−", "-")) for t in re.findall(
        r"(?<![\d.%])[-−+]?(?:\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)",
        normalized(number["reported_value"]))}
    words = {"none": 0, "two": 2}
    tokens.update(v for word, v in words.items() if re.search(
        rf"\b{word}\b", number["reported_value"], re.IGNORECASE))
    for reported in ([value] if value is not None else []) + (number["ci_95"] or []) + (number["reference_interval"] or []):
        if reported not in tokens:
            raise ValueError(f"Value/bound not present in printed estimate: {identifier}: {reported}")




def searchable(claim):
    return " ".join(str(claim[k]) for k in (
        "title", "statement", "supporting_quote", "numbers", "population",
        "study_design", "cohort_family", "topic", "caveats", "source_issues"))


def index_claims(db, claims):
    db.execute("DROP TABLE IF EXISTS claim_search")
    db.execute("DROP TABLE IF EXISTS claims")
    db.execute("""CREATE TABLE claims (
        claim_id TEXT PRIMARY KEY, paper_id TEXT, inventory_number INTEGER,
        topic TEXT, claim_kind TEXT, payload_json TEXT)""")
    db.execute("""CREATE VIRTUAL TABLE claim_search USING fts5(
        claim_id UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')""")
    for claim in claims:
        db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?)", (
            claim["claim_id"], claim["paper_id"], claim["inventory_number"],
            claim["topic"], claim["claim_kind"], json.dumps(claim, ensure_ascii=False, allow_nan=False)))
        db.execute("INSERT INTO claim_search VALUES (?,?)", (claim["claim_id"], searchable(claim)))


def csv_records(claims):
    for claim in claims:
        for number in claim["numbers"]:
            merged = {**claim, **number}
            row = {}
            for key in CSV_FIELDS:
                value = merged.get(key)
                if value is None:
                    row[key] = ""
                elif isinstance(value, (list, dict)):
                    row[key] = json.dumps(value, ensure_ascii=False, allow_nan=False)
                else:
                    row[key] = str(value)
                if row[key].startswith(("=", "+", "-", "@")):
                    row[key] = "'" + row[key]
            yield row


def paper_markdown(claims):
    first = claims[0]
    lines = [f"# {first['title']}", "", f"DOI: {first['doi']}", "",
             "**Selected principal claims, AI text-passage checked; not human-reviewed.**",
             "Not an exhaustive review. Check the original source/image before research reuse.", "",
             f"- Study design: {first['study_design']}", f"- Population: {first['population']}",
             f"- Cohort family: {first['cohort_family']}",
             f"- [Original PDF](../../../{first['source_path']})",
             "- [Readable extracted text](text.md) | [Source metadata](metadata.json)",
             "- [Collection knowledge base](../../knowledge/index.html)", ""]
    if first["deeper_pilot_results"]:
        lines += ["- [Deeper pilot summary](summary.md) | [Pilot findings](results.jsonl)", ""]
    if first["related_papers"]:
        lines += ["Related papers: " + ", ".join(
            f"[{r['doi']}](../{r['paper_id']}/knowledge.md)" for r in first["related_papers"]), ""]
    for claim in claims:
        lines += [f"## {claim['claim_id']}", "", f"**{claim['claim_kind']}**: {claim['statement']}", "",
                  f"Population for this claim: {claim['population']}", "",
                  f"[Source PDF page {claim['pdf_page']} image](pages/page-{claim['pdf_page']:03d}.png)",
                  "", f"> {claim['source_anchor']['text']}", ""]
        for key in ("comparator", "adjustment", "follow_up"):
            if claim[key]:
                lines += [f"- {key.replace('_', ' ').title()}: {claim[key]}"]
        lines += [""]
        for number in claim["numbers"]:
            lines += [f"- **{number['label']}**: `{number['reported_value']}`"
                      f" ({number['measure']}; {number['unit']})."]
            if number["numerator"] is not None or number["denominator"] is not None:
                lines += [f"  Count context: numerator {number['numerator']}; denominator {number['denominator']}."
                          " Not a recomputation of the reported percentage."]
            if number["context"]:
                lines += [f"  {number['context']}"]
            lines += [f"  Source quote: {number['source_anchor']['text']}"]
        lines += ["", "**Caveats**"]
        lines += [f"- {caveat}" for caveat in claim["caveats"]]
        if claim["source_issues"]:
            lines += ["- Additional source discrepancies: see the linked metadata and pilot summary."]
        if claim["supporting_sources"]:
            lines += ["", "**Additional source context**"]
            for source in claim["supporting_sources"]:
                lines += [f"- [PDF page {source['pdf_page']}](pages/page-{source['pdf_page']:03d}.png): "
                          f"{source['source_anchor']['text']}"]
        if claim["supplements_identified"] > claim["supplements_acquired"]:
            lines += ["- At least one identified supplement remains unavailable; see the acquisition inventory."]
        lines += ["- " + GLOBAL_CAVEATS[2], "",
                  f"Source SHA-256: `{claim['source_sha256']}`", ""]
    return "\n".join(lines) + "\n"


def browser_html(claims):
    topics = sorted({c["topic"] for c in claims})
    lines = ["<!doctype html><html lang='en'><meta charset='utf-8'>",
             "<title>iStopMM collection knowledge base</title>",
             "<style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}"
             "section{border-top:1px solid #aaa;padding:1rem 0}blockquote{margin-left:1rem}"
             ".kind{font-weight:bold}input,select{font:inherit;margin:.5rem}</style>",
             "<h1>Source-linked knowledge base</h1>",
             f"<p>45 papers; {len(claims)} selected claims; {sum(len(c['numbers']) for c in claims)} "
             "numerical records. AI text-passage checked, not human-reviewed. "
             "Review/recommendation claims are not new empirical evidence.</p>",
             "<p><a href='claims.jsonl'>Claims JSONL</a> | <a href='numbers.csv'>Numbers CSV</a> | "
             "<a href='../qa/report.md'>QA report</a> | <a href='comparisons.md'>Cross-publication QA</a> | "
             "<a href='../README.md'>Evidence guide</a> | <a href='../papers.html'>Main sources</a></p>",
             "<ul>" + "".join(f"<li>{html.escape(c)}</li>" for c in GLOBAL_CAVEATS) + "</ul>",
             "<label>Search <input id='search' type='search'></label>",
             "<label>Topic <select id='topic'><option value=''>All topics</option>" +
             "".join(f"<option>{html.escape(t)}</option>" for t in topics) + "</select></label>",
             "<label>Kind <select id='kind'><option value=''>All claim kinds</option>" +
             "".join(f"<option>{html.escape(t)}</option>" for t in sorted(KINDS)) + "</select></label>",
             "<p id='count' aria-live='polite'></p>"]
    for claim in claims:
        esc = html.escape
        lines += [f"<section id='{esc(claim['claim_id'], quote=True)}' data-topic='{esc(claim['topic'])}' "
                  f"data-kind='{esc(claim['claim_kind'])}'>",
                  f"<h2>{claim['inventory_number']:02}. {esc(claim['title'])}</h2>",
                  f"<p>{esc(claim['doi'])} | <span class='kind'>{esc(claim['claim_kind'])}</span> | "
                  f"{esc(claim['topic'])}</p>",
                  f"<p>{esc(claim['statement'])}</p>",
                  f"<p>{esc(claim['study_design'])}; {esc(claim['population'])}</p>",
                  f"<p><a href='../papers/{claim['paper_id']}/knowledge.md'>Paper brief</a> | "
                  f"<a href='../../{esc(claim['source_path'], quote=True)}#page={claim['pdf_page']}'>"
                  f"Original PDF page {claim['pdf_page']}</a> | "
                  f"<a href='../../{claim['source_page_image']}'>Page image</a></p>",
                  f"<blockquote>{esc(claim['source_anchor']['text'])}</blockquote><ul>"]
        for number in claim["numbers"]:
            lines += [f"<li>{esc(number['label'])}: <strong>{esc(number['reported_value'])}</strong> "
                      f"({esc(number['measure'])}; {esc(number['unit'])})"
                      + (f" — {esc(number['context'])}" if number["context"] else "") + "</li>"]
        lines += ["</ul>"]
        for key in ("comparator", "adjustment", "follow_up"):
            if claim[key]:
                lines += [f"<p>{key.replace('_', ' ').title()}: {esc(claim[key])}</p>"]
        for source in claim["supporting_sources"]:
            lines += [f"<p>Additional source context: "
                      f"<a href='../../{source['source_page_image']}'>PDF page {source['pdf_page']}</a>: "
                      f"{esc(source['source_anchor']['text'])}</p>"]
        if claim["source_issues"]:
            lines += [f"<p>Known source discrepancies: "
                      f"<a href='../papers/{claim['paper_id']}/metadata.json'>source metadata</a></p>"]
        lines += ["<ul>" + "".join(f"<li>{esc(c)}</li>" for c in claim["caveats"]) + "</ul></section>"]
    lines += ["""<script>
const search = document.getElementById('search');
const topic = document.getElementById('topic');
const kind = document.getElementById('kind');
function filter() {
  let count = 0;
  for (const section of document.querySelectorAll('section')) {
    section.hidden = !section.textContent.toLowerCase().includes(search.value.toLowerCase())
      || (topic.value !== '' && section.dataset.topic !== topic.value)
      || (kind.value !== '' && section.dataset.kind !== kind.value);
    if (!section.hidden) count++;
  }
  document.getElementById('count').textContent = count + ' claims shown';
}
for (const control of [search, topic, kind]) control.addEventListener('input', filter);
filter();
</script></html>"""]
    return "\n".join(lines) + "\n"
