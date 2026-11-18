# Independent projects

## Terminology

- **Core papers**: the papers explicitly selected as discovery seeds.
- **References**: papers cited by Core papers.
- **Citing papers**: papers that cite Core papers.
- **All papers**: the combined library. These groups can overlap.

Discovery actions are **Find references** and **Find citing papers**.
Saved/API direction identifiers remain `background` (References) and `impact`
(Citing papers) for compatibility. Neither discovery nor extraction adds a paper
to Core papers automatically.

The project command creates a private `core` collection. It does not
automatically acquire PDFs, verify PDF-to-DOI
identity, or publish discovered citations to the browser or MCP metadata catalog.
Use **Configure/update metadata catalog** on Publication metadata to publish
saved discovery results as a private catalog.
For a complete, runnable example with real papers and an openly licensed demo
PDF, see [the open-science demo](open-science-demo.md).

Run `make setup` first. The default project directory is `releases/project/`,
which is ignored by Git. Original PDFs, extracted text, project metadata and
discovery responses can be private; if using `--root` or discovery output
elsewhere, ensure that location is private and ignored by version control.

Start the website and initialize the project from **Project settings**:

```sh
make web
```

Open <http://127.0.0.1:8081/settings>, enter the project name and authorization
basis, and select either a CSV with `doi` and `title` columns or a JSON
manifest exported by this project. The authorization note is a user statement,
not independent verification. `make web` runs detached and writes its PID and
log under `releases/`; stop it with `make web-off`. The default project root is
`releases/project/`. Use the same `PROJECT_ROOT=/path/to/project` with both
commands to select another private location.

### Restart setup while keeping material

Use **Soft reset (keep material)** in Settings and type `SOFT RESET`. It moves
the entire configured project into a dated folder under the sibling
`<project-root>-preserved/` directory, then returns to Core papers upload.
For the default root, this is `releases/project-preserved/`. Nothing inside
the old project is deleted: PDFs, extracted text, cleaned text, structured
tables, correction/publication history, settings and discovery results remain
in the preserved copy.

Upload the Core papers CSV or JSON again to initialize a fresh project.
Exact DOI matches reuse the newest available retained attachment and editorial
text, with PDF checksum verification and no re-extraction. Adding Core papers
later or encountering papers in new discovery also restores matching material.
Papers without retained matches remain awaiting PDF. Corrupted retained PDFs
cause an explicit error instead of being silently skipped.

Reused text is always an unpublished draft, even if it was previously published.
Admins must explicitly publish it for current search/AI retrieval. Restoring
discovered papers does not promote them to Core papers. Old discovery results
remain in the archive; the new project's discovery selections and settings start
fresh. Retained projects are not automatically merged back wholesale.

The CLI equivalent, after stopping background operations, is:

```sh
.venv/bin/research-project --root releases/project soft-reset --confirm
```

Preserved folders and shared PDF folders are outside the active project.
Purging or factory-resetting the new active project does **not** remove these
retained copies. Inspect and manage them separately when permanent erasure
is intended. Keep preserved material private and out of version control.

Settings also offers the destructive **Reset to factory settings**. Typing `RESET PROJECT`
confirms permanent deletion of everything inside the configured project root,
including PDFs, extracted evidence, snapshots, discovery output and settings.
Files outside that root, including previously preserved projects, are not affected.

Alternatively, initialize from the command line:

```sh
make project-init PROJECT_NAME="My research project" \
  PROJECT_AUTHORIZATION="I am authorized to extract and retrieve these PDFs privately." \
  PROJECT_PAPERS=/path/to/core-papers.csv
make project-status
```

To add or import papers after initialization, use the project CLI:

```sh
.venv/bin/research-project add \
  --doi 10.1234/example --title "Example paper"
```

Register a batch using a UTF-8 CSV with `doi` and `title` columns:

```sh
.venv/bin/research-project import-csv my-papers.csv
```

Each DOI must be distinct. A registered paper is initially `awaiting_pdf`.
Project forks can track a metadata-only manifest, provided the titles and DOIs
are suitable for that repository's visibility:

```sh
.venv/bin/research-project export-manifest core-papers.json
```

The manifest contains only DOI and title, not PDFs, text, permissions, source
paths or credentials. A fresh copy can initialize from it using `init
--manifest core-papers.json`; PDFs must be supplied again from authorized
private storage. The export refuses to overwrite an existing file.

After checking that the PDF actually matches its registered title and DOI,
and confirming you have permission for private extraction, attach it:

In **Manage library → Core papers and PDFs**, click **awaiting_pdf** in a paper's status to select
a local PDF from the shared `releases/pdf-data/` folder. Choose the file, confirm that
you checked the identity and private-use authorization, and submit. Files in
that folder are not served and survive project reset. To use another folder,
restart with `make web PDF_DATA_ROOT=/path/to/pdfs`. The selection excludes
symlinks, does not accept paths outside that folder, and omits PDFs whose
checksums are already attached to papers in the active project.

