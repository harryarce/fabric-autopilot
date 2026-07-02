"""Tests for the deterministic auditors (:mod:`app.intelligence.audit`).

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.intelligence.audit import audit_report, audit_semantic_model, report_agent
from app.intelligence.audit.base import ERROR, INFO, WARNING, AuditFinding
from app.intelligence.audit.dax import extract_references
from app.intelligence.audit.report_formatting import contrast_ratio
from app.intelligence.report_spec import (
    ReportField,
    ReportPage,
    ReportSpec,
    ReportTheme,
    ReportVisual,
)
from app.intelligence.spec import (
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticRelationship,
    SemanticTable,
)


def _flawed_model() -> SemanticModelSpec:
    return SemanticModelSpec(
        name="RiskMart",
        tables=[
            SemanticTable(
                name="FACT_CLAIMS",
                source_schema="dbo",
                source_table="FACT_CLAIMS",
                columns=[
                    SemanticColumn(name="claim_id", source_column="claim_id", data_type="int64", is_key=True),
                    SemanticColumn(name="PaidAmount", source_column="PaidAmount", data_type="decimal", summarize_by="sum"),
                    SemanticColumn(name="IBNR", source_column="IBNR", data_type="decimal", summarize_by="sum"),
                    SemanticColumn(name="CustomerKey", source_column="CustomerKey", data_type="int64"),
                ],
                measures=[
                    SemanticMeasure(name="Loss Ratio", expression="SUM('FACT_CLAIMS'[PaidAmount]) / SUM('FACT_CLAIMS'[Premium])"),
                    SemanticMeasure(name="Bad Ref", expression="[NonExistentThing] + 1"),
                    SemanticMeasure(name="Empty", expression=""),
                ],
            ),
            SemanticTable(
                name="Calendar",
                source_schema="dbo",
                source_table="DimDate",
                columns=[SemanticColumn(name="Date", source_column="Date", data_type="dateTime")],
            ),
        ],
        relationships=[
            SemanticRelationship(from_table="FACT_CLAIMS", from_column="CustomerKey", to_table="Customer", to_column="CustomerKey"),
        ],
    )


class SemanticModelAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reports = audit_semantic_model(_flawed_model())
        self.codes = {f.code for f in self.reports["semantic-model-health"].findings}

    def test_dax_findings(self) -> None:
        self.assertIn("SM_DAX_EMPTY_EXPRESSION", self.codes)
        self.assertIn("SM_DAX_UNKNOWN_COLUMN", self.codes)  # Premium missing
        self.assertIn("SM_DAX_UNRESOLVED_REFERENCE", self.codes)  # [NonExistentThing]
        self.assertIn("SM_DAX_UNSAFE_DIVISION", self.codes)

    def test_design_findings(self) -> None:
        self.assertIn("SM_DESIGN_DANGLING_RELATIONSHIP", self.codes)
        self.assertIn("SM_DESIGN_UNMARKED_DATE_TABLE", self.codes)

    def test_usability_findings(self) -> None:
        self.assertIn("SM_USAB_NO_DATE_TABLE", self.codes)
        self.assertIn("SM_USAB_TABLE_TECHNICAL_NAME", self.codes)

    def test_copilot_findings(self) -> None:
        self.assertIn("SM_COPILOT_DOMAIN_TERM_UNDESCRIBED", self.codes)  # IBNR

    def test_health_has_errors(self) -> None:
        self.assertFalse(self.reports["semantic-model-health"].ok)
        self.assertLess(self.reports["semantic-model-health"].score, 50)

    def test_clean_model_scores_well(self) -> None:
        clean = SemanticModelSpec(
            name="Clean",
            description="A tidy model.",
            tables=[
                SemanticTable(
                    name="Sales",
                    source_schema="dbo",
                    source_table="Sales",
                    description="Sales facts.",
                    columns=[
                        SemanticColumn(name="Amount", source_column="Amount", data_type="decimal", summarize_by="sum", description="Net amount."),
                    ],
                    measures=[
                        SemanticMeasure(name="Total", expression="SUM('Sales'[Amount])", format_string="$#,0", description="Total sales."),
                    ],
                ),
                SemanticTable(
                    name="Calendar",
                    source_schema="dbo",
                    source_table="Calendar",
                    description="Dates.",
                    is_date_table=True,
                    columns=[SemanticColumn(name="Date", source_column="Date", data_type="dateTime", description="Day.")],
                ),
            ],
        )
        health = audit_semantic_model(clean)["semantic-model-health"]
        self.assertTrue(health.ok)


class BestPracticeAnalyzerTests(unittest.TestCase):
    def test_bpa_off_by_default(self) -> None:
        reports = audit_semantic_model(_flawed_model())
        self.assertNotIn("semantic-model-bpa", reports)

    def test_bpa_runs_when_enabled(self) -> None:
        reports = audit_semantic_model(_flawed_model(), include_bpa=True)
        self.assertIn("semantic-model-bpa", reports)
        codes = {f.code for f in reports["semantic-model-bpa"].findings}
        self.assertIn("BPA_PROVIDE_FORMAT_STRING_FOR_MEASURES", codes)
        self.assertIn("BPA_NUMERIC_COLUMN_SUMMARIZE_BY", codes)
        self.assertIn("BPA_MODEL_SHOULD_HAVE_A_DATE_TABLE", codes)
        self.assertIn("BPA_USE_THE_DIVIDE_FUNCTION_FOR_DIVISION", codes)
        self.assertIn("BPA_HIDE_FOREIGN_KEYS", codes)

    def test_bpa_folds_into_health(self) -> None:
        with_bpa = audit_semantic_model(_flawed_model(), include_bpa=True)
        health_codes = {f.code for f in with_bpa["semantic-model-health"].findings}
        self.assertTrue(any(c.startswith("BPA_") for c in health_codes))

    def test_catalog_loads_and_checklist_builds(self) -> None:
        from app.intelligence.bpa import (
            best_practice_checklist,
            finding_code,
            load_bpa_rules,
            rule,
        )

        rules = load_bpa_rules()
        self.assertGreater(len(rules), 50)
        self.assertIsNotNone(rule("PROVIDE_FORMAT_STRING_FOR_MEASURES"))
        self.assertTrue(best_practice_checklist().strip())
        self.assertEqual(
            finding_code("DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE"),
            "BPA_DATE_CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE",
        )


class DaxReferenceTests(unittest.TestCase):
    def test_extract_qualified_and_bare(self) -> None:
        qualified, bare = extract_references(
            "DIVIDE(SUM('Sales'[Amount]), [Order Count]) + Customer[Rank]"
        )
        self.assertIn(("Sales", "Amount"), qualified)
        self.assertIn(("Customer", "Rank"), qualified)
        self.assertIn("Order Count", bare)
        # Qualified columns must not leak into the bare list.
        self.assertNotIn("Amount", bare)


class ContrastTests(unittest.TestCase):
    def test_black_on_white_is_maximum(self) -> None:
        self.assertAlmostEqual(contrast_ratio("#000000", "#FFFFFF"), 21.0, places=1)

    def test_low_contrast_detected(self) -> None:
        ratio = contrast_ratio("#777777", "#808080")
        assert ratio is not None
        self.assertLess(ratio, 3.0)

    def test_invalid_hex_returns_none(self) -> None:
        self.assertIsNone(contrast_ratio("not-a-color", "#FFFFFF"))


class ReportAuditTests(unittest.TestCase):
    def test_flags_missing_titles_and_low_contrast(self) -> None:
        spec = ReportSpec(
            name="R",
            theme=ReportTheme(name="Bad", data_colors=["#EEEEEE"], background="#FFFFFF"),
            pages=[
                ReportPage(
                    name="p1",
                    display_name="P1",
                    visuals=[
                        ReportVisual(name="v1", visual_type="card", x=0, y=0, width=200, height=120),  # no title, no value
                    ],
                )
            ],
        )
        report = audit_report(spec)
        codes = {f.code for f in report.findings}
        self.assertIn("RPT_FMT_VISUAL_NO_TITLE", codes)
        self.assertIn("RPT_FMT_VISUAL_NO_VALUE", codes)
        self.assertIn("RPT_FMT_DATACOLOR_CONTRAST", codes)

    def test_detects_overlap_and_off_canvas(self) -> None:
        spec = ReportSpec(
            name="R",
            pages=[
                ReportPage(
                    name="p1",
                    display_name="P1",
                    width=1280,
                    height=720,
                    visuals=[
                        ReportVisual(name="a", visual_type="card", title="A", x=0, y=0, width=300, height=200,
                                     projections={"Values": [ReportField(kind="measure", entity="S", property="M")]}),
                        ReportVisual(name="b", visual_type="card", title="B", x=100, y=50, width=300, height=200,
                                     projections={"Values": [ReportField(kind="measure", entity="S", property="M")]}),
                        ReportVisual(name="c", visual_type="card", title="C", x=1200, y=600, width=300, height=300,
                                     projections={"Values": [ReportField(kind="measure", entity="S", property="M")]}),
                    ],
                )
            ],
        )
        codes = {f.code for f in audit_report(spec).findings}
        self.assertIn("RPT_FMT_OVERLAP", codes)
        self.assertIn("RPT_FMT_OFF_CANVAS", codes)


def _sample_report() -> ReportSpec:
    return ReportSpec(
        name="Claims Overview",
        dataset_name="RiskMart",
        theme=ReportTheme(name="FabricDevAI", data_colors=["#0F6CBD"], background="#FFFFFF"),
        pages=[
            ReportPage(
                name="page1",
                display_name="Overview",
                width=1280,
                height=720,
                visuals=[
                    ReportVisual(
                        name="paid_by_line",
                        visual_type="lineChart",
                        title="Paid by Line",
                        x=0,
                        y=0,
                        width=400,
                        height=300,
                        projections={
                            "Values": [ReportField(kind="measure", entity="FACT_CLAIMS", property="PaidAmount")]
                        },
                    )
                ],
            )
        ],
    )


class ReportAgentParsingTests(unittest.TestCase):
    """The agent-output parser is pure and runs without the Agent Framework."""

    def test_build_brief_is_grounded(self) -> None:
        brief = report_agent.build_report_brief(_sample_report())
        self.assertEqual(brief["report"], "Claims Overview")
        self.assertEqual(brief["dataset"], "RiskMart")
        self.assertEqual(brief["theme"], "FabricDevAI")
        self.assertEqual(len(brief["pages"]), 1)
        visual = brief["pages"][0]["visuals"][0]
        self.assertEqual(visual["type"], "lineChart")
        self.assertEqual(visual["fields"][0]["ref"], "FACT_CLAIMS.PaidAmount")

    def test_parse_valid_findings(self) -> None:
        text = (
            '{"findings": [{"severity": "warning", "code": "CHART_TYPE", '
            '"message": "Line chart has no time axis.", '
            '"object_ref": "Overview / Paid by Line", '
            '"recommendation": "Use a bar chart."}]}'
        )
        refs = report_agent._valid_object_refs(_sample_report())
        findings = report_agent._parse_findings(text, valid_refs=refs)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.severity, WARNING)
        self.assertEqual(f.code, "RPT_AI_CHART_TYPE")
        self.assertNotIn("unverified", (f.object_ref or ""))

    def test_parse_handles_fenced_json(self) -> None:
        text = '```json\n{"findings": [{"severity": "info", "code": "X", "message": "hi"}]}\n```'
        findings = report_agent._parse_findings(text, valid_refs=set())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, INFO)

    def test_invalid_severity_becomes_info(self) -> None:
        text = '{"findings": [{"severity": "catastrophe", "code": "X", "message": "m"}]}'
        findings = report_agent._parse_findings(text, valid_refs=set())
        self.assertEqual(findings[0].severity, INFO)

    def test_unverified_ref_is_flagged(self) -> None:
        text = (
            '{"findings": [{"severity": "info", "code": "X", "message": "m", '
            '"object_ref": "Some Imaginary Visual"}]}'
        )
        refs = report_agent._valid_object_refs(_sample_report())
        findings = report_agent._parse_findings(text, valid_refs=refs)
        self.assertIn("(unverified)", findings[0].object_ref or "")

    def test_empty_or_garbage_returns_no_findings(self) -> None:
        self.assertEqual(report_agent._parse_findings("", valid_refs=set()), [])
        self.assertEqual(report_agent._parse_findings("not json", valid_refs=set()), [])
        self.assertEqual(
            report_agent._parse_findings('{"nope": 1}', valid_refs=set()), []
        )

    def test_findings_without_message_are_dropped(self) -> None:
        text = '{"findings": [{"severity": "info", "code": "X", "message": ""}]}'
        self.assertEqual(report_agent._parse_findings(text, valid_refs=set()), [])


class ReportAgentEnrichmentTests(unittest.TestCase):
    """The public entry point always returns a usable, deterministic-first report."""

    def test_deterministic_fallback_when_unavailable(self) -> None:
        with mock.patch.object(report_agent, "is_available", return_value=False):
            outcome = report_agent.audit_report_with_agent(_sample_report())
        self.assertEqual(outcome.status, "deterministic")
        self.assertEqual(outcome.agent_finding_count, 0)
        self.assertEqual(outcome.report.feature, report_agent.FEATURE)
        # The deterministic findings are still present.
        baseline = audit_report(_sample_report())
        self.assertEqual(
            {f.code for f in outcome.report.findings},
            {f.code for f in baseline.findings},
        )

    def test_agent_error_degrades_to_baseline(self) -> None:
        class Boom:
            def __init__(self, *_a, **_k) -> None:
                raise RuntimeError("model exploded")

        with mock.patch.object(report_agent, "is_available", return_value=True), \
                mock.patch.object(report_agent, "ReportAuditIntelligence", Boom):
            outcome = report_agent.audit_report_with_agent(_sample_report())
        self.assertEqual(outcome.status, "error")
        self.assertIn("model exploded", outcome.error or "")
        baseline = audit_report(_sample_report())
        self.assertEqual(
            {f.code for f in outcome.report.findings},
            {f.code for f in baseline.findings},
        )

    def test_agent_findings_merge_and_lower_score(self) -> None:
        extra = [
            AuditFinding(severity=ERROR, code="RPT_AI_CHART_TYPE", message="Bad chart."),
            AuditFinding(severity=WARNING, code="RPT_AI_TITLE", message="Weak title."),
        ]

        class FakeIntel:
            def __init__(self, *_a, **_k) -> None:
                pass

            def enrich_sync(self, _spec):
                return extra

        with mock.patch.object(report_agent, "is_available", return_value=True), \
                mock.patch.object(report_agent, "ReportAuditIntelligence", FakeIntel):
            outcome = report_agent.audit_report_with_agent(_sample_report())

        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.agent_finding_count, 2)
        codes = {f.code for f in outcome.report.findings}
        self.assertIn("RPT_AI_CHART_TYPE", codes)
        self.assertIn("RPT_AI_TITLE", codes)
        # The merged report scores no higher than the deterministic baseline.
        baseline = audit_report(_sample_report())
        self.assertLess(outcome.report.score, baseline.score)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
