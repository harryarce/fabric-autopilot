"""Parse a Fabric semantic-model *definition* back into a
:class:`~app.intelligence.spec.SemanticModelSpec`.

This is the inverse of :mod:`app.intelligence.definition`: given the decoded
``{path: text}`` files of a semantic-model definition (TMDL folder **or** a TMSL
``model.bim``), reconstruct the format-agnostic IR so the rest of the
intelligence layer — auditors, suggestion engine, re-renderers — can reason over
an *existing* model fetched from Fabric, not just one we generated.

Why hand-rolled
---------------
TMDL has no official Python parser. The one third-party package on PyPI is a
single-maintainer, untested, lossy project unsuitable for write-back. Rather
than depend on it, this module implements a small, tolerant, indentation-aware
parser for exactly the TMDL grammar Fabric emits (and a strict JSON reader for
TMSL). It is deliberately *lenient on read*: unknown properties and objects are
ignored rather than rejected, so a real-world model authored in Power BI Desktop
(with ``lineageTag``, ``annotation`` and other extras our emitter never writes)
still parses.

Everything here is deterministic and dependency-free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .spec import (
    VALID_DATA_TYPES,
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticRelationship,
    SemanticTable,
)

# Declaration keywords that introduce a TMDL *object* (everything else on a
# child line is treated as a property of the enclosing object).
_OBJECT_KEYWORDS = {
    "model",
    "database",
    "table",
    "column",
    "measure",
    "partition",
    "relationship",
    "hierarchy",
    "level",
    "role",
    "perspective",
    "calculationGroup",
    "calculationItem",
    "expression",
    "namedExpression",
    "annotation",
    "extendedProperty",
}

# A property line: ``name: value`` (the colon form). The default-property /
# expression form uses ``=`` and is handled separately.
_PROP_COLON_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
# A bare boolean shortcut, e.g. ``isHidden`` on its own line means ``= true``.
_BARE_BOOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TmdlParseError(ValueError):
    """Raised when a definition contains no recognisable model content."""


# ---------------------------------------------------------------------------
# Tokeniser + indentation tree
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    """One declaration line plus its indented children."""

    keyword: str
    name: str | None
    value: str | None  # text after a top-level ``=`` (``None`` if absent)
    content: str  # the raw stripped line (for property interpretation)
    level: int
    description: str | None = None
    children: list["_Node"] = field(default_factory=list)


def _tokenize(text: str) -> list[tuple[int, str]]:
    """Return ``(level, content)`` for each non-blank line.

    Indentation is normalised to integer levels: tabs count one-per-level
    (Fabric's native style); a space-indented file is supported by treating the
    smallest non-zero indent as one level.
    """
    rows: list[tuple[int, str]] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            continue
        stripped = raw.lstrip(" \t")
        lead = raw[: len(raw) - len(stripped)]
        indent = lead.count("\t") if "\t" in lead else len(lead)
        rows.append((indent, stripped))
    positives = [i for i, _ in rows if i > 0]
    unit = min(positives) if positives else 1
    unit = unit or 1
    return [(i // unit if i > 0 else 0, c) for i, c in rows]


def _find_top_level_eq(content: str) -> int:
    """Index of the first ``=`` not inside a single-quoted name, else ``-1``."""
    in_quote = False
    for idx, ch in enumerate(content):
        if ch == "'":
            in_quote = not in_quote
        elif ch == "=" and not in_quote:
            return idx
    return -1


def _unquote(name: str) -> str:
    """Strip TMDL single-quote quoting from an object name."""
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _parse_declaration(content: str) -> tuple[str, str | None, str | None]:
    """Split a declaration line into ``(keyword, name, value)``."""
    eq = _find_top_level_eq(content)
    if eq != -1:
        left = content[:eq].rstrip()
        value: str | None = content[eq + 1 :].strip()
    else:
        left = content.strip()
        value = None
    parts = left.split(None, 1)
    keyword = parts[0] if parts else ""
    name = _unquote(parts[1]) if len(parts) > 1 else None
    return keyword, name, value


def _build_tree(tokens: list[tuple[int, str]]) -> list[_Node]:
    """Build a forest from ``(level, content)`` tokens.

    ``///`` lines are accumulated and attached as the ``description`` of the
    next declared object.
    """
    roots: list[_Node] = []
    stack: list[_Node] = []
    pending_desc: list[str] = []
    for level, content in tokens:
        if content.startswith("///"):
            pending_desc.append(content[3:].lstrip())
            continue
        keyword, name, value = _parse_declaration(content)
        node = _Node(
            keyword=keyword,
            name=name,
            value=value,
            content=content,
            level=level,
            description="\n".join(pending_desc) if pending_desc else None,
        )
        pending_desc = []
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _is_expression_line(node: _Node) -> bool:
    """Whether a child node is part of a multi-line DAX/M expression.

    Property lines use the ``name: value`` colon form or a bare boolean
    keyword; everything else under a ``measure`` or ``source =`` declaration is
    expression text — including DAX lines such as ``VAR x = 1`` that contain an
    ``=`` (which must NOT be mistaken for a default property).
    """
    if node.keyword in _OBJECT_KEYWORDS:
        return False
    if _PROP_COLON_RE.match(node.content):
        return False
    if _BARE_BOOL_RE.match(node.content):
        return False
    return True


def _properties(node: _Node) -> dict[str, str]:
    """Collect ``name: value`` and bare-boolean child properties of ``node``."""
    props: dict[str, str] = {}
    for child in node.children:
        if child.keyword in _OBJECT_KEYWORDS:
            continue
        m = _PROP_COLON_RE.match(child.content)
        if m:
            props[m.group(1)] = m.group(2).strip()
        elif child.value is not None:
            # ``name = value`` default-property form (rare on columns/tables).
            key = child.content[: _find_top_level_eq(child.content)].strip()
            props[key] = child.value
        elif _BARE_BOOL_RE.match(child.content):
            props[child.content] = "true"
    return props


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# TMDL interpretation
# ---------------------------------------------------------------------------


_SQL_DATABASE_RE = re.compile(
    r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"', re.IGNORECASE
)
_NAV_RE = re.compile(
    r'Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"', re.IGNORECASE
)


def _interpret_column(node: _Node) -> SemanticColumn:
    props = _properties(node)
    data_type = props.get("dataType", "string")
    if data_type not in VALID_DATA_TYPES:
        data_type = "string"
    name = node.name or "(column)"
    return SemanticColumn(
        name=name,
        source_column=props.get("sourceColumn", name),
        data_type=data_type,
        summarize_by=props.get("summarizeBy", "none"),
        is_hidden=_truthy(props.get("isHidden")),
        is_key=_truthy(props.get("isKey")),
        format_string=props.get("formatString"),
        description=node.description,
        data_category=props.get("dataCategory"),
    )


def _interpret_measure(node: _Node) -> SemanticMeasure:
    props = _properties(node)
    if node.value:
        expression = node.value
    else:
        expr_lines = [
            child.content for child in node.children if _is_expression_line(child)
        ]
        expression = "\n".join(expr_lines).strip()
    return SemanticMeasure(
        name=node.name or "(measure)",
        expression=expression,
        format_string=props.get("formatString"),
        description=node.description,
        display_folder=props.get("displayFolder"),
    )


def _interpret_partition(node: _Node) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Return ``(mode, schema, table, server, database)`` recovered from M source."""
    props = _properties(node)
    mode = props.get("mode", "import")
    source_node = next(
        (c for c in node.children if c.keyword == "source" or c.content.startswith("source")),
        None,
    )
    schema = table = server = database = None
    # Direct Lake ``entity`` partitions select the table via ``entityName`` /
    # ``schemaName`` properties rather than inline ``Sql.Database`` M; the
    # connection itself lives in a shared expression (recovered separately).
    if source_node is not None:
        source_props = _properties(source_node)
        if source_props.get("entityName") or source_props.get("schemaName"):
            table = source_props.get("entityName") or table
            schema = source_props.get("schemaName") or schema
            return mode, schema, table, server, database
    m_text = ""
    if source_node is not None:
        if source_node.value:
            m_text = source_node.value
        m_text += "\n".join(
            c.content for c in source_node.children if _is_expression_line(c)
        )
    db_match = _SQL_DATABASE_RE.search(m_text)
    if db_match:
        server, database = db_match.group(1), db_match.group(2)
    nav_match = _NAV_RE.search(m_text)
    if nav_match:
        schema, table = nav_match.group(1), nav_match.group(2)
    return mode, schema, table, server, database


def _interpret_table(node: _Node) -> tuple[SemanticTable, str, str | None, str | None]:
    """Return ``(table, storage_mode, source_server, source_database)``."""
    props = _properties(node)
    table = SemanticTable(
        name=node.name or "(table)",
        source_schema="",
        source_table=node.name or "",
        description=node.description,
        is_hidden=_truthy(props.get("isHidden")),
        is_date_table=props.get("dataCategory", "").strip().lower() == "time",
    )
    storage_mode = "import"
    server = database = None
    for child in node.children:
        if child.keyword == "column":
            table.columns.append(_interpret_column(child))
        elif child.keyword == "measure":
            table.measures.append(_interpret_measure(child))
        elif child.keyword == "partition":
            mode, schema, src_table, server, database = _interpret_partition(child)
            mode_lower = mode.lower()
            if mode_lower == "directlake":
                storage_mode = "directLake"
            elif mode_lower == "directquery":
                storage_mode = "directQuery"
            else:
                storage_mode = "import"
            if schema:
                table.source_schema = schema
            if src_table:
                table.source_table = src_table
    return table, storage_mode, server, database


def _split_column_ref(ref: str) -> tuple[str, str]:
    """Split a ``Table.Column`` (possibly quoted) reference into its parts."""
    in_quote = False
    for idx, ch in enumerate(ref):
        if ch == "'":
            in_quote = not in_quote
        elif ch == "." and not in_quote:
            return _unquote(ref[:idx]), _unquote(ref[idx + 1 :])
    return _unquote(ref), ""


def _interpret_relationship(node: _Node) -> SemanticRelationship | None:
    props = _properties(node)
    from_ref = props.get("fromColumn")
    to_ref = props.get("toColumn")
    if not from_ref or not to_ref:
        return None
    from_table, from_column = _split_column_ref(from_ref)
    to_table, to_column = _split_column_ref(to_ref)
    cross = props.get("crossFilteringBehavior", "oneDirection")
    return SemanticRelationship(
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        to_column=to_column,
        from_cardinality=props.get("fromCardinality", "many"),
        to_cardinality=props.get("toCardinality", "one"),
        cross_filtering_behavior=cross,
        is_active=not (props.get("isActive", "true").strip().lower() == "false"),
    )


def _parse_tmdl(files: dict[str, str], *, name: str | None, default_name: str) -> SemanticModelSpec:
    culture = "en-US"
    compatibility_level = 1604
    description: str | None = None
    model_name = name or default_name
    tables: list[SemanticTable] = []
    relationships: list[SemanticRelationship] = []
    storage_mode = "import"
    source_server: str | None = None
    source_database: str | None = None

    for path, text in files.items():
        lower = path.replace("\\", "/").lower()
        if not lower.endswith(".tmdl"):
            continue
        roots = _build_tree(_tokenize(text))
        for root in roots:
            if root.keyword == "model":
                props = _properties(root)
                culture = props.get("culture", culture)
                description = root.description or description
                if name is None and root.name and root.name != "Model":
                    model_name = root.name
            elif root.keyword == "database":
                props = _properties(root)
                try:
                    compatibility_level = int(props.get("compatibilityLevel", compatibility_level))
                except (TypeError, ValueError):
                    pass
            elif root.keyword == "table":
                table, mode, server, database = _interpret_table(root)
                tables.append(table)
                if mode == "directLake":
                    storage_mode = "directLake"
                elif mode == "directQuery" and storage_mode != "directLake":
                    storage_mode = "directQuery"
                source_server = source_server or server
                source_database = source_database or database
            elif root.keyword in ("expression", "namedExpression"):
                # Direct Lake partitions reference a shared expression for the
                # connection; recover the SQL endpoint from its M body.
                m_text = root.value or ""
                m_text += "\n".join(
                    c.content for c in root.children if _is_expression_line(c)
                )
                db_match = _SQL_DATABASE_RE.search(m_text)
                if db_match:
                    source_server = source_server or db_match.group(1)
                    source_database = source_database or db_match.group(2)
            elif root.keyword == "relationship":
                rel = _interpret_relationship(root)
                if rel is not None:
                    relationships.append(rel)

    if not tables and not relationships:
        raise TmdlParseError("No TMDL table or relationship content found.")

    return SemanticModelSpec(
        name=model_name,
        tables=tables,
        relationships=relationships,
        culture=culture,
        compatibility_level=compatibility_level,
        description=description,
        source_server=source_server,
        source_database=source_database,
        storage_mode=storage_mode,
    )


# ---------------------------------------------------------------------------
# TMSL (model.bim) interpretation
# ---------------------------------------------------------------------------


def _expr_to_text(expression: object) -> str:
    if isinstance(expression, list):
        return "\n".join(str(line) for line in expression)
    return str(expression or "")


def _parse_tmsl(files: dict[str, str], *, name: str | None) -> SemanticModelSpec:
    bim_text = next(
        (t for p, t in files.items() if p.replace("\\", "/").lower().endswith("model.bim")),
        None,
    )
    if bim_text is None:
        raise TmdlParseError("No model.bim found in TMSL definition.")
    try:
        data = json.loads(bim_text)
    except json.JSONDecodeError as exc:  # pragma: no cover - guarded by callers
        raise TmdlParseError(f"model.bim is not valid JSON: {exc}") from exc

    model = data.get("model", {}) or {}
    tables: list[SemanticTable] = []
    storage_mode = "import"
    source_server: str | None = None
    source_database: str | None = None

    for t in model.get("tables", []) or []:
        columns: list[SemanticColumn] = []
        for c in t.get("columns", []) or []:
            data_type = c.get("dataType", "string")
            if data_type not in VALID_DATA_TYPES:
                data_type = "string"
            col_name = c.get("name", "(column)")
            columns.append(
                SemanticColumn(
                    name=col_name,
                    source_column=c.get("sourceColumn", col_name),
                    data_type=data_type,
                    summarize_by=c.get("summarizeBy", "none"),
                    is_hidden=bool(c.get("isHidden", False)),
                    is_key=bool(c.get("isKey", False)),
                    format_string=c.get("formatString"),
                    description=_join_description(c.get("description")),
                    data_category=c.get("dataCategory"),
                )
            )
        measures: list[SemanticMeasure] = []
        for m in t.get("measures", []) or []:
            measures.append(
                SemanticMeasure(
                    name=m.get("name", "(measure)"),
                    expression=_expr_to_text(m.get("expression")),
                    format_string=m.get("formatString"),
                    description=_join_description(m.get("description")),
                    display_folder=m.get("displayFolder"),
                )
            )
        schema = ""
        src_table = t.get("name", "")
        for partition in t.get("partitions", []) or []:
            part_mode = partition.get("mode", "").lower()
            if part_mode == "directlake":
                storage_mode = "directLake"
            elif part_mode == "directquery" and storage_mode != "directLake":
                storage_mode = "directQuery"
            source = partition.get("source", {}) or {}
            if source.get("type", "").lower() == "entity" or source.get("entityName"):
                src_table = source.get("entityName", src_table)
                schema = source.get("schemaName", schema)
                continue
            m_text = _expr_to_text(source.get("expression"))
            db_match = _SQL_DATABASE_RE.search(m_text)
            if db_match:
                source_server = source_server or db_match.group(1)
                source_database = source_database or db_match.group(2)
            nav_match = _NAV_RE.search(m_text)
            if nav_match:
                schema, src_table = nav_match.group(1), nav_match.group(2)
        tables.append(
            SemanticTable(
                name=t.get("name", "(table)"),
                source_schema=schema,
                source_table=src_table,
                columns=columns,
                measures=measures,
                description=_join_description(t.get("description")),
                is_hidden=bool(t.get("isHidden", False)),
                is_date_table=t.get("dataCategory", "").strip().lower() == "time",
            )
        )

    # Direct Lake entity partitions don't carry inline M; recover the SQL
    # endpoint from the shared model expression(s) instead.
    for expr in model.get("expressions", []) or []:
        m_text = _expr_to_text(expr.get("expression"))
        db_match = _SQL_DATABASE_RE.search(m_text)
        if db_match:
            source_server = source_server or db_match.group(1)
            source_database = source_database or db_match.group(2)

    relationships: list[SemanticRelationship] = []
    for r in model.get("relationships", []) or []:
        relationships.append(
            SemanticRelationship(
                from_table=r.get("fromTable", ""),
                from_column=r.get("fromColumn", ""),
                to_table=r.get("toTable", ""),
                to_column=r.get("toColumn", ""),
                cross_filtering_behavior=r.get("crossFilteringBehavior", "oneDirection"),
                is_active=bool(r.get("isActive", True)),
            )
        )

    return SemanticModelSpec(
        name=name or data.get("name", "Model"),
        tables=tables,
        relationships=relationships,
        culture=model.get("culture", "en-US"),
        compatibility_level=int(data.get("compatibilityLevel", 1604)),
        source_server=source_server,
        source_database=source_database,
        storage_mode=storage_mode,
    )


def _join_description(description: object) -> str | None:
    if description is None:
        return None
    if isinstance(description, list):
        return "\n".join(str(line) for line in description)
    return str(description)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_semantic_model(
    files: dict[str, str],
    *,
    name: str | None = None,
    default_name: str = "Model",
) -> SemanticModelSpec:
    """Parse a semantic-model definition into a :class:`SemanticModelSpec`.

    Args:
        files: the decoded ``{relative_path: text}`` definition parts.
        name: the model's display name (from the Fabric item). TMDL does not
            carry a meaningful model name, so pass the item name to preserve it.
        default_name: fallback name when none can be determined.

    Returns:
        The reconstructed IR.

    Raises:
        TmdlParseError: when no recognisable model content is present.
    """
    is_tmsl = any(p.replace("\\", "/").lower().endswith("model.bim") for p in files)
    if is_tmsl:
        return _parse_tmsl(files, name=name)
    return _parse_tmdl(files, name=name, default_name=default_name)
