"""Deterministic natural-language → DAX translator.

This module turns a free-form question (``"top 10 customers by revenue"``)
into a DAX query that can be sent to the
``@microsoft/powerbi-modeling-mcp`` server's ``dax_query_operations`` tool.

The translator is deliberately **deterministic and offline-first** — it does
not call any model service and never invents column names. It matches
tokens from the question against the names that actually live in the
provided :class:`~app.intelligence.spec.SemanticModelSpec` (tables, columns,
measures) and assembles a small number of well-known DAX patterns:

* ``EVALUATE TOPN(N, table, ORDER BY col DESC)`` for *top/bottom* questions.
* ``EVALUATE ROW("Total <X>", SUM(table[col]))`` for *total/sum/average/min/
  max/count* aggregation questions. Uses ``COUNTA`` for text columns and
  ``DISTINCTCOUNT`` when *"distinct"* / *"unique"* appears in the question.
* ``EVALUATE ROW("Count", COUNTROWS(table))`` for *how many rows* questions
  (with ``COUNTROWS(DISTINCT(table))`` for *"how many distinct X"*).
* ``EVALUATE SUMMARIZECOLUMNS(k1, k2, …, "Measure", measure_expr)`` when a
  question references one or more grouping columns after ``by`` / ``per``
  — multi-key grouping is supported so *"Total Sales by Country and
  Category"* produces both group keys.
* ``EVALUATE CALCULATETABLE(<expr>, <col> = <literal>, …)`` when the
  question includes one or more simple equality filters such as
  *"where Country = 'USA'"* or *"for Year is 2024"*.
* ``EVALUATE TOPN(N, table)`` as a safe default — "show me the data".

A confidence label and a human-readable explanation are returned alongside
the DAX so the UI can warn the user when the heuristics had to guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .spec import (
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticTable,
    dax_qualified_column,
    dax_table_ref,
)

# Default row cap for "top/show" style questions. Power BI's executeQueries
# endpoint already caps each table at 100,000 rows, but we keep things small
# for an interactive UI by default.
_DEFAULT_TOPN = 50
_MAX_TOPN = 1000

# Tokens that switch the query intent into "show the rows".
_BROWSE_TOKENS = {"show", "list", "browse", "preview", "rows", "records"}
# Aggregate verbs → DAX function. Order matters for matching priority.
_AGG_VERBS: tuple[tuple[str, str], ...] = (
    ("average", "AVERAGE"),
    ("avg", "AVERAGE"),
    ("mean", "AVERAGE"),
    ("total", "SUM"),
    ("sum", "SUM"),
    ("minimum", "MIN"),
    ("smallest", "MIN"),
    ("min", "MIN"),
    ("maximum", "MAX"),
    ("largest", "MAX"),
    ("max", "MAX"),
    ("count", "COUNT"),
)
# Words that mean "ascending" — opposite of TOPN's default descending order.
_ASC_TOKENS = {"bottom", "lowest", "smallest", "least", "min", "minimum"}
_DESC_TOKENS = {"top", "highest", "largest", "most", "biggest", "max", "maximum"}
# "How many <thing>" questions → COUNTROWS(<thing's table>).
_HOW_MANY_RE = re.compile(r"\bhow\s+many\b", re.IGNORECASE)
# "Top N" / "Bottom N" pattern; N is optional and defaults to 10.
_TOPN_RE = re.compile(
    r"\b(top|bottom|first|last|highest|lowest)\s*(\d+)?\b", re.IGNORECASE
)

# Tokenisation pattern shared by token-set lookups.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# DAX aggregation functions that only accept numeric values. Wrapping a
# string column in one of these produces the Fabric-side error *"The function
# <FN> cannot work with values of type String."* — we skip string columns
# when picking an aggregation target for these functions.
_NUMERIC_AGG_FUNCTIONS: frozenset[str] = frozenset(
    {"SUM", "AVERAGE", "MIN", "MAX"}
)

# Column ``data_type`` values (from the TMDL/TMSL importer) that DAX treats
# as numeric for the purposes of SUM/AVG/MIN/MAX. Kept broad enough to cover
# the tabular type names emitted by both the on-the-fly import (see
# ``app/intelligence/tmdl_parser.py``) and the spec dataclass defaults
# (``app/intelligence/spec.py``).
_NUMERIC_DATA_TYPES: frozenset[str] = frozenset(
    {
        "int64",
        "integer",
        "int",
        "long",
        "decimal",
        "double",
        "float",
        "single",
        "number",
        "numeric",
        "money",
        "currency",
    }
)

# Tokens that flip a COUNT or "how many" question into DISTINCTCOUNT and
# stand-alone questions like *"unique customers"* into a distinct-count
# scalar. Matches the natural way users talk about cardinality in DAX
# (`DISTINCTCOUNT`, `DISTINCT`, `VALUES`).
_DISTINCT_TOKENS: frozenset[str] = frozenset({"distinct", "unique"})

# Keywords that introduce a grouping specification — everything after one
# of these tokens is treated as a column list for ``SUMMARIZECOLUMNS``.
# Supports multi-key grouping via *"by X and Y"* / *"per X, Y"*.
_GROUPING_KEYWORDS: frozenset[str] = frozenset({"by", "per"})

# Regex used to extract simple equality filters from a question, one column
# at a time. Recognises: ``=``, ``==``, ``is``, ``equals``, ``equal to``,
# with a double-quoted string, single-quoted string, or plain number as the
# right-hand side. Applied per column so we only ever produce filters that
# reference a column that actually exists in the model.
_FILTER_OP_RE = r"(?:==|=|equal(?:s)?\s+to|equal(?:s)?|is)"
_FILTER_VALUE_RE = (
    r"""(?:"([^"]+)"|'([^']+)'|(-?\d+(?:\.\d+)?))"""
)