For a paper awaiting PDF, **Find PDF** first searches the configured local PDF
data folder, including subfolders and preserved project PDF copies. It suggests
matches using recorded acquisition DOI/checksum, a title-based filename, or
DOI/title on the first page or in PDF metadata. Symlinks are excluded.
Local matches skip online lookup and downloads, but do not verify identity,
licensing or extraction quality. Unreadable local PDFs are reported rather than
treated as matches; scanned PDFs without usable metadata may need manual selection.
Correction notices are not suggested as original papers merely because they cite
the original DOI; they may be suggested for separately registered correction records.

If no local match is found, **Find PDF** searches OpenAlex for a location with an
explicit Creative Commons/public-domain license and tries downloading it in the
background. The progress/result page shows failures or the saved filename and
acquisition URL. This uses the installation's OpenAlex key when configured and
may encounter provider quotas. It does not bypass access controls or infer rights
from free-to-read status. A missing result is not proof that no copy exists.
If official PubMed Central distribution metadata lacks an explicit license,
that location is skipped and the reason is recorded; other eligible locations
and papers in the batch can still be checked.
Successful PDFs and DOI/checksum-bound provenance (including year, license and
download URL) are saved in the PDF data folder. Nothing is attached or extracted
automatically: inspect the file locally, select it, and confirm identity and
private-use authorization as above.
Downloaded filenames use the paper title, for example
`a-manifesto-for-reproducible-science.pdf`. A short checksum suffix distinguishes
different PDFs with the same title; existing files are never overwritten.

### View and correct extracted data

### Clear-text drafts and publication

PDF attachment and extraction now create **private editorial drafts**, not active
RAG evidence. The admin is responsible for a faithful clear-text version, prepared
by hand or with their own LLM/tools. The application does not send documents to
a model. In **View and correct extracted data**, download the page-marked draft,
edit it externally or in whole-document edit mode, and **Save clear-text draft**.
You can also import a UTF-8 `.txt`/`.md` file in the editor. An uploaded file replaces
the text field for that save. Preserve page markers and source attribution.

Saving does not assert human verification. From the read-only view, provide a
reviewer and reason, confirm comparison with the source, then **Publish clear text
for retrieval**. Only this action updates current search, reader, Sources available
to AI, JSON API and MCP evidence. A subsequent draft revision leaves the previous
published text active until published again. Tables are included in publication,
with their page locators, headers, units and footnotes. Publication is transcription
approval, not scientific validation.

**Attach only; I will provide clear text separately** skips extraction and creates
empty page anchors for manual input. Extraction, when used, is merely a draft
generator. The command-line equivalents are:

```sh
research-project --root releases/project attach-pdf --doi 10.0000/example \
  --pdf /path/to/authorized.pdf --confirm-identity --no-extract
research-project --root releases/project export-text --doi 10.0000/example \
  --file /path/to/draft.txt
# Edit draft.txt locally, retaining page markers.
research-project --root releases/project import-text --doi 10.0000/example \
  --file /path/to/draft.txt --reviewer "Admin" --reason "Prepared clear text"
research-project --root releases/project status
research-project --root releases/project publish-text --doi 10.0000/example \
  --snapshot-id EDITORIAL_SNAPSHOT_FROM_STATUS --reviewer "Admin" \
  --reason "Checked transcription against source" --confirm-checked
```

For existing libraries, `migrate-text-workflow` preserves existing active text as
legacy retrieval content and copies it into editorial storage. Add
`--withdraw-existing` to exclude it from current RAG pending publication. Neither
option deletes PDFs or historical snapshot-pinned content. No migration runs
silently at startup. See [architecture](architecture.md) for storage, boundaries
and current retrieval limitations.

**Search evidence** provides **All papers**, **Core papers**, **References**
(a core paper cites it), and **Citing papers** (it cites a core paper) scopes.
These search retained extracted text only, not metadata-only discoveries.
Relationship membership uses the same current core list and saved discovery
results as Library; papers can match both directions, including core papers
linked to other core papers. Filtering happens before ranking and pagination,
and result-page links retain the selected scope and evidence snapshot.

The web app keeps explanations behind **?** controls. Hover to preview, click
to keep one open, or focus it and press Enter/Space. Escape or clicking outside
closes it. Without JavaScript, the controls still open on click. Snapshot hashes
are omitted from the everyday view; stable links, copied citations, API responses
and expandable provenance retain their technical identities. Status, errors,
identity/permission confirmations and destructive-action warnings stay visible.
Editor metadata, structured tables, correction history and reader copy fields
are expandable when needed.
Manage library presents discovery coverage as a limitations list and per-core-paper
provider summaries, with reported/retrieved counts and errors. Technical provider
details remain expandable rather than appearing as raw JSON.

For an extracted paper in **Library** or **Sources available to AI**, click
**View and correct extracted data**. It opens in read-only mode with the whole
retained document in page order, separated by two newlines without altering page
text. Paper metadata, provenance, page warnings, structured tables from all pages
and correction history are expandable. The reader also links to this view for
the current version of its retained page.

