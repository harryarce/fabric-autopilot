"""Tests for the report IR, renderer/parser and grounded builder.

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.intelligence import (
    ReportField,
    ReportPage,
    ReportSpec,
    ReportTheme,
    ReportVisual,
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticTable,
    VisualSuggestion,
    build_report_definition,
    compose_report,
    deterministic_visual_suggestions,
    ground_visual_suggestions,
    parse_report,
    report_design_agent,
    suggest_report,
)


def _model() -> SemanticModelSpec:
    return SemanticModelSpec(
        name="Sales",
        tables=[
            SemanticTable(
                name="Sales",
                source_schema="dbo",
                source_table="FactSales",
                columns=[
                    SemanticColumn(name="Amount", source_column="Amount", data_type="decimal", summarize_by="sum"),
                    SemanticColumn(name="CustomerKey", source_column="CustomerKey", data_type="int64", is_key=True, is_hidden=True),
                ],
                measures=[
                    SemanticMeasure(name="Total Sales", expression="SUM('Sales'[Amount])", format_string="$#,0"),
                    SemanticMeasure(name="Order Count", expression="COUNTROWS('Sales')"),
                ],
            ),
            SemanticTable(
                name="Customer",
                source_schema="dbo",
                source_table="DimCustomer",
                columns=[
                    SemanticColumn(name="CustomerKey", source_column="CustomerKey", data_type="int64", is_key=True, is_hidden=True),
                    SemanticColumn(name="Segment", source_column="Segment", data_type="string"),
                ],
            ),
            SemanticTable(
                name="Calendar",
                source_schema="dbo",
                source_table="DimDate",
                is_date_table=True,
                columns=[SemanticColumn(name="Date", source_column="Date", data_type="dateTime")],
            ),
        ],
    )


class ReportRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_visuals_and_binding(self) -> None:
        spec = ReportSpec(
            name="Sales Report",
            dataset_id="sm-123",
            theme=ReportTheme(name="Brand", data_colors=["#003366"], background="#FFFFFF"),
            pages=[
                ReportPage(
                    name="page1",
                    display_name="Overview",
                    visuals=[
                        ReportVisual(
                            name="v1",
                            visual_type="clusteredColumnChart",
                            title="Sales by Segment",
                            x=10,
                            y=20,
                            width=400,
                            height=300,
                            projections={
                                "Category": [ReportField(kind="column", entity="Customer", property="Segment")],
                                "Y": [ReportField(kind="measure", entity="Sales", property="Total Sales")],
                            },
                        )
                    ],
                )
            ],
        )
        files = build_report_definition(spec).files
        self.assertIn("definition.pbir", files)
        self.assertIn("definition/pages/page1/visuals/v1/visual.json", files)

        parsed = parse_report(files, name="Sales Report")
        self.assertEqual(parsed.dataset_id, "sm-123")
        assert parsed.theme is not None
        self.assertEqual(parsed.theme.data_colors, ["#003366"])
        v = parsed.pages[0].visuals[0]
        self.assertEqual(v.visual_type, "clusteredColumnChart")
        self.assertEqual(v.title, "Sales by Segment")
        cat = v.projections["Category"][0]
        self.assertEqual((cat.kind, cat.entity, cat.property), ("column", "Customer", "Segment"))
        y = v.projections["Y"][0]
        self.assertEqual((y.kind, y.entity, y.property), ("measure", "Sales", "Total Sales"))
        self.assertEqual(v.x, 10)
        self.assertEqual(v.width, 400)


class SuggestReportTests(unittest.TestCase):
    def test_all_fields_are_grounded_in_the_model(self) -> None:
        model = _model()
        spec = suggest_report(model, dataset_id="sm-1")

        model_cols = {(t.name, c.name) for t in model.tables for c in t.columns}
        model_meas = {(t.name, m.name) for t in model.tables for m in t.measures}
        for visual in spec.all_visuals():
            for field in visual.all_fields():
                key = (field.entity, field.property)
                self.assertTrue(
                    key in model_cols or key in model_meas,
                    f"ungrounded field {key}",
                )

    def test_includes_cards_chart_and_table(self) -> None:
        spec = suggest_report(_model(), dataset_id="sm-1")
        types = [v.visual_type for v in spec.all_visuals()]
        self.assertIn("card", types)
        self.assertIn("lineChart", types)  # date table present
        self.assertIn("clusteredColumnChart", types)
        self.assertIn("tableEx", types)

    def test_model_without_measures_still_yields_a_table(self) -> None:
        model = SemanticModelSpec(
            name="Bare",
            tables=[
                SemanticTable(
                    name="Items",
                    source_schema="dbo",
                    source_table="Items",
                    columns=[SemanticColumn(name="Name", source_column="Name", data_type="string")],
                )
            ],
        )
        spec = suggest_report(model)
        self.assertTrue(spec.all_visuals())
        self.assertTrue(any(v.visual_type == "tableEx" for v in spec.all_visuals()))


class ComposeReportTests(unittest.TestCase):
    def test_grounding_drops_ungrounded_fields_and_visuals(self) -> None:
        model = _model()
        suggestions = [
            # Valid: column category + measure value.
            VisualSuggestion(
                visual_type="clusteredColumnChart",
                title="Sales by Segment",
                role_bindings={
                    "Category": [ReportField(kind="column", entity="Customer", property="Segment")],
                    "Y": [ReportField(kind="measure", entity="Sales", property="Total Sales")],
                },
                source="agent",
            ),
            # Invalid: references a measure that does not exist -> dropped.
            VisualSuggestion(
                visual_type="card",
                title="Ghost",
                role_bindings={
                    "Values": [ReportField(kind="measure", entity="Sales", property="Nope")]
                },
                source="agent",
            ),
        ]
        grounded = ground_visual_suggestions(suggestions, model)
        self.assertEqual(len(grounded), 1)
        self.assertEqual(grounded[0].title, "Sales by Segment")

    def test_compose_lays_out_within_canvas_and_grounds(self) -> None:
        model = _model()
        suggestions = deterministic_visual_suggestions(model)
        spec = compose_report(suggestions, model, dataset_id="sm-1")
        self.assertTrue(spec.pages)
        model_cols = {(t.name, c.name) for t in model.tables for c in t.columns}
        model_meas = {(t.name, m.name) for t in model.tables for m in t.measures}
        for visual in spec.all_visuals():
            self.assertGreaterEqual(visual.x, 0)
            self.assertGreaterEqual(visual.y, 0)
            self.assertLessEqual(visual.x + visual.width, spec.pages[0].width + 1)
            for fld in visual.all_fields():
                key = (fld.entity, fld.property)
                self.assertTrue(key in model_cols or key in model_meas)

    def test_compose_falls_back_when_nothing_grounds(self) -> None:
        model = _model()
        bogus = [
            VisualSuggestion(
                visual_type="card",
                title="Bogus",
                role_bindings={
                    "Values": [ReportField(kind="measure", entity="X", property="Y")]
                },
                source="agent",
            )
        ]
        spec = compose_report(bogus, model)
        # Falls back to the deterministic baseline, which is non-empty.
        self.assertTrue(spec.all_visuals())

    def test_unknown_visual_type_is_coerced(self) -> None:
        model = _model()
        suggestion = VisualSuggestion(
            visual_type="sankeyDiagram",  # unsupported
            title="",
            role_bindings={
                "Category": [ReportField(kind="column", entity="Customer", property="Segment")],
                "Y": [ReportField(kind="measure", entity="Sales", property="Total Sales")],
            },
            source="agent",
        )
        grounded = ground_visual_suggestions([suggestion], model)
        self.assertEqual(len(grounded), 1)
        self.assertEqual(grounded[0].visual_type, "clusteredColumnChart")


class ReportDesignAgentTests(unittest.TestCase):
    def test_parse_visual_suggestions_tags_source_agent(self) -> None:
        text = (
            '{"visuals": [{"visual_type": "card", "title": "Sales", '
            '"fields": {"Values": [{"kind": "measure", "entity": "Sales", '
            '"property": "Total Sales"}]}, "rationale": "kpi"}]}'
        )
        suggestions = report_design_agent._parse_visual_suggestions(text)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].source, "agent")
        self.assertEqual(suggestions[0].visual_type, "card")

    def test_parse_visual_suggestions_tolerates_junk(self) -> None:
        self.assertEqual(report_design_agent._parse_visual_suggestions(""), [])
        self.assertEqual(report_design_agent._parse_visual_suggestions("not json"), [])
        self.assertEqual(
            report_design_agent._parse_visual_suggestions('{"nope": 1}'), []
        )

    def test_falls_back_to_deterministic_when_unavailable(self) -> None:
        with mock.patch.object(report_design_agent, "is_available", return_value=False):
            outcome = report_design_agent.suggest_report_with_agent(
                _model(), dataset_id="sm-1"
            )
        self.assertEqual(outcome.status, "deterministic")
        self.assertFalse(outcome.used_agent)
        self.assertTrue(outcome.report.all_visuals())

    def test_uses_agent_visuals_when_grounded(self) -> None:
        agent_suggestions = [
            VisualSuggestion(
                visual_type="donutChart",
                title="Sales by Segment",
                role_bindings={
                    "Category": [ReportField(kind="column", entity="Customer", property="Segment")],
                    "Y": [ReportField(kind="measure", entity="Sales", property="Total Sales")],
                },
                source="agent",
            )
        ]

        class _Intel:
            def __init__(self, *_a, **_k) -> None:
                pass

            def suggest_visuals_sync(self, *_a, **_k):
                return agent_suggestions

        with mock.patch.object(report_design_agent, "is_available", return_value=True), \
                mock.patch.object(report_design_agent, "ReportDesignIntelligence", _Intel):
            outcome = report_design_agent.suggest_report_with_agent(
                _model(), dataset_id="sm-1"
            )
        self.assertEqual(outcome.status, "ok")
        self.assertTrue(outcome.used_agent)
        self.assertEqual(outcome.agent_visual_count, 1)
        types = [v.visual_type for v in outcome.report.all_visuals()]
        self.assertIn("donutChart", types)

    def test_agent_error_degrades_to_deterministic(self) -> None:
        class _Boom:
            def __init__(self, *_a, **_k) -> None:
                raise RuntimeError("kaboom")

        with mock.patch.object(report_design_agent, "is_available", return_value=True), \
                mock.patch.object(report_design_agent, "ReportDesignIntelligence", _Boom):
            outcome = report_design_agent.suggest_report_with_agent(_model())
        self.assertEqual(outcome.status, "error")
        self.assertTrue(outcome.used_agent)
        self.assertTrue(outcome.report.all_visuals())


class PublishBindingTests(unittest.TestCase):
    """``ReportService.publish_report`` must bind reports byConnection."""

    def _service(self):
        from app.fabric_client import CreatedItem
        from fabric_services.context import TenantContext
        from fabric_services.report_service import ReportService

        captured: dict = {}

        class _FakeFabric:
            def create_report(self_inner, workspace_id, display_name, definition, *, description=None):
                captured["definition"] = definition
                captured["display_name"] = display_name
                return CreatedItem(id="rep-1", display_name=display_name, workspace_id=workspace_id)

        class _FakeStore:
            def save_definition(self_inner, ref, files, *, source):
                captured["saved"] = True

        service = ReportService(_FakeFabric(), _FakeStore(), TenantContext.default())
        return service, captured

    def _spec(self, *, dataset_id=None) -> ReportSpec:
        return suggest_report(_model(), report_name="Sales Report", dataset_id=dataset_id)

    @staticmethod
    def _pbir(definition: dict) -> str:
        import base64

        for part in definition["parts"]:
            if part["path"] == "definition.pbir":
                return base64.b64decode(part["payload"]).decode("utf-8")
        raise AssertionError("definition.pbir not found in parts")

    def test_publish_without_dataset_id_raises_validation_error(self) -> None:
        from fabric_services.errors import ValidationError

        service, _ = self._service()
        with self.assertRaises(ValidationError):
            service.publish_report("ws-1", "Sales Report", self._spec())

    def test_publish_with_spec_dataset_id_emits_byconnection(self) -> None:
        service, captured = self._service()
        created = service.publish_report(
            "ws-1", "Sales Report", self._spec(dataset_id="sm-99")
        )
        self.assertEqual(created.id, "rep-1")
        pbir = self._pbir(captured["definition"])
        self.assertIn("byConnection", pbir)
        self.assertIn("semanticmodelid=sm-99", pbir)
        self.assertNotIn("byPath", pbir)

    def test_publish_dataset_id_override_binds_unbound_spec(self) -> None:
        service, captured = self._service()
        created = service.publish_report(
            "ws-1", "Sales Report", self._spec(), dataset_id="sm-override"
        )
        self.assertEqual(created.id, "rep-1")
        pbir = self._pbir(captured["definition"])
        self.assertIn("semanticmodelid=sm-override", pbir)
        self.assertNotIn("byPath", pbir)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

