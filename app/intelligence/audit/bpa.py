"""Best Practice Analyzer (BPA) audit.

Evaluates the **statically-checkable subset** of Microsoft's official Best
Practice Rules (``BPARules.json``) against a
:class:`~app.intelligence.spec.SemanticModelSpec`.

The full BPA rule set is authored in Tabular Editor's C#-style expression DSL
and many rules depend on runtime VertiPaq statistics (cardinality, row counts,
referential-integrity violations) that a static spec simply does not carry. This
auditor therefore implements a curated registry of evaluators for the rules that
*can* be judged from the in-memory model, and pulls each finding's name,
description and severity straight from the downloaded catalog so the rule
metadata is never duplicated. Rules that require runtime data are intentionally
skipped.

Pure function of a spec; no network, no AI. Toggleable from the UI via
``audit_semantic_model(spec, include_bpa=...)``.
"""

from __future__ import annotations

import re
from typing import Callable

from ..bpa import finding_code, rule, severity_to_level
from ..spec import SemanticModelSpec
from .base import AuditReport

FEATURE = "semantic-model-bpa"

_DATE_NAME_RE = re.compile(r"date|calendar", re.IGNORECASE)
# A division operator that is not part of "//" or "/*" — mirrors the BPA intent
# of flagging raw "/" division (which DIVIDE() handles safely).
_RAW_DIVISION_RE = re.compile(r"[\]\)]\s*/(?![/*])")
_NUMERIC_TYPES = {"int64", "decimal", "double"}


def _emit(report: AuditReport, rule_id: str, message: str, object_ref: str | None) -> None:
    """Add a finding sourced from the BPA catalog entry ``rule_id``."""
    meta = rule(rule_id)
    if meta is None:  # rule absent from the bundled catalog → skip silently.
        return
    level = severity_to_level(meta.get("Severity", 2))
    name = meta.get("Name", rule_id)
    report.add(
        level,
        finding_code(rule_id),
        f"{name}: {message}",
        object_ref=object_ref,
        recommendation=meta.get("Description") or None,
    )


def _relationship_columns(spec: SemanticModelSpec):
    many: set[tuple[str, str]] = set()  # foreign keys (the "many" side)
    one: set[tuple[str, str]] = set()  # primary keys referenced (the "one" side)
    used: set[tuple[str, str]] = set()
    for rel in spec.relationships:
        many.add((rel.from_table, rel.from_column))
        one.add((rel.to_table, rel.to_column))
        used.add((rel.from_table, rel.from_column))
        used.add((rel.to_table, rel.to_column))
    return many, one, used


# ---------------------------------------------------------------------------
# Individual rule evaluators. Each appends zero or more findings to the report.
# ---------------------------------------------------------------------------


def _rule_measure_format_strings(spec: SemanticModelSpec, report: AuditReport) -> None:
    for table in spec.tables:
        for m in table.measures:
            if not m.format_string:
                _emit(
                    report,
                    "PROVIDE_FORMAT_STRING_FOR_MEASURES",
                    f"measure '{m.name}' has no format string.",
                    f"{table.name}[{m.name}]",
                )


def _rule_numeric_summarize_by(spec: SemanticModelSpec, report: AuditReport) -> None:
    for table in spec.tables:
        for c in table.columns:
            if (
                c.data_type in _NUMERIC_TYPES
                and c.summarize_by not in ("none", "", None)
                and not c.is_hidden
                and not table.is_hidden
            ):
                _emit(
                    report,
                    "NUMERIC_COLUMN_SUMMARIZE_BY",
                    f"column '{c.name}' defaults to '{c.summarize_by}' summarization.",
                    f"{table.name}[{c.name}]",
                )


def _rule_floating_point(spec: SemanticModelSpec, report: AuditReport) -> None:
    for table in spec.tables:
        for c in table.columns:
            if c.data_type == "double":
                _emit(
                    report,
                    "AVOID_FLOATING_POINT_DATA_TYPES",
                    f"column '{c.name}' uses the Double floating-point type.",
                    f"{table.name}[{c.name}]",
                )


def _rule_hide_foreign_keys(spec: SemanticModelSpec, report: AuditReport) -> None:
    many, _one, _used = _relationship_columns(spec)
    for table in spec.tables:
        for c in table.columns:
            if (table.name, c.name) in many and not c.is_hidden and not table.is_hidden:
                _emit(
                    report,
                    "HIDE_FOREIGN_KEYS",
                    f"foreign-key column '{c.name}' is visible.",
                    f"{table.name}[{c.name}]",
                )


def _rule_mark_primary_keys(spec: SemanticModelSpec, report: AuditReport) -> None:
    _many, one, _used = _relationship_columns(spec)
    for table in spec.tables:
        if table.is_date_table:
            continue
        for c in table.columns:
            if (table.name, c.name) in one and not c.is_key:
                _emit(
                    report,
                    "MARK_PRIMARY_KEYS",
                    f"key column '{c.name}' is not marked as a key.",
                    f"{table.name}[{c.name}]",
                )


def _rule_relationship_data_types(spec: SemanticModelSpec, report: AuditReport) -> None:
    for rel in spec.relationships:
        ft = spec.table(rel.from_table)
        tt = spec.table(rel.to_table)
        if ft is None or tt is None:
            continue
        fc = ft.column(rel.from_column)
        tc = tt.column(rel.to_column)
        if fc is None or tc is None:
            continue
        ref = (
            f"{rel.from_table}[{rel.from_column}] -> {rel.to_table}[{rel.to_column}]"
        )
        if fc.data_type != tc.data_type:
            _emit(
                report,
                "RELATIONSHIP_COLUMNS_SAME_DATA_TYPE",
                f"columns differ in type ({fc.data_type} vs {tc.data_type}).",
                ref,
            )
        if fc.data_type != "int64" or tc.data_type != "int64":
            _emit(
                report,
                "RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE",
                "relationship columns are not both Int64.",
                ref,
            )


