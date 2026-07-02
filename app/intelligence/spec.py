"""Provider-agnostic semantic-model intermediate representation (IR).

A :class:`SemanticModelSpec` is a plain, serialisable description of a Power BI /
Fabric tabular model: its tables, columns, measures and relationships. It is
deliberately decoupled from both the source SQL schema and the target file
format (TMDL / TMSL) so that:

* the deterministic mapping (:func:`spec_from_schemas`) can produce a sensible
  default model from extracted SQL schemas, and
* the AI agent can enrich the very same structure (rename columns, add DAX
  measures, mark a date table, hide keys…) by emitting JSON that round-trips
  through :meth:`SemanticModelSpec.from_dict` / :meth:`SemanticModelSpec.to_dict`.

Keeping the IR in one place is what makes the rest of the intelligence layer
reusable: the Streamlit UI, the skill script and the agent all speak ``spec``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# SQL type → tabular (TOM) data type mapping
# ---------------------------------------------------------------------------

# Tabular models only have a small set of data types. Map the common SQL Server
# / Fabric types onto them; anything unknown falls back to ``string`` which is
# always safe.
_TYPE_MAP: dict[str, str] = {
    "bit": "boolean",
    "tinyint": "int64",
    "smallint": "int64",
    "int": "int64",
    "bigint": "int64",
    "decimal": "decimal",
    "numeric": "decimal",
    "money": "decimal",
    "smallmoney": "decimal",
    "float": "double",
    "real": "double",
    "date": "dateTime",
    "datetime": "dateTime",
    "datetime2": "dateTime",
    "smalldatetime": "dateTime",
    "datetimeoffset": "dateTime",
    "time": "dateTime",
    "char": "string",
    "nchar": "string",
    "varchar": "string",
    "nvarchar": "string",
    "text": "string",
    "ntext": "string",
    "uniqueidentifier": "string",
    "xml": "string",
    "binary": "binary",
    "varbinary": "binary",
    "image": "binary",
}

# Data types that are sensible to aggregate implicitly.
_NUMERIC_TABULAR_TYPES = {"int64", "decimal", "double"}

VALID_DATA_TYPES = {"string", "int64", "decimal", "double", "dateTime", "boolean", "binary"}

# Supported semantic-model table storage modes.
#   * ``import``      — data is cached in the VertiPaq engine (refresh copies data).
#   * ``directQuery`` — queries are federated to the source SQL endpoint.
#   * ``directLake``  — data is loaded on demand from OneLake Delta tables via a
#                       shared expression; refresh only re-frames metadata.
VALID_STORAGE_MODES = {"import", "directQuery", "directLake"}

# Name of the shared Power Query expression that Direct Lake partitions point at
# to establish the SQL analytics endpoint connection (Direct Lake on SQL).
DIRECT_LAKE_EXPRESSION = "DatabaseQuery"

# Direct Lake has two valid forms (see
# https://learn.microsoft.com/fabric/fundamentals/direct-lake-develop):
#   * ``onelake`` — the shared expression binds to the OneLake storage location
#                   (Azure Data Lake Storage connector). More modeling features,
#                   faster queries, composite models, and NO DirectQuery
#                   fallback. Preferred whenever an OneLake binding is known.
#   * ``sql``     — the shared expression binds to the SQL analytics endpoint
#                   (SQL Server connector). Use when you depend on SQL-endpoint
#                   security (delegated identity) or need DirectQuery fallback;
#                   it also permits SQL views (queries fall back to DirectQuery).
#   * ``auto``    — resolve to ``onelake`` when an OneLake binding is available,
#                   otherwise ``sql``.
VALID_DIRECT_LAKE_MODES = {"auto", "onelake", "sql"}

# Tabular data types a Direct Lake table cannot store. Fabric fails the dataset
# import with "Column '<x>' with binary data type is not allowed in Direct Lake
# table" when one is present, so they are stripped before rendering a Direct
# Lake model. Import / DirectQuery models keep binary columns.
DIRECT_LAKE_UNSUPPORTED_TYPES = frozenset({"binary"})



def map_sql_type(sql_type: str) -> str:
    """Map a raw SQL data type name onto a tabular (TOM) data type."""
    return _TYPE_MAP.get(sql_type.strip().lower(), "string")


# ---------------------------------------------------------------------------
# DAX reference helpers
# ---------------------------------------------------------------------------
#
# Measure expressions must be valid DAX. The two escaping rules that matter when
# generating column/table references programmatically are:
#
# * A table name is wrapped in single quotes; a literal single quote inside the
#   name is doubled (``O'Brien`` -> ``'O''Brien'``). Wrapping in single quotes is
#   always valid, even when the name has no special characters.
# * A column (or measure) name is wrapped in square brackets; a literal closing
#   bracket inside the name is doubled (``Amount]`` -> ``[Amount]]]``).
#
# See https://learn.microsoft.com/dax/dax-syntax-reference (Naming requirements).


def dax_table_ref(table: str) -> str:
    """Return a DAX-safe, single-quoted table reference, e.g. ``'Sales'``."""
    return "'" + table.replace("'", "''") + "'"


def dax_column_ref(column: str) -> str:
    """Return a DAX-safe, bracketed column reference, e.g. ``[Amount]``."""
    return "[" + column.replace("]", "]]") + "]"


def dax_qualified_column(table: str, column: str) -> str:
    """Return a fully-qualified DAX column reference, e.g. ``'Sales'[Amount]``."""
    return dax_table_ref(table) + dax_column_ref(column)


def dax_measure_ref(measure: str) -> str:
    """Return a DAX-safe measure reference, e.g. ``[Total Sales]``."""
    return dax_column_ref(measure)


# A DAX string literal: double-quoted, with ``""`` as the embedded-quote escape.
# Blanked out before scanning an expression so a bracket inside a literal is not
# mistaken for a column / measure reference.
_DAX_STRING_RE = re.compile(r'"(?:[^"]|"")*"')

# A bracketed object reference, optionally qualified by a table. ``table`` is the
# (possibly single-quoted) table name; ``name`` is the column or measure name.
# An unqualified ``[Name]`` leaves ``table`` empty and denotes a measure or a
# column resolved from row context; a qualified ``'Sales'[Amount]`` /
# ``Sales[Amount]`` denotes a specific column.
_DAX_REF_RE = re.compile(
    r"(?P<table>'(?:[^']|'')*'|[A-Za-z_]\w*)?\[(?P<name>(?:[^\]]|\]\])*)\]"
)


def _dax_unescape_name(raw: str) -> str:
    """Undo the bracket-escape (``]]`` -> ``]``) inside a column/measure name."""
    return raw.replace("]]", "]")


def _dax_unescape_table(raw: str) -> str:
    """Strip surrounding quotes and undo ``''`` escaping for a table name."""
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def rewrite_measure_references(expression: str, renames: dict[str, str]) -> str:
    """Rewrite unqualified measure references in ``expression`` after a rename.

    ``renames`` maps *old* measure name -> *new* measure name (the output of a
    deduplication pass). Only unqualified ``[Old]`` references are rewritten,
    because that is how measures are referenced in DAX; qualified
    ``'Table'[Old]`` column references are left untouched. String literals are
    preserved verbatim. Names are matched case-insensitively, as Power BI does.
    """
    if not renames or not expression:
        return expression
    folded = {old.casefold(): new for old, new in renames.items()}

    # Work on the literal-blanked text only to locate matches, then splice the
    # replacements back into the original so embedded literals survive intact.
    masked = _DAX_STRING_RE.sub(lambda m: " " * len(m.group(0)), expression)

    result: list[str] = []
    last = 0
    for match in _DAX_REF_RE.finditer(masked):
        if match.group("table"):
            continue  # qualified -> a column reference; never a measure
        new = folded.get(_dax_unescape_name(match.group("name")).casefold())
        if new is None:
            continue
        result.append(expression[last : match.start()])
        result.append(dax_measure_ref(new))
        last = match.end()
    result.append(expression[last:])
    return "".join(result)


def iter_dax_references(expression: str):
    """Yield ``(table, name)`` for each column/measure reference in ``expression``.

    ``table`` is ``None`` for an unqualified ``[Name]`` reference. String
    literals are ignored so a bracket inside a quoted string is never reported.
    """
    if not expression:
        return
    masked = _DAX_STRING_RE.sub(lambda m: " " * len(m.group(0)), expression)
    for match in _DAX_REF_RE.finditer(masked):
        table = match.group("table")
        yield (
            _dax_unescape_table(table) if table else None,
            _dax_unescape_name(match.group("name")),
        )


# ---------------------------------------------------------------------------
# IR dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SemanticColumn:
    """A single column in a semantic-model table."""

    name: str
    source_column: str
    data_type: str = "string"
    summarize_by: str = "none"  # none | sum | count | min | max | average
    is_hidden: bool = False
    is_key: bool = False
    format_string: str | None = None
    description: str | None = None
    data_category: str | None = None  # e.g. "Years", "City", "Country"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False, "")}


@dataclass
class SemanticMeasure:
    """A DAX measure. Populated by the AI agent, never by the default mapping."""

    name: str
    expression: str
    format_string: str | None = None
    description: str | None = None
    display_folder: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


# ---------------------------------------------------------------------------
# Suggestions — relationships and measures the user can opt in to
# ---------------------------------------------------------------------------


# Provenance values for suggestions. ``fk`` is an explicit SQL foreign key;
# ``pk-match`` and ``suffix`` are deterministic name-based inferences; ``agent``
# is anything the AI proposes. Surfaced in the UI so the user can judge trust.
_REL_SOURCES = ("fk", "pk-match", "suffix", "agent")
_MEASURE_SOURCES = ("deterministic", "agent")


@dataclass
class SuggestedRelationship:
    """A relationship the engine (or agent) believes the user might want.

    Carries the underlying :class:`SemanticRelationship` plus the metadata
    needed for the UI to render a meaningful pick list: a short ``rationale``
    string, a ``confidence`` in ``[0, 1]`` and a ``source`` tag.

    A stable :meth:`key` is exposed so the UI can checkbox-select instances
    without depending on object identity across reruns.
    """

    relationship: SemanticRelationship
    rationale: str = ""
    confidence: float = 0.8
    source: str = "deterministic"

    @property
    def key(self) -> str:
        r = self.relationship
        return f"{r.from_table}.{r.from_column}->{r.to_table}.{r.to_column}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship": self.relationship.to_dict(),
            "rationale": self.rationale,
            "confidence": self.confidence,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuggestedRelationship":
        rel_data = data["relationship"]
        return cls(
            relationship=SemanticRelationship(
                from_table=rel_data["from_table"],
                from_column=rel_data["from_column"],
                to_table=rel_data["to_table"],
                to_column=rel_data["to_column"],
                from_cardinality=rel_data.get("from_cardinality", "many"),
                to_cardinality=rel_data.get("to_cardinality", "one"),
                cross_filtering_behavior=rel_data.get(
                    "cross_filtering_behavior", "oneDirection"
                ),
                is_active=bool(rel_data.get("is_active", True)),
            ),
            rationale=data.get("rationale", ""),
            confidence=float(data.get("confidence", 0.8)),
            source=data.get("source", "deterministic"),
        )


@dataclass
class SuggestedMeasure:
    """A DAX measure the engine (or agent) proposes for a specific table."""

    table: str
    measure: SemanticMeasure
    rationale: str = ""
    confidence: float = 0.8
    source: str = "deterministic"

    @property
    def key(self) -> str:
        return f"{self.table}::{self.measure.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "measure": self.measure.to_dict(),
            "rationale": self.rationale,
            "confidence": self.confidence,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuggestedMeasure":
        m = data["measure"]
        return cls(
            table=data["table"],
            measure=SemanticMeasure(
                name=m["name"],
                expression=m["expression"],
                format_string=m.get("format_string"),
                description=m.get("description"),
                display_folder=m.get("display_folder"),
            ),
            rationale=data.get("rationale", ""),
            confidence=float(data.get("confidence", 0.8)),
            source=data.get("source", "deterministic"),
        )


@dataclass
class SemanticModelSuggestions:
    """Combined suggestion bundle returned by the suggestion engine/agent."""

    relationships: list[SuggestedRelationship] = field(default_factory=list)
    measures: list[SuggestedMeasure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationships": [r.to_dict() for r in self.relationships],
            "measures": [m.to_dict() for m in self.measures],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SemanticModelSuggestions":
        return cls(
            relationships=[
                SuggestedRelationship.from_dict(r) for r in data.get("relationships", [])
            ],
            measures=[
                SuggestedMeasure.from_dict(m) for m in data.get("measures", [])
            ],
        )


@dataclass
class SemanticTable:
    """A table (fact or dimension) in the semantic model."""

    name: str
    source_schema: str
    source_table: str
    columns: list[SemanticColumn] = field(default_factory=list)
    measures: list[SemanticMeasure] = field(default_factory=list)
    description: str | None = None
    is_hidden: bool = False
    # Marks a Date dimension so the model can use time-intelligence.
    is_date_table: bool = False

    def column(self, name: str) -> SemanticColumn | None:
        return next((c for c in self.columns if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "source_schema": self.source_schema,
            "source_table": self.source_table,
            "columns": [c.to_dict() for c in self.columns],
        }
        if self.measures:
            data["measures"] = [m.to_dict() for m in self.measures]
        if self.description:
            data["description"] = self.description
        if self.is_hidden:
            data["is_hidden"] = True
        if self.is_date_table:
            data["is_date_table"] = True
        return data


@dataclass
class SemanticRelationship:
    """A single relationship between two tables.

    Cardinality is expressed from the *many* side to the *one* side, which is the
    overwhelmingly common case generated from a foreign key.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    from_cardinality: str = "many"  # many | one
    to_cardinality: str = "one"  # one | many
    cross_filtering_behavior: str = "oneDirection"  # oneDirection | bothDirections
    is_active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticModelValidationIssue:
    """One consistency finding for a semantic-model spec."""

    severity: str  # error | warning
    code: str
    message: str
    object_ref: str | None = None