Choose **Turn on edit mode**, or **Edit** on a paper's **Clear text** tab, to
edit visually and save all pages in a single audited draft version. The toolbar
offers headings, bold, italic, bulleted/numbered lists, links and undo/redo.
Page boundaries are protected in visual mode. Use **Reading width** or **Full
width**, or drag the lower-right edge of the editor, to adjust its width.
Formatting is stored as Markdown, not HTML, and rendered in the paper's reading
view. Saving a draft does not publish it.

**Edit Markdown source** switches to the plain-text draft. This is also the
fallback if JavaScript is unavailable or the draft contains HTML/image markup
that the visual editor cannot safely preserve. Untouched pages retain their
original text exactly. Visible `[[PAGE …]]` markers in source mode
identify the original PDF pages. Keep every marker unchanged and in order, with
two newlines before each marker after the first. Text between markers is assigned
to that page for citations; move text across boundaries only when correcting its
page assignment. Missing, duplicated, reordered or damaged markers reject the
save and retain your draft rather than guessing provenance. Markers are editor
scaffolding and do not appear in saved evidence or the read-only document.

**Appearance** in the navigation offers **Light**, **Dark**, or the device's
color setting, plus an **Increased contrast** option. Preferences are stored in
this browser, not the project manifest, and do not affect other operators.
Device increased-contrast, reduced-motion and forced-color settings also apply.
If browser storage is blocked, the controls still work for the current page and
show that the preference could not be saved.

A page's **Edit** link under **Pages and extraction warnings** still opens the
individual-page editor, including **Repair paragraph layout**. Metadata changes
apply to the whole paper in either mode. **Return to read-only view** discards
unsaved drafts, and saving returns to the readable document.
This private-project editor provides:

- Direct page/location links with the current page highlighted, and the full
  retained text, not the reader's 2,048-character slice. Save before switching
  pages; navigation does not carry unsaved changes to another page.
- Editable title, publication year, citation/source URL and acquisition details.
  The DOI, source checksum, source ID and original page locators are not editable.
  These edits affect evidence metadata, not provider catalogs or the core DOI seed list.
- A rendered view of structured tables at the selected location, and a JSON
  editor for their titles, headers, column units, rows and footnotes. An example
  shows how to add a manually transcribed table when extraction retained only page
  text. Preserve existing table IDs; use an empty ID for a new table. Removing
  a table from the list removes it only from the new version. Tables are limited
  to 100 columns/rows each by the evidence contract. Structured table IDs are
  retained in the editor data and can be retrieved through `get_table`.
  In whole-document mode the JSON includes all tables and requires a `unit_id`
  for each table to identify its PDF page. Existing table page assignments cannot
  be changed. New tables use an empty table ID and one of the listed page unit IDs.
  A correction accepts at most 100 tables. Whole-document text is limited to
  10,000,000 characters including markers, with at most 1,000,000 per page.

New native PDF extractions automatically order text blocks by column/section
whitespace gutters and join wrapped lines within blocks, keeping blank lines
between blocks. Raw native text and its checksum remain in the private extraction
checkpoint alongside the paragraph-formatted text. This is machine-inferred
layout, not human verification. At original line boundaries within a block,
split words such as `cov-` / `ered` are joined as `covered` when that complete
word occurs elsewhere in the PDF and the hyphenated compound does not.
Known compounds retain their hyphen (for example `quasi-experimental`).
Uncertain splits retain the hyphen without inserting a space; explicit soft
hyphens at line breaks are removed. Within-line hyphens and block boundaries
are not rewritten. Complex layouts, tables and ambiguous hyphenation still
need review. The versioned extraction policy prevents resuming old checkpoints
with changed formatting rules; use a new extraction workspace/run instead.
Already-saved evidence and corrections are not automatically rewritten.

If an older native PDF extraction mixed columns or inserted layout spacing, use **Repair
paragraph layout**. It verifies the local PDF checksum, orders text blocks using
column/section whitespace gutters, and joins wrapped lines within blocks,
leaving blank lines between blocks. This replaces only the text draft in the
form, without saving or changing retained evidence. Other form fields are kept.
Review the draft against the PDF: complex layouts, tables, paragraph boundaries
and line-end hyphenation still need manual checking. Pages with too little
native text cannot use this repair; correct their OCR text manually.

Inspect the original PDF locally, enter your name and correction reason, then
**Save clear-text draft**. It validates and stages a private editorial version.
Use **Publish clear text for retrieval** separately to activate it for search,
reader, source metadata, JSON API and MCP. Published text/tables are labelled
operator-reviewed, not independently scientifically validated; existing
extraction/OCR caveats are retained. The immutable source
checksum continues to identify the original PDF, not the corrected transcription.

Existing evidence releases and snapshot-pinned links keep their original data.
The editor shows a before/after correction history including reviewer, reason,
time, source checksum, parent snapshot and new release. Saved corrections live
in private import runs; a purge retains surviving sources' audit records under
`corrections/` when cumulative import runs must be deleted. Purging a source
removes its correction history too. Factory reset removes all project history.