def _rule_date_table(spec: SemanticModelSpec, report: AuditReport) -> None:
    if not any(t.is_date_table for t in spec.tables):
        _emit(
            report,
            "MODEL_SHOULD_HAVE_A_DATE_TABLE",
            "the model has no table marked as a date table.",
            spec.name,
        )
    for table in spec.tables:
        if _DATE_NAME_RE.search(table.name) and not table.is_date_table:
            _emit(
                report,
                "DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE",
                f"table '{table.name}' looks like a date table but is not marked.",
                table.name,
            )


def _rule_dax_expressions(spec: SemanticModelSpec, report: AuditReport) -> None:
    seen: dict[str, str] = {}
    for table in spec.tables:
        for m in table.measures:
            ref = f"{table.name}[{m.name}]"
            expr = m.expression or ""
            if _RAW_DIVISION_RE.search(expr):
                _emit(
                    report,
                    "USE_THE_DIVIDE_FUNCTION_FOR_DIVISION",
                    f"measure '{m.name}' uses '/' instead of DIVIDE().",
                    ref,
                )
            if re.search(r"(?i)\bIFERROR\s*\(", expr):
                _emit(
                    report,
                    "AVOID_USING_THE_IFERROR_FUNCTION",
                    f"measure '{m.name}' uses IFERROR().",
                    ref,
                )
            if re.search(r"(?i)\bINTERSECT\s*\(", expr):
                _emit(
                    report,
                    "USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT",
                    f"measure '{m.name}' uses INTERSECT(); prefer TREATAS().",
                    ref,
                )
            norm = re.sub(r"\s+", "", expr)
            if norm:
                if norm in seen:
                    _emit(
                        report,
                        "AVOID_DUPLICATE_MEASURES",
                        f"measure '{m.name}' duplicates '{seen[norm]}'.",
                        ref,
                    )
                else:
                    seen[norm] = m.name


def _rule_naming(spec: SemanticModelSpec, report: AuditReport) -> None:
    def check(name: str, ref: str) -> None:
        if name != name.strip():
            _emit(
                report,
                "OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE",
                f"'{name}' starts or ends with a space.",
                ref,
            )
        first = name.strip()[:1]
        if first and first != first.upper():
            _emit(
                report,
                "FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED",
                f"'{name}' does not start with a capital letter.",
                ref,
            )

    for table in spec.tables:
        check(table.name, table.name)
        for m in table.measures:
            check(m.name, f"{table.name}[{m.name}]")


def _rule_tables_have_relationships(spec: SemanticModelSpec, report: AuditReport) -> None:
    if len(spec.tables) <= 1:
        return
    related: set[str] = set()
    for rel in spec.relationships:
        related.add(rel.from_table)
        related.add(rel.to_table)
    for table in spec.tables:
        if table.name not in related:
            _emit(
                report,
                "ENSURE_TABLES_HAVE_RELATIONSHIPS",
                f"table '{table.name}' has no relationships.",
                table.name,
            )


def _rule_m2m_single_direction(spec: SemanticModelSpec, report: AuditReport) -> None:
    for rel in spec.relationships:
        if (
            rel.from_cardinality == "many"
            and rel.to_cardinality == "many"
            and rel.cross_filtering_behavior == "bothDirections"
        ):
            _emit(
                report,
                "MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION",
                "a many-to-many relationship uses bidirectional filtering.",
                f"{rel.from_table}[{rel.from_column}] -> {rel.to_table}[{rel.to_column}]",
            )


def _rule_no_description(spec: SemanticModelSpec, report: AuditReport) -> None:
    for table in spec.tables:
        if not table.is_hidden and not table.description:
            _emit(
                report,
                "OBJECTS_WITH_NO_DESCRIPTION",
                f"table '{table.name}' has no description.",
                table.name,
            )
        for c in table.columns:
            if not c.is_hidden and not table.is_hidden and not c.description:
                _emit(
                    report,
                    "OBJECTS_WITH_NO_DESCRIPTION",
                    f"column '{c.name}' has no description.",
                    f"{table.name}[{c.name}]",
                )
        for m in table.measures:
            if not m.description:
                _emit(
                    report,
                    "OBJECTS_WITH_NO_DESCRIPTION",
                    f"measure '{m.name}' has no description.",
                    f"{table.name}[{m.name}]",
                )


_EVALUATORS: tuple[Callable[[SemanticModelSpec, AuditReport], None], ...] = (
    _rule_measure_format_strings,
    _rule_numeric_summarize_by,
    _rule_floating_point,
    _rule_hide_foreign_keys,
    _rule_mark_primary_keys,
    _rule_relationship_data_types,
    _rule_date_table,
    _rule_dax_expressions,
    _rule_naming,
    _rule_tables_have_relationships,
    _rule_m2m_single_direction,
    _rule_no_description,
)


def audit(spec: SemanticModelSpec) -> AuditReport:
    """Run the Best Practice Analyzer rule subset over ``spec``."""
    report = AuditReport(feature=FEATURE, target=spec.name)
    for evaluator in _EVALUATORS:
        evaluator(spec, report)
    return report