@dataclass
class SemanticModelValidationResult:
    """Structured consistency-check result for a semantic-model spec."""

    issues: list[SemanticModelValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[SemanticModelValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[SemanticModelValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        object_ref: str | None = None,
    ) -> None:
        self.issues.append(
            SemanticModelValidationIssue(
                severity=severity,
                code=code,
                message=message,
                object_ref=object_ref,
            )
        )


@dataclass
class SemanticModelSpec:
    """The complete, format-agnostic description of a semantic model."""

    name: str
    tables: list[SemanticTable] = field(default_factory=list)
    relationships: list[SemanticRelationship] = field(default_factory=list)
    culture: str = "en-US"
    compatibility_level: int = 1604
    description: str | None = None
    # Connection details for the Power Query partition source. When present a
    # deployable DirectQuery partition is generated; otherwise a stub is used.
    source_server: str | None = None
    source_database: str | None = None
    # "import", "directQuery" or "directLake". DirectQuery avoids data refresh for
    # SQL sources; Direct Lake loads OneLake Delta data on demand via a shared
    # expression (see ``DIRECT_LAKE_EXPRESSION``).
    storage_mode: str = "directQuery"
    # Data-source discriminator: "sql" for SQL analytics endpoints/warehouses,
    # "lakehouse" for a Fabric Lakehouse bound via Direct Lake on OneLake.
    source_kind: str = "sql"
    # Lakehouse (Direct Lake on OneLake) binding. Populated when
    # ``source_kind == "lakehouse"`` and used to generate the OneLake entity
    # partitions and shared expression instead of a ``Sql.Database`` source.
    lakehouse_id: str | None = None
    lakehouse_name: str | None = None
    onelake_workspace_id: str | None = None
    onelake_tables_path: str | None = None
    default_schema: str | None = None
    # Which Direct Lake form to render when ``storage_mode == "directLake"``:
    # "auto" (prefer OneLake when bound, else SQL), "onelake" or "sql". Ignored
    # for non-Direct-Lake storage modes.
    direct_lake_mode: str = "auto"

    # -- lookup helpers ---------------------------------------------------

    def table(self, name: str) -> SemanticTable | None:
        return next((t for t in self.tables if t.name == name), None)

    @property
    def has_onelake_binding(self) -> bool:
        """True when the spec carries enough info to bind on OneLake."""
        if self.onelake_tables_path:
            return True
        return bool(self.onelake_workspace_id and self.lakehouse_id)

    def resolved_direct_lake_mode(self) -> str:
        """Resolve ``direct_lake_mode`` to a concrete ``"onelake"`` or ``"sql"``.

        Direct Lake on OneLake is preferred whenever an OneLake binding is
        available; otherwise Direct Lake on SQL is used. An explicit mode wins
        over auto-detection so the user's toggle is always honoured.
        """
        mode = (self.direct_lake_mode or "auto").lower()
        if mode == "onelake":
            return "onelake"
        if mode == "sql":
            return "sql"
        return "onelake" if self.has_onelake_binding else "sql"


    # -- (de)serialisation ------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "culture": self.culture,
            "compatibility_level": self.compatibility_level,
            "storage_mode": self.storage_mode,
            "source_kind": self.source_kind,
            "tables": [t.to_dict() for t in self.tables],
            "relationships": [r.to_dict() for r in self.relationships],
        }
        if self.description:
            data["description"] = self.description
        if self.source_server:
            data["source_server"] = self.source_server
        if self.source_database:
            data["source_database"] = self.source_database
        if self.lakehouse_id:
            data["lakehouse_id"] = self.lakehouse_id
        if self.lakehouse_name:
            data["lakehouse_name"] = self.lakehouse_name
        if self.onelake_workspace_id:
            data["onelake_workspace_id"] = self.onelake_workspace_id
        if self.onelake_tables_path:
            data["onelake_tables_path"] = self.onelake_tables_path
        if self.default_schema:
            data["default_schema"] = self.default_schema
        if self.storage_mode == "directLake":
            data["direct_lake_mode"] = self.direct_lake_mode
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SemanticModelSpec":
        """Rebuild a spec from a (possibly AI-produced) dictionary.

        Unknown keys are ignored so the agent can be a little loose with its
        JSON without breaking deterministic rendering.
        """
        tables = [
            SemanticTable(
                name=t["name"],
                source_schema=t.get("source_schema", "dbo"),
                source_table=t.get("source_table", t["name"]),
                description=t.get("description"),
                is_hidden=bool(t.get("is_hidden", False)),
                is_date_table=bool(t.get("is_date_table", False)),
                columns=[
                    SemanticColumn(
                        name=c["name"],
                        source_column=c.get("source_column", c["name"]),
                        data_type=c.get("data_type", "string")
                        if c.get("data_type", "string") in VALID_DATA_TYPES
                        else "string",
                        summarize_by=c.get("summarize_by", "none"),
                        is_hidden=bool(c.get("is_hidden", False)),
                        is_key=bool(c.get("is_key", False)),
                        format_string=c.get("format_string"),
                        description=c.get("description"),
                        data_category=c.get("data_category"),
                    )
                    for c in t.get("columns", [])
                ],
                measures=[
                    SemanticMeasure(
                        name=m["name"],
                        expression=m["expression"],
                        format_string=m.get("format_string"),
                        description=m.get("description"),
                        display_folder=m.get("display_folder"),
                    )
                    for m in t.get("measures", [])
                ],
            )
            for t in data.get("tables", [])
        ]
        relationships = [
            SemanticRelationship(
                from_table=r["from_table"],
                from_column=r["from_column"],
                to_table=r["to_table"],
                to_column=r["to_column"],
                from_cardinality=r.get("from_cardinality", "many"),
                to_cardinality=r.get("to_cardinality", "one"),
                cross_filtering_behavior=r.get("cross_filtering_behavior", "oneDirection"),
                is_active=bool(r.get("is_active", True)),
            )
            for r in data.get("relationships", [])
        ]
        return cls(
            name=data["name"],
            tables=tables,
            relationships=relationships,
            culture=data.get("culture", "en-US"),
            compatibility_level=int(data.get("compatibility_level", 1604)),
            description=data.get("description"),
            source_server=data.get("source_server"),
            source_database=data.get("source_database"),
            storage_mode=data.get("storage_mode", "directQuery"),
            source_kind=data.get("source_kind", "sql"),
            lakehouse_id=data.get("lakehouse_id"),
            lakehouse_name=data.get("lakehouse_name"),
            onelake_workspace_id=data.get("onelake_workspace_id"),
            onelake_tables_path=data.get("onelake_tables_path"),
            default_schema=data.get("default_schema"),
            direct_lake_mode=data.get("direct_lake_mode", "auto"),
        )


# ---------------------------------------------------------------------------
# Deterministic mapping: SQL schema → SemanticModelSpec
# ---------------------------------------------------------------------------


def _sanitize_name(name: str) -> str:
    """Make a friendly model name from a raw object name (best-effort)."""
    return name.strip()


# ---------------------------------------------------------------------------
# Self-documenting descriptions
# ---------------------------------------------------------------------------
#
# The deterministic mapping keeps the *names* exactly as they appear in the
# source (no business-friendly renaming), but it does populate human-readable
# ``description`` text on every table and column so the generated model is
# self-documenting in Power BI / Fabric. The renderers already emit these as
# TMDL ``///`` lines and TMSL ``description`` properties.


def _humanize_label(name: str) -> str:
    """Turn a raw object name into a readable label for *description text only*.

    Splits ``snake_case`` and ``camelCase``/``PascalCase`` into spaced words,
    title-cases ordinary words and preserves all-caps acronyms (e.g. ``IBNR``).
    The original column/table name is never changed — this output is used only
    inside the generated description sentences.
    """
    s = name.strip().replace("_", " ")
    # Insert a space at lower/digit -> upper boundaries (``UnitPrice`` -> ``Unit Price``).
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    # And at acronym -> word boundaries (``IBNRValue`` -> ``IBNR Value``).
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return name.strip()
    words: list[str] = []
    for w in s.split(" "):
        if w.isupper():
            words.append(w)  # keep acronyms as-is
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words)