Saving rejects stale forms if the active snapshot changed in another tab or job.
Invalid input is shown without discarding the form; no partial version is
activated. Editing is blocked during discovery, PDF processing, metadata update
or purge. Retained original PDFs can be viewed in a paper's local **PDF** tab.

### Data quality actions

Open **Data quality** and select a finding. No terminal commands are needed:

- **Build location inventory** reads the checksum-matched stored original.
  If it is unavailable, select the original PDF, Word document, or spreadsheet.
  A mismatched file is rejected. Inventory counts original locations; it does not
  verify the extracted text.
- **Record comparison** requires your name, a comparison note, the appropriate
  result, and confirmation that you inspected the original. Blank/non-text
  decisions are offered only for findings where they are valid. A review
  cannot waive a known extraction defect.
- Select a retained location and use **Report an extraction error** to record
  a blocker, or **Edit extracted data** to open the existing versioned editor.
- For an omitted project PDF page, use **Add missing page text** to enter its
  complete transcription. Saving requires an original-comparison confirmation
  and saves an editorial draft with the same correction audit as other edits.
  Publish the completed clear text separately before it becomes RAG evidence.

Reviews and problems are saved locally, bound to the source/extraction identity,
without changing evidence text. Corrections preserve earlier versions and
require a fresh quality check; they do not imply scientific validation.
Stale forms and requests made while project-changing jobs run are rejected.
Retained originals are available only through the private project's local **PDF**
tab, not through evidence retrieval or MCP. Text editing requires private
project mode; review, problem reporting, and uploaded-original inventory are
also available for development evidence collections.

### Batch PDF location and extraction

In **Manage library → Core papers and PDFs**, **Find all** starts the same local-first
batch lookup for every Core paper awaiting PDF, without including discovered
papers selected elsewhere. Existing local matches and downloads are reused.
Review the batch results and explicitly confirm each PDF before extraction;
finding PDFs does not attach them or start discovery.

In **Library**, filter by relationship and PDF/extraction status, then switch
**Locate PDF: off/on** for individual papers. These are toggle buttons, not
identity/permission confirmations. Toggles update in place without navigating or
changing scroll position (ordinary form navigation is retained without JavaScript).
Open **Manage PDFs for these results** to carry the Library's filters, sort order
and page into **Manage library → Batch PDFs**. There, **Mark papers on this page
for PDF location** selects eligible records on that page; **Mark all matching
papers for PDF location** selects eligible records across pages. **Clear all PDF
location marks** clears the selection. Marking papers does not start PDF location.
Only records with a title and DOI awaiting PDF are eligible.

**Locate selected PDFs** starts a sequential, rate-limited background job,
checking local PDFs first for each paper using the same matching rules as
**Find PDF**. The
**Review batch results and confirm extraction** page shows persistent per-paper
results, acquisition links, license/year/version metadata and errors. A PDF can
also be selected from the local data folder, including when online location
failed. Inspect each file locally and individually confirm its title/DOI and
private-use authorization. **Attach and extract confirmed PDFs on this page**
queues only those explicitly confirmed PDFs, with their checksums bound to the
confirmation. Review pages contain 25 papers; unconfirmed papers are not extracted.
Discovered papers remain outside the core seed list.

Use **Review suggested papers and PDFs side by side** to open a confirmation
page pairing each paper's title/DOI with its suggested local PDF and match
information. Change the selected file if necessary and click **Accept** for each
pair you have inspected. Accept confirms identity and private-use permission,
but does not start extraction. Acceptance is saved across pages and app restarts
and bound to the PDF checksum. Accepted pairs leave the pending list; a success
message, accepted count and expandable accepted list show what has been saved.
Use **Reject suggestion** for an incorrect match, even if the suggested file is
missing. This dismisses the match and unmarks the paper for PDF location without
deleting any files or removing the paper from the Library. Choose a replacement
in the review queue, or mark the paper again to retry PDF location.
Choose **Extract accepted PDFs** to extract all
accepted pairs across all pages; changed PDFs require fresh acceptance.
Confirmation pages show 25 suggested pairs.
Clicking the extraction button without checking any pairs opens this confirmation
page with guidance instead of rejecting the request. Missing local files are
flagged and must be replaced before confirmation.

**Pause after current paper** finishes the current item before stopping.
Selection and results are saved in private `batch-pdf.json` within the project.
After a manual pause or an interrupted location pass, use **Resume PDF location
from checkpoint** to continue the saved pass without repeating completed attempts.
The paper that was in progress may be retried. **Locate selected / retry missing
PDFs** starts a new pass, reuses local matches and completed downloads, and
retries selected missing/failed entries. Provider
metadata errors pause the queue so remaining papers are not repeatedly attempted
after a quota error. Resume only after the provider permits it; daily resets are
not scheduled automatically. Interrupted or failed extraction requires fresh
confirmation, and already attached sources are not extracted again.
Other project mutations are blocked while a batch is running.

Open **Batch diagnostics** on the batch review page to see the last saved stage,
paper DOI when known, UTC checkpoint time, and saved task count. The report
distinguishes a requested pause, a provider pause, and a recorded worker error.
For worker errors, look for `batch_pdf_worker_stopped` in the web server logs.
If the worker disappears without recording a reason, the report shows any
available checkpoint and whether it came from a different server instance;
it cannot tell a restart from a crash or forced stop. Older checkpoints may
lack a stage or timestamp. Resuming a location pass keeps the previous stop
report visible.

