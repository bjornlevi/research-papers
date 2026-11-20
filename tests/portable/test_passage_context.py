import unittest

from research_library import passage_context


class PassageContextTests(unittest.TestCase):
    def test_reading_parts_preserve_table_notes_and_figure_boundaries(self):
        table = ("| Target | n |\n| --- | --- |\n| Example | 7 |\n\n"
                 "**Note:** Test.\n\n**a** Prior study.\n\nUnrelated finding.")
        figure = "**Figure 1** Examples. **(A)** First. **(B)** Second.\n\nUnrelated finding."
        for kind, text, expected in (
            ("table", table, ["table_header", "table_row", "table_notes"]),
            ("figure", figure, ["figure_caption", "figure_intro", "figure_panel", "figure_panel"]),
        ):
            parts = passage_context.reading_parts(text, {"elements": [{"kind": kind, "role": "label"}],
                                                        "section_paths": []})
            self.assertEqual([part["kind"] for part in parts], expected)
            self.assertNotIn("Unrelated", text[parts[-1]["start"]:parts[-1]["end"]])

    def test_detached_figure_caption_and_qualified_table_row(self):
        figure = "**Figure 2**. Distribution by group."
        table = ("## Table 1. Characteristics\n\n| Measure | Value |\n| --- | --- |\n"
                 "| Median age (range), y | 73 (41-92) |\n\n**Note:** Years.")
        pages = passage_context.blocks([
            {"id": "p1", "text": "## Key points\n\nSummary."},
            {"id": "p2", "text": figure},
            {"id": "p3", "text": table},
        ])
        for page, text, query, section, expected in (
            ("p2", figure, "What does Figure 2 show?", "Figure 2", "Distribution by group."),
            ("p3", table, "Table 1 median age", "Table 1", "**Note:** Years."),
        ):
            metadata = passage_context.describe(None, pages[page])
            self.assertEqual(metadata["section_paths"], [[section]])
            _, evidence = passage_context.exact_evidence(
                text, metadata, passage_context.explicit_target(query, "plain"))[0]
            self.assertIn(expected, evidence["text"])
        self.assertEqual(passage_context.exact_evidence(figure, passage_context.describe(None, pages["p2"]),
            passage_context.explicit_target("Figure 2Z", "plain")), [])

    def test_explicit_structure_and_annotation_roles(self):
        pages = passage_context.blocks([
            {"id": "p1", "text": "# Example.\n\nNo heading here.\n\n## Results\n\n### Follow-up\n\n"
             "#### Table 2: Measurements\n\n```\n# Not a section\nTable 3: Not a caption\n```"},
            {"id": "p2", "text": "**Figure 1 description:** Added context.\n\n"
             "Figure 4 shows the trend.\n\n## References\n\nOne citation."},
        ], title="Example")
        self.assertEqual(passage_context.describe(None, pages["p1"], 1, 1),
                         {"section_paths": [], "elements": [], "block_range": {"first": 1, "last": 1}})
        self.assertEqual(passage_context.describe(None, pages["p1"])["elements"],
                         [{"kind": "table", "label": "Table 2", "role": "label"}])
        self.assertEqual(passage_context.describe("Example", pages["p2"]), {
            "paper_title": "Example",
            "section_paths": [["Results", "Follow-up"], ["References"]],
            "elements": [{"kind": "figure", "label": "Figure 1", "role": "description"}],
        })