def _describe_table(schema: str, table: str, column_count: int) -> str:
    """Build a concise, self-documenting description for a table."""
    label = _humanize_label(table)
    return (
        f"{label} table sourced from [{schema}].[{table}] "
        f"with {column_count} column{'s' if column_count != 1 else ''}."
    )


def _describe_column(
    column_name: str,
    *,
    tabular_type: str,
    is_pk: bool,
    is_fk: bool,
    fk_ref: tuple[str, str, str] | None,
) -> str:
    """Build a concise, self-documenting description for a column.

    ``fk_ref`` is the ``(schema, table, column)`` a foreign key points at, when
    known, so the description records the join target.
    """
    label = _humanize_label(column_name)
    if is_pk:
        return f"Primary key uniquely identifying each row; {label} surrogate/business key."
    if is_fk and fk_ref is not None:
        ref_schema, ref_table, ref_col = fk_ref
        ref_label = _humanize_label(ref_table)
        return (
            f"Foreign key to the {ref_label} table "
            f"([{ref_schema}].[{ref_table}].[{ref_col}])."
        )
    if is_fk:
        return f"Foreign key column ({label}) referencing a related table."
    if tabular_type == "dateTime":
        return f"{label} date/time value."
    if tabular_type == "boolean":
        return f"{label} true/false flag."
    if tabular_type in _NUMERIC_TABULAR_TYPES:
        return f"{label} - numeric ({tabular_type}) measure aggregated by sum by default."
    return f"{label} ({tabular_type})."



