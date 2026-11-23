---
name: clean-paper-clear-text
description: 'Format or clean a registered paper’s Markdown clear text while preserving source meaning, page anchors, and draft/publication boundaries.'
argument-hint: 'Paper DOI, title, or local library URL'
---

# Clean research-paper clear text

Use this skill when asked to format, clean, or correct a paper's Markdown
clear text. For adding/registering a new paper or attaching a PDF, use
[add-research-paper](../add-research-paper/SKILL.md) first.

## Workflow

1. **Identify the exact paper and project.** Resolve the DOI/title against the
   local library and confirm which project is intended. Check whether the
   current text is an extraction, unpublished draft, or published version.
   Never assume another paper's editor tab or draft is the target.
2. **Inspect before editing.** Read the complete current clear text and inspect
   the source PDF pages for requested corrections, tables, figures, and
   ambiguous transcription. Treat the PDF as the authority for transcription;
   preserve uncertainty where it cannot be resolved.
3. **Edit through the paper editor.** Preserve every `[[PAGE ...]]` marker and
   its source identity exactly. Use the app's visual or Markdown editor and
   save a draft with an accurate reviewer and change reason. Do not edit
   immutable releases, extracted page units, or generated exports directly.
4. **Improve structure without changing evidence.**
   - Use a clear title and a readable author line; retain affiliations and
     other front matter when present.
   - Use headings that reflect the printed sections. Keep prose, tables,
     figure captions/descriptions, and references distinguishable.
   - Format tables with their printed headers, row labels, units, denominators,
     footnotes, and notes together. Preserve reported values; do not reconcile
     apparent discrepancies by guessing.
   - Keep citation callouts connected to the correct reference entries. Do not
     renumber or convert citation styles unless the mapping can be verified
     from the source.
   - Keep figures faithful to the source. If an image cannot be extracted,
     link to the matching local PDF page rather than drawing or inventing it.
5. **Verify the saved draft.** Compare changed content with the source pages.
   Confirm page markers and assignments remain intact, citations resolve,
   tables render with their notes, and figures point to the correct page.
   Confirm the app identifies the result as an unpublished draft.

## Boundaries

- Formatting is not scientific validation. Do not add interpretations,
  findings, or corrected values absent from the source.
- Do not publish clear text unless the user explicitly asks for publication
  and the required source comparison is complete.
- Attaching or extracting a paper does not make it one of the project's
  **Core papers**. Use **References**, **Citing papers**, and **All papers**
  consistently for library groups.
- Treat PDFs, extracted text, drafts, and project data as private. Do not
  commit or export paper content unless the user has authorized the destination
  and the repository's tracking workflow.

## Related documentation

- [Independent project workflows](../../../docs/independent-projects.md)
- [Article sources, clear text and retrieval](../../../docs/architecture.md)
