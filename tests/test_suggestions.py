"""Tests for the audit suggestion + write-back surface (Phase 1).

Covers:
* ``app.intelligence.suggestions`` — apply/round-trip on model and report specs.
* ``app.intelligence.audit.remediation`` — deterministic suggestion generators.
* ``fabric_services.artifact_service`` — persist/load/status helpers.

These tests are network-free; they exercise pure-Python dataclasses and the
in-memory artifact store.
"""

from __future__ import annotations

import json
import tempfile
import unittest

from app.artifacts import ArtifactRef, LocalArtifactStore
from app.intelligence import (
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticTable,
    SuggestionSpec,
    apply_model_suggestions,
    apply_report_suggestions,
    suggestions_from_dict,
    suggestions_to_dict,
)
from app.intelligence.audit import (
    load_brand_theme,
    propose_copilot_prep_fixes,
    propose_theme_remediation,
    propose_usability_fixes,
)
from app.intelligence.report_spec import (
    ReportPage,
    ReportSpec,
    ReportTheme,
    ReportVisual,
)
from fabric_services.artifact_service import ArtifactService
from fabric_services.context import TenantContext
from fabric_services.errors import NotFoundError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _model() -> SemanticModelSpec:
    return SemanticModelSpec(
        name="DemoModel",
        tables=[
            SemanticTable(
                name="Sales",
                source_schema="dbo",
                source_table="Sales",
                columns=[
                    SemanticColumn(name="SalesId", source_column="SalesId", data_type="int64"),
                    SemanticColumn(name="Amount", source_column="Amount", data_type="decimal"),
                ],
                measures=[
                    SemanticMeasure(name="Total Sales", expression="SUM(Sales[Amount])"),
                ],
            ),
            SemanticTable(
                name="DateDim",
                source_schema="dbo",
                source_table="DateDim",
                columns=[SemanticColumn(name="Date", source_column="Date", data_type="dateTime")],
            ),
        ],
    )


def _report(with_theme: ReportTheme | None = None) -> ReportSpec:
    return ReportSpec(
        name="DemoReport",
        pages=[
            ReportPage(
                name="page-1",
                display_name="Overview",
                visuals=[
                    ReportVisual(name="visual-1", visual_type="card"),
                    ReportVisual(name="visual-2", visual_type="lineChart", title="Trend"),
                ],
            )
        ],
        theme=with_theme,
    )


# ---------------------------------------------------------------------------
# Suggestion IR — apply round-trips
# ---------------------------------------------------------------------------


class ApplyModelSuggestionTests(unittest.TestCase):
    def test_apply_table_description(self) -> None:
        spec = _model()
        suggestion = SuggestionSpec(
            kind="model_usability",
            object_ref="Sales",
            field="description",
            proposed_value="Sales fact table.",
            status="accepted",
        )
        new_spec, updated = apply_model_suggestions(spec, [suggestion])
        table = next(t for t in new_spec.tables if t.name == "Sales")
        self.assertEqual(table.description, "Sales fact table.")
        self.assertEqual(updated[0].status, "applied")

    def test_apply_column_format_string(self) -> None:
        spec = _model()
        suggestion = SuggestionSpec(
            kind="model_usability",
            object_ref="Sales[Amount]",
            field="format_string",
            proposed_value="$#,0.00",
            status="accepted",
        )
        new_spec, _ = apply_model_suggestions(spec, [suggestion])
        col = next(c for c in new_spec.tables[0].columns if c.name == "Amount")
        self.assertEqual(col.format_string, "$#,0.00")

    def test_apply_measure_display_folder(self) -> None:
        spec = _model()
        suggestion = SuggestionSpec(
            kind="model_usability",
            object_ref="Sales[Total Sales]",
            field="display_folder",
            proposed_value="Metrics",
            status="accepted",
        )
        new_spec, _ = apply_model_suggestions(spec, [suggestion])
        measure = next(m for m in new_spec.tables[0].measures if m.name == "Total Sales")
        self.assertEqual(measure.display_folder, "Metrics")

    def test_apply_is_date_table(self) -> None:
        spec = _model()
        suggestion = SuggestionSpec(
            kind="model_usability",
            object_ref="DateDim",
            field="is_date_table",
            proposed_value=True,
            status="accepted",
        )
        new_spec, _ = apply_model_suggestions(spec, [suggestion])
        date_table = next(t for t in new_spec.tables if t.name == "DateDim")
        self.assertTrue(date_table.is_date_table)

    def test_apply_model_description(self) -> None:
        spec = _model()
        suggestion = SuggestionSpec(
            kind="model_copilot",
            object_ref="DemoModel",
            field="model.description",
            proposed_value="The Demo subject area.",
            status="accepted",
        )
        new_spec, _ = apply_model_suggestions(spec, [suggestion])
        self.assertEqual(new_spec.description, "The Demo subject area.")

    def test_unaccepted_suggestions_pass_through_unchanged(self) -> None:
        spec = _model()
        s = SuggestionSpec(
            kind="model_usability",
            object_ref="Sales",
            field="description",
            proposed_value="X",
            status="proposed",
        )
        new_spec, updated = apply_model_suggestions(spec, [s])
        table = next(t for t in new_spec.tables if t.name == "Sales")
        self.assertIsNone(table.description)
        self.assertEqual(updated[0].status, "proposed")

    def test_unknown_object_marks_failed(self) -> None:
        spec = _model()
        s = SuggestionSpec(
            kind="model_usability",
            object_ref="DoesNotExist",
            field="description",
            proposed_value="X",
            status="accepted",
        )
        _, updated = apply_model_suggestions(spec, [s])
        self.assertEqual(updated[0].status, "failed")
        self.assertIsNotNone(updated[0].error)


