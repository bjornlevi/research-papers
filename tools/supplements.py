#!/usr/bin/env python3
"""Discover and acquire supplementary files, independently of main-paper coverage."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

import pymupdf

if __package__:
    from .workspace import workspace_root
else:
    from workspace import workspace_root

ROOT = workspace_root()
FOLDER = ROOT / "papers/supplements"
INVENTORY = FOLDER / "inventory.json"
XLINK = "{http://www.w3.org/1999/xlink}href"
USER_AGENT = "Mozilla/5.0 (compatible; local academic supplementary-material inventory)"
SOURCES = FOLDER / "additional-sources.json"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=35) as response:
        return response.read(), response.geturl()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.current = [attrs["href"], ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append(self.current)
            self.current = None


def publisher_url(row):
    doi = row["doi"]
    if doi.startswith("10.1038/"):
        return "https://www.nature.com/articles/" + doi.split("/")[1]
    if doi.startswith("10.7326/"):
        return "https://www.acpjournals.org/doi/" + doi
    if doi.startswith("10.1111/"):
        return "https://onlinelibrary.wiley.com/doi/" + doi
    return "https://doi.org/" + doi


def new_file(label, original_name, urls, discovery_url):
    return {"label": label, "original_name": original_name, "urls": list(dict.fromkeys(urls)),
            "discovery_url": discovery_url, "status": "identified", "attempts": []}


def merge_files(files):
    merged = {}
    for file in files:
        key = file["original_name"]
        if key not in merged:
            merged[key] = file
            continue
        prior = merged[key]
        urls = list(dict.fromkeys(prior["urls"] + file["urls"]))
        attempts = prior["attempts"] + file["attempts"]
        if file["status"] == "acquired":
            merged[key] = file
        merged[key].update(urls=urls, attempts=attempts)
    return list(merged.values())


def apply_sources(entries):
    overrides = json.loads(SOURCES.read_text()) if SOURCES.exists() else {}
    for entry in entries:
        extra = overrides.get(str(entry["inventory_number"]), {})
        entry["files"] = merge_files(entry["files"] + [
            new_file(f["label"], f["original_name"], f["urls"], f["discovery_url"])
            for f in extra.get("files", [])])
        if extra.get("evidence"):
            entry["additional_evidence"] = extra["evidence"]
        for file in entry["files"]:
            name = file["original_name"]
            if "_MOESM" in name:
                for url in list(file["urls"]):
                    if "static-content.springer-cdn.com/esm/" in url:
                        file["urls"].append(url.replace("https://static-content.springer-cdn.com/esm/",
                                                       "https://media.springernature.com/original/springer-static/esm/"))
            if name.startswith("pone.") and re.search(r"\.s\d+\.", name):
                file["urls"].insert(0, "https://journals.plos.org/plosone/article/file?type=supplementary&id=10.1371/journal."
                                    + name.rsplit(".", 1)[0])
            file["urls"] = list(dict.fromkeys(file["urls"]))
        entry["files"] = [
            f for f in entry["files"]
            if Path(f["original_name"]).suffix or not all(
                any(url in other["urls"] for other in entry["files"] if other is not f)
                for url in f["urls"])]
        if entry["files"]:
            entry["discovery_status"] = "identified"
        elif extra.get("discovery_status"):
            entry["discovery_status"] = extra["discovery_status"]
    return entries


def check_source(url, checks):
    try:
        body, final = fetch(url)
        checks.append({"url": url, "status": "retrieved", "final_url": final})
        return body, final
    except (OSError, urllib.error.URLError) as exc:
        checks.append({"url": url, "status": "unavailable", "error": str(exc)})
        return None, url


def discover(row):
    entry = {k: row[k] for k in ("paper_id", "doi", "title", "inventory_number")}
    entry.update(checked_at=datetime.now(timezone.utc).isoformat(), checks=[], files=[])
    pid = row.get("pmcid", "")
    if not pid:
        search = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(
            {"query": 'DOI:"' + row["doi"] + '"', "format": "json"})
        body, _ = check_source(search, entry["checks"])
        if body:
            records = json.loads(body)["resultList"]["result"]
            pid = next((r.get("pmcid", "") for r in records if r.get("doi", "").lower() == row["doi"].lower()), "")
    entry["pmcid"] = pid
    xml_checked = False
    if pid:
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pid}/fullTextXML"
        body, _ = check_source(url, entry["checks"])
        if body:
            root = ET.fromstring(body)
            dois = [e.text.lower() for e in root.iter("article-id") if e.get("pub-id-type") == "doi" and e.text]
            if row["doi"].lower() not in dois:
                raise ValueError(f"XML DOI does not match {row['doi']}")
            xml_checked = True
            for node in root.iter("supplementary-material"):
                label = " ".join(" ".join(node.itertext()).split())
                for child in node.iter():
                    href = child.get(XLINK)
                    if not href or not re.search(r"\.(pdf|docx?|xlsx?|zip|csv|txt|pptx?)(?:$|\?)", href, re.I):
                        continue
                    name = href.rsplit("/", 1)[-1]
                    urls = []
                    if href.startswith("http"):
                        urls.append(href)
                    else:
                        urls.append(f"https://pmc.ncbi.nlm.nih.gov/articles/instance/{pid.removeprefix('PMC')}/bin/{href}")
                        if row["doi"].startswith("10.1038/"):
                            urls.insert(0, "https://static-content.springer-cdn.com/esm/art%3A10.1038%2F"
                                        + row["doi"].split("/")[1] + "/MediaObjects/" + name)
                    entry["files"].append(new_file(label or name, name, urls, url))
            entry["files"] = merge_files(entry["files"])
    # Publisher pages provide attachments absent from, or not deposited in, PMC.
    url = publisher_url(row)
    body, final = check_source(url, entry["checks"])
    publisher_checked = False
    if body:
        text = body.decode("utf-8", errors="replace")
        publisher_checked = not re.search(r"just a moment|verify you are human|access denied", text[:10000], re.I)
        parser = Links()
        parser.feed(text)
        for href, label in parser.links:
            if not re.search(r"supp|esm|moesm|supporting|additional.file|appendix|data supplement|peer.review|reporting.summary", href + " " + label, re.I):
                continue
            if not re.search(r"\.(pdf|docx?|xlsx?|zip|csv|txt|pptx?)(?:$|\?)|article/file\?.*\.s\d+", href, re.I):
                continue
            file_url = urllib.parse.urljoin(final, html.unescape(href))
            name = urllib.parse.urlsplit(file_url).path.rsplit("/", 1)[-1]
            if "journals.plos.org" in file_url:
                attachment_id = urllib.parse.parse_qs(urllib.parse.urlsplit(file_url).query).get("id", [""])[0]
                existing = next((f for f in entry["files"] if f["original_name"].startswith(attachment_id.removeprefix("10.1371/journal.") + ".")), None)
                if existing:
                    existing["urls"].append(file_url)
                    continue
            existing = next((f for f in entry["files"] if f["original_name"] == name or file_url in f["urls"]), None)
            if existing:
                existing["urls"] = list(dict.fromkeys(existing["urls"] + [file_url]))
            else:
                entry["files"].append(new_file(" ".join(label.split()) or name, name, [file_url], url))
    with pymupdf.open(ROOT / row["pdf_path"]) as document:
        mentions = []
        for page_number, page in enumerate(document, 1):
            for line in page.get_text().splitlines():
                if re.search(r"supplement|supporting information|online appendix|web.only", line, re.I):
                    mentions.append({"pdf_page": page_number, "text": line.strip()})
    entry["main_pdf_mentions"] = mentions
    entry["discovery_status"] = ("identified" if entry["files"] else
                                 "referenced_not_located" if mentions else
                                 "none_identified" if xml_checked or publisher_checked else "inconclusive")
    print(f"{row['inventory_number']:02}: {entry['discovery_status']} ({len(entry['files'])} files)", flush=True)
    return entry


def validate_bytes(data, suffix):
    if data.lstrip()[:100].lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("HTML response, not supplementary file")
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise ValueError("Response is not PDF")
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if not len(doc) or doc.is_encrypted:
                raise ValueError("Empty or encrypted PDF")
            for page in doc:
                page.get_text()
            return {"format": "pdf", "pdf_pages": len(doc)}
    if suffix in (".xlsx", ".docx", ".pptx", ".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"Corrupt ZIP member: {bad}")
            names = archive.namelist()
            core = {".xlsx": "xl/workbook.xml", ".docx": "word/document.xml",
                    ".pptx": "ppt/presentation.xml"}.get(suffix)
            if core:
                if core not in names or "[Content_Types].xml" not in names:
                    raise ValueError(f"Not a valid {suffix} package")
                ET.fromstring(archive.read(core))
                ET.fromstring(archive.read("[Content_Types].xml"))
        return {"format": suffix[1:], "archive_members": len(names)}
    if suffix in (".doc", ".xls", ".ppt"):
        if not data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise ValueError("Not a legacy Office document")
        return {"format": suffix[1:]}
    if suffix in (".csv", ".txt"):
        data.decode("utf-8-sig")
        return {"format": suffix[1:]}
    raise ValueError(f"Unsupported file type: {suffix}")


def acquire(entry):
    for number, file in enumerate(entry["files"], 1):
        if file["status"] == "acquired":
            path = ROOT / file["path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != file["sha256"]:
                raise ValueError(f"Existing supplement changed: {path}")
            continue
        suffix = Path(urllib.parse.urlsplit(file["original_name"]).path).suffix.lower()
        if suffix not in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".csv", ".txt", ".ppt", ".pptx"):
            suffix = ".pdf" if ".pdf" in " ".join(file["urls"]) else ""
        target = FOLDER / f"{entry['paper_id']}-supplement-{number:02d}{suffix}"
        if target.exists():
            raise ValueError(f"Untracked target already exists; verify parent identity before importing: {target}")
        for url in file["urls"]:
            try:
                data, final = fetch(url)
                info = validate_bytes(data, suffix)
            except (OSError, urllib.error.URLError, ValueError, zipfile.BadZipFile, pymupdf.FileDataError) as exc:
                file["attempts"].append({"url": url, "error": str(exc)})
                continue
            target.write_bytes(data)
            file.update(status="acquired", path=target.relative_to(ROOT).as_posix(),
                        sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                        downloaded_from=final, download_method="http", **info)
            break
        else:
            file["status"] = "not_acquired"
    print(f"{entry['inventory_number']:02}: {sum(f['status']=='acquired' for f in entry['files'])}/{len(entry['files'])} acquired", flush=True)
    return entry


def save(entries):
    FOLDER.mkdir(parents=True, exist_ok=True)
    INVENTORY.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
    with (FOLDER / "manifest.csv").open("w", newline="") as handle:
        columns = ["inventory_number", "doi", "title", "discovery_status", "label", "status",
                   "path", "original_name", "sha256", "bytes", "source_url", "discovery_url"]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for entry in entries:
            for file in entry["files"] or [{}]:
                writer.writerow({**{k: entry[k] for k in columns[:4]},
                                 **{k: file.get(k, "") for k in columns[4:]},
                                 "source_url": file.get("downloaded_from", next(iter(file.get("urls", [])), ""))})
    lines = ["<!doctype html><html lang='en'><meta charset='utf-8'><title>Supplementary material inventory</title>",
             "<h1>Supplementary material inventory</h1><p>All 45 catalog papers checked. "
             "None identified is not proof that no supplement exists. Referenced/not located and inconclusive "
             "records require further investigation. Files remain separate from main PDFs. "
             "Access does not imply permission to redistribute.</p>"]
    files = [f for e in entries for f in e["files"]]
    acquired = sum(f["status"] == "acquired" for f in files)
    lines.append(f"<p><strong>{acquired} of {len(files)} identified files acquired.</strong> "
                 "<a href='README.md'>Guide</a> | <a href='manifest.csv'>CSV inventory</a> | "
                 "<a href='inventory.json'>Detailed provenance</a></p>")
    missing = [(e, f) for e in entries for f in e["files"] if f["status"] != "acquired"]
    if missing:
        lines.append("<h2>Files still unavailable</h2><ul>")
        for entry, file in missing:
            lines.append(f"<li>{html.escape(entry['doi'])}: {html.escape(file['label'])} "
                         f"— <a href='{html.escape(file['discovery_url'], quote=True)}'>Publisher supplement page</a></li>")
        lines.append("</ul>")
    for entry in entries:
        lines.append(f"<h2 id='{entry['paper_id']}'>{entry['inventory_number']:02}. {html.escape(entry['title'])}</h2>"
                     f"<p>{html.escape(entry['doi'])} — {entry['discovery_status']}</p><ul>")
        if entry.get("additional_evidence"):
            lines.append(f"<li>{html.escape(entry['additional_evidence'])}</li>")
        for file in entry["files"]:
            links = " | ".join(f"<a href='{html.escape(url, quote=True)}'>Source {n}</a>" for n, url in enumerate(file["urls"], 1))
            links += f" | <a href='{html.escape(file['discovery_url'], quote=True)}'>Publication source</a>"
            local = (" | <a href='" + html.escape(Path(file["path"]).name, quote=True) + "'>Local file</a>") if file.get("path") else ""
            lines.append(f"<li>{html.escape(file['label'])}: {file['status']} — {links}{local}</li>")
        lines.append("</ul>")
    (FOLDER / "inventory.html").write_text("\n".join(lines) + "</html>\n")


def validate(entries):
    catalog = {r["paper_id"]: r for r in (
        json.loads(line) for line in (ROOT / "evidence/catalog.jsonl").read_text().splitlines())}
    if len(entries) != 45 or {e["paper_id"] for e in entries} != set(catalog):
        raise ValueError("Supplement inventory must cover all 45 catalog sources")
    paths = set()
    for entry in entries:
        parent = catalog[entry["paper_id"]]
        if (entry["doi"], entry["inventory_number"]) != (parent["doi"], parent["inventory_number"]):
            raise ValueError("Supplement parent identity mismatch")
        if len({f["original_name"] for f in entry["files"]}) != len(entry["files"]):
            raise ValueError("Duplicate supplementary file records")
        for file in entry["files"]:
            if not file["urls"] or not file["discovery_url"]:
                raise ValueError("Supplement source links missing")
            if file["status"] != "acquired":
                continue
            path = ROOT / file["path"]
            if not path.resolve().is_relative_to(FOLDER.resolve()) or file["path"] in paths:
                raise ValueError("Supplement path outside collection or used twice")
            paths.add(file["path"])
            data = path.read_bytes()
            if len(data) != file["bytes"] or hashlib.sha256(data).hexdigest() != file["sha256"]:
                raise ValueError(f"Supplement file changed: {path}")
            info = validate_bytes(data, path.suffix.lower())
            if any(file.get(key) != value for key, value in info.items()):
                raise ValueError(f"Supplement format metadata mismatch: {path}")
    print(f"Validated 45 parent records and {len(paths)} acquired supplement files.")


def import_browser(entries, output):
    with output.open() as handle:
        records = json.loads(handle.readline().removeprefix("Result: "))
    for record in records:
        if not record.get("base64"):
            print(f"Browser did not retrieve a file: {record['url']} (HTTP {record['status']})")
            continue
        matches = [(e, n, f) for e in entries for n, f in enumerate(e["files"], 1)
                   if record["url"] in f["urls"]]
        if len(matches) != 1:
            raise ValueError(f"Browser file must match exactly one catalog attachment: {record['url']}")
        entry, number, file = matches[0]
        data = base64.b64decode(record["base64"], validate=True)
        suffix = Path(file["original_name"]).suffix.lower()
        info = validate_bytes(data, suffix)
        target = FOLDER / f"{entry['paper_id']}-supplement-{number:02d}{suffix}"
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Refusing to overwrite different supplement bytes: {target}")
        target.write_bytes(data)
        file.update(status="acquired", path=target.relative_to(ROOT).as_posix(),
                    sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                    downloaded_from=record["url"], download_method="browser", **info)
        print(f"Imported {target.name}: {len(data)} bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["discover", "download", "report", "validate", "import-browser"])
    parser.add_argument("--file", type=Path, help="Browser-tool output holding base64 attachments")
    args = parser.parse_args()
    if args.command == "import-browser" and not args.file:
        parser.error("import-browser requires --file")
    if args.command == "discover":
        if INVENTORY.exists():
            parser.error("Inventory already exists; preserve acquired-file provenance rather than overwriting it.")
        catalog = [json.loads(line) for line in (ROOT / "evidence/catalog.jsonl").read_text().splitlines()]
        with (ROOT / "papers/manifest.csv").open(newline="") as handle:
            original = {int(r["number"]): r for r in csv.DictReader(handle)}
        rows = [{**r, "pmcid": original[r["inventory_number"]]["pmcid"]} for r in catalog]
        with ThreadPoolExecutor(max_workers=4) as executor:
            entries = list(executor.map(discover, rows))
    else:
        entries = json.loads(INVENTORY.read_text())
        entries = apply_sources(entries)
        if args.command == "download":
            with ThreadPoolExecutor(max_workers=4) as executor:
                entries = list(executor.map(acquire, entries))
        elif args.command == "import-browser":
            import_browser(entries, args.file)
    if args.command == "validate":
        validate(entries)
    else:
        save(entries)
    statuses = {}
    for entry in entries:
        statuses[entry["discovery_status"]] = statuses.get(entry["discovery_status"], 0) + 1
    files = [f for e in entries for f in e["files"]]
    print(json.dumps({"papers": statuses, "files_identified": len(files),
                      "files_acquired": sum(f["status"] == "acquired" for f in files)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile, pymupdf.FileDataError) as exc:
        print(f"Supplement error: {exc}", file=sys.stderr)
        sys.exit(1)
