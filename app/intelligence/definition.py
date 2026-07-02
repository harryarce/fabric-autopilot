"""Render a :class:`~app.intelligence.spec.SemanticModelSpec` into a Fabric item
definition.

A Fabric *SemanticModel* item is created/updated through the REST API with a
``definition`` object whose ``parts`` each carry a ``path``, a base64 ``payload``
and ``payloadType = "InlineBase64"`` (see
https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/semantic-model-definition).

A semantic model uses **either** TMDL (a ``definition/`` folder of ``.tmdl``
files) **or** TMSL (a single ``model.bim``) — never both. This module can emit
both; TMDL is the default and recommended format.

The output of :func:`build_definition` is a :class:`SemanticModelDefinition`
that exposes:

* ``files`` — the raw ``{relative_path: text}`` map (handy for writing a Power
  BI Project / PBIP folder to disk or zipping), and
* ``parts`` — the ready-to-POST list of base64 parts for the Fabric REST API.

Everything here is deterministic and dependency-free.
"""

from __future__ import annotations

import base64
import json
import re
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .spec import (
    DIRECT_LAKE_EXPRESSION,
    SemanticModelSpec,
    SemanticRelationship,
    SemanticTable,
    dedupe_measure_names,
    drop_direct_lake_incompatible_columns,
    resolvable_relationships,
)

# Default $schema for definition.pbism.
_PBISM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/"
    "definitionProperties/1.0.0/schema.json"
)

_TAB = "\t"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DefinitionFormat(str, Enum):
    """Which on-disk format to emit for the model definition."""

    TMDL = "TMDL"
    TMSL = "TMSL"


@dataclass(frozen=True)
class SemanticModelDefinition:
    """A rendered semantic-model definition ready for Fabric or disk."""

    format: DefinitionFormat
    files: dict[str, str]

    @property
    def parts(self) -> list[dict[str, str]]:
        """Return the Fabric REST ``definition.parts`` array (base64 payloads)."""
        return [
            {
                "path": path,
                "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "payloadType": "InlineBase64",
            }
            for path, text in self.files.items()
        ]

    def definition_payload(self) -> dict[str, Any]:
        """Return the full ``definition`` object for a create/update item call.

        Matches the Fabric ``SemanticModelDefinition`` schema: a ``format``
        (``TMDL`` or ``TMSL``) plus the base64 ``parts`` array.
        """
        return {"format": self.format.value, "parts": self.parts}

    def to_zip_bytes(self) -> bytes:
        """Pack all files into an in-memory ``.zip`` (handy for a download)."""
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, text in self.files.items():
                zf.writestr(path, text)
        return buffer.getvalue()


# ---------------------------------------------------------------------------
# TMDL helpers
# ---------------------------------------------------------------------------


def _tmdl_name(name: str) -> str:
    """Quote a TMDL object name when it isn't a bare identifier."""
    return name if _IDENT_RE.match(name) else f"'{name}'"


def _escape_tmdl_string(value: str) -> str:
    return value.replace('"', '""')


def _partition_source(spec: SemanticModelSpec, table: SemanticTable) -> list[str]:
    """Generate the Power Query (M) source lines for a table partition.

    When the spec carries connection details a real ``Sql.Database`` query is
    produced so the model is deployable; otherwise a clearly-marked stub is
    emitted that the author can complete.
    """
    if spec.source_server and spec.source_database:
        server = spec.source_server
        database = spec.source_database
        return [
            "let",
            f'    Source = Sql.Database("{server}", "{database}"),',
            f'    Navigation = Source{{[Schema="{table.source_schema}",'
            f' Item="{table.source_table}"]}}[Data]',
            "in",
            "    Navigation",
        ]
    return [
        "let",
        "    // TODO: point this at your data source.",
        f'    Source = #table({{}}, {{}}) // placeholder for'
        f' [{table.source_schema}].[{table.source_table}]',
        "in",
        "    Source",
    ]


def _uses_direct_lake(spec: SemanticModelSpec) -> bool:
    return spec.storage_mode == "directLake"


