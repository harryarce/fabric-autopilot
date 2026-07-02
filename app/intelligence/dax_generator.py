"""Deterministic DAX measure generator.

Given a :class:`~app.intelligence.spec.SemanticModelSpec`, a natural-language
intent (``"total revenue"``, ``"YoY growth of paid amount"``, ``"percent of
total claims by status"``) and optional sample rows, produce one or more
candidate :class:`~app.intelligence.spec.SemanticMeasure` definitions plus the
DAX expression and rationale.

This is deterministic / template-driven by design so it can run with no
network/agent dependency and so its output is testable. The Foundry agent
can wrap this generator as a tool and rewrite the rationale + name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .spec import SemanticColumn, SemanticMeasure, SemanticModelSpec, SemanticTable


@dataclass
class GeneratedMeasure:
    """A generated DAX measure plus the rationale and audit-trail metadata."""

    measure: SemanticMeasure
    table: str
    rationale: str
    intent_kind: str  # "sum" | "average" | "distinct_count" | "count" | "ytd"
    #                  | "yoy" | "percent_of_total" | "ratio" | "unmatched"
    confidence: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "measure": {
                "name": self.measure.name,
                "expression": self.measure.expression,
                "format_string": self.measure.format_string,
                "description": self.measure.description,
                "display_folder": self.measure.display_folder,
            },
            "rationale": self.rationale,
            "intent_kind": self.intent_kind,
            "confidence": self.confidence,
        }


_SUM_INTENT = re.compile(r"\b(total|sum|sum of|gross)\b", re.IGNORECASE)
_AVG_INTENT = re.compile(r"\b(average|avg|mean)\b", re.IGNORECASE)
_DISTINCT_INTENT = re.compile(
    r"\b(distinct|unique|number of distinct|distinct count)\b", re.IGNORECASE
)
_COUNT_INTENT = re.compile(r"\b(count|number of|how many)\b", re.IGNORECASE)
_YTD_INTENT = re.compile(r"\b(ytd|year[- ]to[- ]date|year to date)\b", re.IGNORECASE)
_YOY_INTENT = re.compile(r"\b(yoy|year[- ]over[- ]year|year on year|growth)\b", re.IGNORECASE)
_PERCENT_INTENT = re.compile(
    r"\b(percent|percentage|share|\% of total|of total|proportion)\b",
    re.IGNORECASE,
)
_RATIO_INTENT = re.compile(r"\b(ratio|rate|per|divided by)\b", re.IGNORECASE)


_CURRENCY_HINTS = (
    "amount",
    "revenue",
    "sales",
    "cost",
    "price",
    "paid",
    "reserve",
    "premium",
    "loss",
    "expense",
    "incurred",
)
_PERCENT_HINTS = ("rate", "percent", "ratio", "share")
_INT_HINTS = ("count", "quantity", "qty", "number", "units")


def _pick_format(name: str, *, is_percent: bool = False, is_int: bool = False) -> str:
    if is_percent:
        return "0.0%"
    lowered = name.lower()
    if any(h in lowered for h in _PERCENT_HINTS):
        return "0.0%"
    if is_int or any(h in lowered for h in _INT_HINTS):
        return "#,0"
    if any(h in lowered for h in _CURRENCY_HINTS):
        return "$#,0.00"
    return "#,0.00"


def _date_table(spec: SemanticModelSpec) -> SemanticTable | None:
    for t in spec.tables:
        if t.is_date_table:
            return t
    return None


def _date_column(date_table: SemanticTable) -> str | None:
    """Heuristic: pick the first 'date'-typed or 'date'-named column."""
    for c in date_table.columns:
        if (c.data_type or "").lower() in ("date", "datetime"):
            return c.name
    for c in date_table.columns:
        if "date" in c.name.lower():
            return c.name
    return date_table.columns[0].name if date_table.columns else None


def _find_table_column(
    spec: SemanticModelSpec, intent: str
) -> tuple[SemanticTable, SemanticColumn] | None:
    """Pick the table + numeric column that best matches the intent text."""
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_]+", intent.lower()))
    if not tokens:
        return None
    best: tuple[int, SemanticTable, SemanticColumn] | None = None
    for table in spec.tables:
        if table.is_date_table or table.is_hidden:
            continue
        for col in table.columns:
            col_tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_]+", col.name.lower()))
            tbl_tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_]+", table.name.lower()))
            score = len(tokens & col_tokens) * 3 + len(tokens & tbl_tokens)
            # Prefer aggregatable / numeric columns for sum/avg intents.
            if (col.data_type or "").lower() in ("decimal", "double", "int64", "integer"):
                score += 1
            if col.summarize_by not in (None, "", "none"):
                score += 1
            if score and (best is None or score > best[0]):
                best = (score, table, col)
    if best is None:
        return None
    return best[1], best[2]


def _find_fact_table(spec: SemanticModelSpec, intent: str) -> SemanticTable | None:
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_]+", intent.lower()))
    fact_names = {rel.from_table for rel in spec.relationships}
    candidates = [t for t in spec.tables if t.name in fact_names and not t.is_hidden]
    if not candidates:
        candidates = [t for t in spec.tables if not t.is_hidden and not t.is_date_table]
    best: tuple[int, SemanticTable] | None = None
    for t in candidates:
        tbl_tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_]+", t.name.lower()))
        score = len(tokens & tbl_tokens)
        if best is None or score > best[0]:
            best = (score, t)
    return best[1] if best else None


def _humanize(name: str) -> str:
    """Insert spaces between camelCase words and replace underscores."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ")
    return " ".join(part.capitalize() for part in spaced.split())