def spec_from_schemas(
    schemas: Iterable[Any],
    *,
    model_name: str,
    source_server: str | None = None,
    source_database: str | None = None,
    storage_mode: str = "directQuery",
    source_kind: str = "sql",
    lakehouse_id: str | None = None,
    lakehouse_name: str | None = None,
    onelake_workspace_id: str | None = None,
    onelake_tables_path: str | None = None,
    default_schema: str | None = None,
    direct_lake_mode: str = "auto",
) -> SemanticModelSpec:
    """Build a default :class:`SemanticModelSpec` from extracted SQL schemas.

    ``schemas`` is an iterable of :class:`app.sql_client.TableSchema` (duck-typed
    here to avoid a hard import cycle). The mapping is intentionally
    conservative — it produces a structurally valid model that the AI agent can
    then refine:

    * every column is mapped to its nearest tabular data type;
    * primary-key columns are hidden and marked ``isKey`` with ``summarizeBy``
      ``none`` so they never become implicit measures;
    * numeric non-key columns default to ``sum``;
    * each foreign key becomes a many-to-one relationship. Extra foreign keys
      between the same pair of tables are kept but marked inactive (a model may
      only have one active relationship per table pair);
    * when no explicit foreign key links two selected tables, relationships are
      inferred from column-name conventions so the model is still useful on
      Fabric Lakehouse SQL endpoints (which do not declare FK constraints).
    """
    schemas = list(schemas)
    # Only physical tables become model tables; views have no reliable keys/FKs.
    included = {(s.schema, s.name) for s in schemas}

    tables: list[SemanticTable] = []
    for s in schemas:
        pk_names = {c.name for c in s.columns if getattr(c, "is_primary_key", False)}
        fk_names = {fk.column for fk in getattr(s, "foreign_keys", [])}
        # Map each FK column to the (schema, table, column) it references so the
        # generated description can record the join target.
        fk_refs: dict[str, tuple[str, str, str]] = {
            fk.column: (fk.references_schema, fk.references_table, fk.references_column)
            for fk in getattr(s, "foreign_keys", [])
        }
        key_names = pk_names | fk_names
        columns: list[SemanticColumn] = []
        for col in s.columns:
            tabular_type = map_sql_type(col.data_type)
            is_pk = col.name in pk_names
            is_fk = col.name in fk_names
            is_join_key = col.name in key_names
            if is_join_key:
                # Primary and foreign keys are for joins, not aggregation.
                summarize = "none"
            elif tabular_type in _NUMERIC_TABULAR_TYPES:
                summarize = "sum"
            else:
                summarize = "none"
            columns.append(
                SemanticColumn(
                    name=col.name,
                    source_column=col.name,
                    data_type=tabular_type,
                    summarize_by=summarize,
                    is_hidden=is_join_key,
                    is_key=is_pk,
                    description=_describe_column(
                        col.name,
                        tabular_type=tabular_type,
                        is_pk=is_pk,
                        is_fk=is_fk,
                        fk_ref=fk_refs.get(col.name),
                    ),
                )
            )
        tables.append(
            SemanticTable(
                name=_sanitize_name(s.name),
                source_schema=s.schema,
                source_table=s.name,
                columns=columns,
                description=_describe_table(s.schema, s.name, len(columns)),
            )
        )

    # ------------------------------------------------------------------
    # Relationships are built in two passes:
    #
    #   1. Explicit foreign keys reported by the SQL endpoint (highest
    #      confidence). Fabric Warehouses keep these as unenforced metadata;
    #      Lakehouse SQL endpoints almost never declare any.
    #   2. Name-based inference between selected tables, so common dimensional
    #      patterns (``fact_sales.CustomerKey`` -> ``dimension_customer``) still
    #      yield a relationship when no FK constraint exists.
    #
    # We track ``relationship_keys`` to avoid duplicating an inferred edge that
    # an explicit FK already covers, and assign ``is_active`` in a final pass
    # so each (from_table -> to_table) pair has exactly one active edge.
    # ------------------------------------------------------------------
    relationships: list[SemanticRelationship] = []
    relationship_keys: set[tuple[str, str, str, str]] = set()

    for s in schemas:
        for fk in getattr(s, "foreign_keys", []):
            ref_key = (fk.references_schema, fk.references_table)
            if ref_key not in included:
                # The referenced table was not selected; skip the relationship.
                continue
            key = (s.name, fk.references_table, fk.column, fk.references_column)
            if key in relationship_keys:
                continue
            relationship_keys.add(key)
            relationships.append(
                SemanticRelationship(
                    from_table=_sanitize_name(s.name),
                    from_column=fk.column,
                    to_table=_sanitize_name(fk.references_table),
                    to_column=fk.references_column,
                )
            )

    relationships.extend(
        _infer_relationships_by_name(schemas, included, relationship_keys)
    )

    # Tabular models allow only one active relationship per (from, to) pair.
    # Keep the first encountered edge active; mark duplicates inactive.
    active_pairs: set[tuple[str, str]] = set()
    for rel in relationships:
        pair = (rel.from_table, rel.to_table)
        if pair in active_pairs:
            rel.is_active = False
        else:
            active_pairs.add(pair)
            rel.is_active = True

    return SemanticModelSpec(
        name=model_name,
        tables=tables,
        relationships=relationships,
        source_server=source_server,
        source_database=source_database,
        storage_mode=storage_mode,
        source_kind=source_kind,
        lakehouse_id=lakehouse_id,
        lakehouse_name=lakehouse_name,
        onelake_workspace_id=onelake_workspace_id,
        onelake_tables_path=onelake_tables_path,
        default_schema=default_schema,
        direct_lake_mode=direct_lake_mode,
    )


# ---------------------------------------------------------------------------
# Name-based relationship inference
# ---------------------------------------------------------------------------


def _normalise_table_name(name: str) -> str:
    """Strip common dimensional prefixes/separators for fuzzy matching."""
    n = name.lower().strip()
    for prefix in ("dimension_", "dim_", "fact_", "fct_"):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    else:
        for prefix in ("dim", "fact"):
            if n.startswith(prefix) and len(n) > len(prefix):
                n = n[len(prefix):]
                break
    return n.replace("_", "").replace(" ", "")


def _single_pk(table: Any) -> Any | None:
    """Return the table's primary-key column when it has exactly one."""
    pks = [c for c in table.columns if getattr(c, "is_primary_key", False)]
    return pks[0] if len(pks) == 1 else None


def _infer_relationships_by_name(
    schemas: list[Any],
    included: set[tuple[str, str]],
    existing_keys: set[tuple[str, str, str, str]],
) -> list[SemanticRelationship]:
    """Infer many-to-one relationships between selected tables by column name.

    Two conservative heuristics are applied per column (in order):

    1. **Exact PK match** -- the column name equals another selected table's
       single primary-key column name (e.g. ``CustomerKey`` -> a dimension's
       ``CustomerKey``).
    2. **TableName + ``Id``/``Key`` suffix** -- the column is named
       ``<Prefix>Id`` or ``<Prefix>Key`` and another selected table's
       normalised name matches ``<Prefix>`` and has a single PK column named
       ``Id``, ``Key`` or the same as the column.

    Inferred edges are skipped when the column is the table's own primary key,
    when the candidate tabular data types differ, or when an explicit FK has
    already produced the same edge.
    """
    candidates = [s for s in schemas if (s.schema, s.name) in included]
    by_norm_name: dict[str, list[Any]] = {}
    for s in candidates:
        by_norm_name.setdefault(_normalise_table_name(s.name), []).append(s)

    inferred: list[SemanticRelationship] = []

    for s in candidates:
        own_pk_names = {
            c.name for c in s.columns if getattr(c, "is_primary_key", False)
        }
        for col in s.columns:
            if col.name in own_pk_names:
                # A table's own PK never points to another table.
                continue
            col_type = map_sql_type(col.data_type)

            target_table: Any | None = None
            target_pk: Any | None = None

            # 1) Exact PK-name match against another selected table.
            for other in candidates:
                if other is s:
                    continue
                other_pk = _single_pk(other)
                if other_pk is None or other_pk.name != col.name:
                    continue
                if map_sql_type(other_pk.data_type) != col_type:
                    continue
                target_table, target_pk = other, other_pk
                break

            # 2) ``<TableName>Id`` / ``<TableName>Key`` convention.
            if target_table is None:
                lname = col.name.lower()
                stripped: str | None = None
                for suffix in ("key", "id"):
                    if lname.endswith(suffix) and len(lname) > len(suffix):
                        stripped = lname[: -len(suffix)].rstrip("_")
                        break
                if stripped:
                    for other in by_norm_name.get(stripped, []):
                        if other is s:
                            continue
                        other_pk = _single_pk(other)
                        if other_pk is None:
                            continue
                        if other_pk.name.lower() not in {"id", "key", col.name.lower()}:
                            continue
                        if map_sql_type(other_pk.data_type) != col_type:
                            continue
                        target_table, target_pk = other, other_pk
                        break

            if target_table is None or target_pk is None:
                continue

            key = (s.name, target_table.name, col.name, target_pk.name)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            inferred.append(
                SemanticRelationship(
                    from_table=_sanitize_name(s.name),
                    from_column=col.name,
                    to_table=_sanitize_name(target_table.name),
                    to_column=target_pk.name,
                )
            )

    return inferred


