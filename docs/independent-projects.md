# Independent projects

The project command creates a private `core` collection. It does not
automatically acquire PDFs, verify PDF-to-DOI
identity, or publish discovered citations to the browser or MCP metadata catalog.

Run `make setup` first. The default project directory is `releases/project/`,
which is ignored by Git. Original PDFs, extracted text, project metadata and
discovery responses can be private; if using `--root` or discovery output
elsewhere, ensure that location is private and ignored by version control.

Prepare a CSV with `doi` and `title` columns, then create the project:

```sh
.venv/bin/research-project init \
  --name "My research project" \
  --authorization "I am authorized to extract and retrieve these PDFs privately." \
  --papers /path/to/core-papers.csv
.venv/bin/research-project add \
  --doi 10.1234/example --title "Example paper"
.venv/bin/research-project status
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

The browser's library page lists registered papers and their current extraction
state. Search, reader, JSON API and MCP use the same core evidence snapshot.
An empty project has no evidence snapshot, so these servers cannot be started
until the first PDF has been successfully attached.

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
Records are provider occurrences, not a deduplicated
canonical bibliography. Inspect each seed's provider status and the reported
gaps; indexed references and citations are not guaranteed exhaustive. An
interrupted query can resume in the same output directory only while its core
list is unchanged. Choose a new output directory for a fresh snapshot.
If discovery finished but its CSV export failed, run `export-citations --output`
with that same output directory to generate the missing CSV.

This workflow does not implement a general replacement/versioning workflow for
an already attached DOI.
