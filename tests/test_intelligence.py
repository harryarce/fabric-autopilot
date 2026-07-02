"""Validation tests for the semantic-model definition engine.

These tests treat the generated TMDL/TMSL exactly the way the Fabric importer
does — they parse each file line-by-line and enforce the structural rules that
have caused real ``CorruptedPayload`` / ``InvalidLineType`` rejections during
``POST /workspaces/{id}/semanticModels``.

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import re
import unittest
from typing import Any

from app.intelligence import (
    DefinitionFormat,
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticModelSuggestions,
    SemanticRelationship,
    SemanticTable,
    SuggestedMeasure,
    SuggestedRelationship,
    apply_suggestions,
    build_definition,
    dax_column_ref,
    dax_measure_ref,
    dax_qualified_column,
    dax_table_ref,
    dedupe_measure_names,
    direct_lake_incompatible_columns,
    drop_direct_lake_incompatible_columns,
    drop_unresolved_measures,
    parse_semantic_model,
    resolvable_relationships,
    spec_from_schemas,
    suggest_from_schemas,
    suggest_measures,
    validate_semantic_model_spec,
)

# ---------------------------------------------------------------------------
# Test doubles for the SQL schema duck-types
# ---------------------------------------------------------------------------


class _Col:
    def __init__(self, name, data_type, *, pk=False, nullable=True):
        self.name = name
        self.data_type = data_type
        self.type_display = data_type
        self.is_nullable = nullable
        self.is_primary_key = pk
        self.max_length = None
        self.precision = None
        self.scale = None
        self.default = None
        self.ordinal = 0


class _FK:
    def __init__(self, col, rs, rt, rc):
        self.column = col
        self.references_schema = rs
        self.references_table = rt
        self.references_column = rc
        self.constraint_name = f"fk_{col}"
        self.references_full = f"{rs}.{rt}.{rc}"


class _Table:
    def __init__(self, schema, name, columns, fks=None, object_type="TABLE"):
        self.schema = schema
        self.name = name
        self.columns = columns
        self.foreign_keys = fks or []
        self.object_type = object_type
        self.full_name = f"{schema}.{name}"


# ---------------------------------------------------------------------------
# Sample schemas
# ---------------------------------------------------------------------------


def star_schema_tables():
    """A small star schema: Sales fact + Product/Date dimensions."""
    product = _Table(
        "dbo",
        "dimension_product",
        [
            _Col("ProductKey", "int", pk=True),
            _Col("ProductName", "nvarchar"),
            _Col("Category", "nvarchar"),
            _Col("UnitPrice", "decimal"),
        ],
    )
    date_dim = _Table(
        "dbo",
        "dimension_date",
        [
            _Col("DateKey", "int", pk=True),
            _Col("FullDate", "date"),
            _Col("Year", "int"),
        ],
    )
    sales = _Table(
        "dbo",
        "fact_sales",
        [
            _Col("SalesKey", "int", pk=True),
            _Col("ProductKey", "int"),
            _Col("DateKey", "int"),
            _Col("Quantity", "int"),
            _Col("Amount", "money"),
            _Col("Notes", "nvarchar"),
        ],
        fks=[
            _FK("ProductKey", "dbo", "dimension_product", "ProductKey"),
            _FK("DateKey", "dbo", "dimension_date", "DateKey"),
        ],
    )
    return [product, date_dim, sales]


def tricky_names_tables():
    """Tables/columns with spaces and reserved-looking names."""
    return [
        _Table(
            "dbo",
            "Sales Orders",
            [
                _Col("Order Id", "int", pk=True),
                _Col("table", "nvarchar"),  # reserved-looking
                _Col("Total $", "money"),
            ],
        )
    ]


# ---------------------------------------------------------------------------
# Lightweight TMDL line classifier — used by the rule tests
# ---------------------------------------------------------------------------


# An object-introducing line is something the parser will accept after a `///`
# description. The list covers every object type the engine emits.
OBJECT_LINE_RE = re.compile(
    r"^[\t]*"  # any TMDL indentation
    r"(model|database|table|column|measure|partition|relationship|role|"
    r"cultureInfo|perspective|hierarchy|annotation|expression|dataSource)\b"
)


def classify_line(line: str) -> str:
    """Return ``"description" | "object" | "blank" | "other"``."""
    stripped_left = line.lstrip("\t ")
    if not stripped_left.strip():
        return "blank"
    if stripped_left.startswith("///"):
        return "description"
    if OBJECT_LINE_RE.match(line):
        return "object"
    return "other"


# ---------------------------------------------------------------------------
# Generic structural rules — apply to every TMDL file
# ---------------------------------------------------------------------------


def _assert_tmdl_well_formed(test: unittest.TestCase, path: str, text: str) -> None:
    """Apply every structural rule we know to a TMDL file."""
    lines = text.split("\n")

    # Rule 1: every ``///`` description must be followed by another ``///``
    # line or an object declaration. NEVER by a blank/other line.
    for i, line in enumerate(lines):
        if classify_line(line) != "description":
            continue
        # Find the next non-description line.
        j = i + 1
        while j < len(lines) and classify_line(lines[j]) == "description":
            j += 1
        test.assertLess(
            j,
            len(lines),
            f"{path}: trailing description with no following object on line {i + 1}",
        )
        next_kind = classify_line(lines[j])
        test.assertEqual(
            next_kind,
            "object",
            f"{path}: '///' on line {i + 1} must be immediately followed by an "
            f"object declaration, got {next_kind!r}: {lines[j]!r}",
        )

    # Rule 2: indentation must use TABs, never spaces (Fabric is strict).
    for i, line in enumerate(lines):
        if line and line[0] == " ":
            test.fail(f"{path}: line {i + 1} starts with a space, not a tab: {line!r}")

    # Rule 3: must NOT contain ``ref table`` lines (Fabric rejects them).
    for i, line in enumerate(lines):
        if re.match(r"^\s*ref\s+table\b", line):
            test.fail(f"{path}: forbidden 'ref table' on line {i + 1}: {line!r}")

    # Rule 4: must NOT contain ``ref cultureInfo`` (caused an earlier bug).
    for i, line in enumerate(lines):
        if re.match(r"^\s*ref\s+cultureInfo\b", line):
            test.fail(
                f"{path}: forbidden 'ref cultureInfo' on line {i + 1}: {line!r}"
            )


# ===========================================================================
# Tests
# ===========================================================================


class DefinitionPayloadShapeTests(unittest.TestCase):
    """Validate the Fabric REST ``SemanticModelDefinition`` envelope."""

    def setUp(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv.fabric",
            source_database="db",
        )
        self.tmdl = build_definition(spec, DefinitionFormat.TMDL)
        self.tmsl = build_definition(spec, DefinitionFormat.TMSL)

    def test_payload_has_format_and_parts(self):
        for definition in (self.tmdl, self.tmsl):
            payload = definition.definition_payload()
            self.assertIn("format", payload)
            self.assertIn("parts", payload)
            self.assertIn(payload["format"], ("TMDL", "TMSL"))
            self.assertIsInstance(payload["parts"], list)
            self.assertGreater(len(payload["parts"]), 0)

    def test_every_part_has_required_keys(self):
        for part in self.tmdl.definition_payload()["parts"]:
            self.assertEqual(set(part.keys()), {"path", "payload", "payloadType"})
            self.assertEqual(part["payloadType"], "InlineBase64")
            self.assertIsInstance(part["path"], str)
            self.assertTrue(part["path"])

    def test_payloads_are_valid_base64_and_round_trip(self):
        for part in self.tmdl.definition_payload()["parts"]:
            decoded = base64.b64decode(part["payload"]).decode("utf-8")
            self.assertEqual(decoded, self.tmdl.files[part["path"]])

    def test_tmdl_includes_required_files(self):
        paths = set(self.tmdl.files)
        self.assertIn("definition.pbism", paths)
        self.assertIn("definition/database.tmdl", paths)
        self.assertIn("definition/model.tmdl", paths)
        self.assertIn("definition/relationships.tmdl", paths)
        self.assertTrue(any(p.startswith("definition/tables/") for p in paths))
        # TMDL and TMSL are mutually exclusive.
        self.assertNotIn("model.bim", paths)

    def test_tmsl_includes_required_files(self):
        paths = set(self.tmsl.files)
        self.assertIn("definition.pbism", paths)
        self.assertIn("model.bim", paths)
        # TMSL must not also ship a TMDL ``definition/`` folder.
        self.assertFalse(any(p.startswith("definition/") for p in paths))

    def test_definition_pbism_is_valid(self):
        data = json.loads(self.tmdl.files["definition.pbism"])
        self.assertIn("$schema", data)
        self.assertEqual(data["version"], "5.0")
        self.assertIn("settings", data)


class TmdlStructuralTests(unittest.TestCase):
    """Apply the generic TMDL well-formedness rules to every file."""

    def test_default_star_schema(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        definition = build_definition(spec)
        for path, text in definition.files.items():
            if path.endswith(".tmdl"):
                _assert_tmdl_well_formed(self, path, text)

    def test_tricky_names(self):
        spec = spec_from_schemas(
            tricky_names_tables(),
            model_name="Tricky",
            source_server="srv",
            source_database="db",
        )
        definition = build_definition(spec)
        for path, text in definition.files.items():
            if path.endswith(".tmdl"):
                _assert_tmdl_well_formed(self, path, text)

    def test_with_descriptions_everywhere(self):
        """Regression: descriptions must precede their object, never follow it."""
        spec = SemanticModelSpec(
            name="Annotated",
            description="Top-level model description.\nSecond line.",
            source_server="srv",
            source_database="db",
            tables=[
                SemanticTable(
                    name="Sales",
                    source_schema="dbo",
                    source_table="Sales",
                    description="Fact table for sales orders.",
                    columns=[
                        SemanticColumn(
                            name="Amount",
                            source_column="Amount",
                            data_type="decimal",
                            summarize_by="sum",
                            description="Order line amount in USD.",
                        )
                    ],
                    measures=[
                        SemanticMeasure(
                            name="Total Sales",
                            expression="SUM('Sales'[Amount])",
                            description="Sum of all sales.",
                            format_string="\\$#,0.00",
                        )
                    ],
                )
            ],
        )
        definition = build_definition(spec)
        for path, text in definition.files.items():
            if path.endswith(".tmdl"):
                _assert_tmdl_well_formed(self, path, text)

    def test_empty_relationships_file_is_safe(self):
        """An empty relationships.tmdl must not contain a stray ``///`` line."""
        spec = spec_from_schemas(
            [star_schema_tables()[0]],  # one table, no FKs
            model_name="OneTable",
            source_server="srv",
            source_database="db",
        )
        definition = build_definition(spec)
        rel_text = definition.files["definition/relationships.tmdl"]
        # Either truly empty or contains only object declarations.
        for line in rel_text.split("\n"):
            self.assertNotEqual(
                classify_line(line),
                "description",
                f"empty relationships.tmdl must contain no '///' lines: {line!r}",
            )


