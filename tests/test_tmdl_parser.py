"""Tests for the TMDL/TMSL parser (:mod:`app.intelligence.tmdl_parser`).

These verify the parser is a faithful inverse of the definition emitter
(round-trip) and that it tolerates real-world TMDL it did not generate
(extra properties, space indentation, quoted names).

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.intelligence import (
    DefinitionFormat,
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticRelationship,
    SemanticTable,
    build_definition,
    parse_semantic_model,
)
from app.intelligence.tmdl_parser import TmdlParseError


def _sample_spec() -> SemanticModelSpec:
    return SemanticModelSpec(
        name="Sales Model",
        tables=[
            SemanticTable(
                name="Sales",
                source_schema="dbo",
                source_table="FactSales",
                description="Fact table",
                columns=[
                    SemanticColumn(
                        name="Amount",
                        source_column="Amount",
                        data_type="decimal",
                        summarize_by="sum",
                        format_string="$#,0",
                    ),
                    SemanticColumn(
                        name="CustomerKey",
                        source_column="CustomerKey",
                        data_type="int64",
                        is_key=True,
                        is_hidden=True,
                    ),
                ],
                measures=[
                    SemanticMeasure(
                        name="Total Sales",
                        expression="SUM('Sales'[Amount])",
                        format_string="$#,0",
                        display_folder="KPIs",
                        description="Sum of amount",
                    ),
                    SemanticMeasure(
                        name="Multi", expression="VAR x = 1\nRETURN x"
                    ),
                ],
            ),
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
                    )
                ],
            ),
        ],
        relationships=[
            SemanticRelationship(
                from_table="Sales",
                from_column="CustomerKey",
                to_table="Customer",
                to_column="CustomerKey",
            )
        ],
        source_server="myserver",
        source_database="mydb",
        storage_mode="directQuery",
    )


class TmdlRoundTripTests(unittest.TestCase):
    def _assert_round_trip(self, fmt: DefinitionFormat) -> None:
        spec = _sample_spec()
        files = build_definition(spec, fmt).files
        parsed = parse_semantic_model(files, name="Sales Model")

        self.assertEqual(parsed.name, "Sales Model")
        self.assertEqual({t.name for t in parsed.tables}, {"Sales", "Customer"})

        sales = parsed.table("Sales")
        assert sales is not None
        self.assertEqual(sales.description, "Fact table")
        cols = {c.name: c for c in sales.columns}
        self.assertEqual(cols["Amount"].data_type, "decimal")
        self.assertEqual(cols["Amount"].summarize_by, "sum")
        self.assertEqual(cols["Amount"].format_string, "$#,0")
        self.assertTrue(cols["CustomerKey"].is_key)
        self.assertTrue(cols["CustomerKey"].is_hidden)

        measures = {m.name: m for m in sales.measures}
        self.assertEqual(measures["Total Sales"].expression, "SUM('Sales'[Amount])")
        self.assertEqual(measures["Total Sales"].display_folder, "KPIs")
        self.assertIn("VAR x = 1", measures["Multi"].expression)
        self.assertIn("RETURN x", measures["Multi"].expression)

        self.assertEqual(len(parsed.relationships), 1)
        rel = parsed.relationships[0]
        self.assertEqual(
            (rel.from_table, rel.from_column, rel.to_table, rel.to_column),
            ("Sales", "CustomerKey", "Customer", "CustomerKey"),
        )
        self.assertEqual(parsed.storage_mode, "directQuery")

    def test_tmdl_round_trip(self) -> None:
        self._assert_round_trip(DefinitionFormat.TMDL)

    def test_tmsl_round_trip(self) -> None:
        self._assert_round_trip(DefinitionFormat.TMSL)


class TmdlToleranceTests(unittest.TestCase):
    def test_ignores_unknown_properties_and_quoted_names(self) -> None:
        tmdl = (
            "/// A spaced table\n"
            "table 'My Sales'\n"
            "\tlineageTag: abc-123\n"
            "\n"
            "\tcolumn 'Net Amount'\n"
            "\t\tdataType: decimal\n"
            "\t\tlineageTag: def-456\n"
            "\t\tsummarizeBy: sum\n"
            "\t\tsourceColumn: NetAmount\n"
            "\t\tannotation SummarizationSetBy = Automatic\n"
            "\n"
            "\tmeasure 'Total' = SUM('My Sales'[Net Amount])\n"
            "\t\tformatString: 0.00\n"
        )
        spec = parse_semantic_model(
            {"definition/tables/My Sales.tmdl": tmdl}, name="M"
        )
        table = spec.table("My Sales")
        assert table is not None
        self.assertEqual(table.description, "A spaced table")
        col = table.columns[0]
        self.assertEqual(col.name, "Net Amount")
        self.assertEqual(col.data_type, "decimal")
        self.assertEqual(col.source_column, "NetAmount")
        self.assertEqual(table.measures[0].name, "Total")
        self.assertEqual(table.measures[0].format_string, "0.00")

    def test_space_indented_file(self) -> None:
        tmdl = (
            "table Orders\n"
            "    column Id\n"
            "        dataType: int64\n"
            "        sourceColumn: Id\n"
        )
        spec = parse_semantic_model({"definition/tables/Orders.tmdl": tmdl}, name="M")
        table = spec.table("Orders")
        assert table is not None
        self.assertEqual(table.columns[0].name, "Id")
        self.assertEqual(table.columns[0].data_type, "int64")

    def test_empty_definition_raises(self) -> None:
        with self.assertRaises(TmdlParseError):
            parse_semantic_model({"definition/model.tmdl": "model Model\n\tculture: en-US\n"}, name="M")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