@dataclass(frozen=True)
class _FilterClause:
    """A single ``<table>[<col>] = <literal>`` filter extracted from the question."""

    table: str
    column: str
    value_dax: str  # already-escaped DAX literal, ready to splice into DAX

    def to_dax(self) -> str:
        return f"{dax_qualified_column(self.table, self.column)} = {self.value_dax}"

    def describe(self) -> str:
        return f"{self.table}[{self.column}] = {self.value_dax}"


@dataclass
class DaxTranslation:
    """Result of translating a question into a DAX query."""

    dax: str
    intent: str  # "topn" | "aggregate" | "summarize" | "rowcount" | "browse"
    explanation: str
    confidence: str  # "high" | "medium" | "low"
    referenced_tables: list[str] = field(default_factory=list)
    referenced_columns: list[str] = field(default_factory=list)
    referenced_measures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class NlToDaxError(ValueError):
    """Raised when a question cannot be mapped to any DAX query at all."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def nl_to_dax(
    question: str,
    spec: SemanticModelSpec,
    *,
    default_topn: int = _DEFAULT_TOPN,
) -> DaxTranslation:
    """Translate ``question`` into a single DAX query grounded in ``spec``.

    The translator never returns an empty result: if it cannot recognise any
    table or measure, it raises :class:`NlToDaxError` so the caller can ask
    the user to rephrase rather than executing a meaningless query.
    """
    if not question or not question.strip():
        raise NlToDaxError("Question is empty.")
    if not spec.tables:
        raise NlToDaxError("The semantic model has no tables to query.")

    tokens = _tokenise(question)
    token_set = {t.lower() for t in tokens}
    topn_match = _TOPN_RE.search(question)
    direction = _direction(token_set, topn_match)
    requested_n = _requested_n(topn_match, default_topn)

    measure = _match_measure(tokens, spec)
    tables = _match_tables(tokens, spec)
    columns = _match_columns(tokens, spec, prefer_tables=tables)
    agg = _match_aggregate(tokens)
    wants_distinct = _wants_distinct(token_set)
    filters = _match_filters(question, spec)
    # Columns used in an equality filter should never also be treated as a
    # grouping key ("Sales where Country = 'USA'" filters on Country, does
    # not group by it) or as a match trigger for column-based branches.
    filter_col_keys: set[tuple[str, str]] = {
        (f.table, f.column) for f in filters
    }
    if filter_col_keys:
        columns = [
            pair for pair in columns
            if (pair[0].name, pair[1].name) not in filter_col_keys
        ]

    # --- 1. "how many <table>" → COUNTROWS / DISTINCTCOUNT ------------------
    if _HOW_MANY_RE.search(question):
        # "How many distinct customers" → DISTINCTCOUNT of the named column,
        # or COUNTROWS(DISTINCT(<table>)) when only a table was named.
        if wants_distinct and columns:
            tgt_table, tgt_col = columns[0]
            label = f"Distinct {tgt_col.name}"
            dax = (
                "EVALUATE\nROW(\n"
                f'    "{_escape_label(label)}", '
                f"DISTINCTCOUNT({dax_qualified_column(tgt_table.name, tgt_col.name)})\n"
                ")"
            )
            dax = _apply_filters_to_dax(dax, filters)
            return DaxTranslation(
                dax=dax,
                intent="aggregate",
                explanation=(
                    f"Distinct count of '{tgt_table.name}'[{tgt_col.name}]."
                ),
                confidence="high",
                referenced_tables=[tgt_table.name],
                referenced_columns=[tgt_col.name],
            )
        if wants_distinct and tables:
            table = tables[0]
            label = f"Distinct {table.name}"
            dax = (
                "EVALUATE\nROW(\n"
                f'    "{_escape_label(label)}", '
                f"COUNTROWS(DISTINCT({dax_table_ref(table.name)}))\n"
                ")"
            )
            dax = _apply_filters_to_dax(dax, filters)
            return DaxTranslation(
                dax=dax,
                intent="aggregate",
                explanation=f"Count of distinct rows of '{table.name}'.",
                confidence="high",
                referenced_tables=[table.name],
            )
        table = tables[0] if tables else _table_of_columns(columns) or spec.tables[0]
        dax = (
            f"EVALUATE\nROW(\n    \"Row count\", COUNTROWS({dax_table_ref(table.name)})\n)"
        )
        dax = _apply_filters_to_dax(dax, filters)
        return DaxTranslation(
            dax=dax,
            intent="rowcount",
            explanation=f"Count rows of '{table.name}'.",
            confidence="high" if tables else "medium",
            referenced_tables=[table.name],
            warnings=[]
            if tables
            else ["No table was named — defaulted to the first table."],
        )

    # --- 2. Measure mentioned ----------------------------------------------
    if measure is not None:
        m_table, m_obj = measure
        # Multi-key grouping: use every column named after 'by'/'per'.
        exclude_from_group = filter_col_keys | {
            (t.name, c.name)
            for t in spec.tables
            for c in t.columns
            if c.name.lower() == m_obj.name.lower()
        }
        grouping_columns = _group_columns_from_by(
            tokens,
            spec,
            exclude=exclude_from_group,
            prefer_tables=tables,
        )
        # Backwards-compatibility: if the user did *not* write "by/per" but
        # named exactly one non-measure column, still group by it — this
        # keeps the intuitive *"Total Sales Country"* pattern working.
        if not grouping_columns:
            for t, c in columns:
                if c.name.lower() != m_obj.name.lower() and (t.name, c.name) not in filter_col_keys:
                    grouping_columns = [(t, c)]
                    break
        filter_note = (
            " filtered by " + ", ".join(f.describe() for f in filters)
            if filters
            else ""
        )
        if grouping_columns:
            key_lines = ",\n".join(
                f"    {dax_qualified_column(t.name, c.name)}"
                for t, c in grouping_columns
            )
            dax = (
                "EVALUATE\nSUMMARIZECOLUMNS(\n"
                f"{key_lines},\n"
                f'    "{_escape_label(m_obj.name)}", [{m_obj.name}]\n'
                ")"
            )
            if direction is not None:
                dax = _wrap_topn(dax, requested_n, m_obj.name, direction)
            dax = _apply_filters_to_dax(dax, filters)
            group_desc = ", ".join(
                f"{t.name}[{c.name}]" for t, c in grouping_columns
            )
            return DaxTranslation(
                dax=dax,
                intent="summarize",
                explanation=(
                    f"Summarise measure '[{m_obj.name}]' by {group_desc}"
                    + filter_note
                    + (f" — {direction.lower()} {requested_n}." if direction else ".")
                ),
                confidence="high",
                referenced_tables=sorted(
                    {m_table.name} | {t.name for t, _ in grouping_columns}
                ),
                referenced_columns=sorted({c.name for _, c in grouping_columns}),
                referenced_measures=[m_obj.name],
            )
        dax = f"EVALUATE\nROW(\n    \"{_escape_label(m_obj.name)}\", [{m_obj.name}]\n)"
        dax = _apply_filters_to_dax(dax, filters)
        return DaxTranslation(
            dax=dax,
            intent="aggregate",
            explanation=(
                f"Return the value of measure [{m_obj.name}]" + filter_note + "."
            ),
            confidence="high",
            referenced_tables=[m_table.name],
            referenced_measures=[m_obj.name],
        )

    # --- 2b. "distinct/unique <column>" without a count verb ---------------
    # A stand-alone *"distinct customers"* or *"unique products"* is a
    # cardinality question — surface it as a DISTINCTCOUNT scalar.
    if wants_distinct and not agg and (columns or tables):
        if columns:
            tgt_table, tgt_col = columns[0]
            label = f"Distinct {tgt_col.name}"
            dax = (
                "EVALUATE\nROW(\n"
                f'    "{_escape_label(label)}", '
                f"DISTINCTCOUNT({dax_qualified_column(tgt_table.name, tgt_col.name)})\n"
                ")"
            )
            dax = _apply_filters_to_dax(dax, filters)
            return DaxTranslation(
                dax=dax,
                intent="aggregate",
                explanation=(
                    f"Distinct count of '{tgt_table.name}'[{tgt_col.name}]."
                ),
                confidence="high",
                referenced_tables=[tgt_table.name],
                referenced_columns=[tgt_col.name],
            )
        table = tables[0]
        label = f"Distinct {table.name}"
        dax = (
            "EVALUATE\nROW(\n"
            f'    "{_escape_label(label)}", '
            f"COUNTROWS(DISTINCT({dax_table_ref(table.name)}))\n"
            ")"
        )
        dax = _apply_filters_to_dax(dax, filters)
        return DaxTranslation(
            dax=dax,
            intent="aggregate",
            explanation=f"Count of distinct rows of '{table.name}'.",
            confidence="high",
            referenced_tables=[table.name],
        )

    # --- 3. Aggregation verb (sum/avg/min/max/count) on a column -----------
    if agg and columns:
        agg_label, agg_fn = agg
        # SUM/AVERAGE/MIN/MAX only work on numeric columns; picking a string
        # column produces the Fabric error *"The function SUM cannot work
        # with values of type String."* When the question named a string
        # column, prefer the first *numeric* match instead. If nothing
        # numeric was named, drop through to the TopN/browse branches
        # instead of generating a doomed query.
        if agg_fn in _NUMERIC_AGG_FUNCTIONS:
            target = _pick_numeric_target(columns)
            if target is None:
                # Skip to the next intent — a TopN sort or a row preview is
                # more useful than a query the server will reject outright.
                pass
            else:
                target_col_table, target_col = target
                target_name_lc = target_col.name.lower()
                exclude_from_group = filter_col_keys | {
                    (t.name, c.name)
                    for t in spec.tables
                    for c in t.columns
                    if c.name.lower() == target_name_lc
                }
                grouping_columns = _group_columns_from_by(
                    tokens,
                    spec,
                    exclude=exclude_from_group,
                    prefer_tables=tables,
                )
                # Fallback: no 'by/per' keyword, but the user named a second
                # non-target column — treat it as an implicit group key so
                # *"average OrderQuantity Category"* keeps working.
                if not grouping_columns:
                    grouping_columns = [
                        (t, c)
                        for t, c in columns
                        if c.name.lower() != target_name_lc
                    ][:1]
                return _emit_aggregate(
                    agg_label=agg_label,
                    agg_fn=agg_fn,
                    target=target,
                    grouping_columns=grouping_columns,
                    filters=filters,
                    direction=direction,
                    requested_n=requested_n,
                )
        else:
            # COUNT is defined for numeric columns only; text columns need
            # COUNTA (blanks-aware). ``distinct`` / ``unique`` flip this to
            # DISTINCTCOUNT which works uniformly on any column type.
            target_col_table, target_col = columns[0]
            if wants_distinct:
                fn_for_type = "DISTINCTCOUNT"
                agg_label = f"distinct {agg_label}"
            else:
                fn_for_type = "COUNT" if _is_numeric_column(target_col) else "COUNTA"
            target_name_lc = target_col.name.lower()
            exclude_from_group = filter_col_keys | {
                (t.name, c.name)
                for t in spec.tables
                for c in t.columns
                if c.name.lower() == target_name_lc
            }
            grouping_columns = _group_columns_from_by(
                tokens,
                spec,
                exclude=exclude_from_group,
                prefer_tables=tables,
            )
            if not grouping_columns:
                grouping_columns = [
                    (t, c)
                    for t, c in columns[1:]
                    if c.name.lower() != target_name_lc
                ][:1]
            return _emit_aggregate(
                agg_label=agg_label,
                agg_fn=fn_for_type,
                target=(target_col_table, target_col),
                grouping_columns=grouping_columns,
                filters=filters,
                direction=direction,
                requested_n=requested_n,
            )

    # --- 4. TopN by column on a single table -------------------------------
    if direction is not None and columns:
        sort_col_table, sort_col = columns[0]
        table = sort_col_table
        dax = (
            "EVALUATE\nTOPN(\n"
            f"    {requested_n},\n"
            f"    {dax_table_ref(table.name)},\n"
            f"    {dax_qualified_column(table.name, sort_col.name)}, {direction}\n"
            ")"
        )
        dax = _apply_filters_to_dax(dax, filters)
        return DaxTranslation(
            dax=dax,
            intent="topn",
            explanation=(
                f"Top {requested_n} rows of '{table.name}' ordered by "
                f"[{sort_col.name}] {direction}."
            ),
            confidence="high",
            referenced_tables=[table.name],
            referenced_columns=[sort_col.name],
        )

    # --- 5. Browse / show rows of a table ---------------------------------
    if tables and (token_set & _BROWSE_TOKENS or direction is not None):
        table = tables[0]
        n = requested_n if direction is not None else default_topn
        dax = (
            "EVALUATE\nTOPN(\n"
            f"    {n},\n    {dax_table_ref(table.name)}\n)"
        )
        dax = _apply_filters_to_dax(dax, filters)
        return DaxTranslation(
            dax=dax,
            intent="browse",
            explanation=f"First {n} rows of '{table.name}'.",
            confidence="medium",
            referenced_tables=[table.name],
        )

    # --- 6. Fallbacks ------------------------------------------------------
    # If the question named a table, use it; otherwise fall back to the
    # table of the first matched column so a question like *"sum of Name"*
    # (where "Name" is a string column) still returns a useful row preview
    # instead of raising.
    fallback_table = tables[0] if tables else _table_of_columns(columns)
    if fallback_table is not None:
        dax = (
            "EVALUATE\nTOPN(\n"
            f"    {default_topn},\n    {dax_table_ref(fallback_table.name)}\n)"
        )
        dax = _apply_filters_to_dax(dax, filters)
        warning = (
            "Question was ambiguous — heuristic fell back to a row preview."
        )
        if agg and not tables:
            # We had an aggregation verb but no numeric column matched, so
            # the caller almost certainly meant a numeric aggregate on a
            # column that doesn't exist as a number. Give a targeted hint.
            warning = (
                f"No numeric column matched '{agg[0]}' — showing a preview "
                f"of '{fallback_table.name}' instead. Try naming a numeric "
                "column explicitly."
            )
        return DaxTranslation(
            dax=dax,
            intent="browse",
            explanation=(
                f"Could not match the question to a measure or aggregate; "
                f"returning the first {default_topn} rows of '{fallback_table.name}'."
            ),
            confidence="low",
            referenced_tables=[fallback_table.name],
            warnings=[warning],
        )

    raise NlToDaxError(
        "Could not match the question to any table, column, or measure in the "
        "semantic model. Try naming the table or measure explicitly."
    )


# ---------------------------------------------------------------------------
# Internal matchers
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def _direction(tokens: set[str], topn_match: re.Match[str] | None) -> str | None:
    if topn_match:
        verb = topn_match.group(1).lower()
        if verb in {"bottom", "lowest", "last"}:
            return "ASC"
        return "DESC"
    if tokens & _DESC_TOKENS:
        return "DESC"
    if tokens & _ASC_TOKENS:
        return "ASC"
    return None


def _requested_n(topn_match: re.Match[str] | None, default_topn: int) -> int:
    if topn_match and topn_match.group(2):
        try:
            return max(1, min(int(topn_match.group(2)), _MAX_TOPN))
        except ValueError:
            pass
    return max(1, min(default_topn, _MAX_TOPN))


def _match_aggregate(tokens: Iterable[str]) -> tuple[str, str] | None:
    lowered = [t.lower() for t in tokens]
    for verb, fn in _AGG_VERBS:
        if verb in lowered:
            return verb, fn
    return None


def _match_tables(
    tokens: list[str], spec: SemanticModelSpec
) -> list[SemanticTable]:
    """Return tables whose names appear in ``tokens``, preserving spec order.

    A match is recorded when the full table name appears as a sequence of
    consecutive tokens (case-insensitive). Singular/plural variants are
    accepted so questions like *"top customers"* resolve to a ``Customer``
    table and vice-versa.
    """
    matches: list[SemanticTable] = []
    seen: set[str] = set()
    lowered = [t.lower() for t in tokens]
    for table in spec.tables:
        if _phrase_in_tokens(table.name, lowered):
            if table.name not in seen:
                matches.append(table)
                seen.add(table.name)
    return matches


def _match_columns(
    tokens: list[str],
    spec: SemanticModelSpec,
    *,
    prefer_tables: list[SemanticTable] | None = None,
) -> list[tuple[SemanticTable, SemanticColumn]]:
    """Return ``(table, column)`` pairs whose column name appears in tokens.

    Columns belonging to a table already mentioned in the question are
    listed first; this keeps grouping/aggregation choices stable.
    """
    preferred_names = {t.name for t in (prefer_tables or [])}
    lowered = [t.lower() for t in tokens]
    preferred: list[tuple[SemanticTable, SemanticColumn]] = []
    others: list[tuple[SemanticTable, SemanticColumn]] = []
    for table in spec.tables:
        for column in table.columns:
            if _phrase_in_tokens(column.name, lowered):
                bucket = preferred if table.name in preferred_names else others
                bucket.append((table, column))
    return preferred + others


def _match_measure(
    tokens: list[str], spec: SemanticModelSpec
) -> tuple[SemanticTable, SemanticMeasure] | None:
    lowered = [t.lower() for t in tokens]
    best: tuple[SemanticTable, SemanticMeasure, int] | None = None
    for table in spec.tables:
        for measure in table.measures:
            if _phrase_in_tokens(measure.name, lowered):
                # Prefer the longest match, so "Total Sales Amount" beats
                # "Total Sales" when both exist.
                length = len(_tokenise(measure.name))
                if best is None or length > best[2]:
                    best = (table, measure, length)
    if best is None:
        return None
    return best[0], best[1]


def _table_of_columns(
    columns: list[tuple[SemanticTable, SemanticColumn]],
) -> SemanticTable | None:
    return columns[0][0] if columns else None


def _is_numeric_column(col: SemanticColumn) -> bool:
    """True when ``col`` can be aggregated by SUM/AVERAGE/MIN/MAX in DAX.

    A column is considered numeric if either its ``summarize_by`` marks it
    as numerically aggregable (mirrors the check in
    ``app/intelligence/dax_generator.py``) or its declared ``data_type``
    is a known numeric tabular type.
    """
    summ = (col.summarize_by or "").lower()
    if summ in {"sum", "average", "min", "max"}:
        return True
    return (col.data_type or "").lower() in _NUMERIC_DATA_TYPES


def _pick_numeric_target(
    columns: list[tuple[SemanticTable, SemanticColumn]],
) -> tuple[SemanticTable, SemanticColumn] | None:
    """Return the first ``(table, column)`` whose column is numeric, or None."""
    for pair in columns:
        if _is_numeric_column(pair[1]):
            return pair
    return None


def _wants_distinct(token_set: set[str]) -> bool:
    """True when the question contains a distinct-count intent token."""
    return bool(token_set & _DISTINCT_TOKENS)


def _grouping_split_index(lowered_tokens: list[str]) -> int | None:
    """Return the position of the last ``by``/``per`` token, else ``None``.

    The *last* occurrence is used so a question like *"average sales per
    region by category"* groups on the columns after ``by`` (Category),
    matching the natural English convention where the final grouping
    keyword introduces the primary grouping specification.
    """
    last: int | None = None
    for i, tok in enumerate(lowered_tokens):
        if tok in _GROUPING_KEYWORDS:
            last = i
    return last


def _group_columns_from_by(
    tokens: list[str],
    spec: SemanticModelSpec,
    *,
    exclude: set[tuple[str, str]] | None = None,
    prefer_tables: list[SemanticTable] | None = None,
) -> list[tuple[SemanticTable, SemanticColumn]]:
    """Return grouping columns explicitly named after ``by``/``per``.

    Powers multi-key grouping: *"Total Sales by Country and Category"*
    resolves to ``[Country, Category]`` so we can emit
    ``SUMMARIZECOLUMNS(Country, Category, ...)`` instead of dropping the
    second group key. Columns in ``exclude`` (as ``(table_name, column_name)``
    tuples) are filtered out so the aggregation target or an equality-filter
    column is never also used as a group key. Duplicates are removed while
    preserving spec order.
    """
    lowered = [t.lower() for t in tokens]
    split = _grouping_split_index(lowered)
    if split is None:
        return []
    tail = tokens[split + 1 :]
    if not tail:
        return []
    matches = _match_columns(tail, spec, prefer_tables=prefer_tables)
    excluded = set(exclude or set())
    out: list[tuple[SemanticTable, SemanticColumn]] = []
    seen: set[tuple[str, str]] = set()
    for table, col in matches:
        key = (table.name, col.name)
        if key in excluded or key in seen:
            continue
        seen.add(key)
        out.append((table, col))
    return out


def _match_filters(question: str, spec: SemanticModelSpec) -> list[_FilterClause]:
    """Extract simple equality filters like ``Country = "USA"`` from ``question``.

    Recognised operators: ``=``, ``==``, ``is``, ``equals``, ``equal to``.
    Recognised literals: double-quoted string, single-quoted string, plain
    number. Only exact column-name matches against the model spec are
    considered — ambiguous or partial matches are ignored so every emitted
    filter references a real column in the model.
    """
    if not question:
        return []
    filters: list[_FilterClause] = []
    seen: set[tuple[str, str, str]] = set()
    for table in spec.tables:
        for col in table.columns:
            colname_re = re.escape(col.name)
            pattern = re.compile(
                rf"\b{colname_re}\b\s*{_FILTER_OP_RE}\s+{_FILTER_VALUE_RE}",
                re.IGNORECASE,
            )
            for m in pattern.finditer(question):
                str_val = m.group(1) if m.group(1) is not None else m.group(2)
                num_val = m.group(3)
                if str_val is not None:
                    escaped = str_val.replace('"', '""')
                    value_dax = f'"{escaped}"'
                elif num_val is not None:
                    value_dax = num_val
                else:
                    continue
                key = (table.name, col.name, value_dax)
                if key in seen:
                    continue
                seen.add(key)
                filters.append(
                    _FilterClause(table=table.name, column=col.name, value_dax=value_dax)
                )
    return filters


def _apply_filters_to_dax(dax: str, filters: list[_FilterClause]) -> str:
    """Wrap an ``EVALUATE <expr>`` query in a ``CALCULATETABLE`` with filters.

    ``CALCULATETABLE`` also propagates its filter arguments to the inner
    aggregates of a ``ROW(...)`` expression, so the same wrapper handles
    both table-shaped queries (SUMMARIZECOLUMNS / TOPN) and scalar-in-a-row
    queries (ROW("Label", SUM(...))). Returns ``dax`` unchanged when there
    are no filters or the query is not a standard EVALUATE expression.
    """
    if not filters:
        return dax
    if not dax.startswith("EVALUATE\n"):
        return dax
    inner = dax[len("EVALUATE\n") :].strip()
    # Re-indent the inner expression by four spaces so the wrapped output
    # stays readable in the UI's DAX preview panel.
    indented_inner = inner.replace("\n", "\n    ")
    lines = ["EVALUATE", "CALCULATETABLE(", f"    {indented_inner},"]
    for i, f in enumerate(filters):
        suffix = "," if i < len(filters) - 1 else ""
        lines.append(f"    {f.to_dax()}{suffix}")
    lines.append(")")
    return "\n".join(lines)


def _emit_aggregate(
    *,
    agg_label: str,
    agg_fn: str,
    target: tuple[SemanticTable, SemanticColumn],
    grouping_columns: list[tuple[SemanticTable, SemanticColumn]],
    filters: list[_FilterClause],
    direction: str | None,
    requested_n: int,
) -> DaxTranslation:
    """Build a SUMMARIZECOLUMNS or ROW query for a resolved aggregation.

    Multi-key grouping — ``SUMMARIZECOLUMNS`` accepts any number of group
    keys — is supported when the caller passes more than one grouping
    column. Equality filters are applied by wrapping the result in
    ``CALCULATETABLE``.
    """
    target_col_table, target_col = target
    agg_expr = (
        f"{agg_fn}({dax_qualified_column(target_col_table.name, target_col.name)})"
    )
    label = f"{agg_label.title()} of {target_col.name}"
    filter_note = (
        " filtered by " + ", ".join(f.describe() for f in filters)
        if filters
        else ""
    )
    if grouping_columns:
        key_lines = ",\n".join(
            f"    {dax_qualified_column(t.name, c.name)}"
            for t, c in grouping_columns
        )
        dax = (
            "EVALUATE\nSUMMARIZECOLUMNS(\n"
            f"{key_lines},\n"
            f'    "{_escape_label(label)}", {agg_expr}\n'
            ")"
        )
        if direction is not None:
            dax = _wrap_topn(dax, requested_n, label, direction)
        dax = _apply_filters_to_dax(dax, filters)
        group_desc = ", ".join(
            f"{t.name}[{c.name}]" for t, c in grouping_columns
        )
        return DaxTranslation(
            dax=dax,
            intent="summarize",
            explanation=(
                f"{agg_label.title()} of {target_col.name} grouped by "
                f"{group_desc}"
                + filter_note
                + (f" — {direction.lower()} {requested_n}." if direction else ".")
            ),
            confidence="high",
            referenced_tables=sorted(
                {target_col_table.name} | {t.name for t, _ in grouping_columns}
            ),
            referenced_columns=sorted(
                {target_col.name} | {c.name for _, c in grouping_columns}
            ),
        )
    dax = (
        "EVALUATE\nROW(\n"
        f'    "{_escape_label(label)}", {agg_expr}\n'
        ")"
    )
    dax = _apply_filters_to_dax(dax, filters)
    return DaxTranslation(
        dax=dax,
        intent="aggregate",
        explanation=(
            f"{agg_label.title()} of '{target_col_table.name}'[{target_col.name}]"
            + filter_note
            + "."
        ),
        confidence="high",
        referenced_tables=[target_col_table.name],
        referenced_columns=[target_col.name],
    )


def _pick_group_column(
    columns: list[tuple[SemanticTable, SemanticColumn]],
    measure_table: SemanticTable | None,
    spec: SemanticModelSpec,
    tables: list[SemanticTable],
) -> tuple[SemanticTable, SemanticColumn] | None:
    """Pick a sensible grouping column for a SUMMARIZECOLUMNS query.

    Preference order:
    1. The first column explicitly named in the question that is *not* the
       same name as the measure.
    2. A non-hidden column on a table explicitly named in the question.
    """
    for table, column in columns:
        if measure_table is None or column.name.lower() != measure_table.name.lower():
            return table, column
    for table in tables:
        for column in table.columns:
            if not column.is_hidden:
                return table, column
    return None


def _wrap_topn(dax: str, n: int, sort_label: str, direction: str) -> str:
    """Wrap an EVALUATE expression in a TOPN sorted by ``sort_label``."""
    inner = dax.split("EVALUATE\n", 1)[1] if dax.startswith("EVALUATE\n") else dax
    return (
        "EVALUATE\nTOPN(\n"
        f"    {n},\n"
        f"    {inner.strip()},\n"
        f"    [{sort_label}], {direction}\n"
        ")"
    )


def _escape_label(label: str) -> str:
    """Escape double quotes for use inside a DAX string literal."""
    return label.replace('"', '""')


def _phrase_in_tokens(name: str, lowered_tokens: list[str]) -> bool:
    """True when the words of ``name`` appear consecutively in ``lowered_tokens``.

    Comparison is case-insensitive and trims trailing ``s`` so plural / singular
    forms match a model that uses the other one. Also folds ``-ies`` → ``-y``
    so *"countries"* matches ``Country`` and *"categories"* matches
    ``Category`` (both common English plural patterns).
    """
    target = [t.lower() for t in _tokenise(name)]
    if not target:
        return False

    def _stem(word: str) -> str:
        if len(word) > 4 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 3 and word.endswith("s"):
            return word[:-1]
        return word

    stemmed_target = [_stem(t) for t in target]
    stemmed_tokens = [_stem(t) for t in lowered_tokens]
    n = len(stemmed_target)
    for i in range(len(stemmed_tokens) - n + 1):
        if stemmed_tokens[i : i + n] == stemmed_target:
            return True
    return False