class TmdlContentTests(unittest.TestCase):
    """Content-level checks per file."""

    def setUp(self):
        self.spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv.fabric",
            source_database="db",
        )
        self.definition = build_definition(self.spec)

    def test_database_compatibility_level_is_supported(self):
        text = self.definition.files["definition/database.tmdl"]
        match = re.search(r"compatibilityLevel:\s*(\d+)", text)
        self.assertIsNotNone(match, "database.tmdl must set compatibilityLevel")
        level = int(match.group(1))
        self.assertGreaterEqual(level, 1567, "compatibilityLevel below Power BI minimum")

    def test_model_tmdl_has_no_ref_lines(self):
        text = self.definition.files["definition/model.tmdl"]
        self.assertNotIn("ref table", text)
        self.assertNotIn("ref cultureInfo", text)

    def test_model_tmdl_has_required_properties(self):
        text = self.definition.files["definition/model.tmdl"]
        self.assertRegex(text, r"^model\s+\w+", "model declaration missing")
        self.assertIn("culture:", text)
        self.assertIn("defaultPowerBIDataSourceVersion: powerBI_V3", text)

    def test_table_files_have_partition_and_columns(self):
        for path, text in self.definition.files.items():
            if not path.startswith("definition/tables/"):
                continue
            self.assertRegex(text, r"(?m)^table\s+", f"{path}: missing 'table' header")
            self.assertIn("partition ", text, f"{path}: missing partition")
            self.assertRegex(
                text,
                r"mode:\s+(import|directQuery|directLake)",
                f"{path}: invalid mode",
            )
            self.assertIn("source", text, f"{path}: missing partition source")
            self.assertRegex(text, r"\tcolumn\s+", f"{path}: no columns")

    def test_keys_are_hidden_and_not_summarized(self):
        text = self.definition.files["definition/tables/fact_sales.tmdl"]
        # PK columns must have ``isHidden`` and ``summarizeBy: none``.
        for key in ("SalesKey", "ProductKey", "DateKey"):
            block = _extract_column_block(text, key)
            self.assertIn("isHidden", block, f"key {key} should be hidden")
            self.assertIn("summarizeBy: none", block, f"key {key} should not summarize")

    def test_numeric_non_key_defaults_to_sum(self):
        text = self.definition.files["definition/tables/fact_sales.tmdl"]
        amount = _extract_column_block(text, "Amount")
        self.assertIn("summarizeBy: sum", amount)

    def test_partition_source_uses_correct_server_and_database(self):
        text = self.definition.files["definition/tables/fact_sales.tmdl"]
        self.assertIn('Sql.Database("srv.fabric", "db")', text)
        self.assertIn('[Schema="dbo", Item="fact_sales"]', text)

    def test_all_column_data_types_valid(self):
        valid = {"string", "int64", "decimal", "double", "dateTime", "boolean", "binary"}
        for path, text in self.definition.files.items():
            if not path.startswith("definition/tables/"):
                continue
            for match in re.finditer(r"dataType:\s+(\w+)", text):
                self.assertIn(
                    match.group(1),
                    valid,
                    f"{path}: invalid dataType {match.group(1)!r}",
                )

    def test_relationships_have_required_fields(self):
        text = self.definition.files["definition/relationships.tmdl"]
        rel_blocks = re.findall(
            r"relationship\s+\S+(?:\n[^\n]+)+", text, flags=re.MULTILINE
        )
        self.assertGreater(len(rel_blocks), 0)
        for block in rel_blocks:
            self.assertRegex(block, r"fromColumn:\s+\S+\.\S+")
            self.assertRegex(block, r"toColumn:\s+\S+\.\S+")


class TmslContentTests(unittest.TestCase):
    """Checks for the TMSL ``model.bim`` output."""

    def setUp(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        self.definition = build_definition(spec, DefinitionFormat.TMSL)

    def test_model_bim_is_valid_json(self):
        data = json.loads(self.definition.files["model.bim"])
        self.assertIn("compatibilityLevel", data)
        self.assertIn("model", data)
        self.assertIn("tables", data["model"])
        self.assertGreater(len(data["model"]["tables"]), 0)

    def test_each_tmsl_table_has_partition_with_m_expression_array(self):
        data = json.loads(self.definition.files["model.bim"])
        for table in data["model"]["tables"]:
            self.assertIn("partitions", table)
            partition = table["partitions"][0]
            self.assertIn("source", partition)
            self.assertEqual(partition["source"]["type"], "m")
            # Per doc, ``expression`` in TMSL is a string array.
            self.assertIsInstance(partition["source"]["expression"], list)
            self.assertTrue(
                all(isinstance(line, str) for line in partition["source"]["expression"])
            )

    def test_tmsl_relationships_present(self):
        data = json.loads(self.definition.files["model.bim"])
        self.assertIn("relationships", data["model"])


class DirectLakeTests(unittest.TestCase):
    """Direct Lake (on SQL) rendering and round-trip behaviour."""

    def setUp(self):
        self.spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv.fabric",
            source_database="db",
            storage_mode="directLake",
        )

    def test_spec_stores_direct_lake_storage_mode(self):
        self.assertEqual(self.spec.storage_mode, "directLake")

    def test_tmdl_partition_uses_entity_and_direct_lake_mode(self):
        definition = build_definition(self.spec)
        text = definition.files["definition/tables/fact_sales.tmdl"]
        self.assertIn("partition fact_sales = entity", text)
        self.assertIn("mode: directLake", text)
        self.assertIn("entityName: fact_sales", text)
        self.assertIn("schemaName: dbo", text)
        self.assertIn("expressionSource: DatabaseQuery", text)
        # Direct Lake partitions never carry inline M source.
        self.assertNotIn("Sql.Database", text)

    def test_tmdl_emits_shared_expression_with_connection(self):
        definition = build_definition(self.spec)
        self.assertIn("definition/expressions.tmdl", definition.files)
        expr = definition.files["definition/expressions.tmdl"]
        self.assertIn("expression DatabaseQuery =", expr)
        self.assertIn('Sql.Database("srv.fabric", "db")', expr)

    def test_non_direct_lake_omits_expressions_file(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
            storage_mode="directQuery",
        )
        definition = build_definition(spec)
        self.assertNotIn("definition/expressions.tmdl", definition.files)

    def test_tmsl_partition_uses_entity_source_and_expressions(self):
        definition = build_definition(self.spec, DefinitionFormat.TMSL)
        data = json.loads(definition.files["model.bim"])
        table = next(t for t in data["model"]["tables"] if t["name"] == "fact_sales")
        partition = table["partitions"][0]
        self.assertEqual(partition["mode"], "directLake")
        self.assertEqual(partition["source"]["type"], "entity")
        self.assertEqual(partition["source"]["entityName"], "fact_sales")
        self.assertEqual(partition["source"]["schemaName"], "dbo")
        self.assertEqual(partition["source"]["expressionSource"], "DatabaseQuery")
        expressions = data["model"]["expressions"]
        self.assertEqual(expressions[0]["name"], "DatabaseQuery")
        self.assertTrue(
            any("Sql.Database" in line for line in expressions[0]["expression"])
        )

    def test_tmdl_round_trip_preserves_direct_lake(self):
        definition = build_definition(self.spec)
        parsed = parse_semantic_model(definition.files, name="SalesModel")
        self.assertEqual(parsed.storage_mode, "directLake")
        self.assertEqual(parsed.source_server, "srv.fabric")
        self.assertEqual(parsed.source_database, "db")
        fact = parsed.table("fact_sales")
        self.assertIsNotNone(fact)
        self.assertEqual(fact.source_schema, "dbo")
        self.assertEqual(fact.source_table, "fact_sales")

    def test_tmsl_round_trip_preserves_direct_lake(self):
        definition = build_definition(self.spec, DefinitionFormat.TMSL)
        parsed = parse_semantic_model(definition.files, name="SalesModel")
        self.assertEqual(parsed.storage_mode, "directLake")
        self.assertEqual(parsed.source_server, "srv.fabric")
        self.assertEqual(parsed.source_database, "db")


class DirectLakeBinaryColumnTests(unittest.TestCase):
    """Binary columns must be stripped from Direct Lake models (not feasible).

    Fabric rejects a Direct Lake dataset import with "Column '<x>' with binary
    data type is not allowed in Direct Lake table", so the render path drops
    them proactively and the preflight validator warns about them.
    """

    def _spec(self, storage_mode):
        return SemanticModelSpec(
            name="WithPhoto",
            storage_mode=storage_mode,
            source_server="srv.fabric",
            source_database="db",
            tables=[
                SemanticTable(
                    name="dimension_employee",
                    source_schema="dbo",
                    source_table="dimension_employee",
                    columns=[
                        SemanticColumn(name="EmployeeKey", source_column="EmployeeKey",
                                       data_type="int64", is_key=True),
                        SemanticColumn(name="Name", source_column="Name",
                                       data_type="string"),
                        SemanticColumn(name="Photo", source_column="Photo",
                                       data_type="binary"),
                    ],
                )
            ],
        )

    def test_identifies_binary_column_under_direct_lake(self):
        bad = direct_lake_incompatible_columns(self._spec("directLake"))
        self.assertEqual(bad, [("dimension_employee.Photo", "binary")])

    def test_no_op_for_import_storage(self):
        spec = self._spec("import")
        self.assertEqual(direct_lake_incompatible_columns(spec), [])
        sanitized, dropped = drop_direct_lake_incompatible_columns(spec)
        self.assertEqual(dropped, [])
        self.assertIs(sanitized, spec)
        # The binary column survives for import models.
        self.assertIsNotNone(sanitized.table("dimension_employee").column("Photo"))

    def test_drop_removes_only_binary_column(self):
        spec = self._spec("directLake")
        sanitized, dropped = drop_direct_lake_incompatible_columns(spec)
        self.assertEqual(dropped, [("dimension_employee.Photo", "binary")])
        table = sanitized.table("dimension_employee")
        self.assertIsNone(table.column("Photo"))
        self.assertIsNotNone(table.column("Name"))
        self.assertIsNotNone(table.column("EmployeeKey"))
        # Original spec is left untouched (non-mutating).
        self.assertIsNotNone(spec.table("dimension_employee").column("Photo"))

    def test_build_definition_strips_binary_column(self):
        definition = build_definition(self._spec("directLake"))
        text = definition.files["definition/tables/dimension_employee.tmdl"]
        self.assertNotIn("Photo", text)
        self.assertIn("column Name", text)

    def test_validation_warns_about_binary_column(self):
        result = validate_semantic_model_spec(self._spec("directLake"))
        codes = {i.code for i in result.warnings}
        self.assertIn("direct-lake-unsupported-column-type", codes)


