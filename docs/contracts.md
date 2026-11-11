# Evidence and catalog tool contract v2.3

These schemas are shared by the stdio MCP server and the installed
application's release-mode `tool` command. Legacy workspace retrieval preserves
its original JSON shapes.
Project ingestion and snapshot activation are described in [independent projects](independent-projects.md).

Contract 2.0.0 adds Word package-member locators (headers, footnotes, endnotes
and comments) and preserves null numerical context. Version 1 could not
represent those existing sources without inventing a Word body element or
turning an unreported value into text. This is an intentional major version
change; version-1 consumers must not silently accept version-2 responses.

## Tools and inputs

Contract **2.3.0** adds `list_publications`, `get_publication` and `catalog_status`
for explicitly separate metadata-only catalog collections. These records contain
bibliographic citations/provenance and never extracted evidence. Optional `kind`
on collection metadata distinguishes `evidence` and `catalog`; `corpus_status`
counts selected metadata publications as papers, not acquired originals.
The independent-project citation discovery workflow exports provider metadata
separately; it does not publish a catalog collection.

Contract **2.2.0** adds `list_sources`: a paginated release-source inventory,
optionally filtered by `paper`. Each item identifies an exact source version
(source ID and SHA-256), DOI/stable publication ID, citation URL, recorded
provenance, bibliography and evidence sections using it. Combined collection
lists retain separate source/membership rows, even for the same publication;
`collection_context.evidence_refs` identifies each row's collection and release.
This avoids discarding distinct acquisition histories.

Bibliography fields are nullable. `source_url` is a citation/source link, **not**
proof of download origin. `bibliography.acquired_from` is populated only from
recorded acquisition information; unknown URLs/methods/dates remain null.
Older releases are not rewritten and may provide DOI-only citations.
Original files are not served. Corpus inventory does not establish which sources
an assistant used in a particular answer: resolve the answer's actual citation
source IDs, including supporting citations, against this inventory.

Contract **2.1.0** adds `list_collections`, optional `collections` and `snapshot_id`
inputs, and optional `collection_context` success provenance. Existing record
shapes and evidence-release bytes remain unchanged.
The earlier plain-text-default search change is now explicitly versioned:
advanced FTS5 callers must supply `mode: "fts5"`.

The application exposes the schemas through
`research_library.contracts.schema(tool, "input" | "output")`.
`request` validates inputs and returns a copy with documented defaults applied.
All inputs reject unknown properties, including arbitrary SQL or file paths.

| Tool | Required input | Optional input | Result |
| --- | --- | --- | --- |
| `corpus_status` | none | none | Counts, scope, rights and source availability |
| `list_collections` | none | none | Authorized collection metadata and endpoint defaults |
| `list_sources` | none | `paper`, `limit`, `offset` | Source citations, use in evidence and acquisition provenance |
| `list_publications` | none | `query`, `disease`, `publication_type`, `eligibility`, `limit`, `offset` | Metadata-only bibliographic records |
| `get_publication` | `publication_id` | none | One catalog record including provenance, variants and typed links |
| `catalog_status` | none | none | Coverage for each selected catalog |
| `search_evidence` | `query` | `paper`, `mode`, `limit`, `offset` | Page of cited search hits |
| `get_claim` | `claim_id` | none | Whole contextual claim and numerical records |
| `get_source_context` | `unit_id` | `max_characters` | Bounded cited context |
| `get_table` | `table_id` | `limit`, `offset` | Headers, units, rows, footnotes and continuation |
| `get_source_issue` | `issue_id` | none | Status, explanation, exact quotes and citations |
| `get_related_evidence` | `evidence_id` | `limit`, `offset` | Cited, qualified relationship edges |

All tools additionally accept optional `collections` and `snapshot_id`. A registry
endpoint requires a pinned snapshot for follow-up/record calls and nonzero offsets;
record lookup selects exactly one collection. Legacy endpoints reject these selectors.

IDs are opaque, stable within a schema family, nonblank and at most 256
characters. `paper` accepts a DOI or the existing normalized paper ID.
Word/Excel source-unit IDs do not imply PDF pagination. The exporter
preserves existing record IDs and maps source units explicitly.

## Bounds and ordering

