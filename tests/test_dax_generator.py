"""Tests for the DAX measure generator (`app.intelligence.dax_generator`)."""

from __future__ import annotations

import unittest

from app.intelligence import GeneratedMeasure, generate_dax_measure
from app.intelligence.spec import (
    SemanticColumn,
    SemanticModelSpec,
    SemanticTable,
)


def _model() -> SemanticModelSpec:
    return SemanticModelSpec(
        name="Sales",
        tables=[
            SemanticTable(
                name="FactSales",
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
                    SemanticColumn(
                        name="CustomerKey",
                        source_column="CustomerKey",
                        data_type="int64",
                    ),
                    SemanticColumn(
                        name="DiscountPct",
                        source_column="DiscountPct",
                        data_type="decimal",
                        summarize_by="average",
                    ),
                ],
                measures=[],
            ),
            SemanticTable(
                name="DimDate",
                source_schema="dbo",
                source_table="DimDate",
                is_date_table=True,
                columns=[
                    SemanticColumn(
                        name="Date",
                        source_column="Date",
                        data_type="dateTime",
                    )
                ],
            ),
        ],
    )


class DaxGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = _model()

    def test_sum_intent(self) -> None:
        result = generate_dax_measure(self.spec, "total sales amount")
        self.assertIsInstance(result, GeneratedMeasure)
        self.assertEqual(result.intent_kind, "sum")
        self.assertIn("SUM", result.measure.expression)
        self.assertEqual(result.table, "FactSales")

    def test_average_intent(self) -> None:
        result = generate_dax_measure(self.spec, "average discount pct")
        self.assertEqual(result.intent_kind, "average")
        self.assertIn("AVERAGE", result.measure.expression)

    def test_distinct_count_intent(self) -> None:
        result = generate_dax_measure(self.spec, "distinct customer count")
        self.assertEqual(result.intent_kind, "distinct_count")
        self.assertIn("DISTINCTCOUNT", result.measure.expression)

    def test_count_intent(self) -> None:
        result = generate_dax_measure(self.spec, "number of orders")
        self.assertIn(result.intent_kind, {"count", "distinct_count"})
        self.assertTrue(
            "COUNT" in result.measure.expression
            or "DISTINCTCOUNT" in result.measure.expression
        )

    def test_ytd_intent(self) -> None:
        result = generate_dax_measure(self.spec, "ytd sales amount")
        self.assertEqual(result.intent_kind, "ytd")
        self.assertIn("DATESYTD", result.measure.expression)

    def test_yoy_intent(self) -> None:
        result = generate_dax_measure(self.spec, "yoy growth in sales amount")
        self.assertEqual(result.intent_kind, "yoy")
        self.assertIn("SAMEPERIODLASTYEAR", result.measure.expression)
        self.assertIn("DIVIDE", result.measure.expression)

    def test_percent_of_total_intent(self) -> None:
        result = generate_dax_measure(self.spec, "sales amount % of total")
        self.assertEqual(result.intent_kind, "percent_of_total")
        self.assertIn("ALL(", result.measure.expression.replace(" ", ""))
        self.assertIn("DIVIDE", result.measure.expression)

    def test_ratio_intent(self) -> None:
        result = generate_dax_measure(
            self.spec, "ratio of sales amount per order quantity"
        )
        self.assertEqual(result.intent_kind, "ratio")
        self.assertIn("DIVIDE", result.measure.expression)

    def test_unmatched_returns_fallback(self) -> None:
        # No intent keyword matches; generator falls back to a sum measure
        # against the best-scoring numeric column. We just assert the surface
        # is well-formed rather than asserting a specific confidence.
        result = generate_dax_measure(self.spec, "xyzzy nonexistent metric")
        self.assertIsInstance(result, GeneratedMeasure)
        self.assertTrue(result.measure.expression)

    def test_empty_intent_is_unmatched(self) -> None:
        result = generate_dax_measure(self.spec, "")
        self.assertEqual(result.intent_kind, "unmatched")
        self.assertEqual(result.confidence, 0.0)

    def test_to_dict_serializable(self) -> None:
        result = generate_dax_measure(self.spec, "total sales amount")
        doc = result.to_dict()
        self.assertIn("measure", doc)
        self.assertIn("expression", doc["measure"])
        self.assertEqual(doc["table"], "FactSales")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