The same **Find PDF** and **Select PDF and attach/extract** controls are available
on **Library** for core and discovered papers with a resolved
DOI and title. Discovered papers retain their relationship categories after extraction: they do not
become core papers or seeds for further citation expansion. Library categories
show **References** (a core paper cites it), **Citing papers** (it cites a
core paper), or both. Their extraction remains a private draft until an admin
publishes clear text. Only published text is available in Sources available to AI,
search, reader, JSON API and MCP.
For compatibility, all attached project evidence uses the existing internal
`core` collection; core discovery membership is separately defined by the
registered DOI/title list. No additional MCP collection authorization is needed.
Existing MCP servers see the new active snapshot on their next request.
The Find PDF result page also offers attachment with the downloaded file selected;
identity/permission confirmation is still required. DOI-unresolved records cannot
use these controls until their identity has been resolved.

Or use the CLI:

```sh
.venv/bin/research-project attach-pdf \
  --doi 10.1234/example --pdf /path/to/paper.pdf --confirm-identity
```

The identity confirmation records an operator assertion, **not an automatic
identity check**. `--license` optionally records the operator-supplied licence;
possession does not establish a licence or authorize redistribution. A PDF must
be readable and within the existing size and page limits. Text is extracted per
original page, with automatic column-aware paragraph formatting for native PDF
text; pages without native text go through local Tesseract OCR, with
the usual not-human-verified and confidence caveats. Missing OCR dependencies,
and unreadable PDFs surface as errors. A PDF with no usable extracted text retains
empty page anchors for manual transcription; it is not published as usable text.
Each successful attachment validates and activates a new immutable editorial
snapshot; old snapshots remain available. The command prints the location of
regenerated, formatting-only `text.md` views for all attached project papers; the release
units remain authoritative. Originals are not served.

Once at least one PDF has been added, start the browser or MCP:

```sh
make web
make mcp
```

The website starts detached and remains available at
<http://127.0.0.1:8081>; repeat `make web` to check/start it and use
`make web-off` to stop it. Its PID and log are under `releases/`. The browser's
library page lists registered papers and their current extraction state.
Search, reader, JSON API and MCP use the same project evidence snapshot. The
website can also start before initialization so the project can be created in
Project settings; evidence search and MCP retrieval require an attached PDF.

### Read a whole retained document

In **Sources available to AI**, expand a PDF entry and select **Read whole
document**. The same link is available from a page's **Read source context**
view; **Library → Read retained documents** opens the source inventory.
All retained PDF page text is displayed in one scrollable view, without the
2048-character slices used by the page reader. Page navigation, exact citations,
OCR warnings, empty pages and stored truncation flags remain available.
The URL pins the evidence version, so a later correction does not change it.

AI clients can call `get_source_document` with the `source_id` from a passage
citation or `list_sources`. On registry endpoints, select one authorized evidence
collection and pin `snapshot_id`. Follow `next_offset` until null to read all
retained text; each slice includes exact page/unit provenance in `segments`.
The local JSON API exposes the same response at
`/api/document?source_id=…&collection=core&snapshot_id=…`, with optional
`offset` and `max_characters` (up to 8192).

The combined text is a reading view, not a new extraction: two newlines separate
pages, and the text inside each page is unchanged. Headers, footers, split words
and missing text are not guessed or repaired. An abrupt ending may be the old
page reader's slice boundary rather than missing extracted text. Check the next
slice/page and compare with the original before correcting a real extraction
gap. Existing attachments need no re-extraction. Use the paper's **PDF** tab to
compare its checksum-verified retained original when available.

To collect **metadata only** for direct references and citing works of the
registered core papers:

```sh
.venv/bin/research-project discover \
  --output releases/project/discovery-001
```

