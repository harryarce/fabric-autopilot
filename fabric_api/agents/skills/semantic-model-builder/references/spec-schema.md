# Semantic model spec — JSON contract

This is the single source of truth the agent emits. The deterministic engine
(`app/intelligence/spec.py` and `definition.py`) renders it to TMDL / TMSL.
Round-trips through `SemanticModelSpec.from_dict` / `to_dict`.

```json
{
  "name": "Sales",
  "culture": "en-US",
  "compatibility_level": 1604,
  "storage_mode": "directQuery",
  "source_server": "<sql-endpoint>.datawarehouse.fabric.microsoft.com",
  "source_database": "<database>",
  "description": "Optional model description.",
  "tables": [
    {
      "name": "Sales",
      "source_schema": "dbo",
      "source_table": "FactSales",
      "description": "Order line transactions.",
      "is_hidden": false,
      "is_date_table": false,
      "columns": [
        {
          "name": "SalesKey",
          "source_column": "SalesKey",
          "data_type": "int64",
          "summarize_by": "none",
          "is_hidden": true,
          "is_key": true,
          "description": "Primary key uniquely identifying each row."
        },
        {
          "name": "SalesAmount",
          "source_column": "SalesAmount",
          "data_type": "decimal",
          "summarize_by": "sum",
          "format_string": "\\$#,0.00",
          "description": "Sales amount - numeric value aggregated by sum."
        }
      ],
      "measures": [
        {
          "name": "Total Sales",
          "expression": "SUM('Sales'[Sales Amount])",
          "format_string": "\\$#,0.00",
          "display_folder": "Sales"
        }
      ]
    }
  ],
  "relationships": [
    {
      "from_table": "Sales",
      "from_column": "ProductKey",
      "to_table": "Product",
      "to_column": "ProductKey",
      "from_cardinality": "many",
      "to_cardinality": "one",
      "cross_filtering_behavior": "oneDirection",
      "is_active": true
    }
  ]
}
```

## Field reference

### Top level
| Field | Required | Notes |
|-------|----------|-------|
| `name` | yes | Model/dataset name. |
| `culture` | no | Default `en-US`. |
| `compatibility_level` | no | Default `1604`. |
| `storage_mode` | no | `directQuery` (default), `import`, or `directLake`. |
| `source_server` / `source_database` | no | When present, deployable Power Query partitions are generated. Always pass these through from the schema brief. |
| `tables` | yes | At least one. |
| `relationships` | no | Many-to-one edges. |

### Column
- `data_type` must be one of: `string`, `int64`, `decimal`, `double`,
  `dateTime`, `boolean`, `binary`.
- `summarize_by`: `none`, `sum`, `count`, `min`, `max`, `average`.
- Hide and key-mark surrogate keys (`is_hidden: true`, `is_key: true`,
  `summarize_by: none`).
- `description` is **required** for self-documentation: emit a short,
  business-readable sentence on every column (and every table). Primary keys
  describe identity; foreign keys name the table they reference.
- `data_category` is optional (e.g. `City`, `Country`, `Years`).

### Descriptions (self-documenting models)
- Populate `description` on **every** table, column and measure. Models must be
  self-documenting so the fields explain themselves in Power BI / Fabric.
- Keep names exactly as supplied in the schema brief — do not rename. Only add
  descriptions.

### Measure
- `expression` is DAX. Reference columns as `'Table'[Column]` using the
  **model** (friendly) names you assigned.
- `format_string` follows VertiPaq/Excel format codes.

### Relationship
- Express from the **many** side (`from_*`) to the **one** side (`to_*`).
- Only one active relationship per table pair; set `is_active: false` on
  duplicates (role-playing dimensions).
- Use `cross_filtering_behavior: bothDirections` sparingly.
