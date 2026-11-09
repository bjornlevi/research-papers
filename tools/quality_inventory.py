"""Inventory expected source locations from checksum-matched originals, without editing them."""

import re
import zipfile

import pymupdf

from .supplementary_extraction import A, R, S, W, read_xml, relationships, word_text


def expected_locations(path, format_):
    if format_ == "pdf":
        with pymupdf.open(path) as document:
            if document.is_encrypted or not len(document):
                raise ValueError("Cannot inventory an encrypted or empty PDF")
            return [{"kind": "pdf", "pdf_page": page} for page in range(1, len(document) + 1)]
    locators = []
    with zipfile.ZipFile(path) as archive:
        if format_ == "docx":
            body = read_xml(archive, "word/document.xml").find(W + "body")
            if body is None:
                raise ValueError("Word document body missing")
            rels = relationships(archive, "word/document.xml")
            for number, node in enumerate(body, 1):
                graphics = any((blip.get(R + "embed") or blip.get(R + "link")) in rels
                               for blip in node.iter(A + "blip"))
                if word_text(node) or graphics:
                    locators.append({"kind": "word_table" if node.tag == W + "tbl" else "word_paragraph",
                                     "body_element": number})
            for member in archive.namelist():
                if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", member):
                    if word_text(read_xml(archive, member)):
                        locators.append({"kind": "word_package", "package_member": member})
        elif format_ == "xlsx":
            rels = relationships(archive, "xl/workbook.xml")
            for sheet in read_xml(archive, "xl/workbook.xml").find(S + "sheets"):
                member = rels[sheet.get(R + "id")]["resolved"]
                for row in read_xml(archive, member).findall(S + "sheetData/" + S + "row"):
                    if row.findall(S + "c"):
                        locators.append({"kind": "worksheet", "worksheet": sheet.get("name"),
                                         "row": int(row.get("r"))})
        else:
            raise ValueError("Unsupported original format")
    return locators
