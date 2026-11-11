# Research Library

Build a private, source-linked research library from your own core papers.
Search extracted PDF pages in a browser, through a read-only JSON API, or
through an MCP client. Papers, extracts and project data are not included in
the repository and are not served as original files.

## Start a project

Install Python 3, then create the local environment:

```sh
make setup
```

Prepare a CSV containing at least `doi` and `title` columns, one paper per row,
or a JSON manifest exported by this project.
The [example list](examples/core-papers.csv) is a real, small illustrative
starter set about open and reproducible research, not a systematic review.
Follow the [complete demo](docs/open-science-demo.md) to register all six
papers and test PDF extraction, browser search, and MCP retrieval using one
openly licensed paper. The CSV contains metadata only; the demo guide explains
how to get the paper and record your use authorization.

Start the local website and open **Project settings** to initialize your
project:

```sh
make web
```

Open <http://127.0.0.1:8081/settings>, enter a project name and authorization
basis, and select the core-papers CSV or JSON file. The example authorization
statement is prefilled and editable; it records your statement, not verified
rights. The web app runs detached, with its PID and log under `releases/`.
Stop it with `make web-off`. The project is stored under `releases/project/`
by default. Use `PROJECT_ROOT=/path/to/project` consistently with both make
commands to select another project.

Project settings also includes **Reset to factory settings**. After typing
`RESET PROJECT` to confirm, it permanently removes everything inside the
configured project root, including PDFs, extracted evidence, snapshots,
discovery output, and settings. It cannot be undone.

Registered papers start as **awaiting PDF**. After checking that a PDF matches
the title and DOI and that you can use it privately, put it in
`releases/pdf-data/` and click **awaiting_pdf** in that paper's status in
Project settings. Select the matching local file, confirm its identity and authorization,
then click **Attach and extract PDF**. This shared folder is preserved by project
reset; imported project copies and extracted evidence are not. Override the
folder with `make web PDF_DATA_ROOT=/path/to/pdfs` (restart the web app first).
PDFs are never served as files.

Alternatively, click **Find PDF** for a core paper awaiting PDF. It looks for an
explicitly licensed open-access copy and downloads it, with acquisition provenance,
to the PDF data folder. Review the progress/result page, inspect the PDF locally,
then select and confirm it before extraction; finding a PDF does not attach it.
These controls are also available on **Library** for discovered
papers with a DOI and title. Extracting a discovered paper makes its text
available to AI without adding it to core or expanding discovery seeds.
For batches, use the per-paper **Locate PDF: off/on** buttons or select the page/all
matching papers, then **Locate selected PDFs**. Review the saved results and
individually confirm PDFs before **Attach and extract confirmed PDFs on this page**.
Relationship/status filters, background progress, pause/resume and failed-item
retries are available in the [batch workflow](docs/independent-projects.md#batch-pdf-location-and-extraction).

For extracted papers, **View and correct extracted data** opens a page-by-page
editor for text/OCR, paper metadata and structured tables. Saving requires your
name, a reason and confirmation against the original PDF; it creates a new
version for search/AI while preserving the original and a before/after audit.
See [correcting extracted data](docs/independent-projects.md#view-and-correct-extracted-data).
New PDF extractions automatically format native text into column-aware
paragraphs while retaining raw text in private extraction checkpoints.

Choose **Find related papers** in the sidebar for **Find references** or
**Find citing papers**. Searches work before attaching
PDFs, run in the background, and save metadata results with provider coverage
and gaps. They do not download PDFs or add papers to your core library.
Saved discoveries appear alongside core papers in **Library**,
labelled **References**, **Citing papers**, or both, with their PDF/metadata
status and links to the searches.
To populate **Publication metadata**, follow the setup instructions on that page
and click **Configure/update metadata catalog** after discovery completes.
Repeat after new searches; attaching additional core PDFs preserves the catalog.

Use **Project settings → Add core papers** to enter a DOI/title or import more
papers from CSV/JSON. Each core entry also has **Remove…**: review its
deletion preview and type the DOI to confirm. This purges that paper, exclusive
discovered relationships, matching PDFs (including shared-folder copies), and
affected historical data while preserving remaining core evidence and shared
connections. Stop MCP servers first; see the [purge details](docs/independent-projects.md#manage-core-papers).

Alternatively, add a PDF from the terminal:

```sh
.venv/bin/research-project attach-pdf --doi 10.1234/example \
  --pdf /path/to/paper.pdf --confirm-identity
```

The identity confirmation is an operator assertion, not an automatic check.
Repeat for other papers. Pages without usable native text are processed with
local Tesseract OCR when available; OCR is labelled as not human verified.
After the first successful addition:

```sh
make mcp    # authenticated MCP server, in another terminal
```

The website remains available at <http://127.0.0.1:8081> while running;
`make web` is safe to repeat and `make web-off` stops it.

Use `make connect` to configure the local Claude Code client, or see
[AI connection guidance](docs/connect-ai.md). The web reader and MCP return
page-level citations with source checksums and retained snapshot IDs.

For further commands, adding papers later, citation discovery and limitations,
see [independent projects](docs/independent-projects.md). Additional cited
literature discovery is metadata-only and provider-indexed; it is not an
exhaustive bibliography and does not download additional papers.

## Privacy and limitations

Project data defaults to the Git-ignored `releases/project/` directory. Do not
commit PDFs, extracted text, private bibliographies, tokens or local settings.
Possessing a PDF does not authorize redistribution. Public deployment requires
separate rights and security review. This tool does not verify scientific
claims, resolve PDF identities automatically, or replace researcher review.

The software originated in an iStopMM research project. This repository does not
contain that project's papers or research data. Historical collection identifiers
and saved metadata fields retain their original names for data compatibility.

## Package and commands

The Python distribution is **research-library**, imported as `research_library`.
CLI commands include `research-library`, `research-project`, `research-mcp`,
`research-http`, `research-web`, `research-build`, `research-release`,
`research-collections`, `research-literature`, and `research-mcp-check`.
The Make commands remain the recommended interface.

After upgrading from the old package, stop any running servers, uninstall
`istopmm-evidence` from your environment, then run `make setup` to install the
renamed package and commands. Update external launch configurations to use
`research_library` instead of `istopmm_evidence`, and `research-*` commands
instead of `istopmm-*` (the main CLI is `research-library`). Restart MCP clients
and servers. Project files, snapshots, source checksums and collection IDs do
not need migration. Container deployment variables now use the
`RESEARCH_LIBRARY_` prefix.

## Development

Run `make test` for portable tests and `make test-project` for the
independent-project ingestion and citation tests. Further implementation and
deployment references are in [docs](docs/).