class DirectLakeOneLakeTests(unittest.TestCase):
    """Direct Lake on OneLake (lakehouse-bound) rendering and round-trip."""

    def setUp(self):
        self.spec = spec_from_schemas(
            star_schema_tables(),
            model_name="LakehouseModel",
            source_server="srv.fabric",
            source_database="DemoLakehouse",
            storage_mode="directLake",
            source_kind="lakehouse",
            lakehouse_id="abc-123",
            lakehouse_name="DemoLakehouse",
            onelake_workspace_id="ws-789",
            onelake_tables_path=(
                "https://onelake.dfs.fabric.microsoft.com/ws-789/abc-123/Tables"
            ),
            default_schema="dbo",
        )

    def test_spec_records_lakehouse_binding(self):
        self.assertEqual(self.spec.source_kind, "lakehouse")
        self.assertEqual(self.spec.lakehouse_id, "abc-123")
        self.assertEqual(self.spec.onelake_workspace_id, "ws-789")

    def test_spec_round_trip_via_dict(self):
        restored = SemanticModelSpec.from_dict(self.spec.to_dict())
        self.assertEqual(restored.source_kind, "lakehouse")
        self.assertEqual(restored.lakehouse_id, "abc-123")
        self.assertEqual(restored.lakehouse_name, "DemoLakehouse")
        self.assertEqual(restored.onelake_workspace_id, "ws-789")
        self.assertEqual(
            restored.onelake_tables_path,
            "https://onelake.dfs.fabric.microsoft.com/ws-789/abc-123/Tables",
        )
        self.assertEqual(restored.default_schema, "dbo")

    def test_tmdl_expression_binds_to_onelake(self):
        definition = build_definition(self.spec)
        self.assertIn("definition/expressions.tmdl", definition.files)
        expr = definition.files["definition/expressions.tmdl"]
        self.assertIn("expression DatabaseQuery =", expr)
        self.assertIn("AzureStorage.DataLake", expr)
        self.assertIn(
            "https://onelake.dfs.fabric.microsoft.com/ws-789/abc-123",
            expr,
        )
        self.assertIn("HierarchicalNavigation=true", expr)
        # Direct Lake on OneLake must not use the SQL connector.
        self.assertNotIn("Sql.Database", expr)

    def test_tmdl_partitions_keep_entity_mode(self):
        definition = build_definition(self.spec)
        text = definition.files["definition/tables/fact_sales.tmdl"]
        self.assertIn("partition fact_sales = entity", text)
        self.assertIn("mode: directLake", text)
        self.assertIn("expressionSource: DatabaseQuery", text)

    def test_onelake_root_derived_from_tables_path(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="LakehouseModel",
            source_server="srv.fabric",
            source_database="DemoLakehouse",
            storage_mode="directLake",
            source_kind="lakehouse",
            onelake_tables_path=(
                "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-2/Tables"
            ),
        )
        definition = build_definition(spec)
        expr = definition.files["definition/expressions.tmdl"]
        self.assertIn(
            "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-2",
            expr,
        )
        self.assertNotIn("/Tables", expr)

    def test_tmsl_expression_binds_to_onelake(self):
        definition = build_definition(self.spec, DefinitionFormat.TMSL)
        data = json.loads(definition.files["model.bim"])
        expressions = data["model"]["expressions"]
        self.assertEqual(expressions[0]["name"], "DatabaseQuery")
        self.assertTrue(
            any("AzureStorage.DataLake" in line for line in expressions[0]["expression"])
        )
        self.assertFalse(
            any("Sql.Database" in line for line in expressions[0]["expression"])
        )


class DirectLakeModeToggleTests(unittest.TestCase):
    """The user-facing Direct Lake type toggle (OneLake vs SQL)."""

    def _onelake_binding(self, **overrides: Any):
        params = dict(
            model_name="LakehouseModel",
            source_server="srv.fabric",
            source_database="DemoLakehouse",
            storage_mode="directLake",
            source_kind="lakehouse",
            lakehouse_id="abc-123",
            lakehouse_name="DemoLakehouse",
            onelake_workspace_id="ws-789",
            onelake_tables_path=(
                "https://onelake.dfs.fabric.microsoft.com/ws-789/abc-123/Tables"
            ),
            default_schema="dbo",
        )
        params.update(overrides)
        return spec_from_schemas(star_schema_tables(), **params)

    def test_auto_prefers_onelake_when_bound(self):
        spec = self._onelake_binding(direct_lake_mode="auto")
        self.assertEqual(spec.resolved_direct_lake_mode(), "onelake")

    def test_auto_falls_back_to_sql_without_binding(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SqlModel",
            source_server="srv.fabric",
            source_database="db",
            storage_mode="directLake",
            source_kind="sql",
            direct_lake_mode="auto",
        )
        self.assertEqual(spec.resolved_direct_lake_mode(), "sql")

    def test_explicit_sql_overrides_onelake_binding(self):
        spec = self._onelake_binding(direct_lake_mode="sql")
        self.assertEqual(spec.resolved_direct_lake_mode(), "sql")
        expr = build_definition(spec).files["definition/expressions.tmdl"]
        self.assertIn('Sql.Database("srv.fabric", "DemoLakehouse")', expr)
        self.assertNotIn("AzureStorage.DataLake", expr)

    def test_explicit_onelake_renders_datalake(self):
        spec = self._onelake_binding(direct_lake_mode="onelake")
        self.assertEqual(spec.resolved_direct_lake_mode(), "onelake")
        expr = build_definition(spec).files["definition/expressions.tmdl"]
        self.assertIn("AzureStorage.DataLake", expr)
        self.assertNotIn("Sql.Database", expr)

    def test_direct_lake_mode_round_trips_via_dict(self):
        spec = self._onelake_binding(direct_lake_mode="sql")
        restored = SemanticModelSpec.from_dict(spec.to_dict())
        self.assertEqual(restored.direct_lake_mode, "sql")

    def test_direct_lake_mode_omitted_for_non_direct_lake(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="ImportModel",
            source_server="srv",
            source_database="db",
            storage_mode="import",
            direct_lake_mode="onelake",
        )
        self.assertNotIn("direct_lake_mode", spec.to_dict())

    def test_validation_flags_invalid_mode(self):
        spec = self._onelake_binding(direct_lake_mode="bogus")
        codes = [i.code for i in validate_semantic_model_spec(spec).errors]
        self.assertIn("invalid-direct-lake-mode", codes)

    def test_validation_flags_onelake_without_binding(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SqlModel",
            source_server="srv.fabric",
            source_database="db",
            storage_mode="directLake",
            source_kind="sql",
            direct_lake_mode="onelake",
        )
        codes = [i.code for i in validate_semantic_model_spec(spec).errors]
        self.assertIn("direct-lake-onelake-binding-missing", codes)

    def test_validation_passes_sql_without_binding(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SqlModel",
            source_server="srv.fabric",
            source_database="db",
            storage_mode="directLake",
            source_kind="sql",
            direct_lake_mode="sql",
        )
        codes = [i.code for i in validate_semantic_model_spec(spec).errors]
        self.assertNotIn("direct-lake-onelake-binding-missing", codes)
        self.assertNotIn("invalid-direct-lake-mode", codes)


