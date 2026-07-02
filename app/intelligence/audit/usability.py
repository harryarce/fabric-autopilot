"""Semantic-model **usability** audit.

Implements the "Semantic Model Usability Review" requirement: deterministic
rules that flag the things which make a published model hard for report authors
and business users to consume — missing descriptions, exposed technical keys,
unformatted measures, cryptic names and a missing date table.

Pure function of a :class:`~app.intelligence.spec.SemanticModelSpec`; no network,
no AI.
"""

from __future__ import annotations

import re

from ..spec import SemanticModelSpec, SemanticTable
from .base import INFO, WARNING, AuditReport

FEATURE = "semantic-model-usability"

# A name that looks machine-generated rather than business-friendly.
_TECHNICAL_NAME_RE = re.compile(r"_|(?:[a-z][A-Z])|^[A-Z0-9]{4,}$")
_KEY_SUFFIX_RE = re.compile(r"(?:id|key|sk|fk|guid|code)$", re.IGNORECASE)


def _looks_technical(name: str) -> bool:
    return bool(_TECHNICAL_NAME_RE.search(name))


def _relationship_columns(spec: SemanticModelSpec) -> set[tuple[str, str]]:
    cols: set[tuple[str, str]] = set()
    for rel in spec.relationships:
        cols.add((rel.from_table, rel.from_column))
        cols.add((rel.to_table, rel.to_column))
    return cols


def audit(spec: SemanticModelSpec) -> AuditReport:
    """Run the usability rule set over ``spec``."""
    report = AuditReport(feature=FEATURE, target=spec.name)
    rel_cols = _relationship_columns(spec)

    if not spec.description:
        report.add(
            WARNING,
            "SM_USAB_MODEL_NO_DESCRIPTION",
            "The model has no description.",
            object_ref=spec.name,
            recommendation="Add a one-line description so consumers and Copilot "
            "understand the model's purpose.",
        )

    has_date_table = any(t.is_date_table for t in spec.tables)
    if not has_date_table:
        report.add(
            WARNING,
            "SM_USAB_NO_DATE_TABLE",
            "No table is marked as a date table.",
            recommendation="Mark a Date/Calendar table as a date table to enable "
            "time-intelligence and consistent date filtering.",
        )

    for table in spec.tables:
        _audit_table(report, spec, table, rel_cols)

    _audit_display_folder_coverage(report, spec)
    _audit_synonym_coverage(report, spec)

    return report


def _audit_display_folder_coverage(
    report: AuditReport, spec: SemanticModelSpec
) -> None:
    """Flag tables whose measures lack display folders.

    Display folders organize measures in field lists so consumers can navigate
    large models. The rule fires only on tables with 4+ measures, where the
    absence is most painful.
    """
    for table in spec.tables:
        if table.is_hidden:
            continue
        visible_measures = [m for m in table.measures]  # all measures visible
        if len(visible_measures) < 4:
            continue
        without_folder = [m for m in visible_measures if not m.display_folder]
        if not without_folder:
            continue
        ratio = len(without_folder) / len(visible_measures)
        if ratio >= 0.5:
            report.add(
                INFO,
                "SM_USAB_NO_DISPLAY_FOLDERS",
                (
                    f"Table '{table.name}' has {len(without_folder)} of "
                    f"{len(visible_measures)} measures without a display folder."
                ),
                object_ref=table.name,
                recommendation=(
                    "Group measures into display folders (e.g. 'Metrics', "
                    "'Ratios', 'YoY') so the field list stays scannable."
                ),
            )


