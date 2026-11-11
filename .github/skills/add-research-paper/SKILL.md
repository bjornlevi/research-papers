---
name: add-research-paper
description: 'Add research papers or PDFs to a private project library, extract or OCR pages, update a core paper list, or discover related citations.'
argument-hint: 'DOI or title of the paper to add (optional)'
---

# Add a research paper

First determine which project and registry the operator intends to change.
Do not assume the historical iStopMM collection. For a new independent
project, follow [the project guide](../../../docs/independent-projects.md).

Use the project's vocabulary consistently:
- **Core papers**: papers explicitly selected as the project's discovery seeds.
- **References**: papers cited by Core papers (outgoing citations).
- **Citing papers**: papers that cite Core papers (incoming citations).
- **All papers**: the combined library, not just Core papers.

Use **Find references** and **Find citing papers** for discovery actions.
A paper can belong to multiple groups. Discovery is metadata-only until a PDF
is attached and extracted; extraction never promotes a paper to Core papers.
Do not use background/impact or referenced-by-core/cites-core as display names.
Existing `background` and `impact` identifiers still mean References and Citing
papers respectively; do not rename persisted fields or API parameters.

Register a DOI and title with `research-project add`, or import a CSV. A paper
without a supplied PDF remains awaiting PDF. For an operator-provided copy,
verify with the operator that the PDF matches the registered title and DOI and
that private extraction is authorized before using
`research-project attach-pdf --confirm-identity`. This confirmation is not
independent identity verification. Do not circumvent publisher access controls.

The project workflow creates immutable checksum-bound source/page units and
retains old snapshots. Do not hand-edit releases or reformat unit text.
Originals are not served; OCR is labelled not human verified. Check status and
verify search and page-context retrieval after adding a source.

Citation discovery is metadata-only and provider-indexed; preserve failures,
unresolved references and coverage caveats. Never imply complete coverage.
Original PDFs, extracted text and project data are private by default; do not
commit them or infer redistribution rights.

The historical iStopMM workspace uses a separate legacy update and
provided-PDF workflow. Its existing rules and source scope still apply there;
consult its workspace documentation rather than using legacy commands on an
independent project.