class SpecMappingTests(unittest.TestCase):
    """Tests on the deterministic SQL→IR mapping."""

    def test_pk_and_fk_columns_become_hidden(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        fact = spec.table("fact_sales")
        self.assertIsNotNone(fact)
        for key in ("SalesKey", "ProductKey", "DateKey"):
            col = fact.column(key)
            self.assertTrue(col.is_hidden, f"{key} should be hidden")
            self.assertEqual(col.summarize_by, "none")

    def test_tables_and_columns_have_descriptions(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        for table in spec.tables:
            self.assertTrue(
                table.description, f"{table.name} should have a description"
            )
            for col in table.columns:
                self.assertTrue(
                    col.description,
                    f"{table.name}.{col.name} should have a description",
                )

    def test_pk_and_fk_descriptions_are_self_documenting(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        fact = spec.table("fact_sales")
        self.assertIn("Primary key", fact.column("SalesKey").description)
        # FK descriptions should name the table they reference.
        self.assertIn("Foreign key", fact.column("ProductKey").description)
        self.assertIn("dimension_product", fact.column("ProductKey").description)

    def test_relationships_built_from_foreign_keys(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        self.assertEqual(len(spec.relationships), 2)
        froms = sorted((r.from_table, r.from_column) for r in spec.relationships)
        self.assertEqual(
            froms, [("fact_sales", "DateKey"), ("fact_sales", "ProductKey")]
        )

    def test_round_trip_to_dict_from_dict(self):
        original = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        clone = SemanticModelSpec.from_dict(original.to_dict())
        self.assertEqual(clone.name, original.name)
        self.assertEqual(len(clone.tables), len(original.tables))
        self.assertEqual(len(clone.relationships), len(original.relationships))
        for o_tbl, c_tbl in zip(original.tables, clone.tables):
            self.assertEqual(
                [c.name for c in o_tbl.columns], [c.name for c in c_tbl.columns]
            )


class RelationshipInferenceTests(unittest.TestCase):
    """Relationships must be inferred when explicit FKs are missing."""

    def _star_without_fks(self):
        """Same star schema as ``star_schema_tables`` but with no FK metadata."""
        tables = star_schema_tables()
        for t in tables:
            t.foreign_keys = []
        return tables

    def test_exact_pk_name_match_creates_relationships(self):
        spec = spec_from_schemas(
            self._star_without_fks(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        edges = sorted(
            (r.from_table, r.from_column, r.to_table, r.to_column)
            for r in spec.relationships
        )
        self.assertEqual(
            edges,
            [
                ("fact_sales", "DateKey", "dimension_date", "DateKey"),
                ("fact_sales", "ProductKey", "dimension_product", "ProductKey"),
            ],
        )
        for rel in spec.relationships:
            self.assertEqual(rel.from_cardinality, "many")
            self.assertEqual(rel.to_cardinality, "one")
            self.assertTrue(rel.is_active)

    def test_inference_excludes_unselected_tables(self):
        # Only the fact table is selected — no dim tables → no relationships.
        tables = self._star_without_fks()
        spec = spec_from_schemas(
            [tables[-1]],  # fact_sales only
            model_name="SalesOnly",
            source_server="srv",
            source_database="db",
        )
        self.assertEqual(spec.relationships, [])

    def test_inference_skips_when_pk_types_differ(self):
        # Customer.CustomerKey is string but order's CustomerKey is int → reject.
        customer = _Table(
            "dbo",
            "Customer",
            [
                _Col("CustomerKey", "nvarchar", pk=True),
                _Col("Name", "nvarchar"),
            ],
        )
        order = _Table(
            "dbo",
            "Order",
            [
                _Col("OrderId", "int", pk=True),
                _Col("CustomerKey", "int"),  # type mismatch
            ],
        )
        spec = spec_from_schemas(
            [customer, order],
            model_name="M",
            source_server="srv",
            source_database="db",
        )
        self.assertEqual(spec.relationships, [])

    def test_orm_suffix_convention_infers_relationship(self):
        # `Customer` table with PK `Id`; `Order.CustomerId` should link to it.
        customer = _Table(
            "dbo",
            "Customer",
            [
                _Col("Id", "int", pk=True),
                _Col("Name", "nvarchar"),
            ],
        )
        order = _Table(
            "dbo",
            "Order",
            [
                _Col("Id", "int", pk=True),
                _Col("CustomerId", "int"),
            ],
        )
        spec = spec_from_schemas(
            [customer, order],
            model_name="M",
            source_server="srv",
            source_database="db",
        )
        self.assertEqual(len(spec.relationships), 1)
        rel = spec.relationships[0]
        self.assertEqual(
            (rel.from_table, rel.from_column, rel.to_table, rel.to_column),
            ("Order", "CustomerId", "Customer", "Id"),
        )

    def test_dimensional_prefix_is_normalised(self):
        # `dimension_customer` should match column `customer_id`.
        dim = _Table(
            "dbo",
            "dimension_customer",
            [
                _Col("id", "int", pk=True),
                _Col("name", "nvarchar"),
            ],
        )
        fact = _Table(
            "dbo",
            "fact_orders",
            [
                _Col("order_id", "int", pk=True),
                _Col("customer_id", "int"),
            ],
        )
        spec = spec_from_schemas(
            [dim, fact],
            model_name="M",
            source_server="srv",
            source_database="db",
        )
        self.assertEqual(len(spec.relationships), 1)
        rel = spec.relationships[0]
        self.assertEqual(rel.from_table, "fact_orders")
        self.assertEqual(rel.from_column, "customer_id")
        self.assertEqual(rel.to_table, "dimension_customer")
        self.assertEqual(rel.to_column, "id")

    def test_explicit_fk_takes_precedence_no_duplicate(self):
        # Explicit FK and inference would both produce the same edge.
        # The result must contain exactly one relationship per edge.
        spec = spec_from_schemas(
            star_schema_tables(),  # has FKs
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        edges = [
            (r.from_table, r.from_column, r.to_table, r.to_column)
            for r in spec.relationships
        ]
        self.assertEqual(sorted(edges), sorted(set(edges)))
        self.assertEqual(len(edges), 2)

    def test_inference_renders_into_relationships_tmdl(self):
        # End-to-end: inferred relationships must show up in the TMDL output.
        spec = spec_from_schemas(
            self._star_without_fks(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        definition = build_definition(spec)
        rel_text = definition.files["definition/relationships.tmdl"]
        self.assertIn("fromColumn: fact_sales.ProductKey", rel_text)
        self.assertIn("toColumn: dimension_product.ProductKey", rel_text)
        self.assertIn("fromColumn: fact_sales.DateKey", rel_text)
        self.assertIn("toColumn: dimension_date.DateKey", rel_text)

    def test_only_one_active_relationship_per_pair(self):
        # Two FK columns between the same pair of tables → exactly one active.
        product = _Table(
            "dbo",
            "Product",
            [
                _Col("Id", "int", pk=True),
                _Col("Name", "nvarchar"),
            ],
        )
        sales = _Table(
            "dbo",
            "Sales",
            [
                _Col("Id", "int", pk=True),
                _Col("ProductId", "int"),
                _Col("RelatedProductId", "int"),
            ],
            fks=[
                _FK("ProductId", "dbo", "Product", "Id"),
                _FK("RelatedProductId", "dbo", "Product", "Id"),
            ],
        )
        spec = spec_from_schemas(
            [product, sales],
            model_name="M",
            source_server="srv",
            source_database="db",
        )
        rels = [r for r in spec.relationships if r.to_table == "Product"]
        self.assertEqual(len(rels), 2)
        active = [r for r in rels if r.is_active]
        self.assertEqual(len(active), 1, "exactly one active relationship per pair")


# ---------------------------------------------------------------------------
# Suggestion engine — relationships and measures
# ---------------------------------------------------------------------------


def _star_schema_without_fks():
    """Star schema with FK metadata removed (Lakehouse-style)."""
    tables = star_schema_tables()
    for t in tables:
        t.foreign_keys = []
    return tables


class SuggestRelationshipsTests(unittest.TestCase):

    def test_explicit_foreign_keys_become_fk_suggestions(self):
        bundle = suggest_from_schemas(star_schema_tables())
        sources = {s.source for s in bundle.relationships}
        self.assertIn("fk", sources)
        for s in bundle.relationships:
            if s.source == "fk":
                self.assertGreaterEqual(s.confidence, 0.99)
                self.assertTrue(s.rationale, "FK suggestion must have a rationale")

    def test_inference_when_no_fks_present(self):
        bundle = suggest_from_schemas(_star_schema_without_fks())
        # Sales has two key columns (ProductKey, DateKey) that match dim PKs.
        edges = sorted(
            (s.relationship.from_table, s.relationship.from_column,
             s.relationship.to_table, s.relationship.to_column)
            for s in bundle.relationships
        )
        self.assertIn(
            ("fact_sales", "ProductKey", "dimension_product", "ProductKey"), edges
        )
        self.assertIn(
            ("fact_sales", "DateKey", "dimension_date", "DateKey"), edges
        )
        for s in bundle.relationships:
            self.assertIn(s.source, ("pk-match", "suffix"))

    def test_suffix_convention_tagged_as_suffix(self):
        customer = _Table(
            "dbo",
            "Customer",
            [_Col("Id", "int", pk=True), _Col("Name", "nvarchar")],
        )
        order = _Table(
            "dbo",
            "Order",
            [_Col("Id", "int", pk=True), _Col("CustomerId", "int")],
        )
        bundle = suggest_from_schemas([customer, order])
        self.assertEqual(len(bundle.relationships), 1)
        s = bundle.relationships[0]
        self.assertEqual(s.source, "suffix")
        self.assertGreater(s.confidence, 0.5)

    def test_existing_relationships_are_filtered_out(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        # Spec already has FK-derived relationships; further suggestions for
        # the same edges must NOT appear.
        bundle = suggest_from_schemas(star_schema_tables(), spec=spec)
        for s in bundle.relationships:
            edge = (
                s.relationship.from_table,
                s.relationship.from_column,
                s.relationship.to_table,
                s.relationship.to_column,
            )
            for r in spec.relationships:
                self.assertNotEqual(
                    edge, (r.from_table, r.from_column, r.to_table, r.to_column)
                )

    def test_suggestions_have_stable_keys(self):
        bundle = suggest_from_schemas(_star_schema_without_fks())
        keys = [s.key for s in bundle.relationships]
        self.assertEqual(len(keys), len(set(keys)), "suggestion keys must be unique")
        for s in bundle.relationships:
            self.assertIn("->", s.key)


class SuggestMeasuresTests(unittest.TestCase):

    def setUp(self):
        self.bundle = suggest_from_schemas(star_schema_tables())

    def test_each_table_gets_a_row_count_measure(self):
        table_names = {t.name for t in star_schema_tables()}
        row_count_tables = {
            s.table
            for s in self.bundle.measures
            if "Row Count" in s.measure.name
        }
        self.assertEqual(table_names, row_count_tables)

    def test_numeric_non_key_columns_get_sum_measure(self):
        # ``Amount`` and ``Quantity`` on fact_sales are aggregatable.
        names = {
            (s.table, s.measure.name)
            for s in self.bundle.measures
            if s.table == "fact_sales"
        }
        self.assertIn(("fact_sales", "Total Amount"), names)
        self.assertIn(("fact_sales", "Total Quantity"), names)

    def test_key_columns_are_not_aggregated(self):
        for s in self.bundle.measures:
            # No suggestion should aggregate a key column like ``SalesKey``.
            self.assertNotIn("SalesKey", s.measure.name)
            self.assertNotIn("ProductKey", s.measure.name)
            self.assertNotIn("DateKey", s.measure.name)

    def test_decimal_columns_also_get_average(self):
        # ``Amount`` maps to ``decimal`` so AVERAGE is offered.
        names = {s.measure.name for s in self.bundle.measures if s.table == "fact_sales"}
        self.assertIn("Average Amount", names)

    def test_measure_expression_uses_friendly_names(self):
        for s in self.bundle.measures:
            if s.measure.name.startswith("Total "):
                self.assertIn(f"'{s.table}'[", s.measure.expression)
                self.assertTrue(s.measure.expression.startswith("SUM("))
            elif s.measure.name.startswith("Average "):
                self.assertTrue(s.measure.expression.startswith("AVERAGE("))
            elif "Row Count" in s.measure.name:
                self.assertEqual(s.measure.expression, f"COUNTROWS('{s.table}')")

    def test_amount_column_uses_currency_format(self):
        amount = next(
            s for s in self.bundle.measures
            if s.table == "fact_sales" and s.measure.name == "Total Amount"
        )
        self.assertEqual(amount.measure.format_string, "\\$#,0.00")

    def test_existing_measures_are_filtered_out(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        fact = spec.table("fact_sales")
        fact.measures.append(
            SemanticMeasure(name="Total Amount", expression="SUM('fact_sales'[Amount])")
        )
        bundle = suggest_from_schemas(star_schema_tables(), spec=spec)
        self.assertFalse(
            any(
                s.table == "fact_sales" and s.measure.name == "Total Amount"
                for s in bundle.measures
            )
        )


class ApplySuggestionsTests(unittest.TestCase):

    def setUp(self):
        self.spec = spec_from_schemas(
            _star_schema_without_fks(),
            model_name="SalesModel",
            source_server="srv.fabric",
            source_database="db",
        )
        self.bundle = suggest_from_schemas(
            _star_schema_without_fks(), spec=self.spec
        )

    def test_apply_is_noop_when_nothing_accepted(self):
        new_spec = apply_suggestions(self.spec)
        self.assertEqual(len(new_spec.relationships), len(self.spec.relationships))
        self.assertEqual(
            sum(len(t.measures) for t in new_spec.tables),
            sum(len(t.measures) for t in self.spec.tables),
        )

    def test_apply_does_not_mutate_input_spec(self):
        before_rels = list(self.spec.relationships)
        before_measures = [(t.name, len(t.measures)) for t in self.spec.tables]
        custom_rel = SuggestedRelationship(
            relationship=SemanticRelationship(
                from_table="fact_sales",
                from_column="Notes",
                to_table="dimension_product",
                to_column="ProductKey",
            ),
            rationale="custom",
        )
        apply_suggestions(self.spec, relationships=[custom_rel])
        self.assertEqual(self.spec.relationships, before_rels)
        self.assertEqual(
            [(t.name, len(t.measures)) for t in self.spec.tables],
            before_measures,
        )

    def test_apply_adds_only_new_relationships(self):
        # Start from a spec with no relationships so suggestions are non-empty.
        bare = SemanticModelSpec(
            name=self.spec.name,
            tables=list(self.spec.tables),
            relationships=[],
            source_server=self.spec.source_server,
            source_database=self.spec.source_database,
            storage_mode=self.spec.storage_mode,
        )
        bundle = suggest_from_schemas(_star_schema_without_fks(), spec=bare)
        self.assertTrue(bundle.relationships, "suggestion engine should propose edges")
        first = apply_suggestions(bare, relationships=[bundle.relationships[0]])
        second = apply_suggestions(first, relationships=[bundle.relationships[0]])
        # Second apply is a noop: the relationship is already in the spec.
        self.assertEqual(len(first.relationships), len(second.relationships))

    def test_apply_adds_measure_to_correct_table(self):
        sm = next(s for s in self.bundle.measures if s.table == "fact_sales")
        new_spec = apply_suggestions(self.spec, measures=[sm])
        fact = new_spec.table("fact_sales")
        self.assertTrue(any(m.name == sm.measure.name for m in fact.measures))

    def test_apply_skips_measure_for_unknown_table(self):
        ghost = SuggestedMeasure(
            table="DoesNotExist",
            measure=SemanticMeasure(name="X", expression="COUNTROWS('X')"),
        )
        new_spec = apply_suggestions(self.spec, measures=[ghost])
        # No table called DoesNotExist exists; the measure is silently dropped.
        self.assertEqual(
            sum(len(t.measures) for t in new_spec.tables),
            sum(len(t.measures) for t in self.spec.tables),
        )

    def test_active_relationships_per_pair_recomputed(self):
        # Manually craft two relationships between the same pair of tables and
        # accept both; only the first must remain active.
        dup_rels = [
            SuggestedRelationship(
                relationship=SemanticRelationship(
                    from_table="fact_sales",
                    from_column="ProductKey",
                    to_table="dimension_product",
                    to_column="ProductKey",
                )
            ),
            SuggestedRelationship(
                relationship=SemanticRelationship(
                    from_table="fact_sales",
                    from_column="DateKey",  # same fact->same dim shape
                    to_table="dimension_product",
                    to_column="ProductKey",
                )
            ),
        ]
        new_spec = apply_suggestions(self.spec, relationships=dup_rels)
        same_pair = [
            r for r in new_spec.relationships
            if r.from_table == "fact_sales" and r.to_table == "dimension_product"
        ]
        active = [r for r in same_pair if r.is_active]
        self.assertEqual(
            len(active), 1, "exactly one active relationship per (from, to) pair"
        )


class SuggestionsRenderingTests(unittest.TestCase):
    """Accepted suggestions must end up in the TMDL/TMSL output."""

    def setUp(self):
        self.spec = spec_from_schemas(
            _star_schema_without_fks(),
            model_name="SalesModel",
            source_server="srv.fabric",
            source_database="db",
        )
        self.bundle = suggest_from_schemas(
            _star_schema_without_fks(), spec=self.spec
        )

    def test_accepted_relationship_appears_in_tmdl(self):
        # Start from a single-table spec so there is no existing relationship,
        # then accept one inferred suggestion and check the rendered TMDL.
        tables = _star_schema_without_fks()
        # Spec only contains the fact + product dim so the suggestion is fresh.
        spec_two = spec_from_schemas(
            [tables[0], tables[2]],  # dim_product + fact_sales
            model_name="M",
            source_server="srv",
            source_database="db",
        )
        # Clear inferred relationships so we can re-accept them via the picker.
        spec_two.relationships.clear()
        bundle = suggest_from_schemas([tables[0], tables[2]], spec=spec_two)
        self.assertTrue(bundle.relationships, "need at least one suggestion")
        accepted = [bundle.relationships[0]]
        new_spec = apply_suggestions(spec_two, relationships=accepted)
        definition = build_definition(new_spec, DefinitionFormat.TMDL)
        rel_text = definition.files["definition/relationships.tmdl"]
        r = accepted[0].relationship
        self.assertIn(f"fromColumn: {r.from_table}.{r.from_column}", rel_text)
        self.assertIn(f"toColumn: {r.to_table}.{r.to_column}", rel_text)
        # And the TMDL must still be well-formed.
        for path, text in definition.files.items():
            if path.endswith(".tmdl"):
                _assert_tmdl_well_formed(self, path, text)

    def test_accepted_measure_appears_in_tmdl_table_file(self):
        sm = next(s for s in self.bundle.measures if s.table == "fact_sales")
        new_spec = apply_suggestions(self.spec, measures=[sm])
        definition = build_definition(new_spec, DefinitionFormat.TMDL)
        table_text = definition.files["definition/tables/fact_sales.tmdl"]
        self.assertIn(
            f"measure {sm.measure.name} = {sm.measure.expression}".replace(
                f"measure {sm.measure.name}", f"measure {sm.measure.name}"
            )[:7],
            table_text,
        )
        # More precise: the rendered measure line must contain the DAX.
        self.assertIn(sm.measure.expression, table_text)
        # And TMDL must still be valid.
        for path, text in definition.files.items():
            if path.endswith(".tmdl"):
                _assert_tmdl_well_formed(self, path, text)

    def test_accepted_measure_carries_format_string_into_tmdl(self):
        sm = next(
            s for s in self.bundle.measures
            if s.table == "fact_sales" and s.measure.name == "Total Amount"
        )
        new_spec = apply_suggestions(self.spec, measures=[sm])
        definition = build_definition(new_spec, DefinitionFormat.TMDL)
        table_text = definition.files["definition/tables/fact_sales.tmdl"]
        self.assertIn("formatString:", table_text)
        self.assertIn("Total Amount", table_text)

    def test_accepted_measure_appears_in_tmsl_model_bim(self):
        sm = next(s for s in self.bundle.measures if s.table == "fact_sales")
        new_spec = apply_suggestions(self.spec, measures=[sm])
        definition = build_definition(new_spec, DefinitionFormat.TMSL)
        data = json.loads(definition.files["model.bim"])
        fact = next(t for t in data["model"]["tables"] if t["name"] == "fact_sales")
        self.assertIn("measures", fact)
        names = {m["name"] for m in fact["measures"]}
        self.assertIn(sm.measure.name, names)

    def test_accepted_relationship_appears_in_tmsl_model_bim(self):
        tables = _star_schema_without_fks()
        spec_two = spec_from_schemas(
            [tables[0], tables[2]],
            model_name="M",
            source_server="srv",
            source_database="db",
        )
        spec_two.relationships.clear()
        bundle = suggest_from_schemas([tables[0], tables[2]], spec=spec_two)
        accepted = [bundle.relationships[0]]
        new_spec = apply_suggestions(spec_two, relationships=accepted)
        definition = build_definition(new_spec, DefinitionFormat.TMSL)
        data = json.loads(definition.files["model.bim"])
        self.assertEqual(len(data["model"]["relationships"]), 1)
        rel = data["model"]["relationships"][0]
        expected = accepted[0].relationship
        self.assertEqual(rel["fromTable"], expected.from_table)
        self.assertEqual(rel["fromColumn"], expected.from_column)
        self.assertEqual(rel["toTable"], expected.to_table)
        self.assertEqual(rel["toColumn"], expected.to_column)


class SuggestionsSerialisationTests(unittest.TestCase):
    """Suggestions must round-trip through JSON (agent input/output, session state)."""

    def test_bundle_round_trip(self):
        bundle = suggest_from_schemas(star_schema_tables())
        clone = SemanticModelSuggestions.from_dict(bundle.to_dict())
        self.assertEqual(len(clone.relationships), len(bundle.relationships))
        self.assertEqual(len(clone.measures), len(bundle.measures))
        for o, c in zip(bundle.relationships, clone.relationships):
            self.assertEqual(o.key, c.key)
            self.assertEqual(o.source, c.source)
            self.assertAlmostEqual(o.confidence, c.confidence)
        for o, c in zip(bundle.measures, clone.measures):
            self.assertEqual(o.key, c.key)
            self.assertEqual(o.measure.expression, c.measure.expression)

    def test_relationship_suggestion_from_dict_defaults(self):
        s = SuggestedRelationship.from_dict({
            "relationship": {
                "from_table": "A", "from_column": "x",
                "to_table": "B", "to_column": "y",
            }
        })
        self.assertEqual(s.relationship.from_cardinality, "many")
        self.assertEqual(s.relationship.to_cardinality, "one")
        self.assertEqual(s.source, "deterministic")
        self.assertEqual(s.confidence, 0.8)


class AgentSuggestionsParserTests(unittest.TestCase):
    """Smoke tests for the agent helpers — pure functions, no Foundry needed."""

    def test_backfill_descriptions_fills_missing_from_baseline(self):
        from app.intelligence.agent import _backfill_descriptions

        baseline = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        # Simulate an agent spec that dropped descriptions on a table/column.
        agent_spec = SemanticModelSpec.from_dict(baseline.to_dict())
        fact = agent_spec.table("fact_sales")
        fact.description = None
        fact.column("SalesKey").description = None
        # And an agent-supplied description that must be preserved.
        fact.column("ProductKey").description = "Agent-authored description."

        _backfill_descriptions(agent_spec, baseline)

        self.assertTrue(fact.description, "table description should be backfilled")
        self.assertTrue(
            fact.column("SalesKey").description,
            "column description should be backfilled",
        )
        self.assertEqual(
            fact.column("ProductKey").description,
            "Agent-authored description.",
            "agent-authored description must be preserved",
        )

    def test_parse_suggestions_handles_fenced_json(self):
        from app.intelligence.agent import _parse_suggestions

        text = (
            "Sure, here's the plan:\n```json\n"
            + json.dumps(
                {
                    "relationships": [
                        {
                            "relationship": {
                                "from_table": "Sales", "from_column": "ProductKey",
                                "to_table": "Product", "to_column": "ProductKey",
                            },
                            "rationale": "obvious",
                            "confidence": 0.99,
                        }
                    ],
                    "measures": [
                        {
                            "table": "Sales",
                            "measure": {"name": "Total Sales",
                                        "expression": "SUM('Sales'[Amount])"},
                            "rationale": "main fact",
                            "confidence": 0.9,
                        }
                    ],
                }
            )
            + "\n```\nLet me know!"
        )
        parsed = _parse_suggestions(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed.relationships), 1)
        self.assertEqual(len(parsed.measures), 1)
        # Source defaults to "agent" when missing.
        self.assertEqual(parsed.relationships[0].source, "agent")
        self.assertEqual(parsed.measures[0].source, "agent")

    def test_merge_prefers_deterministic_over_agent_on_collision(self):
        from app.intelligence.agent import _merge_suggestions

        det_rel = SuggestedRelationship(
            relationship=SemanticRelationship(
                from_table="A", from_column="x", to_table="B", to_column="y"
            ),
            source="fk",
            confidence=1.0,
        )
        agent_rel = SuggestedRelationship(
            relationship=SemanticRelationship(
                from_table="A", from_column="x", to_table="B", to_column="y"
            ),
            source="agent",
            confidence=0.6,
        )
        det = SemanticModelSuggestions(relationships=[det_rel])
        ai = SemanticModelSuggestions(relationships=[agent_rel])
        merged = _merge_suggestions(det, ai)
        self.assertEqual(len(merged.relationships), 1)
        self.assertEqual(merged.relationships[0].source, "fk")

    def test_merge_unions_distinct_items(self):
        from app.intelligence.agent import _merge_suggestions

        det = suggest_from_schemas(star_schema_tables())
        agent_rel = SuggestedRelationship(
            relationship=SemanticRelationship(
                from_table="fact_sales",
                from_column="Notes",
                to_table="dimension_product",
                to_column="ProductKey",
            ),
            source="agent",
            rationale="model felt creative",
            confidence=0.3,
        )
        ai = SemanticModelSuggestions(relationships=[agent_rel])
        merged = _merge_suggestions(det, ai)
        self.assertEqual(
            len(merged.relationships), len(det.relationships) + 1
        )


class SuggestionOutcomeTests(unittest.TestCase):
    """The diagnostics wrapper used to make the agent fallback visible."""

    def test_default_status_is_deterministic_with_no_contribution(self):
        from app.intelligence.agent import SuggestionOutcome

        outcome = SuggestionOutcome(suggestions=SemanticModelSuggestions())
        self.assertEqual(outcome.status, "deterministic")
        self.assertEqual(outcome.agent_contributed, 0)

    def test_agent_contributed_counts_agent_sourced_items(self):
        from app.intelligence.agent import SuggestionOutcome

        bundle = suggest_from_schemas(star_schema_tables())
        # All deterministic so far → no agent contribution.
        outcome = SuggestionOutcome(suggestions=bundle, status="ok")
        self.assertEqual(outcome.agent_contributed, 0)

        bundle.measures.append(
            SuggestedMeasure(
                table="fact_sales",
                measure=SemanticMeasure(
                    name="YoY Amount",
                    expression="CALCULATE(SUM('fact_sales'[Amount]))",
                ),
                source="agent",
                confidence=0.7,
            )
        )
        bundle.relationships.append(
            SuggestedRelationship(
                relationship=SemanticRelationship(
                    from_table="fact_sales",
                    from_column="Notes",
                    to_table="dimension_product",
                    to_column="ProductKey",
                ),
                source="agent",
                confidence=0.3,
            )
        )
        self.assertEqual(outcome.agent_contributed, 2)

    def test_error_status_carries_message(self):
        from app.intelligence.agent import SuggestionOutcome

        outcome = SuggestionOutcome(
            suggestions=SemanticModelSuggestions(),
            status="error",
            error="boom",
        )
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error, "boom")


class DanglingRelationshipTests(unittest.TestCase):
    """Relationships referencing unknown tables/columns must never reach Fabric.

    Regression for the Fabric 500 "Cannot resolve all the paths … Property
    FromColumn/ToColumn … refers to an object which cannot be found".
    """

    def _base_spec(self):
        return spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )

    def _bad_rel(self, **kwargs):
        defaults = dict(
            from_table="fact_sales",
            from_column="DoesNotExist",
            to_table="dimension_product",
            to_column="ProductKey",
        )
        defaults.update(kwargs)
        return SuggestedRelationship(
            relationship=SemanticRelationship(**defaults),
            source="agent",
            confidence=0.5,
        )

    def test_apply_drops_relationship_with_unknown_column(self):
        spec = self._base_spec()
        before = len(spec.relationships)
        merged = apply_suggestions(spec, relationships=[self._bad_rel()])
        # The hallucinated edge must not be added.
        self.assertEqual(len(merged.relationships), before)
        self.assertFalse(
            any(r.from_column == "DoesNotExist" for r in merged.relationships)
        )

    def test_apply_drops_relationship_with_unknown_table(self):
        spec = self._base_spec()
        before = len(spec.relationships)
        merged = apply_suggestions(
            spec,
            relationships=[self._bad_rel(from_table="ghost_table",
                                         from_column="X")],
        )
        self.assertEqual(len(merged.relationships), before)

    def test_apply_keeps_valid_relationship(self):
        # Start from a spec with no relationships, then add a real one.
        spec = self._base_spec()
        spec.relationships.clear()
        good = SuggestedRelationship(
            relationship=SemanticRelationship(
                from_table="fact_sales",
                from_column="ProductKey",
                to_table="dimension_product",
                to_column="ProductKey",
            ),
            source="agent",
            confidence=0.9,
        )
        merged = apply_suggestions(spec, relationships=[good])
        self.assertTrue(
            any(
                r.from_column == "ProductKey" and r.to_table == "dimension_product"
                for r in merged.relationships
            )
        )

    def test_resolvable_relationships_splits_valid_and_dropped(self):
        spec = self._base_spec()
        spec.relationships.append(
            SemanticRelationship(
                from_table="fact_sales",
                from_column="Nope",
                to_table="dimension_date",
                to_column="DateKey",
            )
        )
        valid, dropped = resolvable_relationships(spec)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0].from_column, "Nope")
        self.assertTrue(all(r.from_column != "Nope" for r in valid))

    def test_build_definition_drops_dangling_relationship_with_warning(self):
        spec = self._base_spec()
        spec.relationships.append(
            SemanticRelationship(
                from_table="fact_sales",
                from_column="Nope",
                to_table="dimension_date",
                to_column="DateKey",
            )
        )
        with self.assertWarns(UserWarning):
            definition = build_definition(spec, DefinitionFormat.TMDL)
        rel_tmdl = definition.files["definition/relationships.tmdl"]
        # The dangling column must not appear in the rendered TMDL.
        self.assertNotIn("Nope", rel_tmdl)

    def test_build_definition_tmsl_excludes_dangling_relationship(self):
        spec = self._base_spec()
        spec.relationships.append(
            SemanticRelationship(
                from_table="fact_sales",
                from_column="Nope",
                to_table="dimension_date",
                to_column="DateKey",
            )
        )
        with self.assertWarns(UserWarning):
            definition = build_definition(spec, DefinitionFormat.TMSL)
        model = json.loads(definition.files["model.bim"])
        cols = {r["fromColumn"] for r in model["model"]["relationships"]}
        self.assertNotIn("Nope", cols)


class SemanticModelValidationTests(unittest.TestCase):
    """Preflight consistency checks before TMDL/TMSL rendering."""

    def _base_spec(self):
        return spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )

    def _codes(self, result):
        return {issue.code for issue in result.issues}

    def test_valid_generated_spec_has_no_errors(self):
        result = validate_semantic_model_spec(self._base_spec())
        self.assertTrue(result.ok)
        self.assertEqual(result.errors, [])

    def test_missing_model_name_and_tables_are_errors(self):
        spec = SemanticModelSpec(name="", source_server="srv", source_database="db")
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("model-name-empty", self._codes(result))
        self.assertIn("no-tables", self._codes(result))

    def test_missing_source_connection_is_error(self):
        spec = self._base_spec()
        spec.source_server = None
        spec.source_database = None
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("source-server-missing", self._codes(result))
        self.assertIn("source-database-missing", self._codes(result))

    def test_dangling_relationship_is_error(self):
        spec = self._base_spec()
        spec.relationships.append(
            SemanticRelationship(
                from_table="fact_sales",
                from_column="Nope",
                to_table="dimension_product",
                to_column="ProductKey",
            )
        )
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("relationship-endpoint-missing", self._codes(result))

    def test_duplicate_table_column_and_measure_names_are_errors(self):
        spec = self._base_spec()
        spec.tables.append(spec.tables[0])
        fact = spec.table("fact_sales")
        fact.columns.append(fact.columns[0])
        fact.measures.extend(
            [
                SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])"),
                SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])"),
            ]
        )
        result = validate_semantic_model_spec(spec)
        self.assertIn("duplicate-table-name", self._codes(result))
        self.assertIn("duplicate-column-name", self._codes(result))
        self.assertIn("duplicate-measure-name", self._codes(result))

    def test_invalid_storage_cardinality_and_filtering_are_errors(self):
        spec = self._base_spec()
        spec.storage_mode = "hybrid"
        spec.relationships[0].from_cardinality = "zero"
        spec.relationships[0].cross_filtering_behavior = "everywhere"
        result = validate_semantic_model_spec(spec)
        self.assertIn("invalid-storage-mode", self._codes(result))
        self.assertIn("invalid-relationship-cardinality", self._codes(result))
        self.assertIn("invalid-cross-filtering-behavior", self._codes(result))

    def test_empty_measure_expression_is_error(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Blank", expression="")
        )
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("measure-expression-empty", self._codes(result))

    def test_warning_only_result_remains_ok(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Has Equals", expression="=SUM('fact_sales'[Amount])")
        )
        result = validate_semantic_model_spec(spec)
        self.assertTrue(result.ok)
        self.assertIn("measure-expression-leading-equals", self._codes(result))

    def test_measure_name_reused_across_tables_is_error(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])")
        )
        spec.table("dimension_product").measures.append(
            SemanticMeasure(name="Total", expression="COUNTROWS('dimension_product')")
        )
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("duplicate-measure-name", self._codes(result))

    def test_measure_name_collision_is_case_insensitive(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.extend(
            [
                SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])"),
                SemanticMeasure(name="TOTAL", expression="COUNTROWS('fact_sales')"),
            ]
        )
        result = validate_semantic_model_spec(spec)
        self.assertIn("duplicate-measure-name", self._codes(result))

    def test_measure_referencing_unknown_object_is_error(self):
        spec = self._base_spec()
        # 'Profit' is neither a column nor a measure in the model — exactly the
        # case that fails at query time with "The value for 'Profit' cannot be
        # determined".
        spec.table("fact_sales").measures.append(
            SemanticMeasure(
                name="Profit Margin %",
                expression="DIVIDE([Profit], SUM('fact_sales'[Amount]))",
            )
        )
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("measure-references-unknown-object", self._codes(result))

    def test_measure_referencing_unknown_qualified_column_is_error(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Bad", expression="SUM('fact_sales'[Nope])")
        )
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("measure-references-unknown-column", self._codes(result))

    def test_measure_referencing_unknown_table_is_error(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Bad", expression="SUM('Ghost'[Amount])")
        )
        result = validate_semantic_model_spec(spec)
        self.assertFalse(result.ok)
        self.assertIn("measure-references-unknown-table", self._codes(result))

    def test_measure_referencing_known_column_and_measure_is_ok(self):
        spec = self._base_spec()
        fact = spec.table("fact_sales")
        fact.measures.append(
            SemanticMeasure(name="Total Sales", expression="SUM('fact_sales'[Amount])")
        )
        # References the real 'Amount' column and the 'Total Sales' measure.
        fact.measures.append(
            SemanticMeasure(
                name="Avg Sale",
                expression="DIVIDE([Total Sales], COUNTROWS('fact_sales'))",
            )
        )
        result = validate_semantic_model_spec(spec)
        self.assertTrue(result.ok)
        self.assertNotIn("measure-references-unknown-object", self._codes(result))

    def test_measure_reference_inside_string_literal_is_ignored(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(
                name="Labelled",
                expression='SUM(\'fact_sales\'[Amount]) + 0 * LEN("[Ghost]")',
            )
        )
        result = validate_semantic_model_spec(spec)
        self.assertTrue(result.ok)


