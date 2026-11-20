# Article sources, clear text and retrieval

This architecture is shared by independent libraries. Project names, DOI lists,
PDFs and admin data are configuration or private data, not application code.
The historical collection build tools remain separate from this project workflow.

## Entities and responsibilities

- **Article**: bibliographic identity. The project manifest defines Core papers;
  discovery provides References and Citing papers. These groups can overlap.
- **Source document**: an attached PDF, identified by its exact byte checksum.
  Attachment confirms identity and permission, not extraction quality.
- **Clear-text draft**: editable text tied to the source and page locators, with
  optional structured tables. Extraction is one way to create a draft; admins may
  instead supply UTF-8 text/Markdown prepared by hand or with their own tools.
- **Published clear text**: an admin-approved transcription version explicitly
  authorized for retrieval. This is not scientific approval of the article.
- **Retrieval index**: a derived, rebuildable representation of published text.
  It is never the editable source of truth.

```text
Article -> Source PDF -> optional extraction -> clear-text draft
                            external/manual editing ----^
                                         |
                              explicit admin publication
                                         |
                        immutable published evidence + index
                                         |
                             search / JSON API / MCP / AI
```

The application does not require, host or call an LLM. External cleanup must
preserve source meaning, tables, caveats, references and page attribution.
Descriptions of figures are annotations, not transcribed findings. Do not invent
missing values, repair contradictory printed results, or interpret an unread
supplement as extracted evidence.

## Storage and activation

`project.json` retains article registration. `papers/<sha256>.pdf` retains the
original private source. `editorial/` contains immutable draft releases and an
active editorial pointer. `registry/` contains only the published retrieval
collection plus any configured metadata catalogs. Both stores use the existing
validated release/snapshot machinery; they are not interchangeable endpoints.
Source attachment and draft edits never activate a retrieval release.

The editor and quality tools read editorial snapshots. Search, Sources available
to AI, JSON API and MCP continue to read the published registry. A source may be:

- Awaiting PDF.
- Clear-text draft, excluded from current retrieval.
- Published for retrieval.
- Draft revision, while the last published text remains active.

Publishing requires a reviewer, reason, source-comparison confirmation and the
current editorial snapshot. It stages a release, a derived index and a private
audit before activating the retrieval pointer. Failed or stale submissions leave
the active published version unchanged. Editing a draft does not mark its text
human-reviewed; publication records that assertion on the published version.

Empty retrieval releases are valid: a library with attached drafts but no
published text must return empty results rather than fall back to drafts.
Metadata discovery is neither clear-text publication nor evidence availability.
Purge filters both stores and their history; factory reset removes both stores.

## Non-destructive restart

Soft reset moves the entire active project, without rewriting its files, into
a dated directory under the sibling `<project-root>-preserved/` folder. Setup
then starts again with Core papers upload. Initialization stages a fresh project
before activation; invalid retained material cannot leave a partial active project.

Core registration and new citation discovery can reuse exact DOI matches from
retained editorial stores (or the published store for legacy projects). Newer
archives take precedence. Reuse verifies the attached PDF checksum, copies the
source, page units, tables and audit history, and activates only editorial
material. Previously published text still requires explicit publication in the
new project. Unmatched papers and old discovery selections are not automatically
restored; restoration never promotes discovered papers to Core papers.

Preserved projects remain immutable to this workflow and outside the active
root. Active-project purge and factory reset do not erase them or shared PDF
folders; operators must manage retained copies separately.

## Import/export and provenance

The initial whole-document interchange is UTF-8 text or Markdown with visible
`[[PAGE ...]]` boundaries. Markers preserve existing source/page identities;
they are validated, removed from canonical page text, and never indexed.
The admin can download a draft, clean it externally, import it, inspect it and
publish it. Blank-page anchors allow manual transcription without extraction.
Original PDF checksums do not change when a transcription changes.

Structured tables keep source/page citations, headers, column units, rows and
footnotes. No automatic figure interpretation or figure-asset publication is
implemented by this refactor. Local cleanup artifacts are not implicitly
published. A future asset/annotation contract should distinguish the original
image, printed caption and added description, each with provenance.

## Retrieval design for research papers

The goal is to help readers find, inspect and cite **what a paper reports**, not
to decide whether its findings are correct. Published clear text is the approved
transcription; PDF pages remain the citation anchors. A search result should
let a reader verify a claim against the source, not just identify a topical paper.

### Current baseline

