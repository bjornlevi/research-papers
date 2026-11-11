# Research library terminology

Use these terms in the app, documentation, AI prompts and explanations:

- **Core papers**: explicitly selected papers defining the project's discovery seeds.
- **References**: papers cited by Core papers (outgoing citations).
- **Citing papers**: papers that cite Core papers (incoming citations).
- **All papers**: the combined library.
- Discovery actions: **Find references** and **Find citing papers**.

Groups may overlap. Being a Reference or Citing paper does not imply full-text
availability, scientific support or validation. Attaching/extracting a paper
does not add it to Core papers.

Avoid alternate category labels such as background, impact, referenced by core,
or cites core. Preserve existing storage/API identifiers: `core` means Core
papers, `background` means References, and `impact` means Citing papers.
The internal `core` evidence collection includes extracted non-core papers too;
the project manifest, not collection membership, defines Core papers.
