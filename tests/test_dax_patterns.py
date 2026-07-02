"""Tests for the DAX-anti-pattern auditor (`app.intelligence.audit.dax_patterns`)."""

from __future__ import annotations

import unittest

from app.intelligence.audit import dax_patterns
from app.intelligence.spec import (
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticTable,
)


def _table(
    name: str,
    *,
    columns: list[SemanticColumn] | None = None,
    measures: list[SemanticMeasure] | None = None,
    is_date_table: bool = False,
) -> SemanticTable:
    return SemanticTable(
        name=name,
        source_schema="dbo",
        source_table=name,
        columns=columns or [],
        measures=measures or [],
        is_date_table=is_date_table,
    )


class DaxPatternAuditTests(unittest.TestCase):
    def test_implicit_measure_pattern(self) -> None:
        spec = SemanticModelSpec(
            name="Demo",
            tables=[
                _table(
                    "Sales",
                    columns=[
                        SemanticColumn(
                            name="Amount",
                            source_column="Amount",
                            data_type="decimal",
                            summarize_by="sum",
                        )
                    ],
                )
            ],
        )
        codes = {f.code for f in dax_patterns.audit(spec).findings}
        self.assertIn("DAX_PATTERN_IMPLICIT_MEASURE", codes)

    def test_filter_over_boolean_pattern(self) -> None:
        spec = SemanticModelSpec(
            name="Demo",
            tables=[
                _table(
                    "Sales",
                    columns=[
                        SemanticColumn(name="Amount", source_column="Amount", data_type="decimal")
                    ],
                    measures=[
                        SemanticMeasure(
                            name="EU Sales",
                            expression=(
                                "CALCULATE(SUM(Sales[Amount]), "
                                "FILTER(Sales, Sales[Region] = \"EU\"))"
                            ),
                        )
                    ],
                )
            ],
        )
        codes = {f.code for f in dax_patterns.audit(spec).findings}
        self.assertIn("DAX_PATTERN_FILTER_OVER_BOOLEAN", codes)

    def test_time_intel_without_date_table(self) -> None:
        spec = SemanticModelSpec(
            name="Demo",
            tables=[
                _table(
                    "Sales",
                    columns=[
                        SemanticColumn(name="Amount", source_column="Amount", data_type="decimal")
                    ],
                    measures=[
                        SemanticMeasure(
                            name="YTD Sales",
                            expression=(
                                "CALCULATE(SUM(Sales[Amount]), "
                                "DATESYTD(Calendar[Date]))"
                            ),
                        )
                    ],
                )
            ],
        )
        codes = {f.code for f in dax_patterns.audit(spec).findings}
        self.assertIn("DAX_PATTERN_TIME_INTEL_NO_DATE", codes)

    def test_missing_divide_pattern(self) -> None:
        spec = SemanticModelSpec(
            name="Demo",
            tables=[
                _table(
                    "Sales",
                    columns=[
                        SemanticColumn(name="Amount", source_column="Amount", data_type="decimal"),
                        SemanticColumn(name="Qty", source_column="Qty", data_type="int64"),
                    ],
                    measures=[
                        SemanticMeasure(
                            name="AvgPrice",
                            expression="SUM(Sales[Amount]) / SUM(Sales[Qty])",
                        )
                    ],
                )
            ],
        )
        codes = {f.code for f in dax_patterns.audit(spec).findings}
        self.assertIn("DAX_PATTERN_MISSING_DIVIDE", codes)

    def test_iterator_table_ref_pattern(self) -> None:
        spec = SemanticModelSpec(
            name="Demo",
            tables=[
                _table(
                    "Sales",
                    columns=[
                        SemanticColumn(name="Amount", source_column="Amount", data_type="decimal"),
                        SemanticColumn(name="Qty", source_column="Qty", data_type="int64"),
                    ],
                    measures=[
                        SemanticMeasure(
                            name="Bad SumX",
                            expression="SUMX(Sales[Amount], Sales[Qty] * 2)",
                        )
                    ],
                )
            ],
        )
        codes = {f.code for f in dax_patterns.audit(spec).findings}
        self.assertIn("DAX_PATTERN_ITERATOR_TABLE_REF", codes)

    def test_bidi_calculate_pattern(self) -> None:
        spec = SemanticModelSpec(
            name="Demo",
            tables=[
                _table(
                    "Sales",
                    columns=[
                        SemanticColumn(name="Amount", source_column="Amount", data_type="decimal")
                    ],
                    measures=[
                        SemanticMeasure(
                            name="BidiCalc",
                            expression=(
                                "CALCULATE(SUM(Sales[Amount]), "
                                "CROSSFILTER(Sales[Region], DimRegion[Region], BOTH))"
                            ),
                        )
                    ],
                )
            ],
        )
        codes = {f.code for f in dax_patterns.audit(spec).findings}
        self.assertIn("DAX_PATTERN_BIDI_CALCULATE", codes)

    def test_clean_model_no_findings(self) -> None:
        spec = SemanticModelSpec(
            name="Demo",
            tables=[
                _table(
                    "Sales",
                    columns=[
                        SemanticColumn(name="Amount", source_column="Amount", data_type="decimal")
                    ],
                    measures=[
                        SemanticMeasure(
                            name="Total",
                            expression="SUM(Sales[Amount])",
                        )
                    ],
                ),
                _table(
                    "Calendar",
                    columns=[
                        SemanticColumn(name="Date", source_column="Date", data_type="dateTime")
                    ],
                    is_date_table=True,
                ),
            ],
        )
        codes = {f.code for f in dax_patterns.audit(spec).findings}
        # No anti-patterns expected for a tiny clean spec.
        for unwanted in (
            "DAX_PATTERN_FILTER_OVER_BOOLEAN",
            "DAX_PATTERN_MISSING_DIVIDE",
            "DAX_PATTERN_ITERATOR_TABLE_REF",
            "DAX_PATTERN_BIDI_CALCULATE",
            "DAX_PATTERN_TIME_INTEL_NO_DATE",
        ):
            self.assertNotIn(unwanted, codes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
