# Suggestions JSON contract

The app's **Suggest** workflow asks for additional relationships and DAX
measures the user might want to add to an already-designed semantic model.
This is *not* a full spec — only suggestions the user will pick from.

## Shape

```json
{
  "relationships": [
    {
      "relationship": {
        "from_table": "Sales",
        "from_column": "ProductKey",
        "to_table": "Product",
        "to_column": "ProductKey",
        "from_cardinality": "many",
        "to_cardinality": "one",
        "cross_filtering_behavior": "oneDirection",
        "is_active": true
      },
      "rationale": "Sales.ProductKey matches the PK of Product by name; classic star-schema edge.",
      "confidence": 0.95,
      "source": "agent"
    }
  ],
  "measures": [
    {
      "table": "Sales",
      "measure": {
        "name": "Total Sales",
        "expression": "SUM('Sales'[Sales Amount])",
        "format_string": "\\$#,0.00",
        "display_folder": "Sales",
        "description": "Total revenue across all sales rows."
      },
      "rationale": "Sales Amount is the headline fact column; sum is the canonical aggregation.",
      "confidence": 0.95,
      "source": "agent"
    }
  ]
}
```

## Rules

- Use the **model-friendly table and column names from the supplied spec**, not
  raw SQL names. Those names are what the user has already accepted.
- `relationship` follows the spec contract (see `spec-schema.md`): express
  every edge from the *many* side to the *one* side.
- `measure.expression` is DAX; reference columns as `'Table'[Column]`. See
  `dax-measures.md`.
- `confidence` is in `[0, 1]`. Use `≥ 0.9` only when you are essentially
  certain (explicit join semantics or a textbook aggregation).
- `rationale` is shown in the picker; keep it short and concrete (one sentence).
- `source` should be `"agent"` for AI-generated items. The app tags
  deterministic suggestions separately.
- Do **not** repeat items already listed under `existing_relationships` or
  `existing_measure_names` in the prompt — those are already in the spec.

## How accepted suggestions reach the Fabric model

When the user ticks a suggestion and clicks **Apply**, the deterministic engine
calls `apply_suggestions(spec, relationships=..., measures=...)`:

- Each accepted relationship is appended to `spec.relationships`. A final pass
  enforces *one active relationship per (from_table, to_table) pair*; extras
  are kept but marked `is_active: false` (role-playing dimensions).
- Each accepted measure is appended to `spec.tables[<table>].measures`. The
  build pipeline then renders it into:
  - **TMDL** — a `measure '<Name>' = <DAX>` block (with optional
    `formatString:` and `displayFolder:`) inside
    `definition/tables/<Table>.tmdl`.
  - **TMSL** — an entry in the table's `measures[]` array inside `model.bim`,
    with `name`, `expression`, `formatString`, `displayFolder`, `description`.

Relationships render into:

- **TMDL** — a `relationship rel_NNN` block in `definition/relationships.tmdl`
  with `fromColumn:` / `toColumn:` and optional `isActive: false` /
  `crossFilteringBehavior: bothDirections`.
- **TMSL** — an entry in `model.relationships[]` with the matching JSON
  fields.

The Fabric REST `POST /workspaces/{id}/semanticModels` call uses the same
base64-encoded `parts` array; suggestions do not need any new transport.