def generate_dax_measure(
    spec: SemanticModelSpec,
    intent: str,
    *,
    sample_rows: list[dict[str, Any]] | None = None,
) -> GeneratedMeasure:
    """Deterministically generate a DAX measure for ``intent``.

    The generator pattern-matches the intent against a small library of
    measure shapes (sum, average, distinct count, YoY, YTD, percent-of-total,
    ratio) and renders the corresponding DAX. ``sample_rows`` is currently
    used only as a tiebreaker hint (which columns actually have values) and
    is reserved for future agent-assisted refinement.
    """
    intent = (intent or "").strip()
    if not intent:
        return _unmatched(spec, intent)
    sample_rows = sample_rows or []

    if _YOY_INTENT.search(intent):
        return _yoy_measure(spec, intent)
    if _YTD_INTENT.search(intent):
        return _ytd_measure(spec, intent)
    if _PERCENT_INTENT.search(intent):
        return _percent_of_total_measure(spec, intent)
    if _RATIO_INTENT.search(intent):
        return _ratio_measure(spec, intent)
    if _DISTINCT_INTENT.search(intent):
        return _distinct_count_measure(spec, intent)
    if _AVG_INTENT.search(intent):
        return _average_measure(spec, intent)
    if _COUNT_INTENT.search(intent):
        return _count_measure(spec, intent)
    if _SUM_INTENT.search(intent):
        return _sum_measure(spec, intent)
    # Default fallback: try sum-style.
    return _sum_measure(spec, intent)


def _sum_measure(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    pick = _find_table_column(spec, intent)
    if not pick:
        return _unmatched(spec, intent)
    table, col = pick
    name = f"Total {_humanize(col.name)}"
    expr = f"SUM({table.name}[{col.name}])"
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string=_pick_format(col.name),
            description=f"Sum of {table.name}[{col.name}] (auto-generated from intent).",
        ),
        table=table.name,
        rationale=(
            f"Intent '{intent}' matched a sum aggregation; the closest "
            f"numeric column is {table.name}[{col.name}]."
        ),
        intent_kind="sum",
    )


def _average_measure(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    pick = _find_table_column(spec, intent)
    if not pick:
        return _unmatched(spec, intent)
    table, col = pick
    name = f"Average {_humanize(col.name)}"
    expr = f"AVERAGE({table.name}[{col.name}])"
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string=_pick_format(col.name),
            description=f"Average of {table.name}[{col.name}].",
        ),
        table=table.name,
        rationale=(
            f"Intent '{intent}' matched an average aggregation on "
            f"{table.name}[{col.name}]."
        ),
        intent_kind="average",
    )


def _distinct_count_measure(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    pick = _find_table_column(spec, intent)
    if not pick:
        return _unmatched(spec, intent)
    table, col = pick
    name = f"Distinct {_humanize(col.name)}"
    expr = f"DISTINCTCOUNT({table.name}[{col.name}])"
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string="#,0",
            description=f"Distinct count of {table.name}[{col.name}].",
        ),
        table=table.name,
        rationale=(
            f"Intent '{intent}' matched distinct-count semantics on "
            f"{table.name}[{col.name}]."
        ),
        intent_kind="distinct_count",
    )


def _count_measure(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    table = _find_fact_table(spec, intent)
    if table is None:
        return _unmatched(spec, intent)
    name = f"{_humanize(table.name)} Count"
    expr = f"COUNTROWS({table.name})"
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string="#,0",
            description=f"Row count of {table.name}.",
        ),
        table=table.name,
        rationale=(
            f"Intent '{intent}' matched a row-count semantics on the "
            f"fact-like table {table.name}."
        ),
        intent_kind="count",
    )


