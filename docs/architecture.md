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

## Retrieval boundary and future chunking

Current retrieval uses SQLite FTS5/BM25 over canonical published page units.
The existing page-context and whole-document API/MCP tools read the same active
published versions. Structured tables remain separately available via
`get_table`; table-only content is not yet a unified semantic search index.

Section-aware chunks, table-row chunks carrying headers/units/footnotes,
multi-page continuation chunks and embeddings can be derived from published
versions without changing the editorial workflow. Such an index must retain the
publication version, source checksum and all supporting page locators; its
activation must remain coupled to the content version. Neither embeddings nor
LLM summarization should become the canonical clear text.

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