def _uses_direct_lake_onelake(spec: SemanticModelSpec) -> bool:
    """True for Direct Lake bound to OneLake (vs the SQL analytics endpoint).

    Honours the resolved Direct Lake mode (``auto``/``onelake``/``sql``) so the
    user's toggle wins, while ``auto`` prefers OneLake whenever a lakehouse
    OneLake binding is available.
    """
    return _uses_direct_lake(spec) and spec.resolved_direct_lake_mode() == "onelake"


def _onelake_root_url(spec: SemanticModelSpec) -> str | None:
    """Return the OneLake DFS root URL for the bound lakehouse, if known.

    Direct Lake on OneLake binds to the lakehouse item root
    (``https://onelake.dfs.fabric.microsoft.com/{workspaceId}/{lakehouseId}``).
    We derive it from the explicit workspace/lakehouse ids when available, and
    otherwise strip the trailing ``/Tables`` segment from the OneLake tables
    path reported by the Lakehouse API.
    """
    if spec.onelake_workspace_id and spec.lakehouse_id:
        return (
            "https://onelake.dfs.fabric.microsoft.com/"
            f"{spec.onelake_workspace_id}/{spec.lakehouse_id}"
        )
    tables_path = (spec.onelake_tables_path or "").rstrip("/")
    if tables_path.endswith("/Tables"):
        return tables_path[: -len("/Tables")]
    return tables_path or None


def _direct_lake_expression_m(spec: SemanticModelSpec) -> list[str]:
    """M source lines for the shared Direct Lake connection expression.

    Two flavours are supported:

    * **Direct Lake on OneLake** (resolved mode ``onelake``): the shared
      expression points at the lakehouse item root in OneLake via
      ``AzureStorage.DataLake``. This binding reads Delta tables directly from
      OneLake and never falls back to DirectQuery.
    * **Direct Lake on SQL** (resolved mode ``sql``): the expression connects
      to the SQL analytics endpoint with ``Sql.Database`` for Delta-table
      discovery, and supports DirectQuery fallback / SQL views.

    When the required connection details are missing a clearly-marked stub is
    emitted for the author to complete.
    """
    if _uses_direct_lake_onelake(spec):
        root = _onelake_root_url(spec)
        if root:
            return [
                "let",
                f'    Source = AzureStorage.DataLake("{root}",'
                " [HierarchicalNavigation=true])",
                "in",
                "    Source",
            ]
        return [
            "let",
            "    // TODO: point this at your lakehouse OneLake tables path.",
            '    Source = AzureStorage.DataLake('
            '"https://onelake.dfs.fabric.microsoft.com/<workspaceId>/<lakehouseId>",'
            " [HierarchicalNavigation=true])",
            "in",
            "    Source",
        ]
    if spec.source_server and spec.source_database:
        return [
            "let",
            f'    Source = Sql.Database("{spec.source_server}", "{spec.source_database}")',
            "in",
            "    Source",
        ]
    return [
        "let",
        "    // TODO: point this at your lakehouse/warehouse SQL analytics endpoint.",
        '    Source = Sql.Database("<sql-endpoint>", "<database>")',
        "in",
        "    Source",
    ]