# ---------------------------------------------------------------------------
# Suggestion engine — what the user can opt in to before building the model
# ---------------------------------------------------------------------------

# How many measures to suggest per table at most, so the picker stays usable.
_MAX_MEASURES_PER_TABLE = 8

# Column-name hints that imply a column should never be aggregated even when
# its SQL type is numeric (surrogate IDs and similar).
_NON_AGG_NAME_TOKENS = ("id", "key", "code", "number", "no", "num")


def _format_for(column_name: str, data_type: str) -> str | None:
    """Pick a sensible Power BI formatString hint for a numeric measure."""
    n = column_name.lower()
    if any(token in n for token in ("amount", "price", "cost", "revenue", "sales", "value", "total")):
        return "\\$#,0.00"
    if any(token in n for token in ("rate", "ratio", "pct", "percent")):
        return "0.0%"
    if data_type in ("decimal", "double"):
        return "#,0.00"
    if data_type == "int64":
        return "#,0"
    return None


def _looks_like_identifier(name: str) -> bool:
    """Return True when the column name looks like an identifier/key."""
    lname = name.lower()
    return any(lname.endswith(token) or lname == token for token in _NON_AGG_NAME_TOKENS)


def suggest_relationships(
    schemas: Iterable[Any],
    *,
    existing: Iterable[SemanticRelationship] | None = None,
) -> list[SuggestedRelationship]:
    """Propose many-to-one relationships among the selected ``schemas``.

    Returns the union of:

    * **Explicit foreign keys** reported by the source endpoint (highest
      confidence, ``source="fk"``).
    * **Name-based inferences**: exact PK-name match (``source="pk-match"``)
      or the ``<TableName>Id|Key`` convention (``source="suffix"``).

    Suggestions already present in ``existing`` (matched by from/to
    column tuple) are filtered out so the picker only shows *additions*.
    """
    schemas = list(schemas)
    included = {(s.schema, s.name) for s in schemas}
    excluded: set[tuple[str, str, str, str]] = set()
    for rel in existing or []:
        excluded.add(
            (rel.from_table, rel.to_table, rel.from_column, rel.to_column)
        )

    suggestions: list[SuggestedRelationship] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _add(
        from_t: str,
        from_c: str,
        to_t: str,
        to_c: str,
        *,
        source: str,
        rationale: str,
        confidence: float,
    ) -> None:
        key = (from_t, to_t, from_c, to_c)
        if key in seen or key in excluded:
            return
        seen.add(key)
        suggestions.append(
            SuggestedRelationship(
                relationship=SemanticRelationship(
                    from_table=from_t,
                    from_column=from_c,
                    to_table=to_t,
                    to_column=to_c,
                ),
                rationale=rationale,
                confidence=confidence,
                source=source,
            )
        )

    # Pass 1: explicit foreign keys.
    for s in schemas:
        for fk in getattr(s, "foreign_keys", []):
            ref_key = (fk.references_schema, fk.references_table)
            if ref_key not in included:
                continue
            _add(
                _sanitize_name(s.name),
                fk.column,
                _sanitize_name(fk.references_table),
                fk.references_column,
                source="fk",
                rationale=(
                    f"Foreign key {getattr(fk, 'constraint_name', '')} on "
                    f"{s.name}.{fk.column} references "
                    f"{fk.references_table}.{fk.references_column}."
                ).strip(),
                confidence=1.0,
            )

    # Pass 2: name-based inference (reuses the existing private helper).
    inferred = _infer_relationships_by_name(schemas, included, set())
    by_norm_name: dict[str, list[Any]] = {}
    for s in schemas:
        if (s.schema, s.name) in included:
            by_norm_name.setdefault(_normalise_table_name(s.name), []).append(s)
    for rel in inferred:
        # Decide whether the inference came from an exact PK-name match or
        # the suffix convention so we can label the suggestion accurately.
        norm_target = _normalise_table_name(rel.to_table)
        from_col_l = rel.from_column.lower()
        is_suffix = (
            from_col_l.endswith("id") or from_col_l.endswith("key")
        ) and from_col_l[:-2].rstrip("_") in {norm_target, rel.to_table.lower()}
        _add(
            rel.from_table,
            rel.from_column,
            rel.to_table,
            rel.to_column,
            source="suffix" if is_suffix else "pk-match",
            rationale=(
                f"Column {rel.from_table}.{rel.from_column} matches the "
                f"primary key of {rel.to_table} by "
                + ("name suffix convention." if is_suffix else "column name.")
            ),
            confidence=0.85 if is_suffix else 0.95,
        )

    return suggestions


def suggest_measures(
    schemas: Iterable[Any],
    *,
    existing: Iterable[tuple[str, str]] | None = None,
) -> list[SuggestedMeasure]:
    """Propose default DAX measures for the selected tables.

    For every table the engine proposes:

    * a ``Row Count`` measure (``COUNTROWS('Table')``), and
    * a ``Total <Column>`` (``SUM``) measure for each numeric, non-key
      column. Decimal/double columns also get an ``Average <Column>``
      (``AVERAGE``) suggestion so the user can pick what they need.

    The number of measures per table is capped to keep the picker usable.
    ``existing`` is a set of ``(table, measure_name)`` pairs that should be
    skipped — typically what the spec already contains.
    """
    schemas = list(schemas)
    skip = set(existing or [])
    suggestions: list[SuggestedMeasure] = []

    for s in schemas:
        table_name = _sanitize_name(s.name)
        pk_names = {
            c.name for c in s.columns if getattr(c, "is_primary_key", False)
        }
        fk_names = {fk.column for fk in getattr(s, "foreign_keys", [])}
        key_names = pk_names | fk_names

        per_table: list[SuggestedMeasure] = []

        # Row count is always useful, even on dimension tables.
        rc_name = f"{table_name} Row Count"
        if (table_name, rc_name) not in skip:
            per_table.append(
                SuggestedMeasure(
                    table=table_name,
                    measure=SemanticMeasure(
                        name=rc_name,
                        expression=f"COUNTROWS({dax_table_ref(table_name)})",
                        format_string="#,0",
                        display_folder="Counts",
                        description=f"Number of rows in {table_name}.",
                    ),
                    rationale=f"Row count for {table_name} is a universally useful measure.",
                    confidence=0.9,
                    source="deterministic",
                )
            )

        for col in s.columns:
            if col.name in key_names or _looks_like_identifier(col.name):
                continue
            tabular = map_sql_type(col.data_type)
            if tabular not in _NUMERIC_TABULAR_TYPES:
                continue

            sum_name = f"Total {col.name}"
            if (table_name, sum_name) not in skip:
                per_table.append(
                    SuggestedMeasure(
                        table=table_name,
                        measure=SemanticMeasure(
                            name=sum_name,
                            expression=f"SUM({dax_qualified_column(table_name, col.name)})",
                            format_string=_format_for(col.name, tabular),
                            display_folder="Measures",
                            description=(
                                f"Sum of {col.name} across all rows of {table_name}."
                            ),
                        ),
                        rationale=(
                            f"{col.name} is a numeric, non-key column "
                            f"in {table_name} — typically aggregated by sum."
                        ),
                        confidence=0.85,
                        source="deterministic",
                    )
                )

            if tabular in ("decimal", "double"):
                avg_name = f"Average {col.name}"
                if (table_name, avg_name) not in skip:
                    per_table.append(
                        SuggestedMeasure(
                            table=table_name,
                            measure=SemanticMeasure(
                                name=avg_name,
                                expression=f"AVERAGE({dax_qualified_column(table_name, col.name)})",
                                format_string=_format_for(col.name, tabular),
                                display_folder="Measures",
                                description=(
                                    f"Average of {col.name} across all rows of {table_name}."
                                ),
                            ),
                            rationale=(
                                f"{col.name} is a continuous numeric column; "
                                "averaging may be more meaningful than summing."
                            ),
                            confidence=0.65,
                            source="deterministic",
                        )
                    )

        suggestions.extend(per_table[:_MAX_MEASURES_PER_TABLE])

    return suggestions


