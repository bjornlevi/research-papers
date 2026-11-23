---
name: configure-paper-rag
description: 'Configure and evaluate a paper’s private RAG/MCP passages, editorial search terms, and retrieval benchmark checks.'
argument-hint: 'Published paper DOI, title, or local library URL'
---

# Configure paper RAG/MCP retrieval

Use this skill when asked to improve a paper's searchable passages, search
terms, or retrieval checks. Prepare and verify clear text first with
[clean-paper-clear-text](../clean-paper-clear-text/SKILL.md). RAG/MCP retrieves
published clear text, not an unpublished editorial draft.

## Workflow

1. **Confirm the retrieval source.** Identify the exact project, paper, DOI,
   published snapshot, and current retrieval mode. Check for an existing
   private RAG draft and saved checks before editing. If clear text is not
   published, explain that it must be published before RAG configuration can
   use it; do not bypass the publication boundary.
2. **Establish the baseline.** Review the paper's numbered source blocks and
   inspect relevant pages in the local PDF. Run representative queries against
   live retrieval and record which evidence appears in the first five results.
   Do not assume block numbers from an older snapshot still refer to the same
   text.
3. **Design passages around evidence.**
   - Start from the app's suggestions and use whole pages if custom passages
     do not provide a measurable improvement.
   - Keep findings with the population, comparison, outcome, and qualifications
     needed to interpret them. Keep table headers, rows, units, denominators,
     and footnotes together; keep figure labels and shared captions accessible.
   - Use section boundaries and paragraph meaning rather than arbitrary token
     counts. Preserve continuous coverage of the document when using numbered
     ranges; avoid gaps and accidental separation of notes from the evidence
     they qualify.
   - PDF pages remain citation anchors. A passage may span pages; verify all
     relevant page citations and source context.
4. **Add editorial search terms only where useful.** Use the RAG/MCP editor's
   extra-search-terms field for accurate synonyms, alternate wording, or
   concise descriptions that help readers find the printed evidence. These
   are search metadata, not author text: never insert them into the clear text
   or present them as a quotation. Preserve existing terms when changing
   ranges; review any terms the app cannot match to the new passage.
   Use the word `frequency` only for passages that report how common something
   was, so frequency questions can find relevant evidence.
5. **Create paper-grounded benchmark checks.** Include varied questions about
   the main finding, population/denominator, tables or figures, methods,
   limitations, and useful paraphrases. For each check, provide the exact PDF
   page and a short exact excerpt from the published text. Include a frequency
   question when the paper reports counts or proportions. Do not use a query
   whose excerpt is not present on its selected page.
6. **Preview and compare.** Run each saved check for both live retrieval and
   the proposed draft. Inspect result order, source excerpts, page citations,
   table notes, and relevant context—not just whether a passage appears.
   Compare with the baseline and adjust boundaries or labels only to address
   observed misses. A rank check evaluates retrieval, not generated-answer
   correctness.
7. **Save privately and verify.** Save the RAG configuration as a private
   draft by default. Reload the tab and confirm ranges, labels, and checks
   persist, while the active/live retrieval snapshot is unchanged. Apply to
   live search only when the user explicitly requests activation. After an
   authorized activation, verify both search and MCP use the updated snapshot.

## Reporting

Summarize what changed and report benchmark outcomes precisely (for example,
expected evidence in the top five for *n* of *N* checks). Distinguish:

- **retrieval quality**: whether the right passage and source context rank well;
- **answer quality**: whether a generated response is supported and correctly
  cited.

Do not claim answer quality, scientific validation, or live activation based
only on a successful preview. For numerical evidence, verify the value,
population, denominator, units, and qualifiers against the source. Preserve
distinctions such as **Core papers**, **References**, **Citing papers**, and
**All papers**; library group membership is not evidence that a paper's full
text is available or supports a claim.

## Related documentation

- [RAG/MCP editor workflow](../../../docs/independent-projects.md)
- [Research-paper retrieval design and evaluation](../../../docs/architecture.md)
- [MCP retrieval and passage-context contract](../../../docs/contracts.md)