class ApplyReportSuggestionTests(unittest.TestCase):
    def test_apply_visual_title(self) -> None:
        spec = _report()
        s = SuggestionSpec(
            kind="report_formatting",
            object_ref="page-1/visual-1",
            field="title",
            proposed_value="Card Title",
            status="accepted",
        )
        new_spec, updated = apply_report_suggestions(spec, [s])
        visual = new_spec.pages[0].visuals[0]
        self.assertEqual(visual.title, "Card Title")
        self.assertEqual(updated[0].status, "applied")

    def test_apply_theme_creates_theme_if_missing(self) -> None:
        spec = _report(with_theme=None)
        suggestions = [
            SuggestionSpec(
                kind="report_theme",
                object_ref="theme:Brand",
                field="theme.background",
                proposed_value="#FFFFFF",
                status="accepted",
            ),
            SuggestionSpec(
                kind="report_theme",
                object_ref="theme:Brand",
                field="theme.foreground",
                proposed_value="#1F2937",
                status="accepted",
            ),
            SuggestionSpec(
                kind="report_theme",
                object_ref="theme:Brand",
                field="theme.data_colors",
                proposed_value=["#005EB8", "#0072CE"],
                status="accepted",
            ),
            SuggestionSpec(
                kind="report_theme",
                object_ref="theme:Brand",
                field="theme.name",
                proposed_value="Brand",
                status="accepted",
            ),
        ]
        new_spec, updated = apply_report_suggestions(spec, suggestions)
        self.assertIsNotNone(new_spec.theme)
        assert new_spec.theme is not None
        self.assertEqual(new_spec.theme.background, "#FFFFFF")
        self.assertEqual(new_spec.theme.foreground, "#1F2937")
        self.assertEqual(new_spec.theme.data_colors, ["#005EB8", "#0072CE"])
        self.assertEqual(new_spec.theme.name, "Brand")
        self.assertTrue(all(s.status == "applied" for s in updated))


# ---------------------------------------------------------------------------
# Suggestion IR — serialisation
# ---------------------------------------------------------------------------


class SuggestionSerialisationTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        s = SuggestionSpec(
            kind="model_usability",
            object_ref="Sales",
            field="description",
            proposed_value="hi",
            current_value=None,
            rationale="r",
            code="C",
        )
        encoded = suggestions_to_dict([s])
        decoded = suggestions_from_dict(encoded)
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].id, s.id)
        self.assertEqual(decoded[0].kind, s.kind)
        self.assertEqual(decoded[0].proposed_value, "hi")

    def test_json_round_trip(self) -> None:
        s = SuggestionSpec(
            kind="report_theme",
            object_ref="theme:Brand",
            field="theme.data_colors",
            proposed_value=["#000", "#fff"],
        )
        text = json.dumps(suggestions_to_dict([s]))
        decoded = suggestions_from_dict(json.loads(text))
        self.assertEqual(decoded[0].proposed_value, ["#000", "#fff"])


# ---------------------------------------------------------------------------
# Deterministic remediation generators
# ---------------------------------------------------------------------------