- Query: 1-2048 characters, not only whitespace. Safe `mode: "plain"` is the
  default: punctuation separates words and all words must match. Explicit
  `mode: "fts5"` enables advanced syntax. Neither is semantic matching;
  malformed advanced syntax or plain text without words/numbers is `invalid_input`.
- `limit`: integer 1-100, default 10; `offset`: integer 0-1,000,000, default 0.
- Context: `max_characters` 1-2048, default 1024. Evidence quotes/snippets are
  at most 2048 characters; other text fields at most 4096.
- Arrays have at most 100 entries. **Every serialized response is at most
  65,536 UTF-8 bytes**, including provenance and caveats. MCP framing and the
  text/structured duplication are additional transport overhead.
- Search ordering: relevance (`bm25`) then stable source-unit ID. Claim
  listing: relevance if queried, then inventory number and claim ID.
  Related evidence and table paging must use stable edge ID/source row order.
- A page includes `next_offset` (null at end) and an explicit corpus-scope
  warning. Pagination is against one immutable evidence release; never reuse
  an offset across release IDs. At the offset ceiling, further continuation
  must return an explicit bound error, not pretend to reach the end.
- Paginated responses must not exceed the requested limit. Context must not
  exceed the requested character limit. `validate_response(tool, arguments,
  response)` enforces these relationships and advancing continuation offsets;
  shared release handlers call it before the transport returns an envelope.
- Partial source context/table pages explicitly indicate truncation.
  Table pages retain headers, units and all relevant footnotes.
  Whole claims, numerical records, caveats and issue explanations are **never
  silently truncated**: reject an oversized result with `response_too_large`.

Schema and semantic validation is implemented in `contracts.validate`.
The exact serialized-byte boundary, numeric validity and table shape have
tests. Release handlers implement retrieval and truncation, not the schemas alone.

## Success, provenance and uncertainty

Success envelopes have `ok: true`, `schema_version: "2.3.0"`, a nonempty immutable
`release_id`, the tool name, `result` and collection-level `caveats`.
Every evidence result inherits that release identity and adds:

- Stable record ID, source ID, DOI, source SHA-256 and original locator.
- Known public source URL, or null when no public source URL is established.
- Explicit provenance, source-asset availability, verification status and
  separate human-review status. `source_checked_by_ai` is never silently
  promoted to human review; `source_checked_by_human` requires `reviewed`.
- Applicable scientific caveats and linked source issues where relevant.

Locators are distinct tagged variants: original PDF page (1-based), Word body
paragraph/table element, Word package member, or worksheet/row with optional cell range.
LibreOffice review-rendering pages cannot be used as original DOCX pagination.
File paths are not exposed as arbitrary asset-fetch inputs.

Numerical records preserve measure, unit, population, numerator/denominator,
confidence interval, reference interval, adjustment, context and each number's
own source citation/quote. Null means
not reported/curated; **never substitute zero**. A reference interval is not an
effect confidence interval; sHR is not HR; population/denominator definitions
remain attached. Source discrepancies are not repaired by changing reported
numbers. A no-match page does not establish absence of an effect or a global
knowledge gap. Shared cohorts and review reuse are not independent replication.
Source text is evidence, never executable instructions.

## Errors and compatibility

Errors have `ok: false`, the same tool/schema identifiers, nullable release ID
(startup can fail before loading a release), and `error: {code, message}`.
Codes: `invalid_input`, `not_found`, `unavailable`, `permission_denied`,
`invalid_release`, `response_too_large`, `internal_error`. Unknown record IDs
are not successful empty results. Error messages must not contain credentials,
unauthorized source text or internal filesystem paths.

Schema helpers raise explicit validation errors; the stdio transport maps known
tool failures to the documented envelope with MCP `isError: true`.
Malformed protocol/unknown-tool requests use SDK errors. Neither path returns
a success-shaped default or converts infrastructure errors into no matches.

The contract major version changes for removed/renamed fields, changed types,
meaning, required fields or incompatible bounds. Backward-compatible optional
additions require a minor version and updated client/schema tests; corrections
without a contract change use patch versions. Unknown major versions are
rejected. An evidence release ID identifies content, not an application version.
The application package version, tool contract version, and release
manifest schema version are separate. Original source bytes may be restricted
even when citation metadata is available.