SQLite FTS5/BM25 searches published PDF pages by default. From the RAG/MCP tab,
an editor can draft document-wide ranges of section and paragraph blocks, preview lexical
search and activate the ranges without editing the publication. Search and MCP
use the activated snapshot. Whole-document reading still uses canonical pages;
structured tables are separately available through `get_table`. A new
publication with changed content falls back to page retrieval for that paper.

### Guidelines for retrieval changes

| Research task | What retrieval should provide |
| --- | --- |
| Understand a finding | Return the result with enough context to identify the study population, comparison, and outcome where reported. Make the relevant methods or limitations available as separate cited passages rather than assuming a result paragraph contains everything. |
| Check a table value | Keep the value with its row label, column header, units, denominator and any qualifying footnote as printed. If the source is ambiguous or inconsistent, show the uncertainty rather than silently correcting it. |
| Find or interpret a figure | Identify the figure and panel when explicit. Keep the printed caption separate from any added description, and label the description as an annotation rather than author text. Do not infer values or conclusions the figure does not support. |
| Follow a citation | Distinguish the paper's own findings from background claims attributed to another paper. A bibliography entry or metadata-only record is a lead, not retrieved evidence from that work. |
| Compare papers | Cite each paper's relevant passage and keep its cohort, definitions, time points and uncertainty separate. Flag disagreements or non-comparable measurements rather than merging them into a single finding. |

Attach **paper title, DOI when available, PDF page, published section heading
when identifiable, and explicit figure/table label when relevant** to each
retrieved block. Keep those fields separate from the verbatim excerpt and show
them consistently in the RAG/MCP tab and MCP results. Retain the source checksum
and snapshot identity for verification. If the heading or label cannot be
determined reliably, omit it rather than inventing one. Chunks can span pages;
cite every supporting page rather than treating a PDF page as a topic boundary.

Use whole paragraphs and source structure before targeting a token count. Keep
captions, table headers and footnotes with the evidence they qualify, or provide
a direct way to retrieve that context. Use overlap only when a boundary would
otherwise drop necessary context. Prefer automatic suggestions and use manual
ranges for exceptions. Treat returning metadata and indexing it as separate
decisions: a heading can clarify a passage even if adding it to search would
produce noisy matches. Never replace the transcription with generated context.

### Evaluation and rollout

Build a small benchmark of research questions with expected DOI, PDF page and
supporting passage or table cell. Include exact terminology, paraphrases,
numerical lookups, figure/caption questions, cross-paper comparisons, and
questions not answerable from the published collection. For unanswerable
questions, distinguish missing full text from a source explicitly reporting
no finding. Keep private evaluation excerpts out of upstream PRs.

1. Compare whole-page BM25 with paragraph-aware chunks using the same queries and
   top-k limit. Measure whether the expected evidence appears in the first five
   results, and inspect where repeated or overlapping results crowd it out.
2. Separately check answer quality: does each citation resolve to evidence that
   actually supports the statement? For numerical answers, verify the number,
   units, denominator and qualifiers. Check that the client abstains when the
   available evidence is insufficient. Report latency alongside quality.
3. Add complexity to address measured failures. If paraphrases still miss
   evidence, test hybrid keyword/vector search against BM25. If the right
   passage is retrieved but lacks context, improve metadata or neighboring
   passage access first. Use structured records for questions that need exact
   fields; a knowledge graph is not a prerequisite for serving cited passages.

For exact table and figure lookups, require the first MCP result to include
the requested evidence itself, not just a mention. Check a table value
against its printed column, denominator and footnote, and check a figure
panel against its label and shared caption. Include an absent-panel control.
For broad questions, still check recall at five and verify the client's
answer and citations separately; exact lookup tests do not establish overall
answer quality.

See Microsoft's [RAG design and evaluation guide](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/rag/rag-solution-design-and-evaluation-guide)
for evaluation design and Anthropic's [Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval)
for the distinction between indexing isolated passages and indexing them with
document context.

## Existing libraries and upstream compatibility

New attachments are draft-only. Existing active releases are not silently
deleted, rewritten, approved or withdrawn on startup. An explicit migration
copies their current content to editorial storage. Operators can retain the
legacy published collection temporarily or withdraw it pending admin approval:

```sh
research-project --root releases/project migrate-text-workflow --withdraw-existing
```

Withdrawal publishes an empty current retrieval collection; it does not delete
PDFs, historical snapshots or old pinned links. Historical access is deliberate,
not a revocation mechanism. Deployments requiring revocation need separate
authorization/retention policy.

The module boundary is reusable `project_text` orchestration around the existing
release contracts, not an iStopMM-specific migration. No private paper content,
local model credentials or generated cleanup data belongs in an upstream PR.
