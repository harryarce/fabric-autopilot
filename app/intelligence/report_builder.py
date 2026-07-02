"""Semantic-model-first **report design** — suggestion + deterministic composition.

Report creation is split into two cooperating halves:

* **What to show** — a list of :class:`VisualSuggestion` items, each a
  layout-free idea ("a column chart of *Total Sales* by *Segment*") grounded in
  a real :class:`~app.intelligence.spec.SemanticModelSpec`. These come either
  from a deterministic baseline (:func:`deterministic_visual_suggestions`) or
  from the AI design agent in :mod:`app.intelligence.report_design_agent`, which
  reads the model's tables, measures and relationships to propose *insightful*
  visuals.
* **How to lay it out** — :func:`compose_report` deterministically grounds the
  suggestions against the model (dropping anything that references a field that
  doesn't exist, wiring fields to the correct visual roles) and packs them into
  pages, producing the final :class:`~app.intelligence.report_spec.ReportSpec`.

This keeps the smart, subjective part (which visuals tell the best story) in the
agent while the deterministic compositor guarantees every generated report is
valid and renderable. :func:`suggest_report` remains the no-AI entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .report_spec import (
    DEFAULT_PAGE_HEIGHT,
    DEFAULT_PAGE_WIDTH,
    ReportField,
    ReportPage,
    ReportSpec,
    ReportTheme,
    ReportVisual,
)
from .spec import SemanticModelSpec, SemanticTable, dedupe_measure_names

# Layout grid (pixels) on the default 1280x720 canvas.
_MARGIN = 24
_CARD_WIDTH = 280
_CARD_HEIGHT = 120
_CHART_HEIGHT = 280
_TABLE_HEIGHT = 240
_GUTTER = 16
_MAX_CARDS = 4

# Visual-type families used by the compositor for role wiring and layout banding.
_KPI_TYPES = {"card", "multiRowCard", "kpi", "gauge"}
_TABLE_TYPES = {"table", "tableEx", "matrix"}
_SLICER_TYPES = {"slicer"}
_CHART_TYPES = {
    "columnChart",
    "clusteredColumnChart",
    "barChart",
    "clusteredBarChart",
    "lineChart",
    "pieChart",
    "donutChart",
}

# Role-name hints (case-insensitive) the agent may use, mapped to intent.
_SERIES_HINTS = {"series", "legend"}


def _measures(model: SemanticModelSpec) -> list[tuple[str, str]]:
    """Return ``(table, measure)`` pairs across the whole model."""
    return [(t.name, m.name) for t in model.tables for m in t.measures]


def _date_field(model: SemanticModelSpec) -> ReportField | None:
    for table in model.tables:
        if not table.is_date_table:
            continue
        for col in table.columns:
            if col.data_type == "dateTime":
                return ReportField(kind="column", entity=table.name, property=col.name)
    return None


def _dimension_columns(model: SemanticModelSpec) -> list[ReportField]:
    """Visible, non-key string columns — good category/grouping candidates."""
    fields: list[ReportField] = []
    for table in model.tables:
        if table.is_date_table:
            continue
        for col in table.columns:
            if col.is_hidden or col.is_key:
                continue
            if col.data_type == "string":
                fields.append(
                    ReportField(kind="column", entity=table.name, property=col.name)
                )
    return fields


def _measure_field(pair: tuple[str, str]) -> ReportField:
    table, measure = pair
    return ReportField(kind="measure", entity=table, property=measure)


def _first_visible_table(model: SemanticModelSpec) -> SemanticTable | None:
    return next((t for t in model.tables if not t.is_hidden), None)


# ---------------------------------------------------------------------------
# Visual suggestion IR (layout-free)
# ---------------------------------------------------------------------------


@dataclass
class VisualSuggestion:
    """A layout-free idea for one visual, grounded in a semantic model.

    The compositor (:func:`compose_report`) is responsible for position, size,
    page assignment and wiring ``role_bindings`` into a renderable
    :class:`ReportVisual`. ``importance`` (0-100) drives layout ordering and
    prominence; ``source`` records provenance (``"deterministic"`` or
    ``"agent"``) and ``rationale`` explains *why* the visual is useful.
    """

    visual_type: str
    title: str
    role_bindings: dict[str, list[ReportField]] = field(default_factory=dict)
    rationale: str = ""
    confidence: float = 0.8
    source: str = "deterministic"
    importance: int = 50

    def all_fields(self) -> list[ReportField]:
        return [f for fields in self.role_bindings.values() for f in fields]

    def to_dict(self) -> dict[str, Any]:
        return {
            "visual_type": self.visual_type,
            "title": self.title,
            "fields": {
                role: [f.to_dict() for f in fields]
                for role, fields in self.role_bindings.items()
            },
            "rationale": self.rationale,
            "confidence": self.confidence,
            "source": self.source,
            "importance": self.importance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VisualSuggestion":
        raw_fields = data.get("fields") or data.get("role_bindings") or {}
        bindings: dict[str, list[ReportField]] = {}
        for role, fields in raw_fields.items():
            parsed: list[ReportField] = []
            for f in fields or []:
                try:
                    parsed.append(ReportField.from_dict(f))
                except Exception:  # noqa: BLE001 - skip malformed field refs
                    continue
            if parsed:
                bindings[str(role)] = parsed
        return cls(
            visual_type=str(data.get("visual_type") or data.get("type") or "table"),
            title=str(data.get("title", "")).strip(),
            role_bindings=bindings,
            rationale=str(data.get("rationale", "")).strip(),
            confidence=float(data.get("confidence", 0.8)),
            source=str(data.get("source", "agent")),
            importance=int(data.get("importance", 50)),
        )


def _category(visual_type: str) -> str:
    if visual_type in _KPI_TYPES:
        return "kpi"
    if visual_type in _CHART_TYPES:
        return "chart"
    if visual_type in _TABLE_TYPES:
        return "table"
    if visual_type in _SLICER_TYPES:
        return "slicer"
    return "unknown"


# ---------------------------------------------------------------------------
# Deterministic baseline suggestions
# ---------------------------------------------------------------------------


def deterministic_visual_suggestions(
    model: SemanticModelSpec,
) -> list[VisualSuggestion]:
    """A sensible, always-valid set of visual ideas derived from ``model``.

    This is the no-AI baseline (and the fallback when the agent is unavailable
    or returns nothing usable): headline KPI cards, a time trend, a comparison
    by a key dimension, and a row-level detail table.
    """
    measures = _measures(model)
    dimensions = _dimension_columns(model)
    date_field = _date_field(model)
    out: list[VisualSuggestion] = []

    for table, measure in measures[:_MAX_CARDS]:
        out.append(
            VisualSuggestion(
                visual_type="card",
                title=measure,
                role_bindings={"Values": [_measure_field((table, measure))]},
                rationale="Headline KPI for an at-a-glance summary.",
                confidence=0.9,
                source="deterministic",
                importance=90,
            )
        )

    if date_field and measures:
        out.append(
            VisualSuggestion(
                visual_type="lineChart",
                title=f"{measures[0][1]} over time",
                role_bindings={
                    "Category": [date_field],
                    "Y": [_measure_field(measures[0])],
                },
                rationale="Trend of the primary measure across the date table.",
                confidence=0.85,
                source="deterministic",
                importance=80,
            )
        )

    if dimensions and measures:
        out.append(
            VisualSuggestion(
                visual_type="clusteredColumnChart",
                title=f"{measures[0][1]} by {dimensions[0].property}",
                role_bindings={
                    "Category": [dimensions[0]],
                    "Y": [_measure_field(measures[0])],
                },
                rationale="Compares the primary measure across a key dimension.",
                confidence=0.8,
                source="deterministic",
                importance=70,
            )
        )

    table_values: list[ReportField] = []
    if dimensions:
        table_values.append(dimensions[0])
    if measures:
        table_values.extend(_measure_field(m) for m in measures[:3])
    if not table_values:
        first = _first_visible_table(model)
        if first:
            table_values = [
                ReportField(kind="column", entity=first.name, property=c.name)
                for c in first.columns[:5]
                if not c.is_hidden
            ]
    if table_values:
        out.append(
            VisualSuggestion(
                visual_type="tableEx",
                title="Detail",
                role_bindings={"Values": table_values},
                rationale="Row-level detail backing the summary visuals.",
                confidence=0.7,
                source="deterministic",
                importance=40,
            )
        )

    return out


# ---------------------------------------------------------------------------
# Grounding — keep only fields that exist; wire fields to canonical roles
# ---------------------------------------------------------------------------


def _dedupe(fields: list[ReportField]) -> list[ReportField]:
    seen: set[tuple[str, str, str]] = set()
    out: list[ReportField] = []
    for f in fields:
        key = (f.kind, f.entity, f.property)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _bucket(
    role_bindings: dict[str, list[ReportField]]
) -> tuple[list[ReportField], list[ReportField], list[ReportField]]:
    """Split bound fields into (categories, series, values).

    Measures are always values. Columns hinted as series/legend go to series;
    every other column is treated as a category.
    """
    categories: list[ReportField] = []
    series: list[ReportField] = []
    values: list[ReportField] = []
    for role, fields in role_bindings.items():
        hint = (role or "").strip().casefold()
        for f in fields:
            if f.kind == "measure":
                values.append(f)
            elif hint in _SERIES_HINTS:
                series.append(f)
            else:
                categories.append(f)
    return categories, series, values


def _canonical_binding(
    visual_type: str, role_bindings: dict[str, list[ReportField]]
) -> tuple[str, dict[str, list[ReportField]]] | None:
    """Wire grounded fields into the canonical roles for ``visual_type``.

    Returns ``(final_visual_type, projections)`` or ``None`` when the visual
    lacks the minimum fields it needs to be meaningful. Unknown visual types are
    coerced to a sensible default based on the fields available.
    """
    categories, series, values = _bucket(role_bindings)
    categories, series, values = _dedupe(categories), _dedupe(series), _dedupe(values)
    cat = _category(visual_type)

    if cat == "unknown":
        if categories and values:
            visual_type, cat = "clusteredColumnChart", "chart"
        elif values:
            visual_type, cat = "card", "kpi"
        elif categories:
            visual_type, cat = "tableEx", "table"
        else:
            return None

    if cat == "kpi":
        if not values:
            return None
        if visual_type == "gauge":
            return visual_type, {"Y": values[:1]}
        if visual_type in {"card", "kpi"}:
            return visual_type, {"Values": values[:1]}
        return visual_type, {"Values": values}  # multiRowCard

    if cat == "chart":
        if not values or not categories:
            return None
        single_value = visual_type in {"pieChart", "donutChart"}
        binding: dict[str, list[ReportField]] = {
            "Category": categories[:1],
            "Y": values[:1] if single_value else values,
        }
        if series and not single_value:
            binding["Series"] = series[:1]
        return visual_type, binding

    if cat == "table":
        if visual_type == "matrix":
            binding = {}
            rows = _dedupe(categories + series)
            if rows:
                binding["Rows"] = rows
            if values:
                binding["Values"] = values
            return (visual_type, binding) if binding else None
        all_fields = _dedupe(categories + series + values)
        if not all_fields:
            return None
        return visual_type, {"Values": all_fields}

    if cat == "slicer":
        cols = _dedupe(categories + series)
        if not cols:
            return None
        return visual_type, {"Values": cols[:1]}

    return None


def _default_title(visual_type: str, bindings: dict[str, list[ReportField]]) -> str:
    cat = _category(visual_type)
    flat = [f for fields in bindings.values() for f in fields]
    values = [f for f in flat if f.kind == "measure"]
    columns = [f for f in flat if f.kind == "column"]
    if cat == "kpi" and values:
        return values[0].property
    if cat == "chart" and values and columns:
        return f"{values[0].property} by {columns[0].property}"
    if cat == "table":
        return "Detail"
    if cat == "slicer" and columns:
        return f"Filter by {columns[0].property}"
    if values:
        return values[0].property
    if columns:
        return columns[0].property
    return visual_type


def _ground_one(
    suggestion: VisualSuggestion,
    valid_cols: set[tuple[str, str]],
    valid_meas: set[tuple[str, str]],
) -> VisualSuggestion | None:
    """Drop fields not in the model, then wire what remains to canonical roles."""
    filtered: dict[str, list[ReportField]] = {}
    for role, fields in suggestion.role_bindings.items():
        good: list[ReportField] = []
        for f in fields:
            key = (f.entity, f.property)
            kind = f.kind
            if kind not in ("measure", "column"):
                # Infer the kind from where the reference actually resolves.
                kind = "measure" if key in valid_meas else (
                    "column" if key in valid_cols else None
                )
            if kind == "measure" and key in valid_meas:
                good.append(ReportField(kind="measure", entity=f.entity, property=f.property))
            elif kind == "column" and key in valid_cols:
                good.append(ReportField(kind="column", entity=f.entity, property=f.property))
        if good:
            filtered[role] = good

    result = _canonical_binding(suggestion.visual_type, filtered)
    if result is None:
        return None
    final_type, bindings = result
    title = suggestion.title.strip() or _default_title(final_type, bindings)
    return VisualSuggestion(
        visual_type=final_type,
        title=title,
        role_bindings=bindings,
        rationale=suggestion.rationale,
        confidence=suggestion.confidence,
        source=suggestion.source,
        importance=suggestion.importance,
    )


def ground_visual_suggestions(
    suggestions: list[VisualSuggestion], model: SemanticModelSpec
) -> list[VisualSuggestion]:
    """Return the subset of ``suggestions`` that bind only to real model fields.

    Each surviving suggestion has its fields re-wired into the canonical roles
    for its visual type, so the result is always safe to render.
    """
    valid_cols = {(t.name, c.name) for t in model.tables for c in t.columns}
    valid_meas = {(t.name, m.name) for t in model.tables for m in t.measures}
    grounded: list[VisualSuggestion] = []
    for suggestion in suggestions:
        one = _ground_one(suggestion, valid_cols, valid_meas)
        if one is not None:
            grounded.append(one)
    return grounded


# ---------------------------------------------------------------------------
# Deterministic compositor — suggestions -> laid-out ReportSpec
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    """One horizontal band of placed visuals on a page."""

    height: float
    items: list[tuple[VisualSuggestion, float]]  # (suggestion, width)


def _rows_for_band(
    items: list[VisualSuggestion],
    *,
    per_row: int,
    height: float,
    full_width: float,
    stretch: bool,
) -> list[_Row]:
    rows: list[_Row] = []
    for start in range(0, len(items), per_row):
        chunk = items[start : start + per_row]
        if stretch:
            count = len(chunk)
            width = (full_width - _GUTTER * (count - 1)) / count
            rows.append(_Row(height=height, items=[(s, width) for s in chunk]))
        else:
            rows.append(_Row(height=height, items=[(s, _CARD_WIDTH) for s in chunk]))
    return rows


def _suggestion_to_visual(
    suggestion: VisualSuggestion, name: str, x: float, y: float, w: float, h: float
) -> ReportVisual:
    return ReportVisual(
        name=name,
        visual_type=suggestion.visual_type,
        title=suggestion.title,
        x=x,
        y=y,
        width=w,
        height=h,
        projections={role: list(fields) for role, fields in suggestion.role_bindings.items()},
    )


def _finish_page(index: int, visuals: list[ReportVisual]) -> ReportPage:
    name = "overview" if index == 0 else f"page{index + 1}"
    display = "Overview" if index == 0 else f"Page {index + 1}"
    return ReportPage(
        name=name,
        display_name=display,
        width=DEFAULT_PAGE_WIDTH,
        height=DEFAULT_PAGE_HEIGHT,
        visuals=list(visuals),
    )


def _place_rows(rows: list[_Row]) -> list[ReportPage]:
    """Flow rows top-to-bottom, spilling onto new pages when space runs out."""
    pages: list[ReportPage] = []
    current: list[ReportVisual] = []
    y = _MARGIN
    counter = 0
    bottom = DEFAULT_PAGE_HEIGHT - _MARGIN
    for row in rows:
        if current and y + row.height > bottom:
            pages.append(_finish_page(len(pages), current))
            current = []
            y = _MARGIN
        x = _MARGIN
        for suggestion, width in row.items:
            counter += 1
            current.append(
                _suggestion_to_visual(suggestion, f"visual{counter}", x, y, width, row.height)
            )
            x += width + _GUTTER
        y += row.height + _GUTTER
    pages.append(_finish_page(len(pages), current))
    return pages


def compose_report(
    suggestions: list[VisualSuggestion],
    model: SemanticModelSpec,
    *,
    report_name: str | None = None,
    dataset_id: str | None = None,
    dataset_name: str | None = None,
    theme: ReportTheme | None = None,
) -> ReportSpec:
    """Ground ``suggestions`` against ``model`` and lay them out into a report.

    Visuals are banded — slicers and KPI cards on top, charts (two per row) in
    the middle, tables (full width) at the bottom — each band ordered by
    ``importance``. Bands flow onto additional pages when they overflow the
    canvas. Anything that doesn't ground cleanly is dropped; if nothing
    survives, the deterministic baseline for ``model`` is used instead.
    """
    grounded = ground_visual_suggestions(suggestions, model)
    if not grounded:
        grounded = ground_visual_suggestions(
            deterministic_visual_suggestions(model), model
        )

    name = report_name or f"{model.name} Report"
    full_width = DEFAULT_PAGE_WIDTH - 2 * _MARGIN

    def _band(kind: str) -> list[VisualSuggestion]:
        return sorted(
            (s for s in grounded if _category(s.visual_type) == kind),
            key=lambda s: s.importance,
            reverse=True,
        )

    slicers = _band("slicer")
    kpis = _band("kpi")
    charts = _band("chart")
    tables = _band("table")

    cards_per_row = max(1, int((full_width + _GUTTER) // (_CARD_WIDTH + _GUTTER)))
    rows: list[_Row] = []
    rows += _rows_for_band(
        slicers, per_row=cards_per_row, height=_CARD_HEIGHT, full_width=full_width, stretch=False
    )
    rows += _rows_for_band(
        kpis, per_row=cards_per_row, height=_CARD_HEIGHT, full_width=full_width, stretch=False
    )
    rows += _rows_for_band(
        charts, per_row=2, height=_CHART_HEIGHT, full_width=full_width, stretch=True
    )
    rows += _rows_for_band(
        tables, per_row=1, height=_TABLE_HEIGHT, full_width=full_width, stretch=True
    )

    pages = _place_rows(rows)

    return ReportSpec(
        name=name,
        pages=pages,
        theme=theme,
        dataset_id=dataset_id,
        dataset_name=dataset_name or model.name,
        description=f"Starter report generated from semantic model '{model.name}'.",
    )


def suggest_report(
    model: SemanticModelSpec,
    *,
    report_name: str | None = None,
    dataset_id: str | None = None,
    dataset_name: str | None = None,
    theme: ReportTheme | None = None,
) -> ReportSpec:
    """Build a grounded starter report from ``model`` with no AI involved.

    Equivalent to composing the deterministic baseline suggestions. The agentic
    path lives in :func:`app.intelligence.report_design_agent.suggest_report_with_agent`.

    Args:
        model: the parsed semantic model to build against.
        report_name: report display name (defaults to ``"<model> Report"``).
        dataset_id: Fabric semantic-model item id to bind to (preferred).
        dataset_name: model name for a relative ``byPath`` binding when no id.

    Returns:
        A :class:`ReportSpec` of grounded visuals laid out across one or more pages.
    """
    # Measure names share a single model-wide namespace; resolve any duplicates
    # to unique names up front so every visual binds to an unambiguous measure.
    model, _ = dedupe_measure_names(model)
    suggestions = deterministic_visual_suggestions(model)
    return compose_report(
        suggestions,
        model,
        report_name=report_name,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        theme=theme,
    )
