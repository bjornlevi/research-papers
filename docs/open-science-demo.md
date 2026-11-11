# Complete demo: open and reproducible research

This example exercises the project from paper registration through PDF
extraction and retrieval. The [core paper list](../examples/core-papers.csv)
contains six real DOI/title records. To make the demo work with just one PDF,
it uses an openly licensed paper from that list; the other five papers remain
registered as `awaiting_pdf`.

The demo stores its project, PDF, extracted text and snapshots under the
Git-ignored `releases/open-science-demo/` directory. Do not change this to a
tracked directory.

## Example core papers

The CSV contains the DOI and exact title for each registered paper:

| Title | DOI |
|---|---|
| Why Most Published Research Findings Are False | [10.1371/journal.pmed.0020124](https://doi.org/10.1371/journal.pmed.0020124) |
| Estimating the reproducibility of psychological science | [10.1126/science.aac4716](https://doi.org/10.1126/science.aac4716) |
| Promoting an open research culture | [10.1126/science.aab2374](https://doi.org/10.1126/science.aab2374) |
| A manifesto for reproducible science | [10.1038/s41562-016-0021](https://doi.org/10.1038/s41562-016-0021) |
| How open science helps researchers succeed | [10.7554/eLife.16800](https://doi.org/10.7554/eLife.16800) |
| The FAIR Guiding Principles for scientific data management and stewardship | [10.1038/sdata.2016.18](https://doi.org/10.1038/sdata.2016.18) |

### Attaching local copies through project settings

In **Project settings**, click **awaiting_pdf** in the paper's status cell to
choose a matching PDF from the shared `releases/pdf-data/` folder. Check its
identity and private-use authorization, then attach and extract.
This shared folder survives project reset. Imported copies and evidence inside
the project root do not. Use `PDF_DATA_ROOT=/path/to/pdfs` with `make web`
after stopping the website to choose another folder.

Being about open science does not establish an open license. Publicly accessible
repository copies are available for the two Science papers, but may be accepted
manuscripts with different pagination and retained copyright restrictions:

- [Estimating reproducibility: University of Dundee repository](https://discovery.dundee.ac.uk/ws/files/7385883/RPP_SCIENCE_2015.pdf)
  supplies a peer-reviewed version with a repository cover and additional material.
- [Promoting an open research culture: UNC repository record](https://cdr.lib.unc.edu/concern/articles/9593v607g)
  supplies a manuscript; do not infer a Creative Commons license.
- [A manifesto for reproducible science: Oxford repository](https://ora.ox.ac.uk/objects/uuid:2dcc8584-7b1f-453d-822d-0893aec8428f)
  and [FAIR principles: Leiden repository](https://scholarlypublications.universiteitleiden.nl/handle/1887/46904)
  provide copies carrying CC BY 4.0 notices.

Use the PDF's actual page locations for citations, not publisher pagination
assumed from the DOI. No attachment is automatic: the operator must confirm
the selected file and rights.

## 1. Set up the environment

From the repository root:

```sh
make setup
```

## 2. Get the demo paper

The example paper is Erin C. McKiernan et al., **“How open science helps
researchers succeed”**:

- DOI: [10.7554/eLife.16800](https://doi.org/10.7554/eLife.16800)
- [eLife article page](https://elifesciences.org/articles/16800)
- [Publisher PDF](https://cdn.elifesciences.org/articles/16800/elife-16800-v2.pdf)
- License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

The eLife article metadata identifies this DOI, title, PDF, and CC BY 4.0
license. Download the publisher's PDF to a local, private location:

```sh
mkdir -p "$HOME/Downloads"
curl -fL \
  https://cdn.elifesciences.org/articles/16800/elife-16800-v2.pdf \
  -o "$HOME/Downloads/how-open-science-helps-researchers-succeed.pdf"
```

Open the downloaded PDF and confirm that it is the paper above. The
`--confirm-identity` flag below records your confirmation; the software does
not independently compare the PDF with the DOI. Only continue if the license
and your intended private use are appropriate.

## 3. Initialize the project from the example list

The authorization string is an example of the basis for using this particular
CC BY 4.0 paper. Review it and use it only if it accurately describes your
rights and intended use:

```sh
make project-init PROJECT_ROOT=releases/open-science-demo \
  PROJECT_NAME="Open and reproducible research demo" \
  PROJECT_AUTHORIZATION="CC BY 4.0 license for the attached eLife paper; private local extraction and retrieval with attribution." \
  PROJECT_PAPERS=examples/core-papers.csv
```

This registers all six papers. Check the initial state:

```sh
make project-status PROJECT_ROOT=releases/open-science-demo
```

All six should be `awaiting_pdf` before attaching the sample PDF.

## 4. Attach the licensed PDF and extract its text

```sh
.venv/bin/research-project --root releases/open-science-demo attach-pdf \
  --doi 10.7554/eLife.16800 \
  --pdf "$HOME/Downloads/how-open-science-helps-researchers-succeed.pdf" \
  --confirm-identity \
  --license "CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)"
```

Check the result:

```sh
.venv/bin/research-project --root releases/open-science-demo status
```

The attached paper should be `extracted` (or indicate an OCR/visual-review
caveat); the other five should remain `awaiting_pdf`. The project creates a
source-linked snapshot and formatting-only text views. The original PDF stays
local and is not served as a file; extracted passages are available through
the configured source-linked retrieval services.

## 5. Try search in the browser

Start the browser and JSON API:

```sh
make web PROJECT_ROOT=releases/open-science-demo \
  COLLECTION_REGISTRY=releases/open-science-demo/registry
```

Open <http://127.0.0.1:8081/search> and search for `open research`. Open a
result to inspect its retained page context and citation. The website runs
detached until stopped with `make web-off`, using the same project and registry
overrides.

## 6. Optional: try MCP retrieval

In another terminal, start the authenticated MCP HTTP service against the same
demo collection:

```sh
make mcp COLLECTION_REGISTRY=releases/open-science-demo/registry
```

Follow [the AI connection guide](connect-ai.md) to connect a client. For a
retrieval check independent of an AI model, use `make connect` as described
there and run:

```sh
.venv/bin/research-mcp-check \
  --url http://127.0.0.1:8000/mcp \
  --web-url http://127.0.0.1:8081 \
  --token-file releases/http-local/research-papers-claude-token \
  --collection core --query "open research"
```

The check should report matching source context from MCP, the browser, and the
JSON API.

## 7. Optional: discover cited-literature metadata

Citation discovery queries OpenAlex and Crossref for provider-indexed
references and citing works. It is metadata-only, requires network access, and
does not download PDFs. Coverage is not exhaustive:

```sh
.venv/bin/research-project --root releases/open-science-demo discover \
  --output releases/open-science-demo/discovery-001
```

Inspect the saved provider statuses and gaps rather than treating the results
as a complete bibliography. Discovery output is also private project data.

## Resetting or adapting the example

Initialization refuses to overwrite an existing project. To try the workflow
again, use a fresh `--root` directory and update the registry path in the
`make web` and `make mcp` commands to match it.

For your own research, copy the example CSV and replace the DOI/title rows.
Supply only PDFs that match those records and that you are authorized to
process. Do not commit project files, original PDFs, extracted text, local
settings or access tokens.