def suggest_from_schemas(
    schemas: Iterable[Any],
    *,
    spec: SemanticModelSpec | None = None,
) -> SemanticModelSuggestions:
    """Run both deterministic suggesters and return a combined bundle.

    When ``spec`` is provided, suggestions already present in it
    (relationships by endpoint columns; measures by ``(table, name)``) are
    filtered out so the user only sees *new* options.
    """
    schemas = list(schemas)
    existing_rels = list(spec.relationships) if spec else None
    existing_measures: list[tuple[str, str]] = []
    if spec:
        for t in spec.tables:
            for m in t.measures:
                existing_measures.append((t.name, m.name))
    return SemanticModelSuggestions(
        relationships=suggest_relationships(schemas, existing=existing_rels),
        measures=suggest_measures(schemas, existing=existing_measures or None),
    )


def apply_suggestions(
    spec: SemanticModelSpec,
    *,
    relationships: Iterable[SuggestedRelationship] | None = None,
    measures: Iterable[SuggestedMeasure] | None = None,
) -> SemanticModelSpec:
    """Return a new spec with accepted suggestions merged in.

    * Relationship suggestions are appended to ``spec.relationships`` unless an
      identical edge (same from/to columns) is already present. After the
      merge, ``is_active`` is recomputed so each ``(from_table, to_table)``
      pair has at most one active relationship.
    * Measure suggestions are appended to the matching table by name. Measures
      with the same name on the same table are skipped (idempotent).
    * Tables, columns, source connection and storage mode are preserved
      verbatim — only relationships and measures are added.

    The function never mutates ``spec`` in place; callers can keep both the
    pre-acceptance and post-acceptance versions for diffing/undo.
    """
    new_tables: list[SemanticTable] = []
    for t in spec.tables:
        new_tables.append(
            SemanticTable(
                name=t.name,
                source_schema=t.source_schema,
                source_table=t.source_table,
                description=t.description,
                is_hidden=t.is_hidden,
                is_date_table=t.is_date_table,
                columns=list(t.columns),
                measures=list(t.measures),
            )
        )
    by_name = {t.name: t for t in new_tables}
    # Column index for validating that relationship endpoints actually exist.
    column_index = {t.name: {c.name for c in t.columns} for t in new_tables}

    for sm in measures or []:
        target = by_name.get(sm.table)
        if target is None:
            # Suggestion targets a table that isn't in the spec; skip safely.
            continue
        if any(existing.name == sm.measure.name for existing in target.measures):
            continue
        target.measures.append(
            SemanticMeasure(
                name=sm.measure.name,
                expression=sm.measure.expression,
                format_string=sm.measure.format_string,
                description=sm.measure.description,
                display_folder=sm.measure.display_folder,
            )
        )

    existing_rel_keys = {
        (r.from_table, r.to_table, r.from_column, r.to_column)
        for r in spec.relationships
    }
    new_rels: list[SemanticRelationship] = [
        SemanticRelationship(
            from_table=r.from_table,
            from_column=r.from_column,
            to_table=r.to_table,
            to_column=r.to_column,
            from_cardinality=r.from_cardinality,
            to_cardinality=r.to_cardinality,
            cross_filtering_behavior=r.cross_filtering_behavior,
            is_active=r.is_active,
        )
        for r in spec.relationships
    ]
    for sr in relationships or []:
        r = sr.relationship
        key = (r.from_table, r.to_table, r.from_column, r.to_column)
        if key in existing_rel_keys:
            continue
        # Never accept a relationship whose endpoints are not real columns —
        # Fabric rejects the whole dataset with "Cannot resolve all the paths"
        # (HTTP 500) when FromColumn/ToColumn point at a missing object. This
        # most often happens with AI-suggested edges that hallucinate a column.
        if not relationship_is_resolvable(r, column_index):
            continue
        existing_rel_keys.add(key)
        new_rels.append(
            SemanticRelationship(
                from_table=r.from_table,
                from_column=r.from_column,
                to_table=r.to_table,
                to_column=r.to_column,
                from_cardinality=r.from_cardinality,
                to_cardinality=r.to_cardinality,
                cross_filtering_behavior=r.cross_filtering_behavior,
                is_active=r.is_active,
            )
        )

    # Enforce single active relationship per (from, to) table pair.
    active_pairs: set[tuple[str, str]] = set()
    for rel in new_rels:
        pair = (rel.from_table, rel.to_table)
        if pair in active_pairs:
            rel.is_active = False
        else:
            active_pairs.add(pair)
            rel.is_active = True

    return SemanticModelSpec(
        name=spec.name,
        tables=new_tables,
        relationships=new_rels,
        culture=spec.culture,
        compatibility_level=spec.compatibility_level,
        description=spec.description,
        source_server=spec.source_server,
        source_database=spec.source_database,
        storage_mode=spec.storage_mode,
    )


def dedupe_measure_names(
    spec: SemanticModelSpec,
) -> tuple[SemanticModelSpec, list[tuple[str, str]]]:
    """Return a copy of ``spec`` whose measure names are conflict-free.

    In a tabular model every measure shares a single, global namespace: two
    measures with the same name — even on different tables — make Fabric reject
    the dataset. A measure also cannot share its name with a *column* on the
    same table; Fabric rejects that with ``The '<name>' measure cannot be
    created because a column with the same name already exists.`` This renames
    any such clash (compared case-insensitively, the way Power BI enforces
    uniqueness) by appending a numeric suffix (``Profit`` -> ``Profit 2`` ->
    ``Profit 3`` …). The first occurrence of each measure name is kept verbatim.

    When a measure is renamed, every other measure that referenced it by name
    (``[Profit]``) is rewritten to the new name (``[Profit 2]``) so dependent
    measures keep resolving. Without this, a renamed measure would leave callers
    pointing at a now-missing object — Fabric then raises *"The value for
    'Profit' cannot be determined. Either the column doesn't exist, or there is
    no current row for this column."* at query time.

    The function never mutates ``spec`` in place. It returns the new spec and a
    list of ``(old_name, new_name)`` renames so callers can surface what
    changed; the list is empty when every measure was already unique.
    """
    used: set[str] = set()  # case-folded measure names already taken model-wide
    renames: list[tuple[str, str]] = []
    new_tables: list[SemanticTable] = []
    for t in spec.tables:
        # A measure may not collide with a column on its own table, so the
        # home table's column names are reserved when naming its measures.
        column_names = {c.name.casefold() for c in t.columns}
        new_measures: list[SemanticMeasure] = []
        for m in t.measures:
            if m.name.casefold() not in used and m.name.casefold() not in column_names:
                used.add(m.name.casefold())
                new_measures.append(m)
                continue
            suffix = 2
            candidate = f"{m.name} {suffix}"
            while candidate.casefold() in used or candidate.casefold() in column_names:
                suffix += 1
                candidate = f"{m.name} {suffix}"
            used.add(candidate.casefold())
            renames.append((m.name, candidate))
            new_measures.append(
                SemanticMeasure(
                    name=candidate,
                    expression=m.expression,
                    format_string=m.format_string,
                    description=m.description,
                    display_folder=m.display_folder,
                )
            )
        new_tables.append(
            SemanticTable(
                name=t.name,
                source_schema=t.source_schema,
                source_table=t.source_table,
                description=t.description,
                is_hidden=t.is_hidden,
                is_date_table=t.is_date_table,
                columns=list(t.columns),
                measures=new_measures,
            )
        )
    if not renames:
        return spec, []

    # Propagate the renames into every measure expression so callers that
    # reference a renamed measure (``[Profit]``) point at its new name.
    rename_map = dict(renames)
    for t in new_tables:
        for i, m in enumerate(t.measures):
            rewritten = rewrite_measure_references(m.expression, rename_map)
            if rewritten != m.expression:
                t.measures[i] = SemanticMeasure(
                    name=m.name,
                    expression=rewritten,
                    format_string=m.format_string,
                    description=m.description,
                    display_folder=m.display_folder,
                )

    # ``replace`` preserves every other field (source_kind, lakehouse/OneLake
    # binding, direct_lake_mode, …) so deduping measures never silently drops a
    # Direct Lake on OneLake binding and downgrades the model to Direct Lake on
    # SQL (which would reintroduce the credential-refresh failure).
    return replace(spec, tables=new_tables), renames