def _render_table_tmdl(spec: SemanticModelSpec, table: SemanticTable) -> str:
    lines: list[str] = []
    # Object descriptions in TMDL must appear IMMEDIATELY BEFORE the object
    # declaration line — never after it. Fabric rejects misplaced ``///`` lines.
    if table.description:
        for desc_line in table.description.splitlines() or [""]:
            lines.append(f"/// {desc_line}")
    lines.append(f"table {_tmdl_name(table.name)}")
    if table.is_hidden:
        lines.append(f"{_TAB}isHidden")
    if table.is_date_table:
        lines.append(f"{_TAB}dataCategory: Time")
    lines.append("")

    for measure in table.measures:
        if measure.description:
            for desc_line in measure.description.splitlines():
                lines.append(f"{_TAB}/// {desc_line}")
        # A DAX measure expression may be single- or multi-line. TMDL requires
        # multi-line expressions to start on the line after ``=`` and to be
        # indented one level deeper than the ``measure`` keyword; a single-line
        # expression stays on the ``measure ... = <dax>`` line. See the Fabric
        # semantic-model definition reference and the DAX syntax reference.
        expr = measure.expression.strip()
        expr_lines = expr.splitlines()
        if len(expr_lines) <= 1:
            lines.append(
                f"{_TAB}measure {_tmdl_name(measure.name)} = {expr}"
            )
        else:
            lines.append(f"{_TAB}measure {_tmdl_name(measure.name)} =")
            for expr_line in expr_lines:
                lines.append(f"{_TAB}{_TAB}{_TAB}{expr_line}")
        if measure.format_string:
            lines.append(f'{_TAB}{_TAB}formatString: {measure.format_string}')
        if measure.display_folder:
            lines.append(f"{_TAB}{_TAB}displayFolder: {measure.display_folder}")
        lines.append("")

    for col in table.columns:
        if col.description:
            for desc_line in col.description.splitlines():
                lines.append(f"{_TAB}/// {desc_line}")
        lines.append(f"{_TAB}column {_tmdl_name(col.name)}")
        lines.append(f"{_TAB}{_TAB}dataType: {col.data_type}")
        if col.is_hidden:
            lines.append(f"{_TAB}{_TAB}isHidden")
        if col.is_key:
            lines.append(f"{_TAB}{_TAB}isKey")
        lines.append(f"{_TAB}{_TAB}summarizeBy: {col.summarize_by}")
        if col.format_string:
            lines.append(f"{_TAB}{_TAB}formatString: {col.format_string}")
        if col.data_category:
            lines.append(f"{_TAB}{_TAB}dataCategory: {col.data_category}")
        lines.append(f"{_TAB}{_TAB}sourceColumn: {col.source_column}")
        lines.append("")

    if _uses_direct_lake(spec):
        # Direct Lake tables use an ``entity`` partition that points at a Delta
        # table in OneLake via a shared expression. ``entityName``/``schemaName``
        # select the table; ``expressionSource`` names the shared connection.
        lines.append(f"{_TAB}partition {_tmdl_name(table.name)} = entity")
        lines.append(f"{_TAB}{_TAB}mode: directLake")
        lines.append(f"{_TAB}{_TAB}source")
        lines.append(f"{_TAB}{_TAB}{_TAB}entityName: {table.source_table}")
        if table.source_schema:
            lines.append(f"{_TAB}{_TAB}{_TAB}schemaName: {table.source_schema}")
        lines.append(f"{_TAB}{_TAB}{_TAB}expressionSource: {DIRECT_LAKE_EXPRESSION}")
        lines.append("")
        return "\n".join(lines)

    mode = "directQuery" if spec.storage_mode == "directQuery" else "import"
    lines.append(f"{_TAB}partition {_tmdl_name(table.name)} = m")
    lines.append(f"{_TAB}{_TAB}mode: {mode}")
    lines.append(f"{_TAB}{_TAB}source =")
    for src_line in _partition_source(spec, table):
        lines.append(f"{_TAB}{_TAB}{_TAB}{src_line}")
    lines.append("")
    return "\n".join(lines)


def _render_relationships_tmdl(relationships: list[SemanticRelationship]) -> str:
    if not relationships:
        # Empty file is valid TMDL. NEVER emit a stray ``///`` line with no
        # following object — Fabric's TMDL parser rejects it.
        return ""
    blocks: list[str] = []
    for i, rel in enumerate(relationships, start=1):
        rel_id = f"rel_{i:03d}"
        block = [f"relationship {rel_id}"]
        if not rel.is_active:
            block.append(f"{_TAB}isActive: false")
        if rel.cross_filtering_behavior == "bothDirections":
            block.append(f"{_TAB}crossFilteringBehavior: bothDirections")
        block.append(
            f"{_TAB}fromColumn: {_tmdl_name(rel.from_table)}.{_tmdl_name(rel.from_column)}"
        )
        block.append(
            f"{_TAB}toColumn: {_tmdl_name(rel.to_table)}.{_tmdl_name(rel.to_column)}"
        )
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + "\n"


