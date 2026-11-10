# Independent projects

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

In **Project settings**, click **awaiting_pdf** in a paper's status cell to select
a local PDF from the shared `releases/pdf-data/` folder. Choose the file, confirm that
you checked the identity and private-use authorization, and submit. Files in
that folder are not served and survive project reset. To use another folder,
restart with `make web PDF_DATA_ROOT=/path/to/pdfs`. The selection excludes
symlinks and does not accept paths outside that folder.

Or use the CLI:

```sh
.venv/bin/research-project attach-pdf \
  --doi 10.1234/example --pdf /path/to/paper.pdf --confirm-identity
```

The identity confirmation records an operator assertion, **not an automatic
identity check**. `--license` optionally records the operator-supplied licence;
possession does not establish a licence or authorize redistribution. A PDF must
be readable and within the existing size and page limits. Text is extracted per
original page; pages without native text go through local Tesseract OCR, with
the usual not-human-verified and confidence caveats. Missing OCR dependencies,
unreadable PDFs and empty extraction are errors, not successful additions.
Each successful import validates and activates a new immutable collection
snapshot; old snapshots remain available. The command prints the location of
regenerated, formatting-only `text.md` views for the full core; the release
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
Search, reader, JSON API and MCP use the same core evidence snapshot. The
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
**Project settings**). **Find referenced papers** searches outgoing references
using OpenAlex and Crossref; **Find papers that reference my core library**
searches incoming citations using OpenAlex. Either works before PDFs are
attached. Each search runs in the background; refresh its page to see progress,
results, unresolved counts and provider gaps. Only one search runs at a time.
Results and provider-response provenance persist under `PROJECT_ROOT/discovery/`,
including `citation-edges.csv`; reopening the app preserves completed searches.
**Library & download status** lists both core papers and saved discovered
papers, including searches completed before this feature was added. Discovered
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
metadata catalog. To populate **Publication metadata** and **Discovery coverage**:

1. Attach at least one core PDF in **Project settings** to create the registry.
2. Complete one or both searches on **Find related papers**.
3. Open **Publication metadata** and click **Configure/update metadata catalog**.
4. Repeat step 3 after additional searches to include their saved metadata.

Updates run in the background. A status page shows the current stage and
refreshes every two seconds, then reports completion or an error. You can leave
the page and return later. Duplicate clicks reuse the active update; reset and
new discovery searches are blocked until it finishes. If the app is stopped
mid-update, the status page reports interruption so you can retry.

The action combines all completed saved searches, deduplicates provider records,
and preserves response provenance, unresolved references and coverage caveats.
It performs no network requests and downloads no PDFs. Old registry/catalog
snapshots are retained; adding another core PDF preserves the active catalog.
The catalog is project-local and is removed by factory reset. An active search
must finish before catalog setup or reset.

The project web views label seed matches as **Core library overlap** and omit
legacy disease-specific fields, including those nested in discovery run details.
These display changes do not alter the stored provider metadata.

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
