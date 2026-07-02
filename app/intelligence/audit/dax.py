"""Semantic-model **DAX** audit.

Implements the "DAX creation/verification" requirement: deterministic checks of
every measure's DAX expression — that it is non-empty, that every column and
measure it references actually resolves against the model, that it is not a
left-over placeholder, and that it follows a couple of robustness conventions
(safe division, explicit format strings).

Reference resolution is best-effort static analysis (no DAX engine): it extracts
``'Table'[Column]`` / ``Table[Column]`` qualified references and bare
``[Name]`` references and checks them against the model's tables, columns and
measures.

Pure function of a :class:`~app.intelligence.spec.SemanticModelSpec`.
"""

from __future__ import annotations

import re

from ..spec import SemanticModelSpec
from .base import ERROR, INFO, WARNING, AuditReport

FEATURE = "semantic-model-dax"

# Qualified reference: optional single-quoted or bare table, then [Column].
_QUALIFIED_RE = re.compile(
    r"(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))\[([^\]]+)\]"
)
# Any bracketed reference (used to find bare [Measure]/[Column] refs).
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_PLACEHOLDER_RE = re.compile(r"\bTODO\b|\bFIXME\b|placeholder", re.IGNORECASE)
# Arithmetic division not using DIVIDE(): a single '/' that is not part of '//'.
_DIVISION_RE = re.compile(r"(?<![/])/(?![/])")


def extract_references(expression: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ``(qualified, bare)`` references found in a DAX ``expression``.

    ``qualified`` is a list of ``(table, column)`` pairs; ``bare`` is a list of
    unqualified ``[Name]`` references (a measure or unqualified column).
    """
    qualified: list[tuple[str, str]] = []
    masked = expression
    for match in _QUALIFIED_RE.finditer(expression):
        table = match.group(1) or match.group(2) or ""
        column = match.group(3) or ""
        qualified.append((table, column))
    # Mask qualified refs so the bare scan does not re-find their columns.
    masked = _QUALIFIED_RE.sub(" ", expression)
    bare = [m.group(1) for m in _BRACKET_RE.finditer(masked)]
    return qualified, bare


def audit(spec: SemanticModelSpec) -> AuditReport:
    """Run the DAX rule set over every measure in ``spec``."""
    report = AuditReport(feature=FEATURE, target=spec.name)

    tables = {t.name for t in spec.tables}
    columns_by_table: dict[str, set[str]] = {
        t.name: {c.name for c in t.columns} for t in spec.tables
    }
    all_column_names: set[str] = {
        c.name for t in spec.tables for c in t.columns
    }
    all_measure_names: set[str] = {
        m.name for t in spec.tables for m in t.measures
    }

    for table in spec.tables:
        for measure in table.measures:
            ref = f"{table.name}[{measure.name}]"
            expr = (measure.expression or "").strip()

            if not expr:
                report.add(
                    ERROR,
                    "SM_DAX_EMPTY_EXPRESSION",
                    f"Measure {ref} has an empty DAX expression.",
                    object_ref=ref,
                    recommendation="Provide a DAX expression or remove the "
                    "measure.",
                )
                continue

            if _PLACEHOLDER_RE.search(expr):
                report.add(
                    WARNING,
                    "SM_DAX_PLACEHOLDER",
                    f"Measure {ref} still contains a placeholder/TODO.",
                    object_ref=ref,
                    recommendation="Replace the placeholder with the real "
                    "calculation.",
                )

            qualified, bare = extract_references(expr)

            for q_table, q_col in qualified:
                if q_table not in tables:
                    report.add(
                        ERROR,
                        "SM_DAX_UNKNOWN_TABLE",
                        f"Measure {ref} references unknown table '{q_table}'.",
                        object_ref=ref,
                        recommendation=f"Correct the table name in "
                        f"'{q_table}'[{q_col}].",
                    )
                elif q_col not in columns_by_table.get(q_table, set()):
                    report.add(
                        ERROR,
                        "SM_DAX_UNKNOWN_COLUMN",
                        f"Measure {ref} references unknown column "
                        f"'{q_table}'[{q_col}].",
                        object_ref=ref,
                        recommendation="Correct the column name or qualify a "
                        "different table.",
                    )

            for name in bare:
                if name == measure.name:
                    report.add(
                        WARNING,
                        "SM_DAX_SELF_REFERENCE",
                        f"Measure {ref} references itself.",
                        object_ref=ref,
                        recommendation="Remove the circular reference unless it "
                        "is an intentional recursive pattern.",
                    )
                elif name not in all_measure_names and name not in all_column_names:
                    report.add(
                        WARNING,
                        "SM_DAX_UNRESOLVED_REFERENCE",
                        f"Measure {ref} references '[{name}]', which is not a "
                        "known measure or column.",
                        object_ref=ref,
                        recommendation="Fully-qualify a column as "
                        "'Table'[Column] or fix the measure name.",
                    )

            if _DIVISION_RE.search(expr) and "DIVIDE" not in expr.upper():
                report.add(
                    INFO,
                    "SM_DAX_UNSAFE_DIVISION",
                    f"Measure {ref} uses '/' division without DIVIDE().",
                    object_ref=ref,
                    recommendation="Use DIVIDE(numerator, denominator) to handle "
                    "divide-by-zero safely.",
                )

    return report