class DedupeMeasureNamesTests(unittest.TestCase):
    """Auto-renaming of duplicate measure names before generation."""

    def _base_spec(self):
        return spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )

    def test_no_duplicates_returns_same_spec_unchanged(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])")
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [])
        self.assertIs(new_spec, spec)

    def test_same_table_duplicate_is_renamed(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.extend(
            [
                SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])"),
                SemanticMeasure(name="Total", expression="COUNTROWS('fact_sales')"),
            ]
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [("Total", "Total 2")])
        names = [m.name for m in new_spec.table("fact_sales").measures]
        self.assertEqual(names.count("Total"), 1)
        self.assertIn("Total 2", names)
        # Result must now pass model-wide validation.
        self.assertEqual(
            [i.code for i in validate_semantic_model_spec(new_spec).errors], []
        )

    def test_cross_table_duplicate_is_renamed_model_wide(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])")
        )
        spec.table("dimension_product").measures.append(
            SemanticMeasure(name="Total", expression="COUNTROWS('dimension_product')")
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [("Total", "Total 2")])
        all_names = [m.name for t in new_spec.tables for m in t.measures]
        self.assertEqual(sorted(all_names), ["Total", "Total 2"])

    def test_measure_colliding_with_same_table_column_is_renamed(self):
        spec = self._base_spec()
        # 'Amount' is an existing column on fact_sales; a measure of the same
        # name would make Fabric reject the dataset.
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Amount", expression="SUM('fact_sales'[Amount])")
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [("Amount", "Amount 2")])
        names = [m.name for m in new_spec.table("fact_sales").measures]
        self.assertIn("Amount 2", names)
        self.assertNotIn("Amount", names)
        # Result must now pass model-wide validation.
        self.assertEqual(
            [i.code for i in validate_semantic_model_spec(new_spec).errors], []
        )

    def test_measure_matching_column_on_other_table_is_kept(self):
        spec = self._base_spec()
        # 'Amount' lives on fact_sales; a measure named 'Amount' on a *different*
        # table is allowed and must not be renamed.
        spec.table("dimension_product").measures.append(
            SemanticMeasure(name="Amount", expression="SUM('fact_sales'[Amount])")
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [])
        self.assertIs(new_spec, spec)

    def test_three_duplicates_get_incrementing_suffixes(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.extend(
            SemanticMeasure(name="Total", expression="COUNTROWS('fact_sales')")
            for _ in range(3)
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [("Total", "Total 2"), ("Total", "Total 3")])
        names = [m.name for m in new_spec.table("fact_sales").measures]
        self.assertEqual(sorted(names), ["Total", "Total 2", "Total 3"])

    def test_does_not_mutate_input_spec(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.extend(
            [
                SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])"),
                SemanticMeasure(name="Total", expression="COUNTROWS('fact_sales')"),
            ]
        )
        dedupe_measure_names(spec)
        names = [m.name for m in spec.table("fact_sales").measures]
        self.assertEqual(names, ["Total", "Total"])

    def test_renamed_measure_reference_is_propagated(self):
        spec = self._base_spec()
        fact = spec.table("fact_sales")
        # A 'Profit' column forces the same-named measure to be renamed to
        # 'Profit 2'...
        fact.columns.append(
            SemanticColumn(name="Profit", source_column="Profit", data_type="decimal")
        )
        fact.measures.append(
            SemanticMeasure(name="Profit", expression="SUM('fact_sales'[Amount])")
        )
        # ...and a dependent measure referencing [Profit] must follow the rename
        # so it does not point at a now-missing object at query time.
        fact.measures.append(
            SemanticMeasure(
                name="Profit Margin %",
                expression="DIVIDE([Profit], SUM('fact_sales'[Amount]))",
            )
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [("Profit", "Profit 2")])
        margin = next(
            m
            for m in new_spec.table("fact_sales").measures
            if m.name == "Profit Margin %"
        )
        self.assertIn("[Profit 2]", margin.expression)
        self.assertNotIn("[Profit]", margin.expression)
        # The rewritten model must validate cleanly.
        self.assertEqual(
            [i.code for i in validate_semantic_model_spec(new_spec).errors], []
        )

    def test_qualified_column_reference_is_not_rewritten_by_rename(self):
        spec = self._base_spec()
        fact = spec.table("fact_sales")
        # Force a rename of a 'Quantity' measure (collides with the column)...
        fact.measures.append(
            SemanticMeasure(name="Quantity", expression="SUM('fact_sales'[Quantity])")
        )
        # ...a dependent measure references the *column* 'fact_sales'[Quantity],
        # which must stay intact (only the bare measure ref would be rewritten).
        fact.measures.append(
            SemanticMeasure(
                name="Total Qty", expression="SUMX('fact_sales', 'fact_sales'[Quantity])"
            )
        )
        new_spec, renames = dedupe_measure_names(spec)
        self.assertEqual(renames, [("Quantity", "Quantity 2")])
        total = next(
            m for m in new_spec.table("fact_sales").measures if m.name == "Total Qty"
        )
        self.assertIn("'fact_sales'[Quantity]", total.expression)

    def test_build_definition_renames_duplicate_measures_and_warns(self):
        spec = self._base_spec()
        spec.table("fact_sales").measures.append(
            SemanticMeasure(name="Total", expression="SUM('fact_sales'[Amount])")
        )
        spec.table("dimension_product").measures.append(
            SemanticMeasure(name="Total", expression="COUNTROWS('dimension_product')")
        )
        with self.assertWarns(UserWarning):
            definition = build_definition(spec, DefinitionFormat.TMSL)
        model = json.loads(definition.files["model.bim"])
        measure_names = [
            m["name"]
            for t in model["model"]["tables"]
            for m in t.get("measures", [])
        ]
        self.assertEqual(sorted(measure_names), ["Total", "Total 2"])


