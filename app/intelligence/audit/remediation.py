"""Audit → write-back remediation engine.

Bridges the deterministic auditors (which emit :class:`AuditFinding` records
describing *what is wrong*) and the write-back loop (which needs concrete
:class:`SuggestionSpec` records describing *what to change*).

For every actionable audit finding this module emits a one-field suggestion
the user can review, accept and apply:

* :func:`propose_usability_fixes` — descriptions, hidden keys, format strings,
  display folders, technical-name → friendly-name hints derived from
  :mod:`app.intelligence.audit.usability`.
* :func:`propose_copilot_prep_fixes` — descriptions and synonyms for
  domain-jargon objects, sourced from
  :mod:`app.intelligence.audit.copilot_prep`.
* :func:`propose_theme_remediation` — a brand-aligned, WCAG-AA theme plus
  visual titles, sourced from :mod:`app.intelligence.audit.report_formatting`.

All functions are deterministic and pure — they take a spec, return a list of
:class:`SuggestionSpec`. The agentic layer may *extend* the list later by
appending suggestions tagged ``source="agent"``.
"""

from __future__ import annotations

import json
import os
import re
from importlib import resources

from ..report_spec import ReportSpec, ReportTheme
from ..spec import SemanticModelSpec, SemanticTable
from ..suggestions import SuggestionSpec
from . import copilot_prep, report_formatting, usability

# ---------------------------------------------------------------------------
# Branded default theme — loaded lazily, tenant-overridable
# ---------------------------------------------------------------------------

_THEME_RESOURCE = "default_theme.json"
_THEME_ENV = "FABRIC_REPORT_THEME_PATH"


