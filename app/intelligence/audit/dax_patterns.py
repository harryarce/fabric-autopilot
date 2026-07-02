"""Semantic-model **DAX anti-pattern** audit.

A complement to :mod:`dax` (which checks correctness — references resolve,
expressions are non-empty, etc.). This auditor flags *patterns* that are
syntactically correct but smell bad: implicit measures, ``FILTER`` wrapped
around boolean conditions, time-intelligence on tables that are not marked as
date tables, missing :samp:`DIVIDE`, iterators passed a table reference when a
column is expected, and bidirectional ``CALCULATE`` filter manipulation.

Pure function of a :class:`~app.intelligence.spec.SemanticModelSpec`.
"""

from __future__ import annotations

import re

from ..spec import SemanticModelSpec
from .base import INFO, WARNING, AuditReport

FEATURE = "semantic-model-dax-patterns"

_TIME_INTEL_FUNCS = (
    "DATEADD",
    "DATESYTD",
    "DATESMTD",
    "DATESQTD",
    "SAMEPERIODLASTYEAR",
    "PARALLELPERIOD",
    "PREVIOUSYEAR",
    "PREVIOUSQUARTER",
    "PREVIOUSMONTH",
    "PREVIOUSDAY",
    "NEXTYEAR",
    "NEXTQUARTER",
    "NEXTMONTH",
    "NEXTDAY",
    "TOTALYTD",
    "TOTALMTD",
    "TOTALQTD",
)
# Iterator functions whose first arg must be a TABLE, not a column reference.
_ITERATOR_FUNCS = ("SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "PRODUCTX", "RANKX")

_FILTER_BOOL_RE = re.compile(
    r"\bFILTER\s*\(\s*[^,()]+?\s*,\s*[^()]+?(==|!=|<>|=|<|>|<=|>=)\s*[^()]+?\)",
    re.IGNORECASE,
)
_DIVISION_RE = re.compile(r"(?<![/])/(?![/])")
_BIDI_CALCULATE_RE = re.compile(
    r"CROSSFILTER\s*\([^)]*BOTH", re.IGNORECASE
)


def audit(spec: SemanticModelSpec) -> AuditReport:
    """Run the DAX-anti-pattern rule set over ``spec``."""
    report = AuditReport(feature=FEATURE, target=spec.name)
    date_table_names = {t.name for t in spec.tables if t.is_date_table}

    _audit_implicit_measures(report, spec)

    for table in spec.tables:
        for measure in table.measures:
            expr = measure.expression or ""
            ref = f"{table.name}[{measure.name}]"
            _check_filter_over_boolean(report, ref, expr)
            _check_time_intel_without_date_table(
                report, ref, expr, date_table_names
            )
            _check_missing_divide(report, ref, expr)
            _check_iterator_table_ref(report, ref, expr)
            _check_bidi_calculate(report, ref, expr)

    return report


def _audit_implicit_measures(
    report: AuditReport, spec: SemanticModelSpec
) -> None:
    """Visible aggregatable columns on a table with NO measures = implicit aggs."""
    for table in spec.tables:
        if table.is_hidden or table.is_date_table:
            continue
        if table.measures:
            continue
        aggregatable = [
            c for c in table.columns
            if not c.is_hidden and c.summarize_by not in (None, "", "none")
        ]
        if aggregatable:
            report.add(
                WARNING,
                "DAX_PATTERN_IMPLICIT_MEASURE",
                (
                    f"Table '{table.name}' exposes "
                    f"{len(aggregatable)} aggregatable column(s) but defines "
                    "no explicit measures."
                ),
                object_ref=table.name,
                recommendation=(
                    "Replace implicit aggregation with explicit measures so "
                    "consumers get consistent totals, formats, and Copilot/Q&A "
                    "grounding."
                ),
            )


def _check_filter_over_boolean(
    report: AuditReport, ref: str, expression: str
) -> None:
    """FILTER(<table>, <simple boolean>) should be a CALCULATE filter argument."""
    if _FILTER_BOOL_RE.search(expression):
        report.add(
            INFO,
            "DAX_PATTERN_FILTER_OVER_BOOLEAN",
            f"Measure {ref} wraps a simple boolean in FILTER().",
            object_ref=ref,
            recommendation=(
                "Pass the boolean directly to CALCULATE (e.g. "
                "CALCULATE(SUM(Sales[Amount]), Sales[Region] = \"EU\")) — "
                "FILTER over a table is materially slower than a boolean "
                "filter argument."
            ),
        )


def _check_time_intel_without_date_table(
    report: AuditReport,
    ref: str,
    expression: str,
    date_table_names: set[str],
) -> None:
    upper = expression.upper()
    used = [fn for fn in _TIME_INTEL_FUNCS if re.search(rf"\b{fn}\s*\(", upper)]
    if used and not date_table_names:
        report.add(
            WARNING,
            "DAX_PATTERN_TIME_INTEL_NO_DATE",
            (
                f"Measure {ref} uses time-intelligence ({', '.join(used)}) "
                "but no table is marked as a date table."
            ),
            object_ref=ref,
            recommendation=(
                "Mark a Date/Calendar table as a date table; otherwise the "
                "time-intelligence functions silently return incorrect values."
            ),
        )


def _check_missing_divide(report: AuditReport, ref: str, expression: str) -> None:
    """An expression with raw '/' division (but not DIVIDE)."""
    if "DIVIDE(" in expression.upper():
        return
    if _DIVISION_RE.search(expression):
        report.add(
            INFO,
            "DAX_PATTERN_MISSING_DIVIDE",
            f"Measure {ref} uses raw '/' division.",
            object_ref=ref,
            recommendation=(
                "Use DIVIDE(numerator, denominator) — it handles division by "
                "zero without producing #DIV/0! errors."
            ),
        )


def _check_iterator_table_ref(
    report: AuditReport, ref: str, expression: str
) -> None:
    """Iterator's first argument should be a TABLE expression, not a column.

    Heuristic: ``SUMX(Table[Column], ...)`` is detected by a qualified column
    reference as the first argument of an iterator.
    """
    upper = expression.upper()
    for fn in _ITERATOR_FUNCS:
        pattern = re.compile(
            rf"\b{fn}\s*\(\s*(?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*)\[[^\]]+\]\s*,",
            re.IGNORECASE,
        )
        if pattern.search(expression):
            report.add(
                INFO,
                "DAX_PATTERN_ITERATOR_TABLE_REF",
                (
                    f"Measure {ref} passes a column reference as the first "
                    f"argument to {fn}; iterators expect a table."
                ),
                object_ref=ref,
                recommendation=(
                    f"Pass the table (e.g. {fn}(Sales, ...)) or wrap the "
                    f"column ref in VALUES()/DISTINCT() if you need just its "
                    "distinct values."
                ),
            )
            break
    _ = upper  # quiet linter — we use the case-insensitive pattern above


def _check_bidi_calculate(report: AuditReport, ref: str, expression: str) -> None:
    if _BIDI_CALCULATE_RE.search(expression):
        report.add(
            WARNING,
            "DAX_PATTERN_BIDI_CALCULATE",
            f"Measure {ref} forces bidirectional cross-filter via CROSSFILTER.",
            object_ref=ref,
            recommendation=(
                "Bidirectional cross-filter in CALCULATE is a frequent source "
                "of ambiguity and circular dependencies. Prefer remodelling "
                "(bridge table, hidden helper column) over forcing BOTH."
            ),
        )