This queries OpenAlex and Crossref; it does not scan PDF bibliographies or
download additional PDFs. `neighborhood.json` retains provider records,
seed-to-paper edges, unstructured references, response provenance and
unresolved identifiers. `citation-edges.csv` provides one row per provider
edge, not a deduplicated publication list; spreadsheet-formula-like cells are
prefixed with an apostrophe. Exact provider data remains in `neighborhood.json`.
In the web app, choose **Manage library** in the sidebar (also linked from
**Project settings**). It is the home for discovery and saved searches,
the Core-paper inventory and removal, the awaiting-PDF attachment queue,
batch PDF selection/location/review, and metadata catalog updates.
Add/import Core papers, soft/factory reset and OpenAlex API-key settings remain
in **Project settings**. The **Library** table remains the browsing/filtering
view with contextual per-paper PDF/text actions.
Existing `/discovery` links and management POST endpoints continue to work.
**Find references** searches outgoing references
using OpenAlex and Crossref; **Find citing papers**
searches incoming citations using OpenAlex. Either works before PDFs are
attached. Each search runs in the background; its page automatically refreshes
every two seconds while running to show progress and then results, unresolved
counts and provider gaps. Only one search runs at a time.
Results and provider-response provenance persist under `PROJECT_ROOT/discovery/`,
including `citation-edges.csv`; reopening the app preserves completed searches.
**Library** lists both core papers and saved discovered
papers in a table, including searches completed before this feature was added.
Columns show **Title**, **DOI**, **Library groups**, **PDF / text status**,
**First author**, **Publication year**, and **Journal**, plus PDF/text actions.
Click a column heading to sort ascending, then click again for descending.
Use the filters under each heading together with the global search and click
**Apply filters**. Text filters match substrings without case sensitivity;
publication year requires an exact four-digit year. Filters combine, and both
filtering and sorting apply across the full library before pagination.
Pagination and batch selection preserve the current filters and sort order.
**Mark papers on this page for PDF location** selects eligible papers on the displayed sorted page;
**Mark all matching papers for PDF location** selects eligible papers across all matching pages.
Groups can overlap, including for Core papers. Metadata columns use saved
discovery records, show only the first listed author, and display a dash when
unknown; no metadata is inferred or fetched just to display the table.
Missing values sort last in either direction. On desktop the table fits the
available page width, wrapping long titles, identifiers and controls. On screens
900 pixels wide or smaller it scrolls horizontally without widening the page.

Click a **Title** to open the paper's in-app page (`/library/paper?id=...`).
The **DOI** column still opens the external DOI link. Entries without a DOI also
have a page, using their saved provider identity. Six server-rendered tabs work
with normal links and do not require JavaScript:

1. **Clear text** (default): the current extracted/editorial document with an
   **Edit** button at the upper right. There is no PDF-page navigation in the
   reading view. Retained structured tables appear inline after the text at their
   stored page location; exact within-page table positions are not retained.
   Figures/images are not currently stored as inline assets and must be checked
   in the original PDF. The editor retains page-boundary markers for citations,
   whole-document draft import, table correction and the correction audit.
   Saving from this view returns to the paper's Clear text tab.
2. **References**: known papers cited by this paper, linked to their own pages.
3. **Citing papers**: known papers citing this paper, linked to their own pages.
   Both lists use saved one-hop relationships only, not an exhaustive bibliography
   or proof of scientific support. Discovered papers' own citation neighborhoods
   are not automatically searched.
4. **PDF**: the attached original retained in this project, when available.
   Only a Library identity is accepted, never a user-supplied path. The file must
   match the attached source's checksum. Pages are rendered locally as lazy-loaded
   images so the tab works without a browser PDF plugin; an **Open local PDF** link
   also serves the original inline with byte-range support. Both use the existing
   local host/origin restrictions and no-store policy.
   No PDF is fetched from a provider just to view this tab,
   and MCP/evidence retrieval still does not serve original files.
5. **Technical information**: basic bibliography, all recorded authors,
   journal/year, provider metadata, source identity/provenance, extraction warnings,
   discovery coverage, correction history and Core-paper removal.
6. **Publish** / **Published**: publication controls live here, not below the
   reading view. An unpublished paper requires checked clear text, the manager's
   name, a reason and source-comparison confirmation before publication.
   **Published** shows who published the active version, when (UTC), why, and a
   snapshot-pinned link. If no matching historical audit is available, publisher
   and time are explicitly unknown. A newer draft does not change this tab's
   published status; it offers publication of the updated draft as well.
   **Unpublish** requires a name, reason and explicit confirmation and removes
   only this source from the current AI retrieval snapshot, keeping other papers,
   PDFs, editorial drafts and audit history. Stale snapshot forms are rejected.
   Old snapshot links and content already retrieved by AI clients are not erased;
   clients/servers using cached snapshots need a new retrieval session or restart.
   Publishing and unpublishing return to this tab.

The Clear text tab explicitly distinguishes drafts from published AI content and
links to the snapshot-pinned active RAG/MCP document. An unpublished draft is not
AI evidence; if a newer draft exists, AI still reads the previous published
version until the manager publishes the corrected text. Publication approves
transcription, not scientific findings.

For entries without extracted content, the page offers **Find PDF** and
identity/permission-confirmed attachment. Attaching from the page returns there
to read and check the draft; it does not promote a discovered paper to Core papers.
Core-paper removal still requires the existing preview and DOI confirmation.