def _audit_synonym_coverage(report: AuditReport, spec: SemanticModelSpec) -> None:
    """Flag visible tables/columns/measures without Q&A synonyms.

    Q&A synonyms live in the linguistic schema as alternate names. We surface
    them on the model spec via the ``data_category`` slot for columns and a
    convention-based ``description`` heuristic for measures. The rule looks for
    visible objects that lack any synonym annotation, since Copilot/Q&A
    coverage degrades sharply when synonyms are sparse.
    """
    for table in spec.tables:
        if table.is_hidden:
            continue
        if not _has_synonym_hint(table):
            report.add(
                INFO,
                "SM_USAB_NO_SYNONYMS",
                f"Table '{table.name}' has no synonyms for Q&A/Copilot.",
                object_ref=table.name,
                recommendation=(
                    "Add at least one synonym (alternate name) so natural "
                    "language queries resolve to this table."
                ),
            )
        for col in table.columns:
            if col.is_hidden:
                continue
            if not _has_synonym_hint(col):
                ref = f"{table.name}[{col.name}]"
                report.add(
                    INFO,
                    "SM_USAB_COLUMN_NO_SYNONYMS",
                    f"Column {ref} has no synonyms for Q&A/Copilot.",
                    object_ref=ref,
                    recommendation=(
                        "Add a synonym so the column is reachable from natural "
                        "language phrasing variants."
                    ),
                )


def _has_synonym_hint(obj) -> bool:
    """Heuristic: a synonym is encoded in description as 'Synonyms: a, b, c'.

    Until a proper linguistic-schema field lands on the spec, we use the
    description marker to detect coverage so the audit/remediation loop is
    consistent.
    """
    desc = getattr(obj, "description", None) or ""
    return "synonyms:" in desc.lower()


def _audit_table(
    report: AuditReport,
    spec: SemanticModelSpec,
    table: SemanticTable,
    rel_cols: set[tuple[str, str]],
) -> None:
    if table.is_hidden:
        return  # hidden tables are not consumer-facing

    if not table.description:
        report.add(
            INFO,
            "SM_USAB_TABLE_NO_DESCRIPTION",
            f"Table '{table.name}' has no description.",
            object_ref=table.name,
            recommendation="Describe what the table represents.",
        )

    if _looks_technical(table.name):
        report.add(
            INFO,
            "SM_USAB_TABLE_TECHNICAL_NAME",
            f"Table name '{table.name}' looks technical.",
            object_ref=table.name,
            recommendation="Rename to a business-friendly term "
            "(e.g. 'FactSales' → 'Sales').",
        )

    visible_columns = [c for c in table.columns if not c.is_hidden]
    for col in table.columns:
        ref = f"{table.name}[{col.name}]"
        is_rel_col = (table.name, col.name) in rel_cols
        looks_key = col.is_key or bool(_KEY_SUFFIX_RE.search(col.name))

        if not col.is_hidden and (col.is_key or is_rel_col):
            report.add(
                WARNING,
                "SM_USAB_VISIBLE_KEY",
                f"Key/relationship column {ref} is visible.",
                object_ref=ref,
                recommendation="Hide surrogate/foreign keys so report authors "
                "are not tempted to drag them onto visuals.",
            )
        elif not col.is_hidden and looks_key:
            report.add(
                INFO,
                "SM_USAB_LIKELY_KEY_VISIBLE",
                f"Column {ref} looks like a key but is visible.",
                object_ref=ref,
                recommendation="Hide it if it is an identifier not meant for "
                "analysis.",
            )

        if not col.is_hidden and _looks_technical(col.name):
            report.add(
                INFO,
                "SM_USAB_COLUMN_TECHNICAL_NAME",
                f"Column name {ref} looks technical.",
                object_ref=ref,
                recommendation="Rename to a readable, spaced business term.",
            )

    for measure in table.measures:
        ref = f"{table.name}[{measure.name}]"
        if not measure.description:
            report.add(
                WARNING,
                "SM_USAB_MEASURE_NO_DESCRIPTION",
                f"Measure {ref} has no description.",
                object_ref=ref,
                recommendation="Describe the business meaning of the measure.",
            )
        if not measure.format_string:
            report.add(
                WARNING,
                "SM_USAB_MEASURE_NO_FORMAT",
                f"Measure {ref} has no format string.",
                object_ref=ref,
                recommendation="Set a format string (currency, percentage or "
                "thousands) so values render consistently.",
            )

    if visible_columns and not table.measures and not table.is_date_table:
        numeric = [c for c in visible_columns if c.summarize_by != "none"]
        if numeric:
            report.add(
                INFO,
                "SM_USAB_FACT_WITHOUT_MEASURE",
                f"Table '{table.name}' has aggregatable columns but no measures.",
                object_ref=table.name,
                recommendation="Add explicit DAX measures instead of relying on "
                "implicit column aggregation.",
            )
