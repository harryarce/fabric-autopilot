"""Tests for the deterministic natural-language → DAX translator."""

from __future__ import annotations

import unittest

from app.intelligence import nl_to_dax
from app.intelligence.nl_to_dax import NlToDaxError
from app.intelligence.spec import (
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticTable,
)


def _model() -> SemanticModelSpec:
    return SemanticModelSpec(
        name="Sales",
        tables=[
            SemanticTable(
                name="Customer",
                source_schema="dbo",
                source_table="DimCustomer",
                columns=[
                    SemanticColumn(
                        name="CustomerKey",
                        source_column="CustomerKey",
                        data_type="int64",
                        is_key=True,
                    ),
                    SemanticColumn(
                        name="Name",
                        source_column="CustomerName",
                        data_type="string",
                    ),
                    SemanticColumn(
                        name="Country",
                        source_column="Country",
                        data_type="string",
                    ),
                ],
            ),
            SemanticTable(
                name="Sales",
                source_schema="dbo",
                source_table="FactSales",
                columns=[
                    SemanticColumn(
                        name="SalesAmount",
                        source_column="SalesAmount",
                        data_type="decimal",
                        summarize_by="sum",
                    ),
                    SemanticColumn(
                        name="OrderQuantity",
                        source_column="OrderQuantity",
                        data_type="int64",
                        summarize_by="sum",
                    ),
                ],
                measures=[
                    SemanticMeasure(
                        name="Total Sales",
                        expression="SUM(Sales[SalesAmount])",
                    ),
                ],
            ),
        ],
    )


class NlToDaxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = _model()

    # -- intents -----------------------------------------------------------

    def test_how_many_question_emits_countrows(self) -> None:
        result = nl_to_dax("How many customers do we have?", self.spec)
        self.assertEqual(result.intent, "rowcount")
        self.assertIn("COUNTROWS(", result.dax)
        self.assertIn("'Customer'", result.dax)
        self.assertIn("Customer", result.referenced_tables)

    def test_top_n_by_column_emits_topn_with_sort(self) -> None:
        result = nl_to_dax(
            "Top 5 customers by SalesAmount", self.spec
        )
        # A measure called "Total Sales" is not mentioned, so the heuristic
        # falls back to a TOPN sorted on the SalesAmount column.
        self.assertIn(result.intent, {"topn", "summarize"})
        self.assertIn("TOPN(", result.dax)
        self.assertIn("DESC", result.dax)

    def test_measure_mention_summarizes_by_grouping_column(self) -> None:
        result = nl_to_dax(
            "Total Sales by Country", self.spec
        )
        self.assertEqual(result.intent, "summarize")
        self.assertIn("SUMMARIZECOLUMNS(", result.dax)
        self.assertIn("'Customer'[Country]", result.dax)
        self.assertIn("[Total Sales]", result.dax)
        self.assertIn("Total Sales", result.referenced_measures)

    def test_aggregate_verb_on_column_emits_row(self) -> None:
        result = nl_to_dax(
            "What is the average OrderQuantity?", self.spec
        )
        self.assertEqual(result.intent, "aggregate")
        self.assertIn("AVERAGE(", result.dax)
        self.assertIn("'Sales'[OrderQuantity]", result.dax)

    def test_sum_skips_string_column_and_picks_numeric_match(self) -> None:
        """A "sum ..." question must not wrap a string column in SUM()."""
        spec = SemanticModelSpec(
            name="Sales",
            tables=[
                # Listed first, so `_match_columns` yields this pair first.
                # The old translator would emit SUM('Codes'[Amount]) and
                # Fabric would reject it with a type error.
                SemanticTable(
                    name="Codes",
                    source_schema="dbo",
                    source_table="Codes",
                    columns=[
                        SemanticColumn(
                            name="Amount",
                            source_column="AmountCode",
                            data_type="string",
                        ),
                    ],
                ),
                SemanticTable(
                    name="Orders",
                    source_schema="dbo",
                    source_table="Orders",
                    columns=[
                        SemanticColumn(
                            name="Amount",
                            source_column="Amount",
                            data_type="decimal",
                            summarize_by="sum",
                        ),
                    ],
                ),
            ],
        )
        result = nl_to_dax("Sum of Amount", spec)
        self.assertIn("SUM(", result.dax)
        self.assertIn("'Orders'[Amount]", result.dax)
        self.assertNotIn("'Codes'[Amount]", result.dax)

    def test_sum_of_only_string_matches_falls_back_to_row_preview(self) -> None:
        """If no numeric column was named, the aggregate branch must be skipped."""
        spec = SemanticModelSpec(
            name="Contacts",
            tables=[
                SemanticTable(
                    name="Customer",
                    source_schema="dbo",
                    source_table="Customer",
                    columns=[
                        SemanticColumn(
                            name="Name",
                            source_column="Name",
                            data_type="string",
                        ),
                    ],
                ),
            ],
        )
        result = nl_to_dax("Total Name", spec)
        # No numeric column matched, so we must NOT generate SUM('Customer'[Name]).
        self.assertNotIn("SUM(", result.dax)
        self.assertNotIn("AVERAGE(", result.dax)
        # The fallback path returns a row preview grounded in the same table.
        self.assertTrue(result.dax.lstrip().startswith("EVALUATE"))
        self.assertIn("'Customer'", result.dax)
        # And the caller is warned that the aggregate could not be built.
        self.assertTrue(result.warnings)
        self.assertIn("numeric", result.warnings[0].lower())

    def test_count_of_string_column_uses_counta(self) -> None:
        """DAX COUNT rejects strings — text columns must use COUNTA."""
        result = nl_to_dax("Count of Name", self.spec)
        self.assertIn("COUNTA(", result.dax)
        self.assertNotIn("COUNT('Customer'[Name])", result.dax)

    def test_count_of_numeric_column_still_uses_count(self) -> None:
        result = nl_to_dax("Count of OrderQuantity", self.spec)
        self.assertIn("COUNT(", result.dax)
        self.assertNotIn("COUNTA(", result.dax)

    # -- multi-key grouping ------------------------------------------------

    def test_multi_key_grouping_by_and_and(self) -> None:
        """*"by X and Y"* must emit SUMMARIZECOLUMNS with two group keys."""
        spec = SemanticModelSpec(
            name="Sales",
            tables=[
                SemanticTable(
                    name="Fact",
                    source_schema="dbo",
                    source_table="Fact",
                    columns=[
                        SemanticColumn(
                            name="Amount",
                            source_column="Amount",
                            data_type="decimal",
                            summarize_by="sum",
                        ),
                        SemanticColumn(
                            name="Country",
                            source_column="Country",
                            data_type="string",
                        ),
                        SemanticColumn(
                            name="Category",
                            source_column="Category",
                            data_type="string",
                        ),
                    ],
                ),
            ],
        )
        result = nl_to_dax("Sum of Amount by Country and Category", spec)
        self.assertEqual(result.intent, "summarize")
        self.assertIn("SUMMARIZECOLUMNS(", result.dax)
        self.assertIn("'Fact'[Country]", result.dax)
        self.assertIn("'Fact'[Category]", result.dax)
        self.assertIn("SUM('Fact'[Amount])", result.dax)
        self.assertIn("Country", result.referenced_columns)
        self.assertIn("Category", result.referenced_columns)

    def test_multi_key_grouping_per_keyword_and_commas(self) -> None:
        """*"per X, Y, Z"* also emits multi-key SUMMARIZECOLUMNS."""
        result = nl_to_dax(
            "Total Sales per Country, Name", self.spec
        )
        self.assertEqual(result.intent, "summarize")
        self.assertIn("'Customer'[Country]", result.dax)
        self.assertIn("'Customer'[Name]", result.dax)
        self.assertIn("[Total Sales]", result.dax)

    # -- DISTINCTCOUNT / DISTINCT ------------------------------------------

    def test_how_many_distinct_column_emits_distinctcount(self) -> None:
        result = nl_to_dax("How many distinct countries?", self.spec)
        self.assertEqual(result.intent, "aggregate")
        self.assertIn("DISTINCTCOUNT('Customer'[Country])", result.dax)

    def test_how_many_unique_table_emits_countrows_distinct(self) -> None:
        """When only a table is named, COUNTROWS(DISTINCT(<table>)) is used."""
        result = nl_to_dax("How many unique customers", self.spec)
        # No column was named — the phrase "customers" only matches the
        # Customer table, so we count distinct rows of that table.
        self.assertEqual(result.intent, "aggregate")
        self.assertIn("COUNTROWS(DISTINCT('Customer'))", result.dax)

    def test_distinct_column_without_count_verb(self) -> None:
        """*"distinct customers"* alone is still a cardinality question."""
        result = nl_to_dax("Distinct countries", self.spec)
        self.assertEqual(result.intent, "aggregate")
        self.assertIn("DISTINCTCOUNT('Customer'[Country])", result.dax)

    def test_count_distinct_column_uses_distinctcount(self) -> None:
        """COUNT + distinct token → DISTINCTCOUNT (safer than COUNT / COUNTA)."""
        result = nl_to_dax("Count distinct Name", self.spec)
        self.assertIn("DISTINCTCOUNT('Customer'[Name])", result.dax)
        # And crucially not a bare COUNT / COUNTA on the same column.
        self.assertNotIn("COUNTA(", result.dax)
        # No standalone "COUNT(" (i.e. not immediately preceded by 'DISTINCT').
        for match_pos in range(len(result.dax)):
            if result.dax.startswith("COUNT(", match_pos):
                self.assertTrue(
                    result.dax[:match_pos].endswith("DISTINCT"),
                    f"Bare COUNT( found at pos {match_pos}: {result.dax!r}",
                )

    # -- equality filters --------------------------------------------------

    def test_equality_filter_string_literal_wraps_in_calculatetable(self) -> None:
        result = nl_to_dax(
            'Sum of SalesAmount where Country = "USA"', self.spec
        )
        self.assertIn("CALCULATETABLE(", result.dax)
        self.assertIn("SUM('Sales'[SalesAmount])", result.dax)
        self.assertIn("'Customer'[Country] = \"USA\"", result.dax)

    def test_equality_filter_numeric_literal(self) -> None:
        result = nl_to_dax(
            "Total Sales for CustomerKey = 42", self.spec
        )
        self.assertIn("CALCULATETABLE(", result.dax)
        self.assertIn("[Total Sales]", result.dax)
        self.assertIn("'Customer'[CustomerKey] = 42", result.dax)

    def test_equality_filter_column_not_also_used_as_group_key(self) -> None:
        """A filter column must not also appear as a SUMMARIZECOLUMNS key."""
        result = nl_to_dax(
            'Total Sales by Name where Country = "USA"', self.spec
        )
        # Grouping is on Name (the explicit "by" argument) …
        self.assertIn("'Customer'[Name]", result.dax)
        # … not on Country (which is the filter column).
        # It appears only inside the filter clause, never as a group key.
        summarize_start = result.dax.index("SUMMARIZECOLUMNS(")
        summarize_end = result.dax.index(")", summarize_start)
        summarize_body = result.dax[summarize_start:summarize_end]
        self.assertNotIn("'Customer'[Country]", summarize_body)

    def test_filter_with_is_keyword(self) -> None:
        """*"where X is 'value'"* — 'is' is a valid equality operator."""
        result = nl_to_dax(
            "Sum of SalesAmount where Country is 'USA'", self.spec
        )
        self.assertIn("CALCULATETABLE(", result.dax)
        self.assertIn("'Customer'[Country] = \"USA\"", result.dax)

    def test_show_rows_emits_browse(self) -> None:
        result = nl_to_dax("Show me the first 20 rows of Sales", self.spec)
        self.assertIn(result.intent, {"browse", "topn"})
        self.assertIn("TOPN(", result.dax)
        self.assertIn("'Sales'", result.dax)

    def test_bottom_keyword_switches_to_ascending(self) -> None:
        result = nl_to_dax(
            "Bottom 3 customers by SalesAmount", self.spec
        )
        self.assertIn("ASC", result.dax)

    def test_singular_plural_matching(self) -> None:
        result = nl_to_dax("how many customer records", self.spec)
        self.assertIn("Customer", result.referenced_tables)

    # -- error / fallback paths -------------------------------------------

    def test_empty_question_raises(self) -> None:
        with self.assertRaises(NlToDaxError):
            nl_to_dax("   ", self.spec)

    def test_unknown_question_raises(self) -> None:
        with self.assertRaises(NlToDaxError):
            nl_to_dax("tell me a joke about pineapples", self.spec)

    def test_returned_dax_starts_with_evaluate(self) -> None:
        for question in [
            "How many customers do we have?",
            "Total Sales by Country",
            "Top 5 customers by SalesAmount",
            "Show me the first 20 rows of Sales",
        ]:
            with self.subTest(question=question):
                result = nl_to_dax(question, self.spec)
                self.assertTrue(result.dax.lstrip().startswith("EVALUATE"))