Discovered
records are filtered and paginated in a local SQLite index (50 entries per page
by default; **Rows per page** offers 10, 25, 50, or 100). **Go to page** jumps
directly to a numbered page, retaining the current filters and sort order.
Changing the page size starts at page 1. Pages use the index rather than rereading
every provider graph on every visit. The index is derived private
data, rebuilt on first use and whenever saved graphs, core registrations or the
active evidence snapshot change. A first visit after such changes can take longer;
subsequent visits and pages reuse the index. No API requests are involved.
Paper tabs also read saved bibliography and citation connections from this index,
rather than rereading every discovery graph. Provider metadata is loaded only
for Technical information. Validated evidence snapshots are reused across tab
requests and loaded once per registry per request; backing-file integrity and
active-snapshot changes are still checked. The first visit after an index-format
or project-data change rebuilds the derived index and can take longer.
Discovered
records show **Awaiting PDF** (no attached PDF; no download attempted), or
**Metadata only — DOI unresolved** when no DOI is known. They remain separate
from the core library and are not available to AI as evidence. DOI matches are
combined across searches, while records without a DOI retain provider identity.
Search by title, DOI, first author, year, journal, group or status, and follow each record's search links for provenance
and coverage gaps. Core rows retain their actual extraction status.
**Connections and discovery searches** under each title explicitly says **Core paper [title] cites
this paper** for outgoing references, or **This paper cites core paper [title]**
for incoming citations. Paper titles and DOIs link to the DOI landing page.
Discovery links show the search type and timestamp, rather than a row-local
search number, and lead to the saved provider coverage and gaps.
Missing titles are filled from saved metadata with the same DOI when available.
References still lacking a title sort last in the default or title order; sparse provider deposits may contain
only a DOI or partial citation, which says nothing about PDF availability.
Interrupted searches are labelled as interrupted; start a new search rather than
assuming partial results are complete. Factory reset deletes these results and
is blocked while a search is running. Nothing is added to the core library and
no PDFs are downloaded. Core DOIs are sent to the metadata providers, not PDFs
or extracted text.

### Manage core papers

In **Project settings → Add core papers**, enter a DOI and title or import
additional CSV/JSON papers using the initialization formats. Validation is
atomic: duplicate DOIs or invalid rows reject the entire import. New core
papers await PDF attachment. Existing evidence and metadata catalogs are
preserved; rerun discovery to include new seeds.
Manage library keeps the attachment queue limited to papers still awaiting a
PDF. Attached papers remain registered in the collapsed, searchable core-paper
inventory, where they can still be reviewed or removed. Papers awaiting a PDF
can also be removed directly from the attachment queue.

To permanently remove a core paper:

1. Stop any running MCP servers so they cannot retain old evidence in memory.
2. In **Manage library → Core papers and PDFs**, click the compact **Remove…** button in that paper's entry to open
   the confirmation dialog (or a confirmation page when JavaScript is unavailable).
3. Review the affected relationship count and checksum-matched shared PDFs.
4. Type the full DOI and confirm. If project data changes after the preview,
   review a fresh preview.
5. Keep the web app running until the purge progress page reports completion.
   Restart MCP servers afterward.

Previews use a compact, automatically maintained index of saved core-to-paper
connections. Older searches build this index on first use; subsequent previews
do not reread full provider payloads. Full evidence validation and graph rebuilding
run only after confirmation, and changed search files invalidate the index.

The purge rebuilds and validates remaining current evidence before activation.
It removes the selected core record, its source/page units and project PDFs,
and discovered records with no remaining core-paper connection in either
direction. Shared relationships and remaining core PDFs/text are kept.
Connections are determined from saved provider results, which may omit real
references; this is not an exhaustive review of every bibliography. Matching
shared-folder PDF copies and their download records are deleted too; a PDF
checksum still used by a retained paper is protected.

Saved searches are filtered to remaining core seeds, CSV exports are
regenerated, and raw provider response caches are deleted because they can mix
removed and retained records. A configured metadata catalog is rebuilt.
Affected historical evidence snapshots, catalogs, and import artifacts are
removed; unrelated evidence-only snapshots are retained. Historical links to
removed snapshots stop working. Files outside application-managed project/PDF
folders, external backups, and citations within the original text of retained
papers are not erased or rewritten.

The app blocks other operations during a purge. Add/import/purge actions also
require discovery and metadata updates to finish. Symlinks and unexpected
project artifacts cause a refusal rather than an unsafe deletion. If cleanup
fails, the status page reports the error and any remaining recovery directory;
resolve it before another purge. This is permanent removal, not a reversible
archive action. Removing the last paper leaves an initialized empty project.
The installation's OpenAlex key is not deleted.

### Configure the publication metadata catalog

For incoming-citation searches beyond OpenAlex's 10,000-result numbered-page
limit, open **Project settings → OpenAlex API key**. Follow the links to create a
free OpenAlex account and copy its API key, then enter it and click **Save API key**.
Settings shows only whether a key is configured; you can replace or remove it.
Alternatively, put your OpenAlex API key in `openalex-api-key` beside the project root
(by default `releases/openalex-api-key`) and restrict its permissions to `600`.
It is installation-local, ignored by Git under `releases/`, and survives project
reset. With this file present, project discovery uses authenticated cursor
pagination with 100 results per page. The key is sent only to OpenAlex in an
authorization header, not stored in response URLs or discovery provenance.
Requests remain conservatively paced at about two per second.
Existing saved results are unchanged: start a new incoming-citation search,
then update the catalog. Provider coverage and unresolved references still
require review.

Discovery searches save results locally but do not automatically publish a
metadata catalog. **Literature coverage** shows summary counts and a metadata-quality
table, with publication breakdowns, additional counts, and search provenance
available on demand. These are provider-indexed metadata counts, not a measure
of exhaustive literature coverage or project-wide PDF availability.
To populate **Publication metadata** and **Literature coverage**:

1. Attach at least one PDF in **Manage library** or **Library**
   to create the registry.
2. Complete one or both searches on **Manage library**.
3. In **Manage library → Metadata catalog**, click **Configure/update metadata catalog**.
4. Repeat step 3 after additional searches to include their saved metadata.

Updates run in the background. A status page shows the current stage and
refreshes every two seconds, then reports completion or an error. You can leave
the page and return later. Duplicate clicks reuse the active update; reset and
new discovery searches are blocked until it finishes. If the app is stopped
mid-update, the status page reports interruption so you can retry.

The action combines the latest completed search for each direction and core DOI
set, deduplicates provider records,
and preserves response provenance, unresolved references and coverage caveats.
Older searches remain on disk but no longer keep a successful rerun marked
incomplete. Remaining provider failures and unresolved-reference counts are
included in the catalog coverage explanation.
Each incomplete project run displays **Why incomplete** directly below its
status, using reasons saved with that catalog, followed by suggested next steps.
A finished job is not a guarantee of complete provider coverage.
The next-step links open the newest search in the affected direction at
**What needs attention**. IDs missing from OpenAlex's batch results are checked
individually by the discovery pipeline. A matching direct response recovers the
Reference without expanding its own References. HTTP 404 is recorded as a broken
OpenAlex reference link; temporary lookup failures and mismatched returned IDs
are recorded separately, not called broken links or silently substituted.
The original relationship, batch lookup and direct lookup provenance are
preserved. For now, confirmed HTTP 404s are logged and ignored: they remain in
saved diagnostics but are excluded from unresolved-reference counts, attention
warnings and incomplete-catalog status. They are not identified papers or
PDF-search candidates. Other lookup failures and unverified IDs still require
attention. Ignoring broken links does not establish exhaustive bibliography coverage.
Crossref References with independent bibliographic information remain usable;
they are not guessed to be replacements for broken OpenAlex IDs.
Older saved searches show that individual verification has not been performed;
rerun **Find references** to use these checks, then update the metadata catalog.

Each diagnostic identifies the Core paper whose provider-listed References include the ID. These
are separate identities: the Core paper's OpenAlex link identifies the citing
paper, while the unavailable reference ID identifies a paper it cites. The
unavailable ID is not a replacement ID for the titled Core paper. These
are provider metadata gaps, not PDF or extraction errors. Provider links are
optional diagnostics and may themselves report "no works page"; that does not
provide a way to repair the missing record in this app. Inspect the Core paper's
bibliography independently if needed. An OpenAlex ID is not a bibliography entry
number; an unavailable record cannot be mapped to a specific entry from the ID
alone. Do not guess its title or DOI.
A retry button starts a fresh search in that direction; review the new search,
then update the catalog afterward. The old saved search does not change.
There is no manual reference-matching or "mark resolved" action. Attaching a PDF
does not clear the gap, and adding the paper to Core papers is not required.
Provider records may remain missing, so retrying is optional, not a guarantee;
available results remain usable. Rebuilding the metadata catalog applies the
current ignore policy to already-confirmed 404s without changing saved searches
or historical catalog snapshots.
Citation searches have no
publication-date filter; the UI labels their date range as unrestricted rather
than displaying the internal `1000-01-01` lower bound.
It performs no network requests and downloads no PDFs. Old registry/catalog
snapshots are retained; adding another core PDF preserves the active catalog.
The catalog is project-local and is removed by factory reset. An active search
must finish before catalog setup or reset.

The project web views label seed matches as **Core library overlap** and omit
legacy disease-specific fields, including those nested in discovery run details.
These display changes do not alter the stored provider metadata.
**Sources available to AI** supplements missing publication years from the
DOI-matched configured metadata catalog. Saved `*-downloads.json` records in
the shared PDF folder supply acquisition URLs, dates and version/rights notes
only when both DOI and PDF SHA-256 match. These display supplements are labelled;
retained evidence snapshots are not rewritten. A DOI landing page alone is not
treated as proof of the PDF download origin.

The website can browse the configured catalog immediately. To authorize it for
MCP clients too, stop and restart the MCP server:

```sh
make mcp COLLECTIONS="core metadata" DEFAULT_COLLECTIONS=core
```

Keep your existing `PROJECT_ROOT`, `PROFILE` and other overrides when applicable.
AI clients can explicitly request the `metadata` collection for bibliography;
the default remains core evidence. Metadata is not extracted paper text, and
configured catalog access does not make discovered papers available as evidence.

Records are provider occurrences, not a deduplicated
canonical bibliography. Inspect each seed's provider status and the reported
gaps; indexed references and citations are not guaranteed exhaustive. An
interrupted query can resume in the same output directory only while its core
list is unchanged. Choose a new output directory for a fresh snapshot.
If discovery finished but its CSV export failed, run `export-citations --output`
with that same output directory to generate the missing CSV.

This workflow does not implement a general replacement/versioning workflow for
an already attached DOI.