def _measure_reference_problem(
    expression: str,
    *,
    known_measures: set[str],
    all_columns_cf: set[str],
    columns_cf_by_table: dict[str, set[str]],
    table_lookup: dict[str, str],
) -> str | None:
    """Return why ``expression`` is unresolvable, or ``None`` if it resolves.

    Mirrors the ``measure-references-unknown-*`` validation rules so the same
    definition of "resolvable" is used to *prevent* a broken measure as is used
    to *flag* one.
    """
    if not expression or not expression.strip():
        return None
    for ref_table, ref_name in iter_dax_references(expression):
        name_cf = ref_name.casefold()
        if ref_table is None:
            # Unqualified ``[Name]``: a measure, or a column anywhere.
            if name_cf in known_measures or name_cf in all_columns_cf:
                continue
            return (
                f"references [{ref_name}], which is not a column or measure "
                "in the model"
            )
        owner = table_lookup.get(ref_table.casefold())
        if owner is None:
            return f"references table {ref_table!r}, which is not in the model"
        if name_cf not in columns_cf_by_table[owner] and name_cf not in known_measures:
            return (
                f"references {ref_table}[{ref_name}], which is not a column on "
                "that table"
            )
    return None


def drop_unresolved_measures(
    spec: SemanticModelSpec,
) -> tuple[SemanticModelSpec, list[tuple[str, str]]]:
    """Remove measures whose DAX references an object that is not in the model.

    The AI design agent occasionally emits a measure that calls a helper it
    never created — e.g. ``Average Population per City`` defined as
    ``DIVIDE([Total Population], [City Count])`` when no ``City Count`` measure
    or column exists. Such a measure deploys fine but fails at *query* time
    ("The value for 'City Count' cannot be determined"), and the auditor flags
    it as ``measure-references-unknown-object``. Dropping it here keeps the
    generated model query-clean by construction.

    The check runs to a fixpoint: removing one measure can invalidate another
    that referenced it, so passes repeat until no further measures are dropped.
    ``spec`` is mutated in place (its ``tables`` keep every other field intact)
    and also returned for convenience, alongside a list of
    ``(object_ref, reason)`` for each dropped measure so callers can report it.
    """
    columns_cf_by_table = {
        t.name: {c.name.casefold() for c in t.columns} for t in spec.tables
    }
    all_columns_cf = {c for cols in columns_cf_by_table.values() for c in cols}
    table_lookup = {t.name.casefold(): t.name for t in spec.tables}

    dropped: list[tuple[str, str]] = []
    while True:
        known_measures = {
            m.name.casefold()
            for t in spec.tables
            for m in t.measures
            if m.name.strip()
        }
        removed_this_pass = False
        for table in spec.tables:
            kept: list[SemanticMeasure] = []
            for measure in table.measures:
                reason = _measure_reference_problem(
                    measure.expression,
                    known_measures=known_measures,
                    all_columns_cf=all_columns_cf,
                    columns_cf_by_table=columns_cf_by_table,
                    table_lookup=table_lookup,
                )
                if reason is None:
                    kept.append(measure)
                else:
                    dropped.append((f"{table.name}.{measure.name}", reason))
                    removed_this_pass = True
            table.measures = kept
        if not removed_this_pass:
            break

    return spec, dropped


def _column_index(spec: SemanticModelSpec) -> dict[str, set[str]]:
    """Map each table name to the set of its column names (model names)."""
    return {t.name: {c.name for c in t.columns} for t in spec.tables}


def relationship_is_resolvable(
    rel: SemanticRelationship, index: dict[str, set[str]]
) -> bool:
    """Return True when both endpoints of ``rel`` exist in the model.

    ``index`` is the output of :func:`_column_index`. A relationship is only
    deployable when its ``from``/``to`` tables exist *and* each references a
    real column on that table. Fabric raises ``Cannot resolve all the paths``
    (HTTP 500) when ``FromColumn``/``ToColumn`` point at a missing object —
    typically an AI-suggested edge that named a column the table doesn't have.
    """
    from_cols = index.get(rel.from_table)
    to_cols = index.get(rel.to_table)
    if from_cols is None or to_cols is None:
        return False
    return rel.from_column in from_cols and rel.to_column in to_cols


def resolvable_relationships(
    spec: SemanticModelSpec,
) -> tuple[list[SemanticRelationship], list[SemanticRelationship]]:
    """Split ``spec.relationships`` into ``(valid, dropped)``.

    ``valid`` relationships reference only tables/columns that exist in the
    model; ``dropped`` ones would make Fabric reject the whole dataset.
    """
    index = _column_index(spec)
    valid: list[SemanticRelationship] = []
    dropped: list[SemanticRelationship] = []
    for rel in spec.relationships:
        (valid if relationship_is_resolvable(rel, index) else dropped).append(rel)
    return valid, dropped


def direct_lake_incompatible_columns(
    spec: SemanticModelSpec,
) -> list[tuple[str, str]]:
    """List ``(object_ref, data_type)`` for columns Direct Lake cannot store.

    A Direct Lake table rejects binary columns; Fabric fails the dataset import
    with "Column '<x>' with binary data type is not allowed in Direct Lake
    table". Returns an empty list for non-Direct-Lake models, which support
    binary columns.
    """
    if spec.storage_mode != "directLake":
        return []
    return [
        (f"{table.name}.{col.name}", col.data_type)
        for table in spec.tables
        for col in table.columns
        if col.data_type in DIRECT_LAKE_UNSUPPORTED_TYPES
    ]


def drop_direct_lake_incompatible_columns(
    spec: SemanticModelSpec,
) -> tuple[SemanticModelSpec, list[tuple[str, str]]]:
    """Return a spec with Direct-Lake-incompatible columns removed.

    Strips binary columns from a Direct Lake model so the dataset imports
    instead of failing at publish time ("binary data type is not allowed in
    Direct Lake table"). No-op for non-Direct-Lake models, which support binary
    columns. Does not mutate ``spec``; returns the new spec plus a list of
    ``(object_ref, data_type)`` for each dropped column. Relationships that
    referenced a dropped column are cleaned up later by
    :func:`resolvable_relationships` during rendering.
    """
    bad = direct_lake_incompatible_columns(spec)
    if not bad:
        return spec, []
    bad_refs = {ref for ref, _ in bad}
    new_tables = [
        replace(
            table,
            columns=[
                c for c in table.columns if f"{table.name}.{c.name}" not in bad_refs
            ],
        )
        for table in spec.tables
    ]
    return replace(spec, tables=new_tables), bad