def _render_model_tmdl(spec: SemanticModelSpec) -> str:
    lines: list[str] = []
    if spec.description:
        for desc_line in spec.description.splitlines():
            lines.append(f"/// {desc_line}")
    lines.append("model Model")
    lines.append(f"{_TAB}culture: {spec.culture}")
    lines.append(f"{_TAB}defaultPowerBIDataSourceVersion: powerBI_V3")
    lines.append(f"{_TAB}discourageImplicitMeasures")
    lines.append(f"{_TAB}sourceQueryCulture: {spec.culture}")
    lines.append("")
    # Tables are associated with the model automatically by their files in the
    # ``definition/tables/`` folder. The model document intentionally lists no
    # tables: ``ref table`` lines and ``PBI_QueryOrder`` annotations with array
    # literals both trip the Fabric TMDL parser.
    return "\n".join(lines) + "\n"


def _render_database_tmdl(spec: SemanticModelSpec) -> str:
    return (
        "database\n"
        f"{_TAB}compatibilityLevel: {spec.compatibility_level}\n"
    )


def _render_expressions_tmdl(spec: SemanticModelSpec) -> str:
    """Render the shared expression that Direct Lake partitions reference.

    Emitted only for Direct Lake models; import/DirectQuery tables carry their
    own inline M source instead.
    """
    lines: list[str] = [f"expression {_tmdl_name(DIRECT_LAKE_EXPRESSION)} ="]
    for m_line in _direct_lake_expression_m(spec):
        lines.append(f"{_TAB}{_TAB}{m_line}")
    return "\n".join(lines) + "\n"


def _build_tmdl_files(spec: SemanticModelSpec) -> dict[str, str]:
    files: dict[str, str] = {
        "definition/database.tmdl": _render_database_tmdl(spec),
        "definition/model.tmdl": _render_model_tmdl(spec),
        "definition/relationships.tmdl": _render_relationships_tmdl(spec.relationships),
    }
    if _uses_direct_lake(spec):
        files["definition/expressions.tmdl"] = _render_expressions_tmdl(spec)
    for table in spec.tables:
        files[f"definition/tables/{table.name}.tmdl"] = _render_table_tmdl(spec, table)
    return files


# ---------------------------------------------------------------------------
# TMSL (model.bim) helpers
# ---------------------------------------------------------------------------


def _bim_column(col: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": col.name,
        "dataType": col.data_type,
        "sourceColumn": col.source_column,
        "summarizeBy": col.summarize_by,
    }
    if col.is_hidden:
        data["isHidden"] = True
    if col.is_key:
        data["isKey"] = True
    if col.format_string:
        data["formatString"] = col.format_string
    if col.data_category:
        data["dataCategory"] = col.data_category
    if col.description:
        data["description"] = col.description
    return data


def _bim_table(spec: SemanticModelSpec, table: SemanticTable) -> dict[str, Any]:
    if _uses_direct_lake(spec):
        source: dict[str, Any] = {
            "type": "entity",
            "entityName": table.source_table,
            "expressionSource": DIRECT_LAKE_EXPRESSION,
        }
        if table.source_schema:
            source["schemaName"] = table.source_schema
        partition = {
            "name": table.name,
            "mode": "directLake",
            "source": source,
        }
    else:
        mode = "directQuery" if spec.storage_mode == "directQuery" else "import"
        partition = {
            "name": table.name,
            "mode": mode,
            "source": {
                "type": "m",
                "expression": _partition_source(spec, table),
            },
        }
    data: dict[str, Any] = {
        "name": table.name,
        "columns": [_bim_column(c) for c in table.columns],
        "partitions": [partition],
    }
    if table.measures:
        data["measures"] = [
            {
                k: v
                for k, v in {
                    "name": m.name,
                    "expression": m.expression,
                    "formatString": m.format_string,
                    "description": m.description,
                    "displayFolder": m.display_folder,
                }.items()
                if v not in (None, "")
            }
            for m in table.measures
        ]
    if table.is_hidden:
        data["isHidden"] = True
    if table.description:
        data["description"] = table.description
    if table.is_date_table:
        data["dataCategory"] = "Time"
    return data