def load_brand_theme() -> ReportTheme:
    """Return the branded default report theme.

    Resolution order:

    1. ``$FABRIC_REPORT_THEME_PATH`` — a tenant override JSON file (same shape
       as the bundled resource). Lets operators ship a customer's brand colours
       without changing code.
    2. The packaged ``default_theme.json`` shipped with the intelligence
       layer (WCAG-AA, blue accent + 8-colour qualitative palette).
    """
    override = os.environ.get(_THEME_ENV)
    if override and os.path.isfile(override):
        with open(override, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        text = resources.files("app.intelligence.resources").joinpath(
            _THEME_RESOURCE
        ).read_text(encoding="utf-8")
        data = json.loads(text)
    return ReportTheme.from_dict(data)


# ---------------------------------------------------------------------------
# Helpers shared by the semantic-model remediations
# ---------------------------------------------------------------------------

_TECHNICAL_SPLIT_RE = re.compile(r"_|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_KEY_SUFFIX_RE = re.compile(r"(?:id|key|sk|fk|guid|code)$", re.IGNORECASE)
_DATE_NAME_RE = re.compile(r"date|calendar", re.IGNORECASE)


def _humanise(raw: str) -> str:
    """Turn ``CustomerId`` / ``customer_id`` into ``Customer Id`` for descriptions."""
    parts = [p for p in _TECHNICAL_SPLIT_RE.split(raw) if p]
    if not parts:
        return raw
    words: list[str] = []
    for part in parts:
        if part.isupper():
            words.append(part)
        else:
            words.append(part[:1].upper() + part[1:].lower())
    return " ".join(words)


def _looks_numeric(data_type: str) -> bool:
    return data_type in {"int64", "decimal", "double"}


def _looks_currency(name: str) -> bool:
    n = name.lower()
    return any(token in n for token in ("amount", "price", "cost", "revenue", "value", "salary", "premium"))


def _looks_percent(name: str) -> bool:
    n = name.lower()
    return n.endswith("pct") or n.endswith("percent") or "rate" in n or "ratio" in n


def _proposed_format(column_name: str, data_type: str) -> str | None:
    """Best-effort default format string for a numeric column."""
    if not _looks_numeric(data_type):
        if data_type == "dateTime":
            return "yyyy-mm-dd"
        return None
    if _looks_currency(column_name):
        return "$#,0.00"
    if _looks_percent(column_name):
        return "0.00%"
    return "#,0"


def _proposed_measure_format(measure_name: str, expression: str) -> str:
    n = measure_name.lower()
    if _looks_currency(n) or "sum" in expression.lower():
        return "$#,0.00"
    if _looks_percent(n) or "divide" in expression.lower():
        return "0.00%"
    return "#,0"


def _is_likely_key(name: str) -> bool:
    return bool(_KEY_SUFFIX_RE.search(name))


# ---------------------------------------------------------------------------
# Semantic model — usability
# ---------------------------------------------------------------------------


def _propose_table_usability(
    spec: SemanticModelSpec, table: SemanticTable, rel_cols: set[tuple[str, str]]
) -> list[SuggestionSpec]:
    out: list[SuggestionSpec] = []
    if not table.is_hidden and not table.description:
        out.append(
            SuggestionSpec(
                kind="model_usability",
                object_ref=table.name,
                field="description",
                current_value=None,
                proposed_value=(
                    f"{_humanise(table.name)} table sourced from "
                    f"[{table.source_schema}].[{table.source_table}]."
                ),
                rationale="Adding a table description gives consumers and Copilot context.",
                code="SM_USAB_TABLE_NO_DESCRIPTION",
            )
        )

    for col in table.columns:
        ref = f"{table.name}[{col.name}]"
        is_rel_col = (table.name, col.name) in rel_cols
        if not col.is_hidden and (col.is_key or is_rel_col or _is_likely_key(col.name)):
            out.append(
                SuggestionSpec(
                    kind="model_usability",
                    object_ref=ref,
                    field="is_hidden",
                    current_value=False,
                    proposed_value=True,
                    rationale="Hide surrogate/foreign keys so report authors don't drop them on visuals.",
                    code="SM_USAB_VISIBLE_KEY",
                )
            )
            # Keys don't need a format suggestion as well.
            continue

        if not col.is_hidden and not col.description:
            out.append(
                SuggestionSpec(
                    kind="model_usability",
                    object_ref=ref,
                    field="description",
                    current_value=None,
                    proposed_value=(
                        f"{_humanise(col.name)} value from "
                        f"[{table.source_schema}].[{table.source_table}].[{col.source_column}]."
                    ),
                    rationale="Self-document the column for report authors.",
                    code="SM_USAB_COLUMN_NO_DESCRIPTION",
                )
            )

        if not col.is_hidden and not col.format_string:
            proposed_fmt = _proposed_format(col.name, col.data_type)
            if proposed_fmt:
                out.append(
                    SuggestionSpec(
                        kind="model_usability",
                        object_ref=ref,
                        field="format_string",
                        current_value=None,
                        proposed_value=proposed_fmt,
                        rationale="Apply a consistent format string for predictable rendering.",
                        code="SM_USAB_COLUMN_NO_FORMAT",
                        confidence=0.75,
                    )
                )

    for measure in table.measures:
        ref = f"{table.name}[{measure.name}]"
        if not measure.description:
            out.append(
                SuggestionSpec(
                    kind="model_usability",
                    object_ref=ref,
                    field="description",
                    current_value=None,
                    proposed_value=f"{_humanise(measure.name)} computed by the model.",
                    rationale="Measures without descriptions are hard for report authors to discover.",
                    code="SM_USAB_MEASURE_NO_DESCRIPTION",
                )
            )
        if not measure.format_string:
            out.append(
                SuggestionSpec(
                    kind="model_usability",
                    object_ref=ref,
                    field="format_string",
                    current_value=None,
                    proposed_value=_proposed_measure_format(measure.name, measure.expression),
                    rationale="Set a measure format so values render consistently across visuals.",
                    code="SM_USAB_MEASURE_NO_FORMAT",
                    confidence=0.8,
                )
            )
        if not measure.display_folder:
            out.append(
                SuggestionSpec(
                    kind="model_usability",
                    object_ref=ref,
                    field="display_folder",
                    current_value=None,
                    proposed_value="Metrics",
                    rationale="Group measures into a display folder for an organised field list.",
                    code="SM_USAB_MEASURE_NO_FOLDER",
                    confidence=0.6,
                )
            )
    return out


def propose_usability_fixes(spec: SemanticModelSpec) -> list[SuggestionSpec]:
    """Generate concrete usability write-back suggestions for ``spec``.

    Mirrors the rules in :mod:`app.intelligence.audit.usability` but emits a
    :class:`SuggestionSpec` per actionable finding instead of an advisory
    string. Always returns a list (possibly empty); never raises.
    """
    out: list[SuggestionSpec] = []

    if not spec.description:
        out.append(
            SuggestionSpec(
                kind="model_usability",
                object_ref=spec.name,
                field="model.description",
                current_value=None,
                proposed_value=f"{_humanise(spec.name)} semantic model.",
                rationale="Add a one-line model description so consumers know what the model is for.",
                code="SM_USAB_MODEL_NO_DESCRIPTION",
            )
        )

    # Mark a Date dimension if there is exactly one obvious candidate.
    if not any(t.is_date_table for t in spec.tables):
        candidates = [t for t in spec.tables if "date" in t.name.lower() or "calendar" in t.name.lower()]
        if len(candidates) == 1:
            out.append(
                SuggestionSpec(
                    kind="model_usability",
                    object_ref=candidates[0].name,
                    field="is_date_table",
                    current_value=False,
                    proposed_value=True,
                    rationale="Marking this as the date table unlocks time-intelligence patterns.",
                    code="SM_USAB_NO_DATE_TABLE",
                    confidence=0.85,
                )
            )

    rel_cols: set[tuple[str, str]] = set()
    for rel in spec.relationships:
        rel_cols.add((rel.from_table, rel.from_column))
        rel_cols.add((rel.to_table, rel.to_column))

    for table in spec.tables:
        out.extend(_propose_table_usability(spec, table, rel_cols))
    return out


# ---------------------------------------------------------------------------
# Semantic model — Best Practice Analyzer (statically auto-fixable subset)
# ---------------------------------------------------------------------------


def propose_bpa_fixes(spec: SemanticModelSpec) -> list[SuggestionSpec]:
    """Generate write-back suggestions for the auto-fixable BPA rule subset.

    Mirrors the statically-checkable rules in :mod:`app.intelligence.audit.bpa`
    but only emits suggestions for findings that can be remediated by a concrete
    one-field write-back the apply engine supports (column / measure / table
    attributes). Rules that need renames, relationship edits or DAX rewrites are
    intentionally skipped because they are not safely auto-applicable. Always
    returns a list (possibly empty); never raises.
    """
    out: list[SuggestionSpec] = []

    # Relationship-derived foreign-key ("many" side) and primary-key
    # ("one" side) column sets.
    many: set[tuple[str, str]] = set()
    one: set[tuple[str, str]] = set()
    for rel in spec.relationships:
        many.add((rel.from_table, rel.from_column))
        one.add((rel.to_table, rel.to_column))

    for table in spec.tables:
        # DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE
        if _DATE_NAME_RE.search(table.name) and not table.is_date_table:
            out.append(
                SuggestionSpec(
                    kind="model_bpa",
                    object_ref=table.name,
                    field="is_date_table",
                    current_value=False,
                    proposed_value=True,
                    rationale="BPA: date/calendar tables should be marked as a date table.",
                    code="BPA_DATE_CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE",
                    confidence=0.85,
                )
            )

        for col in table.columns:
            ref = f"{table.name}[{col.name}]"

            # AVOID_FLOATING_POINT_DATA_TYPES — Double → Decimal.
            if col.data_type == "double":
                out.append(
                    SuggestionSpec(
                        kind="model_bpa",
                        object_ref=ref,
                        field="data_type",
                        current_value="double",
                        proposed_value="decimal",
                        rationale="BPA: avoid the Double floating-point type; Decimal is exact and compresses better.",
                        code="BPA_AVOID_FLOATING_POINT_DATA_TYPES",
                        confidence=0.7,
                    )
                )

            # NUMERIC_COLUMN_SUMMARIZE_BY — stop implicit aggregation.
            if (
                col.data_type in {"int64", "decimal", "double"}
                and col.summarize_by not in ("none", "", None)
                and not col.is_hidden
                and not table.is_hidden
            ):
                out.append(
                    SuggestionSpec(
                        kind="model_bpa",
                        object_ref=ref,
                        field="summarize_by",
                        current_value=col.summarize_by,
                        proposed_value="none",
                        rationale="BPA: set default summarization to None so numeric columns aren't auto-aggregated.",
                        code="BPA_NUMERIC_COLUMN_SUMMARIZE_BY",
                        confidence=0.8,
                    )
                )

            # HIDE_FOREIGN_KEYS — hide the "many" side relationship columns.
            if (
                (table.name, col.name) in many
                and not col.is_hidden
                and not table.is_hidden
            ):
                out.append(
                    SuggestionSpec(
                        kind="model_bpa",
                        object_ref=ref,
                        field="is_hidden",
                        current_value=False,
                        proposed_value=True,
                        rationale="BPA: hide foreign-key columns so report authors use the related dimension.",
                        code="BPA_HIDE_FOREIGN_KEYS",
                        confidence=0.8,
                    )
                )

            # MARK_PRIMARY_KEYS — mark the "one" side key columns.
            if (
                (table.name, col.name) in one
                and not col.is_key
                and not table.is_date_table
            ):
                out.append(
                    SuggestionSpec(
                        kind="model_bpa",
                        object_ref=ref,
                        field="is_key",
                        current_value=False,
                        proposed_value=True,
                        rationale="BPA: mark primary-key columns as keys for correct relationship handling.",
                        code="BPA_MARK_PRIMARY_KEYS",
                        confidence=0.8,
                    )
                )

        # PROVIDE_FORMAT_STRING_FOR_MEASURES
        for measure in table.measures:
            if not measure.format_string:
                out.append(
                    SuggestionSpec(
                        kind="model_bpa",
                        object_ref=f"{table.name}[{measure.name}]",
                        field="format_string",
                        current_value=None,
                        proposed_value=_proposed_measure_format(
                            measure.name, measure.expression
                        ),
                        rationale="BPA: every measure should carry an explicit format string.",
                        code="BPA_PROVIDE_FORMAT_STRING_FOR_MEASURES",
                        confidence=0.8,
                    )
                )

    # MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION — switch bidirectional
    # many-to-many relationships back to single-direction cross filtering.
    for rel in spec.relationships:
        if (
            rel.from_cardinality == "many"
            and rel.to_cardinality == "many"
            and rel.cross_filtering_behavior == "bothDirections"
        ):
            out.append(
                SuggestionSpec(
                    kind="model_bpa",
                    object_ref=(
                        f"{rel.from_table}[{rel.from_column}] -> "
                        f"{rel.to_table}[{rel.to_column}]"
                    ),
                    field="cross_filtering_behavior",
                    current_value="bothDirections",
                    proposed_value="oneDirection",
                    rationale="BPA: many-to-many relationships should filter in a single direction to avoid ambiguity.",
                    code="BPA_MANY_TO_MANY_RELATIONSHIPS_SHOULD_BE_SINGLE_DIRECTION",
                    confidence=0.7,
                )
            )

    return out


# ---------------------------------------------------------------------------
# Semantic model — Copilot prep
# ---------------------------------------------------------------------------


_DOMAIN_TERMS = copilot_prep._DOMAIN_TERMS  # share the single source of truth


def _domain_hit(name: str) -> str | None:
    lowered = name.lower()
    for term in _DOMAIN_TERMS:
        if term.strip() in lowered:
            return term.strip()
    return None


def propose_copilot_prep_fixes(spec: SemanticModelSpec) -> list[SuggestionSpec]:
    """Generate Copilot-readiness write-back suggestions for ``spec``."""
    out: list[SuggestionSpec] = []

    if not spec.description:
        out.append(
            SuggestionSpec(
                kind="model_copilot",
                object_ref=spec.name,
                field="model.description",
                current_value=None,
                proposed_value=(
                    f"{_humanise(spec.name)} semantic model — provides the curated "
                    "subject area Copilot grounds natural-language answers on."
                ),
                rationale="Copilot grounds its answers on the model description.",
                code="SM_COPILOT_MODEL_NO_DESCRIPTION",
            )
        )

    for table in spec.tables:
        hit = _domain_hit(table.name)
        if hit and not table.description:
            out.append(
                SuggestionSpec(
                    kind="model_copilot",
                    object_ref=table.name,
                    field="description",
                    current_value=None,
                    proposed_value=(
                        f"{_humanise(table.name)} — {_DOMAIN_TERMS[hit]} "
                        "(see this description so Copilot understands the term)."
                    ),
                    rationale=f"Domain term '{hit.upper()}' on a table needs a description for Copilot.",
                    code="SM_COPILOT_DOMAIN_TERM_UNDESCRIBED",
                )
            )

        for col in table.columns:
            if col.is_hidden:
                continue
            ref = f"{table.name}[{col.name}]"
            hit = _domain_hit(col.name)
            if hit and not col.description:
                out.append(
                    SuggestionSpec(
                        kind="model_copilot",
                        object_ref=ref,
                        field="description",
                        current_value=None,
                        proposed_value=(
                            f"{_humanise(col.name)} — {_DOMAIN_TERMS[hit]}."
                        ),
                        rationale=f"Domain term '{hit.upper()}' needs an explicit description for Copilot.",
                        code="SM_COPILOT_DOMAIN_TERM_UNDESCRIBED",
                    )
                )

        for measure in table.measures:
            ref = f"{table.name}[{measure.name}]"
            if not measure.description:
                out.append(
                    SuggestionSpec(
                        kind="model_copilot",
                        object_ref=ref,
                        field="description",
                        current_value=None,
                        proposed_value=f"{_humanise(measure.name)} — describe what this measure computes.",
                        rationale="Copilot surfaces measure descriptions when answering questions.",
                        code="SM_COPILOT_MEASURE_NO_DESCRIPTION",
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Report — theme remediation
# ---------------------------------------------------------------------------


def _theme_needs_remediation(spec: ReportSpec, target: ReportTheme) -> bool:
    """Decide whether the current theme should be replaced by the branded one."""
    current = spec.theme
    if current is None:
        return True
    findings = report_formatting.audit(spec)
    bad_codes = {
        "RPT_FMT_TEXT_CONTRAST",
        "RPT_FMT_DATACOLOR_CONTRAST",
        "RPT_FMT_NO_CUSTOM_THEME",
    }
    return any(f.code in bad_codes for f in findings.findings)


def propose_theme_remediation(
    spec: ReportSpec, brand_theme: ReportTheme | None = None
) -> list[SuggestionSpec]:
    """Generate report-theme + visual-title suggestions for ``spec``.

    A single ``theme.*`` suggestion replaces the report theme with the branded
    default when the formatting auditor flags accessibility issues (or when no
    custom theme is in use). Per-visual ``title`` suggestions are generated for
    untitled visuals so the report meets WCAG 1.3.1.
    """
    target = brand_theme or load_brand_theme()
    out: list[SuggestionSpec] = []

    if _theme_needs_remediation(spec, target):
        current = spec.theme
        out.extend(
            [
                SuggestionSpec(
                    kind="report_theme",
                    object_ref=f"theme:{target.name}",
                    field="theme.background",
                    current_value=current.background if current else None,
                    proposed_value=target.background,
                    rationale="Apply the WCAG-AA branded background colour.",
                    code="RPT_FMT_THEME_REMEDIATION",
                ),
                SuggestionSpec(
                    kind="report_theme",
                    object_ref=f"theme:{target.name}",
                    field="theme.foreground",
                    current_value=current.foreground if current else None,
                    proposed_value=target.foreground,
                    rationale="Apply the WCAG-AA branded text colour.",
                    code="RPT_FMT_THEME_REMEDIATION",
                ),
                SuggestionSpec(
                    kind="report_theme",
                    object_ref=f"theme:{target.name}",
                    field="theme.data_colors",
                    current_value=list(current.data_colors) if current else [],
                    proposed_value=list(target.data_colors),
                    rationale="Apply the WCAG-AA branded qualitative palette.",
                    code="RPT_FMT_THEME_REMEDIATION",
                ),
                SuggestionSpec(
                    kind="report_theme",
                    object_ref=f"theme:{target.name}",
                    field="theme.name",
                    current_value=current.name if current else None,
                    proposed_value=target.name,
                    rationale="Adopt the branded theme name so consumers recognise the design system.",
                    code="RPT_FMT_THEME_REMEDIATION",
                    confidence=0.9,
                ),
            ]
        )

    for page in spec.pages:
        for visual in page.visuals:
            if not visual.title:
                out.append(
                    SuggestionSpec(
                        kind="report_formatting",
                        object_ref=f"{page.name}/{visual.name}",
                        field="title",
                        current_value=None,
                        proposed_value=_humanise(visual.name),
                        rationale="Add a descriptive visual title for accessibility (WCAG 1.3.1).",
                        code="RPT_FMT_VISUAL_NO_TITLE",
                    )
                )
    return out


__all__ = [
    "load_brand_theme",
    "propose_usability_fixes",
    "propose_copilot_prep_fixes",
    "propose_theme_remediation",
]