class ProposeUsabilityFixesTests(unittest.TestCase):
    def test_emits_model_description_when_missing(self) -> None:
        spec = _model()
        out = propose_usability_fixes(spec)
        codes = {s.code for s in out}
        self.assertIn("SM_USAB_MODEL_NO_DESCRIPTION", codes)

    def test_emits_date_table_mark_with_single_candidate(self) -> None:
        spec = _model()
        out = propose_usability_fixes(spec)
        date_marks = [
            s for s in out if s.field == "is_date_table" and s.proposed_value is True
        ]
        self.assertEqual(len(date_marks), 1)
        self.assertEqual(date_marks[0].object_ref, "DateDim")

    def test_proposes_currency_format_for_amount_column(self) -> None:
        spec = _model()
        out = propose_usability_fixes(spec)
        amount_fmts = [
            s for s in out
            if s.object_ref == "Sales[Amount]" and s.field == "format_string"
        ]
        self.assertTrue(amount_fmts)
        self.assertIn("$", amount_fmts[0].proposed_value)

    def test_apply_proposed_fixes_idempotent(self) -> None:
        spec = _model()
        proposed = propose_usability_fixes(spec)
        # Accept all
        for s in proposed:
            s.status = "accepted"
        new_spec, _ = apply_model_suggestions(spec, proposed)
        # Second round should yield strictly fewer suggestions (most are now satisfied).
        second_round = propose_usability_fixes(new_spec)
        self.assertLess(len(second_round), len(proposed))


class ProposeCopilotPrepFixesTests(unittest.TestCase):
    def test_emits_measure_description(self) -> None:
        spec = _model()
        out = propose_copilot_prep_fixes(spec)
        measures = [
            s for s in out
            if s.object_ref == "Sales[Total Sales]" and s.field == "description"
        ]
        self.assertEqual(len(measures), 1)
        self.assertEqual(measures[0].code, "SM_COPILOT_MEASURE_NO_DESCRIPTION")


class ProposeThemeRemediationTests(unittest.TestCase):
    def test_no_theme_triggers_full_theme_replacement(self) -> None:
        spec = _report(with_theme=None)
        out = propose_theme_remediation(spec)
        theme_fields = {s.field for s in out if s.field.startswith("theme.")}
        self.assertEqual(
            theme_fields,
            {"theme.background", "theme.foreground", "theme.data_colors", "theme.name"},
        )

    def test_untitled_visuals_get_title_suggestions(self) -> None:
        spec = _report(with_theme=load_brand_theme())
        out = propose_theme_remediation(spec)
        titles = [s for s in out if s.field == "title"]
        # Only visual-1 lacks a title; visual-2 already has one.
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].object_ref, "page-1/visual-1")

    def test_high_contrast_custom_theme_skips_theme_replacement(self) -> None:
        # A theme that already passes the WCAG contrast checks should not
        # trigger a theme.* remediation bundle (only visual titles, if any).
        clean_theme = ReportTheme(
            name="HighContrast",
            background="#FFFFFF",
            foreground="#000000",
            data_colors=["#000000", "#333333"],
        )
        spec = _report(with_theme=clean_theme)
        # Give every visual a title so no formatting suggestions remain either.
        for page in spec.pages:
            for visual in page.visuals:
                visual.title = visual.title or "Title"
        out = propose_theme_remediation(spec)
        self.assertFalse([s for s in out if s.field.startswith("theme.")])


# ---------------------------------------------------------------------------
# ArtifactService — persistence + status patch
# ---------------------------------------------------------------------------


class SuggestionPersistenceTests(unittest.TestCase):
    def _make_service(self) -> tuple[ArtifactService, ArtifactRef]:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        store = LocalArtifactStore(root=tmp)
        svc = ArtifactService(store=store, tenant=TenantContext.default())
        ref = ArtifactRef(
            kind="semanticModels",
            workspace_id="ws-1",
            item_id="m-1",
            item_name="DemoModel",
        )
        return svc, ref

    def test_persist_and_reload(self) -> None:
        svc, ref = self._make_service()
        suggestions = [
            SuggestionSpec(
                kind="model_usability",
                object_ref="Sales",
                field="description",
                proposed_value="hi",
            )
        ]
        svc.save_suggestions(ref, suggestions)
        reloaded = svc.load_suggestions(ref)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].object_ref, "Sales")

    def test_update_suggestion_status(self) -> None:
        svc, ref = self._make_service()
        sugg = SuggestionSpec(
            kind="model_usability",
            object_ref="Sales",
            field="description",
            proposed_value="hi",
        )
        svc.save_suggestions(ref, [sugg])
        updated = svc.update_suggestion_status(ref, sugg.id, "accepted")
        self.assertEqual(updated.status, "accepted")
        reloaded = svc.load_suggestions(ref)
        self.assertEqual(reloaded[0].status, "accepted")

    def test_update_unknown_suggestion_raises(self) -> None:
        svc, ref = self._make_service()
        svc.save_suggestions(ref, [])
        with self.assertRaises(NotFoundError):
            svc.update_suggestion_status(ref, "nope", "accepted")

    def test_load_missing_returns_empty(self) -> None:
        svc, ref = self._make_service()
        self.assertEqual(svc.load_suggestions(ref), [])


if __name__ == "__main__":
    unittest.main()