def _duplicates(values: Iterable[str]) -> set[str]:
    """Return values that occur more than once while preserving exact spelling."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes


def _relationship_ref(rel: SemanticRelationship) -> str:
    return f"{rel.from_table}.{rel.from_column}->{rel.to_table}.{rel.to_column}"


def validate_semantic_model_spec(
    spec: SemanticModelSpec,
) -> SemanticModelValidationResult:
    """Run consistency checks before rendering a semantic model definition.

    This is the preflight boundary between semantic-model design and TMDL/TMSL
    generation. It catches issues Fabric would otherwise report late and often
    opaquely, such as relationships that reference columns not present in the
    model, duplicate names, or missing deployment-critical connection details.
    """
    result = SemanticModelValidationResult()

    if not spec.name or not spec.name.strip():
        result.add("error", "model-name-empty", "Model name is required.")
    if not spec.tables:
        result.add("error", "no-tables", "At least one table is required.")

    if spec.storage_mode not in VALID_STORAGE_MODES:
        result.add(
            "error",
            "invalid-storage-mode",
            "Storage mode must be one of "
            f"{', '.join(sorted(VALID_STORAGE_MODES))}, not {spec.storage_mode!r}.",
        )

    if spec.storage_mode == "directLake":
        if spec.direct_lake_mode not in VALID_DIRECT_LAKE_MODES:
            result.add(
                "error",
                "invalid-direct-lake-mode",
                "Direct Lake mode must be one of "
                f"{', '.join(sorted(VALID_DIRECT_LAKE_MODES))}, not "
                f"{spec.direct_lake_mode!r}.",
            )
        else:
            resolved = spec.resolved_direct_lake_mode()
            if resolved == "onelake" and not spec.has_onelake_binding:
                # Direct Lake on OneLake binds to the OneLake storage location;
                # without a lakehouse OneLake path the model cannot be deployed.
                result.add(
                    "error",
                    "direct-lake-onelake-binding-missing",
                    "Direct Lake on OneLake requires a lakehouse OneLake binding "
                    "(OneLake tables path or workspace + lakehouse id). Choose a "
                    "lakehouse source or switch to Direct Lake on SQL.",
                )
        # Binary columns make Fabric reject a Direct Lake dataset at import
        # time. They are stripped automatically before rendering, but surface
        # the issue here so the author can choose import/DirectQuery instead if
        # the column matters.
        for object_ref, data_type in direct_lake_incompatible_columns(spec):
            result.add(
                "warning",
                "direct-lake-unsupported-column-type",
                f"Column {object_ref} has data type {data_type!r}, which a Direct "
                "Lake table cannot store; it will be dropped from the published "
                "model. Use import or DirectQuery storage to keep it.",
                object_ref,
            )

    if not spec.source_server:
        result.add(
            "error",
            "source-server-missing",
            "Source server is required for a deployable partition.",
        )
    if not spec.source_database:
        result.add(
            "error",
            "source-database-missing",
            "Source database is required for a deployable partition.",
        )

    for table_name in _duplicates(t.name for t in spec.tables):
        result.add(
            "error",
            "duplicate-table-name",
            f"Table name {table_name!r} appears more than once.",
            table_name,
        )

    # Measure names share a single, model-wide namespace in a tabular model, so
    # a name reused on a *different* table is just as invalid as one reused on
    # the same table. Compare case-insensitively, matching Power BI's rule.
    measure_owner: dict[str, str] = {}
    for table in spec.tables:
        for measure in table.measures:
            key = measure.name.casefold()
            object_ref = f"{table.name}.{measure.name}"
            if key in measure_owner:
                result.add(
                    "error",
                    "duplicate-measure-name",
                    f"Measure name {measure.name!r} is not unique across the model "
                    f"(also defined as {measure_owner[key]}); measure names must be "
                    f"unique model-wide.",
                    object_ref,
                )
            else:
                measure_owner[key] = object_ref

    for table in spec.tables:
        table_ref = table.name
        if not table.name.strip():
            result.add("error", "table-name-empty", "Table name is required.")
        if not table.source_table.strip():
            result.add(
                "error",
                "source-table-missing",
                f"Table {table.name!r} is missing its source table name.",
                table_ref,
            )
        if not table.columns:
            result.add(
                "warning",
                "table-has-no-columns",
                f"Table {table.name!r} has no columns.",
                table_ref,
            )
        for column_name in _duplicates(c.name for c in table.columns):
            result.add(
                "error",
                "duplicate-column-name",
                f"Column name {column_name!r} appears more than once in table {table.name!r}.",
                f"{table.name}.{column_name}",
            )
        # A measure cannot share its name with a column on the same table;
        # Fabric rejects this with "The '<name>' measure cannot be created
        # because a column with the same name already exists."
        column_names = {c.name.casefold() for c in table.columns}
        for measure in table.measures:
            if measure.name.casefold() in column_names:
                result.add(
                    "error",
                    "measure-name-collides-with-column",
                    f"Measure name {measure.name!r} collides with a column of the "
                    f"same name in table {table.name!r}; a measure cannot share its "
                    f"name with a column on the same table.",
                    f"{table.name}.{measure.name}",
                )
        for col in table.columns:
            object_ref = f"{table.name}.{col.name}"
            if not col.name.strip():
                result.add("error", "column-name-empty", "Column name is required.", object_ref)
            if not col.source_column.strip():
                result.add(
                    "error",
                    "source-column-missing",
                    f"Column {object_ref} is missing its source column name.",
                    object_ref,
                )
            if col.data_type not in VALID_DATA_TYPES:
                result.add(
                    "error",
                    "invalid-column-data-type",
                    f"Column {object_ref} has unsupported data type {col.data_type!r}.",
                    object_ref,
                )
        for measure in table.measures:
            object_ref = f"{table.name}.{measure.name}"
            if not measure.name.strip():
                result.add("error", "measure-name-empty", "Measure name is required.", object_ref)
            if not measure.expression or not measure.expression.strip():
                result.add(
                    "error",
                    "measure-expression-empty",
                    f"Measure {object_ref} has an empty DAX expression.",
                    object_ref,
                )
            elif measure.expression.lstrip().startswith("="):
                result.add(
                    "warning",
                    "measure-expression-leading-equals",
                    f"Measure {object_ref} should store only the DAX expression, without a leading '='.",
                    object_ref,
                )

    # Measure DAX references must resolve to a real column or measure. An
    # unresolved reference — e.g. a 'Profit Margin %' measure calling [Profit]
    # when no Profit measure or column exists (often because a referenced
    # measure was renamed or never created) — deploys fine but fails at query
    # time with "The value for 'Profit' cannot be determined", so we surface it
    # here as a blocking issue before the model is created.
    known_measures = {
        m.name.casefold() for t in spec.tables for m in t.measures if m.name.strip()
    }
    columns_cf_by_table = {
        t.name: {c.name.casefold() for c in t.columns} for t in spec.tables
    }
    all_columns_cf = {c for cols in columns_cf_by_table.values() for c in cols}
    table_lookup = {t.name.casefold(): t.name for t in spec.tables}
    for table in spec.tables:
        for measure in table.measures:
            if not measure.expression or not measure.expression.strip():
                continue
            object_ref = f"{table.name}.{measure.name}"
            for ref_table, ref_name in iter_dax_references(measure.expression):
                name_cf = ref_name.casefold()
                if ref_table is None:
                    # Unqualified ``[Name]``: a measure, or a column anywhere.
                    if name_cf in known_measures or name_cf in all_columns_cf:
                        continue
                    result.add(
                        "error",
                        "measure-references-unknown-object",
                        f"Measure {object_ref} references [{ref_name}], which is not "
                        f"a column or measure in the model.",
                        object_ref,
                    )
                else:
                    owner = table_lookup.get(ref_table.casefold())
                    if owner is None:
                        result.add(
                            "error",
                            "measure-references-unknown-table",
                            f"Measure {object_ref} references table {ref_table!r}, "
                            f"which is not in the model.",
                            object_ref,
                        )
                    elif (
                        name_cf not in columns_cf_by_table[owner]
                        and name_cf not in known_measures
                    ):
                        result.add(
                            "error",
                            "measure-references-unknown-column",
                            f"Measure {object_ref} references {ref_table}[{ref_name}], "
                            f"which is not a column on that table.",
                            object_ref,
                        )

    index = _column_index(spec)
    relationship_keys: set[tuple[str, str, str, str]] = set()
    active_pairs: set[tuple[str, str]] = set()
    for rel in spec.relationships:
        object_ref = _relationship_ref(rel)
        key = (rel.from_table, rel.to_table, rel.from_column, rel.to_column)
        if key in relationship_keys:
            result.add(
                "warning",
                "duplicate-relationship",
                f"Relationship {object_ref} appears more than once.",
                object_ref,
            )
        relationship_keys.add(key)
        if not relationship_is_resolvable(rel, index):
            result.add(
                "error",
                "relationship-endpoint-missing",
                f"Relationship {object_ref} references a table or column that is not in the model.",
                object_ref,
            )
        pair = (rel.from_table, rel.to_table)
        if rel.is_active and pair in active_pairs:
            result.add(
                "warning",
                "multiple-active-relationships",
                f"More than one active relationship exists from {rel.from_table!r} to {rel.to_table!r}.",
                object_ref,
            )
        if rel.is_active:
            active_pairs.add(pair)
        if rel.cross_filtering_behavior not in {"oneDirection", "bothDirections"}:
            result.add(
                "error",
                "invalid-cross-filtering-behavior",
                f"Relationship {object_ref} has invalid cross-filtering behavior {rel.cross_filtering_behavior!r}.",
                object_ref,
            )
        if rel.from_cardinality not in {"one", "many"} or rel.to_cardinality not in {"one", "many"}:
            result.add(
                "error",
                "invalid-relationship-cardinality",
                f"Relationship {object_ref} has invalid cardinality.",
                object_ref,
            )

    return result

