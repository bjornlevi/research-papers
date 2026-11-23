---
name: extract-paper-visuals
description: 'Inspect and faithfully extract or transcribe a paper’s figures, embedded images, and tables into its private clear-text draft.'
argument-hint: 'Paper DOI, title, or local library URL'
---

# Extract research-paper figures, images, and tables

Use this skill when asked to extract, add, or clean up figures, images, or
tables from a paper. First identify the paper and project. For general
clear-text formatting, follow
[clean-paper-clear-text](../clean-paper-clear-text/SKILL.md); for adding a
paper/PDF, follow [add-research-paper](../add-research-paper/SKILL.md).

## Workflow

1. **Inspect the source.** Open the retained original PDF in the local paper
   page's **PDF** tab and inspect the relevant page at readable resolution.
   Record the PDF page number, figure/table label, caption, and source
   checksum. Do not rely on extracted text alone: layout, merged cells,
   superscripts, legends, and figure labels may be missing or misplaced.
2. **Check what is actually extractable.** Determine whether a figure is a
   standalone embedded image or part of vector/page artwork. The app can expose
   supported embedded JPEG/PNG images associated with a PDF page, but vector
   artwork, image masks, composites, and unsupported formats may not be
   extractable as individual images. Do not imply that a page rendering is an
   extracted figure.
3. **Choose a faithful representation.**
   - If the matching PDF contains a supported standalone image, use the app's
     checksum- and page-bound image link in the clear-text editor. Preserve the
     original aspect ratio and avoid cropping labels, legends, or panel
     markers. Do not use a remote image URL or an image copied from a different
     source/version.
   - If it cannot be extracted, retain a clear figure heading and printed
     caption, then link to the original local PDF page. Do not redraw vector
     art, fabricate a replacement, infer panel content, or present a page
     screenshot as the original embedded asset.
   - Transcribe a table from the rendered source page, preserving its exact
     title, headers, row labels, units, value formatting, footnote markers,
     notes, and denominators. Preserve blank or not-applicable cells as the
     paper represents them. Do not resolve source inconsistencies by guessing.
4. **Use the appropriate table representation.** If the table belongs in the
   clear-text Markdown, format it as a Markdown table and keep its notes
   adjacent. Where the app's **Structured tables** editor is used, enter a
   structured record with `title`, `headers`, `units`, `rows`, and `footnotes`;
   keep every row aligned with the header width. For whole-document editing,
   retain existing table IDs and page assignments; give a new table an empty
   ID and the correct PDF page/unit. Structured tables render separately at
   their stored page location—the editor does not preserve exact within-page
   placement.
5. **Save a draft and verify.** Make changes through the app editor and save
   them as an unpublished draft. Reopen the rendered clear text and confirm
   the figure/table appears on the correct page, Markdown tables render
   correctly, structured-table rows and notes are complete, image links
   resolve to the matching source page, and citations/page markers remain
   intact. Compare all transcribed values and labels with the PDF.

## Boundaries

- Extraction and transcription are not scientific validation. Keep the
  authors' reported values and wording distinct from any operator-added
  description; label an annotation as an annotation.
- Do not silently omit a table continuation, caption, footnote, panel, or
  qualifying note. If a visual cannot be read or extracted reliably, say so
  and preserve a link to the source page for inspection.
- Do not publish the clear-text draft unless explicitly asked and source
  comparison is complete. A clear-text draft is not RAG/MCP evidence until
  published.
- Treat original PDFs, derived images, tables, and paper data as private.
  Do not commit or export them without authorization for that destination.

## Related documentation

- [Independent project workflows](../../../docs/independent-projects.md)
- [Article sources, clear text and retrieval](../../../docs/architecture.md)
- [MCP table retrieval contract](../../../docs/contracts.md)