class DropUnresolvedMeasuresTests(unittest.TestCase):
    """Pruning measures that reference objects not present in the model."""

    def _base_spec(self):
        return spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )

    def test_drops_measure_referencing_unknown_object(self):
        spec = self._base_spec()
        fact = spec.table("fact_sales")
        fact.measures.append(
            SemanticMeasure(name="Total Amount", expression="SUM('fact_sales'[Amount])")
        )
        # References [City Count], which is neither a column nor a measure.
        fact.measures.append(
            SemanticMeasure(
                name="Average Population per City",
                expression="DIVIDE([Total Amount], [City Count])",
            )
        )
        _, dropped = drop_unresolved_measures(spec)
        names = [m.name for m in spec.table("fact_sales").measures]
        self.assertIn("Total Amount", names)
        self.assertNotIn("Average Population per City", names)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0][0], "fact_sales.Average Population per City")
        self.assertIn("City Count", dropped[0][1])
        # The pruned model is now query-clean per the auditor's own rule.
        self.assertNotIn(
            "measure-references-unknown-object",
            [i.code for i in validate_semantic_model_spec(spec).errors],
        )

    def test_keeps_resolvable_measures(self):
        spec = self._base_spec()
        fact = spec.table("fact_sales")
        fact.measures.append(
            SemanticMeasure(name="Total Amount", expression="SUM('fact_sales'[Amount])")
        )
        fact.measures.append(
            SemanticMeasure(name="Double Amount", expression="[Total Amount] * 2")
        )
        _, dropped = drop_unresolved_measures(spec)
        self.assertEqual(dropped, [])
        names = [m.name for m in spec.table("fact_sales").measures]
        self.assertIn("Total Amount", names)
        self.assertIn("Double Amount", names)

    def test_cascading_drop_to_fixpoint(self):
        spec = self._base_spec()
        fact = spec.table("fact_sales")
        # 'Derived' depends on 'Broken', which references an unknown object;
        # dropping 'Broken' must then invalidate and drop 'Derived'.
        fact.measures.append(
            SemanticMeasure(name="Broken", expression="DIVIDE([Ghost], 1)")
        )
        fact.measures.append(
            SemanticMeasure(name="Derived", expression="[Broken] + 1")
        )
        _, dropped = drop_unresolved_measures(spec)
        dropped_refs = {ref for ref, _ in dropped}
        self.assertEqual(dropped_refs, {"fact_sales.Broken", "fact_sales.Derived"})
        self.assertEqual(spec.table("fact_sales").measures, [])

    def test_no_measures_is_noop(self):
        spec = self._base_spec()
        _, dropped = drop_unresolved_measures(spec)
        self.assertEqual(dropped, [])