class PbiModelingMcpHelperTests(unittest.TestCase):
    """Cover the pure (no-IO) helpers inside the MCP wrapper.

    The full client requires npx + a live MCP server so it is exercised by
    integration tests; here we only validate the table-parsing helpers used
    to convert tool responses into rows/columns for the UI.
    """

    def test_parse_row_objects(self) -> None:
        from app.intelligence.pbi_modeling_mcp import _parse_table_from_text

        text = '[{"Country":"FR","Total":10},{"Country":"DE","Total":20}]'
        result = _parse_table_from_text(text)
        self.assertIsNotNone(result)
        columns, rows = result
        self.assertEqual(columns, ["Country", "Total"])
        self.assertEqual(rows, [["FR", 10], ["DE", 20]])

    def test_parse_columns_rows_shape(self) -> None:
        from app.intelligence.pbi_modeling_mcp import _parse_table_from_text

        text = '{"columns":["A","B"],"rows":[[1,2],[3,4]]}'
        result = _parse_table_from_text(text)
        self.assertEqual(result, (["A", "B"], [[1, 2], [3, 4]]))

    def test_parse_execute_queries_shape(self) -> None:
        from app.intelligence.pbi_modeling_mcp import _parse_table_from_text

        text = (
            '{"results":[{"tables":[{"rows":'
            '[{"[Total Sales]":100.0},{"[Total Sales]":250.0}]}]}]}'
        )
        result = _parse_table_from_text(text)
        self.assertEqual(result, (["[Total Sales]"], [[100.0], [250.0]]))

    def test_parse_invalid_json_returns_none(self) -> None:
        from app.intelligence.pbi_modeling_mcp import _parse_table_from_text

        self.assertIsNone(_parse_table_from_text("not json"))


if __name__ == "__main__":  # pragma: no cover - test entry point
    unittest.main()