def _ytd_measure(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    base = _sum_measure(spec, intent)
    if base.intent_kind == "unmatched":
        return base
    date_t = _date_table(spec)
    if not date_t:
        return GeneratedMeasure(
            measure=base.measure,
            table=base.table,
            rationale=(
                "YTD intent detected but no date table is marked; falling back "
                "to a plain sum. Mark a Date/Calendar table as a date table "
                "to enable time intelligence."
            ),
            intent_kind="sum",
            confidence=0.4,
        )
    date_col = _date_column(date_t) or "Date"
    name = f"{base.measure.name} YTD"
    expr = (
        f"CALCULATE({base.measure.expression}, "
        f"DATESYTD({date_t.name}[{date_col}]))"
    )
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string=base.measure.format_string,
            description=f"Year-to-date of {base.measure.name}.",
        ),
        table=base.table,
        rationale=(
            f"Wrapped {base.measure.name} with DATESYTD against "
            f"{date_t.name}[{date_col}]."
        ),
        intent_kind="ytd",
    )


def _yoy_measure(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    base = _sum_measure(spec, intent)
    if base.intent_kind == "unmatched":
        return base
    date_t = _date_table(spec)
    if not date_t:
        return GeneratedMeasure(
            measure=base.measure,
            table=base.table,
            rationale=(
                "YoY intent detected but no date table is marked; cannot "
                "build a SAMEPERIODLASTYEAR comparison. Returned the base "
                "sum instead."
            ),
            intent_kind="sum",
            confidence=0.4,
        )
    date_col = _date_column(date_t) or "Date"
    name = f"{base.measure.name} YoY %"
    expr = (
        f"VAR _cur = {base.measure.expression} "
        f"VAR _prior = CALCULATE({base.measure.expression}, "
        f"SAMEPERIODLASTYEAR({date_t.name}[{date_col}])) "
        f"RETURN DIVIDE(_cur - _prior, _prior)"
    )
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string="0.0%",
            description=f"Year-over-year growth of {base.measure.name}.",
        ),
        table=base.table,
        rationale=(
            "Built a YoY % delta using SAMEPERIODLASTYEAR and DIVIDE to "
            "handle a zero prior period safely."
        ),
        intent_kind="yoy",
    )


def _percent_of_total_measure(
    spec: SemanticModelSpec, intent: str
) -> GeneratedMeasure:
    base = _sum_measure(spec, intent)
    if base.intent_kind == "unmatched":
        return base
    name = f"% of Total {base.measure.name}"
    expr = (
        f"DIVIDE({base.measure.expression}, "
        f"CALCULATE({base.measure.expression}, ALL({base.table})))"
    )
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string="0.0%",
            description=(
                f"Share of {base.measure.name} vs. the all-rows total on "
                f"{base.table}."
            ),
        ),
        table=base.table,
        rationale=(
            "Computed share-of-total by dividing the base measure by the "
            "same measure under ALL() to remove the row context."
        ),
        intent_kind="percent_of_total",
    )


def _ratio_measure(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    pick = _find_table_column(spec, intent)
    if not pick:
        return _unmatched(spec, intent)
    table, col = pick
    fact = _find_fact_table(spec, intent) or table
    name = f"{_humanize(col.name)} per {_humanize(fact.name)}"
    expr = (
        f"DIVIDE(SUM({table.name}[{col.name}]), COUNTROWS({fact.name}))"
    )
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name=name,
            expression=expr,
            format_string=_pick_format(col.name),
            description=(
                f"Average {table.name}[{col.name}] per row of {fact.name}."
            ),
        ),
        table=table.name,
        rationale=(
            f"Intent '{intent}' looked like a ratio; built "
            f"SUM({table.name}[{col.name}]) / COUNTROWS({fact.name})."
        ),
        intent_kind="ratio",
    )


def _unmatched(spec: SemanticModelSpec, intent: str) -> GeneratedMeasure:
    # Best-effort placeholder so callers always get a structured result.
    fallback_table = next(
        (t for t in spec.tables if not t.is_hidden and not t.is_date_table),
        spec.tables[0] if spec.tables else None,
    )
    table_name = fallback_table.name if fallback_table else "Table"
    return GeneratedMeasure(
        measure=SemanticMeasure(
            name="New Measure",
            expression="// TODO: define expression",
            format_string="#,0.00",
            description=f"Could not match intent '{intent}' to a measurable column.",
        ),
        table=table_name,
        rationale=(
            f"No numeric column matched intent '{intent}'. Try referencing a "
            "specific column (e.g. 'total premium amount')."
        ),
        intent_kind="unmatched",
        confidence=0.0,
    )


__all__ = ["GeneratedMeasure", "generate_dax_measure"]
