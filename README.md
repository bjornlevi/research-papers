# Research evidence library

Build a private, source-linked research library from your own core papers.
Search extracted PDF pages in a browser, through a read-only JSON API, or
through an MCP client. Papers, extracts and project data are not included in
the repository and are not served as original files.

## Start a project

Install Python 3, then create the local environment:

```sh
make setup
```

Prepare a CSV containing at least `doi` and `title` columns, one paper per row.
See [the example](examples/core-papers.csv). Then initialize the project with
your own list and your basis for private extraction:

```sh
.venv/bin/research-project init --name "My research project" \
  --authorization "I am authorized to extract and retrieve these PDFs privately." \
  --papers /path/to/my-core-papers.csv
.venv/bin/research-project status
```

Registered papers start as **awaiting PDF**. After checking that a PDF matches
the title and DOI and that you can use it privately, add it:

```sh
.venv/bin/research-project attach-pdf --doi 10.1234/example \
  --pdf /path/to/paper.pdf --confirm-identity
```

The identity confirmation is an operator assertion, not an automatic check.
Repeat for other papers. Pages without usable native text are processed with
local Tesseract OCR when available; OCR is labelled as not human verified.
After the first successful addition:

```sh
make web    # browser and JSON API, http://127.0.0.1:8081
make mcp    # authenticated MCP server, in another terminal
```

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

The software originated in an iStopMM research project; some internal package
and command names retain that name for compatibility. This repository does not
contain that project's papers or research data.

## Development

Run `make test` for portable tests and `make test-project` for the
independent-project ingestion and citation tests. Further implementation and
deployment references are in [docs](docs/).