class ModelServiceDedupTests(unittest.TestCase):
    """The layered ModelService must hand back collision-free specs.

    ``dedupe_measure_names`` runs inside ``build_definition``, but the spec the
    service returns from ``design_model`` / ``apply_model_suggestions`` is what
    the UI validates (preflight) and later publishes. If that spec still carried
    a measure whose name matches a column on the same table, the preflight would
    raise ``measure-name-collides-with-column`` and a publish could be rejected
    by Fabric. These tests lock in that the service deduplicates up front.
    """

    def _service(self):
        from fabric_services.context import TenantContext
        from fabric_services.model_service import ModelService

        # These flows are pure spec transforms; no Fabric/store access occurs.
        return ModelService(None, None, TenantContext.default())

    def _base_spec(self):
        return spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )

    def test_apply_suggestions_renames_measure_colliding_with_column(self):
        svc = self._service()
        spec = self._base_spec()
        # 'Amount' is a real column on fact_sales; accepting a measure of the
        # same name would make Fabric reject the dataset.
        suggestion = SuggestedMeasure(
            table="fact_sales",
            measure=SemanticMeasure(
                name="Amount", expression="SUM('fact_sales'[Amount])"
            ),
        )
        merged = svc.apply_model_suggestions(spec, measures=[suggestion])
        # The merged spec must be deployable: no consistency errors remain.
        self.assertEqual(
            [i.code for i in validate_semantic_model_spec(merged).errors], []
        )
        fact_measures = [m.name for m in merged.table("fact_sales").measures]
        self.assertIn("Amount 2", fact_measures)
        self.assertNotIn("Amount", fact_measures)

    def test_design_model_deterministic_returns_collision_free_spec(self):
        svc = self._service()
        result = svc.design_model(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
            use_agent=False,
        )
        self.assertEqual(
            [i.code for i in validate_semantic_model_spec(result.spec).errors],
            [],
        )


