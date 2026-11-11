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

Settings also offers **Reset to factory settings**. Typing `RESET PROJECT`
confirms permanent deletion of everything inside the configured project root,
including PDFs, extracted evidence, snapshots, discovery output and settings.
Files outside that root are not affected.

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

In **Project settings**, click **awaiting_pdf** in a paper's status to select
a local PDF from the shared `releases/pdf-data/` folder. Choose the file, confirm that
you checked the identity and private-use authorization, and submit. Files in
that folder are not served and survive project reset. To use another folder,
restart with `make web PDF_DATA_ROOT=/path/to/pdfs`. The selection excludes
symlinks and does not accept paths outside that folder.

For a paper awaiting PDF, **Find PDF** searches OpenAlex for a location with an
explicit Creative Commons/public-domain license and tries downloading it in the
background. The progress/result page shows failures or the saved filename and
acquisition URL. This uses the installation's OpenAlex key when configured and
may encounter provider quotas. It does not bypass access controls or infer rights
from free-to-read status. A missing result is not proof that no copy exists.
Successful PDFs and DOI/checksum-bound provenance (including year, license and
download URL) are saved in the PDF data folder. Nothing is attached or extracted
automatically: inspect the file locally, select it, and confirm identity and
private-use authorization as above.
Downloaded filenames use the paper title, for example
`a-manifesto-for-reproducible-science.pdf`. A short checksum suffix distinguishes
different PDFs with the same title; existing files are never overwritten.

### View and correct extracted data

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
Find related papers presents coverage as a limitations list and per-core-paper
provider summaries, with reported/retrieved counts and errors. Technical provider
details remain expandable rather than appearing as raw JSON.

For an extracted paper in **Library** or **Sources available to AI**, click
**View and correct extracted data**. The reader also links directly to the
current version of its retained page. This private-project editor provides:

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

Inspect the original PDF locally, enter your name and correction reason, and
confirm you checked the changes. **Save corrected version** validates and stages
a new private evidence snapshot before activating it. Search, reader, source
metadata, JSON API and MCP then use the corrected active version. Changed page
text/tables are labelled operator-reviewed, not independently scientifically
validated; existing extraction/OCR caveats are retained. The immutable source
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
or purge. Original PDFs are not served in the app.

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
  and activates a new version with the same correction audit as other edits.

Reviews and problems are saved locally, bound to the source/extraction identity,
without changing evidence text. Corrections preserve earlier versions and
require a fresh quality check; they do not imply scientific validation.
Stale forms and requests made while project-changing jobs run are rejected.
Originals are never served to the browser. Text editing requires private
project mode; review, problem reporting, and uploaded-original inventory are
also available for development evidence collections.

### Batch PDF location and extraction

In **Library**, filter by relationship and PDF/extraction status, then switch
**Locate PDF: off/on** for individual papers. These are toggle buttons, not
identity/permission confirmations. Toggles update in place without navigating or
changing scroll position (ordinary form navigation is retained without JavaScript).
**Turn on this page** or **Turn on all matching
papers** selects eligible records across pages; **Turn all off** clears the
selection. Only records with a title and DOI awaiting PDF are eligible.

**Locate selected PDFs** starts a sequential, rate-limited background job. The
**Review batch results and confirm extraction** page shows persistent per-paper
results, acquisition links, license/year/version metadata and errors. A PDF can
also be selected from the local data folder, including when online location
failed. Inspect each file locally and individually confirm its title/DOI and
private-use authorization. **Attach and extract confirmed PDFs on this page**
queues only those explicitly confirmed PDFs, with their checksums bound to the
confirmation. Review pages contain 25 papers; unconfirmed papers are not extracted.
Discovered papers remain outside the core seed list.

**Pause after current paper** finishes the current item before stopping.
Selection and results are saved in private `batch-pdf.json` within the project.
After a pause or app restart, **Locate selected / resume / retry missing PDFs**
reuses completed downloads and retries selected missing/failed entries. Provider
metadata errors pause the queue so remaining papers are not repeatedly attempted
after a quota error. Resume only after the provider permits it; daily resets are
not scheduled automatically. Interrupted or failed extraction requires fresh
confirmation, and already activated evidence is not extracted again.
Other project mutations are blocked while a batch is running.

The same **Find PDF** and **Select PDF and attach/extract** controls are available
on **Library** for core and discovered papers with a resolved
DOI and title. Discovered papers retain their relationship categories after extraction: they do not
become core papers or seeds for further citation expansion. Library categories
show **References** (a core paper cites it), **Citing papers** (it cites a
core paper), or both. Their extracted text
is available in Sources available to AI, search, reader, JSON API and MCP.
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
unreadable PDFs and empty extraction are errors, not successful additions.
Each successful import validates and activates a new immutable collection
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
In the web app, choose **Find related papers** in the sidebar (also linked from
**Project settings**). **Find references** searches outgoing references
using OpenAlex and Crossref; **Find citing papers**
searches incoming citations using OpenAlex. Either works before PDFs are
attached. Each search runs in the background; refresh its page to see progress,
results, unresolved counts and provider gaps. Only one search runs at a time.
Results and provider-response provenance persist under `PROJECT_ROOT/discovery/`,
including `citation-edges.csv`; reopening the app preserves completed searches.
**Library** lists both core papers and saved discovered
papers, including searches completed before this feature was added. Discovered
records are filtered and paginated in a local SQLite index (50 entries per page),
not by rereading every provider graph on every visit. The index is derived private
data, rebuilt on first use and whenever saved graphs, core registrations or the
active evidence snapshot change. A first visit after such changes can take longer;
subsequent visits and pages reuse the index. No API requests are involved.
Discovered
records show **Awaiting PDF** (no attached PDF; no download attempted), or
**Metadata only — DOI unresolved** when no DOI is known. They remain separate
from the core library and are not available to AI as evidence. DOI matches are
combined across searches, while records without a DOI retain provider identity.
Search by title or DOI, and follow each record's search links for provenance
and coverage gaps. Core rows retain their actual extraction status.
**Connections to your core papers** explicitly says **Core paper [title] cites
this paper** for outgoing references, or **This paper cites core paper [title]**
for incoming citations. Paper titles and DOIs link to the DOI landing page.
Discovery links show the search type and timestamp, rather than a row-local
search number, and lead to the saved provider coverage and gaps.
Missing titles are filled from saved metadata with the same DOI when available.
References still lacking a title sort last; sparse provider deposits may contain
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

To permanently remove a core paper:

1. Stop any running MCP servers so they cannot retain old evidence in memory.
2. Click the compact **Remove…** button in that paper's settings entry to open
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

1. Attach at least one PDF in **Project settings** or **Library**
   to create the registry.
2. Complete one or both searches on **Find related papers**.
3. Open **Publication metadata** and click **Configure/update metadata catalog**.
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
**What needs attention**. Unresolved OpenAlex IDs link to provider records and
identify the core paper whose bibliography contains the reference. A retry
button starts a fresh search in that direction; update the catalog afterward.
Provider records may remain missing, so retrying is optional, not a guarantee.
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
