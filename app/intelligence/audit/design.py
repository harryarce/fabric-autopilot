"""Semantic-model **design** audit.

Implements the "Semantic Model Creation/Design" review: deterministic structural
rules about relationships, star-schema shape, cardinality and modelling
correctness — the things that make a model *correct and performant* rather than
merely *readable* (which is :mod:`usability`'s job).

Pure function of a :class:`~app.intelligence.spec.SemanticModelSpec`.
"""

from __future__ import annotations

import re

from ..spec import SemanticModelSpec, resolvable_relationships
from .base import ERROR, INFO, WARNING, AuditReport

FEATURE = "semantic-model-design"

_DATE_NAME_RE = re.compile(r"\b(date|calendar|time|period)\b", re.IGNORECASE)


def audit(spec: SemanticModelSpec) -> AuditReport:
    """Run the model-design rule set over ``spec``."""
    report = AuditReport(feature=FEATURE, target=spec.name)

    # 1. Relationships that reference missing tables/columns are fatal.
    _valid, dropped = resolvable_relationships(spec)
    for rel in dropped:
        ref = f"{rel.from_table}[{rel.from_column}] -> {rel.to_table}[{rel.to_column}]"
        report.add(
            ERROR,
            "SM_DESIGN_DANGLING_RELATIONSHIP",
            "Relationship references a table or column that does not exist.",
            object_ref=ref,
            recommendation="Fix or remove the relationship; Fabric rejects a "
            "model with unresolvable relationship paths.",
        )

    # 2. No relationships at all (only meaningful with >1 table).
    if len(spec.tables) > 1 and not spec.relationships:
        report.add(
            WARNING,
            "SM_DESIGN_NO_RELATIONSHIPS",
            "The model has multiple tables but no relationships.",
            recommendation="Relate fact and dimension tables so cross-filtering "
            "works.",
        )

    # 3. Island tables (no relationship touches them).
    related: set[str] = set()
    for rel in spec.relationships:
        related.add(rel.from_table)
        related.add(rel.to_table)
    for table in spec.tables:
        if len(spec.tables) > 1 and not table.is_hidden and table.name not in related:
            report.add(
                WARNING,
                "SM_DESIGN_ISLAND_TABLE",
                f"Table '{table.name}' participates in no relationship.",
                object_ref=table.name,
                recommendation="Relate it to the model or remove it if unused.",
            )

    # 4. Bidirectional cross-filtering invites ambiguity.
    for i, rel in enumerate(spec.relationships, start=1):
        if rel.cross_filtering_behavior == "bothDirections":
            ref = f"{rel.from_table}[{rel.from_column}] -> {rel.to_table}[{rel.to_column}]"
            report.add(
                INFO,
                "SM_DESIGN_BIDIRECTIONAL_FILTER",
                "Relationship uses bidirectional cross-filtering.",
                object_ref=ref,
                recommendation="Prefer single-direction filtering unless a "
                "many-to-many scenario truly requires both directions.",
            )

    # 5. Duplicate relationships between the same column pair.
    seen: set[tuple[str, str, str, str]] = set()
    for rel in spec.relationships:
        sig = (rel.from_table, rel.from_column, rel.to_table, rel.to_column)
        if sig in seen:
            report.add(
                WARNING,
                "SM_DESIGN_DUPLICATE_RELATIONSHIP",
                "Duplicate relationship between the same columns.",
                object_ref=f"{rel.from_table}[{rel.from_column}] -> "
                f"{rel.to_table}[{rel.to_column}]",
                recommendation="Remove the duplicate; keep at most one active "
                "relationship per column pair.",
            )
        seen.add(sig)

    # 6. A table named like a date dimension but not marked as one.
    for table in spec.tables:
        if _DATE_NAME_RE.search(table.name) and not table.is_date_table:
            report.add(
                WARNING,
                "SM_DESIGN_UNMARKED_DATE_TABLE",
                f"Table '{table.name}' looks like a date table but is not marked.",
                object_ref=table.name,
                recommendation="Mark it as a date table to enable time "
                "intelligence.",
            )

    # 7. Fact-shaped tables (many relationships in) with no measures.
    incoming: dict[str, int] = {}
    for rel in spec.relationships:
        incoming[rel.to_table] = incoming.get(rel.to_table, 0) + 1
        # the "many" side (from_table) is the fact in a star schema
    fact_candidates = {rel.from_table for rel in spec.relationships}
    for table in spec.tables:
        if table.name in fact_candidates and not table.measures:
            report.add(
                INFO,
                "SM_DESIGN_FACT_NO_MEASURE",
                f"Fact-like table '{table.name}' defines no measures.",
                object_ref=table.name,
                recommendation="Add measures (e.g. totals, counts) to the fact "
                "table for analysis.",
            )

    _audit_m2m_without_bridge(report, spec)
    _audit_mixed_grain_fact(report, spec)
    _audit_non_conforming_dimensions(report, spec)

    return report


# -- additional design rules ------------------------------------------------