class PublishPreflightTests(unittest.TestCase):
    """The publish/update paths must refuse a spec that fails preflight.

    Agent Studio once published a semantic model with no tables because the
    publish call rendered and shipped the spec without re-running the
    deterministic consistency checks. These tests lock in that the service
    raises before any side-effecting Fabric call.
    """

    def _model_service(self):
        from fabric_services.context import TenantContext
        from fabric_services.model_service import ModelService

        # Validation runs before any Fabric/store access, so None is safe.
        return ModelService(None, None, TenantContext.default())

    def _report_service(self):
        from fabric_services.context import TenantContext
        from fabric_services.report_service import ReportService

        return ReportService(None, None, TenantContext.default())

    def test_publish_model_without_tables_is_rejected(self):
        from dataclasses import replace

        from fabric_services.errors import ValidationError

        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        empty = replace(spec, tables=[])
        with self.assertRaises(ValidationError) as ctx:
            self._model_service().publish_model("ws-1", "SalesModel", empty)
        self.assertIn("no-tables", str(ctx.exception))

    def test_update_model_without_tables_is_rejected(self):
        from dataclasses import replace

        from fabric_services.errors import ValidationError

        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        empty = replace(spec, tables=[])
        with self.assertRaises(ValidationError):
            self._model_service().update_model("ws-1", "m-1", empty)

    def test_publish_report_without_pages_is_rejected(self):
        from app.intelligence.report_spec import ReportSpec
        from fabric_services.errors import ValidationError

        spec = ReportSpec(name="Sales", pages=[], dataset_id="ds-1")
        with self.assertRaises(ValidationError) as ctx:
            self._report_service().publish_report("ws-1", "Sales", spec)
        self.assertIn("no pages", str(ctx.exception))

    def test_publish_report_without_visuals_is_rejected(self):
        from app.intelligence.report_spec import ReportPage, ReportSpec
        from fabric_services.errors import ValidationError

        spec = ReportSpec(
            name="Sales",
            pages=[ReportPage(name="p1", display_name="Page 1", visuals=[])],
            dataset_id="ds-1",
        )
        with self.assertRaises(ValidationError) as ctx:
            self._report_service().publish_report("ws-1", "Sales", spec)
        self.assertIn("no visuals", str(ctx.exception))


# Known scalar DAX functions the deterministic engine emits.
_KNOWN_DAX_FUNCTIONS = {"COUNTROWS", "SUM", "AVERAGE"}
# A fully-qualified DAX column reference: 'Table'[Column] (brackets may contain
# an escaped ``]]``).
_DAX_COLUMN_RE = re.compile(r"'(?:[^']|'')+'\[(?:[^\]]|\]\])+\]")


def _parens_balanced(expr: str) -> bool:
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


class DaxReferenceHelperTests(unittest.TestCase):
    """The DAX-escaping helpers must follow the DAX naming rules."""

    def test_table_ref_is_single_quoted(self):
        self.assertEqual(dax_table_ref("Sales"), "'Sales'")

    def test_table_ref_escapes_single_quote(self):
        # O'Brien -> 'O''Brien'
        self.assertEqual(dax_table_ref("O'Brien"), "'O''Brien'")

    def test_column_ref_is_bracketed(self):
        self.assertEqual(dax_column_ref("Amount"), "[Amount]")

    def test_column_ref_escapes_closing_bracket(self):
        # Amount] -> [Amount]]]
        self.assertEqual(dax_column_ref("Amount]"), "[Amount]]]")

    def test_qualified_column_combines_table_and_column(self):
        self.assertEqual(
            dax_qualified_column("Sales", "Amount"), "'Sales'[Amount]"
        )

    def test_measure_ref_is_bracketed(self):
        self.assertEqual(dax_measure_ref("Total Sales"), "[Total Sales]")


class DaxMeasureValidityTests(unittest.TestCase):
    """Every generated measure expression must be valid DAX.

    Validated against the DAX syntax reference:
    https://learn.microsoft.com/dax/dax-syntax-reference
    """

    def setUp(self):
        self.measures = suggest_from_schemas(star_schema_tables()).measures
        self.assertTrue(self.measures, "expected deterministic measures")

    def test_expression_does_not_include_equals_sign(self):
        # ``expression`` is the scalar formula AFTER ``=`` — never the ``=``.
        for s in self.measures:
            self.assertFalse(s.measure.expression.lstrip().startswith("="))

    def test_expression_uses_known_function_with_parens(self):
        for s in self.measures:
            expr = s.measure.expression
            fn = expr.split("(", 1)[0]
            self.assertIn(fn, _KNOWN_DAX_FUNCTIONS, expr)
            self.assertIn("(", expr, expr)
            self.assertTrue(_parens_balanced(expr), expr)

    def test_column_aggregations_use_qualified_references(self):
        for s in self.measures:
            expr = s.measure.expression
            if expr.startswith(("SUM(", "AVERAGE(")):
                self.assertRegex(expr, _DAX_COLUMN_RE)

    def test_row_count_references_a_single_quoted_table(self):
        for s in self.measures:
            if s.measure.expression.startswith("COUNTROWS("):
                self.assertEqual(
                    s.measure.expression, f"COUNTROWS('{s.table}')"
                )

    def test_special_characters_in_names_are_escaped(self):
        # A table with a single quote and a column with a closing bracket must
        # still yield syntactically valid DAX.
        tricky = _Table(
            "dbo",
            "O'Hara Sales",
            [
                _Col("Id", "int", pk=True),
                _Col("Net]Amount", "decimal"),
            ],
        )
        measures = suggest_measures([tricky])
        sum_measure = next(
            s for s in measures if s.measure.name.startswith("Total ")
        )
        self.assertIn("'O''Hara Sales'", sum_measure.measure.expression)
        self.assertIn("[Net]]Amount]", sum_measure.measure.expression)
        self.assertTrue(_parens_balanced(sum_measure.measure.expression))

    def test_measure_expression_survives_tmdl_round_trip(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        spec = apply_suggestions(spec, measures=self.measures)
        definition = build_definition(spec, fmt=DefinitionFormat.TMDL)
        joined = "\n".join(definition.files.values())
        for s in self.measures:
            # The exact DAX text must appear verbatim somewhere in the TMDL.
            self.assertIn(s.measure.expression, joined, s.measure.name)

    def test_multiline_dax_is_indented_in_tmdl(self):
        spec = spec_from_schemas(
            star_schema_tables(),
            model_name="SalesModel",
            source_server="srv",
            source_database="db",
        )
        fact = spec.table("fact_sales")
        multiline = (
            "VAR _total = SUM('fact_sales'[Amount])\n"
            "RETURN\n"
            "    DIVIDE(_total, COUNTROWS('fact_sales'))"
        )
        fact.measures.append(
            SemanticMeasure(name="Avg Ticket", expression=multiline)
        )
        definition = build_definition(spec, fmt=DefinitionFormat.TMDL)
        fact_part = next(
            text
            for path, text in definition.files.items()
            if path.endswith("fact_sales.tmdl")
        )
        # The measure must open with ``=`` on its own line and indent the body.
        self.assertIn("measure 'Avg Ticket' =\n", fact_part)
        self.assertIn("\t\t\tVAR _total = SUM('fact_sales'[Amount])", fact_part)
        self.assertIn("\t\t\tRETURN", fact_part)
        self.assertIn("\t\t\t    DIVIDE(_total, COUNTROWS('fact_sales'))", fact_part)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_column_block(table_text: str, column_name: str) -> str:
    """Return the lines starting at ``column <name>`` up to the next blank line."""
    lines = table_text.split("\n")
    out: list[str] = []
    capturing = False
    for line in lines:
        if re.match(rf"^\tcolumn\s+'?{re.escape(column_name)}'?\s*$", line):
            capturing = True
            out.append(line)
            continue
        if capturing:
            if not line.strip():
                break
            out.append(line)
    return "\n".join(out)


class AgentErrorClassifierTests(unittest.TestCase):
    """The 403/permission detection and friendly-message formatting."""

    PERMISSION_ERROR = (
        "<class 'FoundryChatClient'> service failed to complete the prompt: "
        "Error code: 403 - {'error': {'code': 'UserError', 'message': "
        "'Identity(object id: c0eb2b76-930c-4d22-b722-855d5bd105d0) does not "
        "have permissions for Microsoft.MachineLearningServices/workspaces/"
        "agents/action actions. Please refer to "
        "https://aka.ms/azureml-auth-troubleshooting to fix the permissions "
        "issue.'}}"
    )

    def test_detects_permission_error(self):
        from app.intelligence import agent as agent_module

        self.assertTrue(
            agent_module.is_permission_error(Exception(self.PERMISSION_ERROR))
        )

    def test_non_permission_error_not_flagged(self):
        from app.intelligence import agent as agent_module

        self.assertFalse(
            agent_module.is_permission_error(Exception("connection reset by peer"))
        )

    def test_describe_permission_error_is_actionable(self):
        from app.intelligence import agent as agent_module

        msg = agent_module.describe_agent_error(Exception(self.PERMISSION_ERROR))
        self.assertIn("c0eb2b76-930c-4d22-b722-855d5bd105d0", msg)
        self.assertIn("Azure AI Developer", msg)
        self.assertIn("azureml-auth-troubleshooting", msg)
        # The raw JSON blob must not leak through.
        self.assertNotIn("messageFormat", msg)
        self.assertLess(len(msg), 400)

    def test_describe_generic_error_is_trimmed(self):
        from app.intelligence import agent as agent_module

        raw = "boom\n" + ("x" * 5000)
        msg = agent_module.describe_agent_error(Exception(raw))
        self.assertEqual(msg, "boom")


if __name__ == "__main__":
    unittest.main()