def _bim_relationship(rel: SemanticRelationship, index: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": f"rel_{index:03d}",
        "fromTable": rel.from_table,
        "fromColumn": rel.from_column,
        "toTable": rel.to_table,
        "toColumn": rel.to_column,
    }
    if not rel.is_active:
        data["isActive"] = False
    if rel.cross_filtering_behavior == "bothDirections":
        data["crossFilteringBehavior"] = "bothDirections"
    return data


def _build_tmsl_files(spec: SemanticModelSpec) -> dict[str, str]:
    model_body: dict[str, Any] = {
        "culture": spec.culture,
        "sourceQueryCulture": spec.culture,
        "defaultPowerBIDataSourceVersion": "powerBI_V3",
        "discourageImplicitMeasures": True,
        "tables": [_bim_table(spec, t) for t in spec.tables],
        "relationships": [
            _bim_relationship(r, i) for i, r in enumerate(spec.relationships, start=1)
        ],
    }
    if _uses_direct_lake(spec):
        model_body["expressions"] = [
            {
                "name": DIRECT_LAKE_EXPRESSION,
                "kind": "m",
                "expression": _direct_lake_expression_m(spec),
            }
        ]
    model = {
        "name": spec.name,
        "compatibilityLevel": spec.compatibility_level,
        "model": model_body,
    }
    return {"model.bim": json.dumps(model, indent=2)}


# ---------------------------------------------------------------------------
# Shared parts
# ---------------------------------------------------------------------------


def _definition_pbism() -> str:
    return json.dumps(
        {"$schema": _PBISM_SCHEMA, "version": "5.0", "settings": {"qnaEnabled": False}},
        indent=2,
    )


def _spec_with_relationships(
    spec: SemanticModelSpec, relationships: list[SemanticRelationship]
) -> SemanticModelSpec:
    """Return a shallow copy of ``spec`` with ``relationships`` swapped in."""
    import dataclasses

    return dataclasses.replace(spec, relationships=relationships)


def build_definition(
    spec: SemanticModelSpec,
    fmt: DefinitionFormat = DefinitionFormat.TMDL,
) -> SemanticModelDefinition:
    """Render ``spec`` into a Fabric semantic-model definition.

    Args:
        spec: the model to render.
        fmt: ``TMDL`` (default, a ``definition/`` folder) or ``TMSL``
            (a single ``model.bim``). The two are mutually exclusive in Fabric.

    Any relationship that references a table or column not present in the model
    is dropped before rendering. Such dangling edges make Fabric reject the
    whole dataset with "Cannot resolve all the paths" (HTTP 500), so we never
    emit them; a warning lists what was removed.
    """
    spec, renamed = dedupe_measure_names(spec)
    if renamed:
        details = ", ".join(f"{old!r}->{new!r}" for old, new in renamed)
        warnings.warn(
            f"Renamed {len(renamed)} duplicate measure name(s) to keep measure "
            f"names unique across the model: {details}",
            stacklevel=2,
        )
    # Strip columns Direct Lake cannot store (binary) before checking
    # relationships, so any edge that used a dropped column is cleaned up too.
    spec, dropped_cols = drop_direct_lake_incompatible_columns(spec)
    if dropped_cols:
        details = ", ".join(f"{ref} ({dtype})" for ref, dtype in dropped_cols)
        warnings.warn(
            f"Dropped {len(dropped_cols)} column(s) Direct Lake cannot store "
            f"(binary columns are not allowed in Direct Lake tables): {details}",
            stacklevel=2,
        )
    valid, dropped = resolvable_relationships(spec)
    if dropped:
        spec = _spec_with_relationships(spec, valid)
        details = ", ".join(
            f"{r.from_table}.{r.from_column}->{r.to_table}.{r.to_column}"
            for r in dropped
        )
        warnings.warn(
            f"Dropped {len(dropped)} relationship(s) referencing unknown "
            f"tables/columns before rendering: {details}",
            stacklevel=2,
        )
    if fmt is DefinitionFormat.TMSL:
        files = _build_tmsl_files(spec)
    else:
        files = _build_tmdl_files(spec)
    # ``definition.pbism`` is required for both formats.
    files["definition.pbism"] = _definition_pbism()
    return SemanticModelDefinition(format=fmt, files=files)