def _audit_m2m_without_bridge(report: AuditReport, spec: SemanticModelSpec) -> None:
    """Flag relationships where both sides are non-key columns (likely M:M).

    A relationship whose ``from_column`` is not the primary/foreign key on
    ``from_table`` AND whose ``to_column`` is not a key on ``to_table`` is most
    likely a many-to-many that should be resolved through a bridge table.
    """
    by_name = {t.name: t for t in spec.tables}
    for rel in spec.relationships:
        from_t = by_name.get(rel.from_table)
        to_t = by_name.get(rel.to_table)
        if not from_t or not to_t:
            continue  # dangling already reported
        if rel.from_cardinality == "many" and rel.to_cardinality == "many":
            report.add(
                WARNING,
                "SM_DESIGN_M2M_NO_BRIDGE",
                (
                    f"Many-to-many relationship "
                    f"{rel.from_table}[{rel.from_column}] <-> "
                    f"{rel.to_table}[{rel.to_column}] has no bridge table."
                ),
                object_ref=f"{rel.from_table}[{rel.from_column}] -> "
                f"{rel.to_table}[{rel.to_column}]",
                recommendation=(
                    "Introduce a bridge (junction) table that holds the "
                    "distinct relationship pairs and connect both sides as "
                    "many-to-one. M:M relationships without a bridge are a "
                    "common source of incorrect totals."
                ),
            )
            continue
        from_col = next(
            (c for c in from_t.columns if c.name == rel.from_column), None
        )
        to_col = next((c for c in to_t.columns if c.name == rel.to_column), None)
        if not from_col or not to_col:
            continue
        from_key = from_col.is_key or _looks_key_name(rel.from_column)
        to_key = to_col.is_key or _looks_key_name(rel.to_column)
        if not from_key and not to_key:
            report.add(
                INFO,
                "SM_DESIGN_M2M_NO_BRIDGE",
                (
                    f"Relationship "
                    f"{rel.from_table}[{rel.from_column}] -> "
                    f"{rel.to_table}[{rel.to_column}] joins non-key columns "
                    "on both sides."
                ),
                object_ref=f"{rel.from_table}[{rel.from_column}] -> "
                f"{rel.to_table}[{rel.to_column}]",
                recommendation=(
                    "Join on a real key column (PK ↔ FK), or introduce a "
                    "bridge table if a many-to-many is intentional."
                ),
            )


def _audit_mixed_grain_fact(report: AuditReport, spec: SemanticModelSpec) -> None:
    """Detect fact tables that mix granularities in their measures.

    Heuristic: a fact-like table (referenced by ≥1 relationship as the "many"
    side) whose measures include BOTH count-style aggregates AND sum-style
    aggregates on columns that are not all marked summarizable suggests
    multiple business processes sharing one fact table.
    """
    fact_names = {rel.from_table for rel in spec.relationships}
    for table in spec.tables:
        if table.name not in fact_names:
            continue
        if len(table.measures) < 2:
            continue
        sum_like = 0
        count_like = 0
        for m in table.measures:
            expr = (m.expression or "").upper()
            if "DISTINCTCOUNT" in expr or "COUNTROWS" in expr or expr.startswith(
                "COUNT("
            ):
                count_like += 1
            elif expr.startswith("SUM(") or "SUMX" in expr or "SUM(" in expr:
                sum_like += 1
        if sum_like >= 1 and count_like >= 1 and (sum_like + count_like) >= 3:
            report.add(
                INFO,
                "SM_DESIGN_MIXED_GRAIN_FACT",
                (
                    f"Fact table '{table.name}' mixes count-style and sum-style "
                    f"measures ({count_like} count, {sum_like} sum)."
                ),
                object_ref=table.name,
                recommendation=(
                    "Verify all measures share the same grain. If they don't, "
                    "split the fact into one table per business process."
                ),
            )


def _audit_non_conforming_dimensions(
    report: AuditReport, spec: SemanticModelSpec
) -> None:
    """Detect the same logical dimension joined to multiple facts via different keys.

    Looks for tables that appear on the ``to`` side of relationships from
    multiple fact-like tables but where the ``to_column`` differs across those
    relationships — a classic non-conforming dimension symptom.
    """
    keys_by_dim: dict[str, set[str]] = {}
    facts_by_dim: dict[str, set[str]] = {}
    for rel in spec.relationships:
        keys_by_dim.setdefault(rel.to_table, set()).add(rel.to_column)
        facts_by_dim.setdefault(rel.to_table, set()).add(rel.from_table)
    for dim, keys in keys_by_dim.items():
        if len(keys) > 1 and len(facts_by_dim.get(dim, set())) > 1:
            report.add(
                WARNING,
                "SM_DESIGN_NON_CONFORMING_DIM",
                (
                    f"Dimension '{dim}' is joined to multiple facts via "
                    f"different keys: {sorted(keys)}."
                ),
                object_ref=dim,
                recommendation=(
                    "Conform the dimension by exposing a single canonical key "
                    "used by every fact, or split into separate dimensions if "
                    "the grains genuinely differ."
                ),
            )


_KEY_NAME_RE = re.compile(r"(id|key|sk|fk|guid|code)$", re.IGNORECASE)


def _looks_key_name(name: str) -> bool:
    return bool(_KEY_NAME_RE.search(name or ""))
