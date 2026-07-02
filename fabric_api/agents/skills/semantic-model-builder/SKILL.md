---
name: semantic-model-builder
description: >-
  Designs and generates a Power BI / Microsoft Fabric semantic (tabular) model
  from a relational SQL schema made of multiple tables and their relationships.
  USE THIS whenever the user wants to turn extracted database tables into a
  semantic model, dataset, or .pbism/TMDL/TMSL definition — including mapping SQL
  data types to tabular types, inferring star-schema relationships from foreign
  keys, marking fact vs. dimension tables, proposing DAX measures, identifying a
  date table, and emitting the Fabric item definition parts. Trigger on phrases
  like "build a semantic model", "create a dataset from these tables", "generate
  TMDL", "model.bim", "semantic model definition", or "relationships between
  tables".
license: MIT
compatibility: Works with any model that supports tool use.
metadata:
  author: fabric-autopilot
  version: "1.0"
---

## Mission

Turn a **relational schema** (one or more tables, their columns, primary keys
and foreign keys) into a **well-formed tabular semantic model** and emit the
exact artifacts Microsoft Fabric needs to create the SemanticModel item.

You are the modeling expert. Apply dimensional-modeling judgment that a
mechanical mapping cannot:

1. **Classify tables** as *fact* (transactions/events, mostly numeric + foreign
   keys) or *dimension* (descriptive lookups). Hide pure bridge/junction tables
   from report view when appropriate.
2. **Build the star schema.** Every foreign key is a many-to-one relationship
   from the fact to the dimension. Keep exactly one *active* relationship per
   table pair; mark additional ones inactive (role-playing dimensions).
3. **Map data types** from SQL to tabular types (see
   `references/type-mapping.md`).
4. **Hide surrogate keys** (`isHidden`, `summarizeBy: none`, `isKey`) so they
   never become implicit measures.
5. **Identify a Date dimension** and mark it (`is_date_table: true`) to unlock
   time-intelligence.
6. **Propose DAX measures** for the fact tables (counts, sums, averages,
   ratios). Put them on the most relevant fact table. See
   `references/dax-measures.md`.
7. **Name things for business users** — friendly column/table names, sensible
   `formatString` for currency, percentages and dates.
8. **Apply the Best Practice Analyzer (BPA) rules** at design time — give every
   visible measure a format string, set numeric column summarization to None,
   hide foreign keys, mark primary keys, keep relationship columns the same
   (ideally Int64) type, avoid Double columns, prefer `DIVIDE()` over `/`, and
   add a description to every visible object. See `references/best-practices.md`.

## Workflow

1. **Read the schema brief** you are given (tables, columns, keys, foreign
   keys). If a JSON schema file path is provided, work from that.
2. **Design the model** by producing a single JSON object that conforms to the
   spec in `references/spec-schema.md`. This JSON is the contract — every other
   artifact is generated deterministically from it. Do **not** hand-write TMDL.
3. **Generate the Fabric definition** by running the script:

   ```bash
   python scripts/build_semantic_model.py --spec <spec.json> --format TMDL --out <dir>
   ```

   - `--format` is `TMDL` (default) or `TMSL`.
   - The script writes the `definition/` folder (or `model.bim`),
     `definition.pbism`, and a `definition.parts.json` containing the base64
     `parts` array ready to POST to the Fabric REST API.
   - Pass `--print-parts` to echo the parts JSON to stdout instead of writing.

4. **Summarize** for the user: how many tables/relationships/measures, which
   table is the date table, and any assumptions or `[NEEDS CLARIFICATION]`
   items (e.g. an ambiguous foreign key or a missing date dimension).

## Suggestion mode

The app exposes a "Suggest" workflow that asks for **extra relationships and
measures** the user might want to add to an existing spec. In suggestion mode:

- Output a **single JSON object** with `relationships` and `measures` arrays
  (see `references/suggestions-schema.md`). One entry per item.
- Each entry must carry a short `rationale`, a `confidence` in `[0, 1]`, and
  `source: "agent"`.
- Reference the model-friendly **table and column names from the supplied
  spec** (not the raw SQL names). The user has already approved them.
- Never repeat items already present in the spec — that's what the
  `existing_relationships` and `existing_measure_names` fields in the prompt
  are for.
- Stay conservative on confidence (≥ 0.7 means "very likely correct").

The picker lets the user toggle each item; only checked items are appended to
the spec. Both relationships and measures land in the **same TMDL artifacts**
your design step would produce:

- Relationships → `definition/relationships.tmdl` (TMDL) or
  `model.relationships[]` (TMSL `model.bim`).
- Measures → the `measure '<Name>' = <DAX>` block inside the owning
  table's `definition/tables/<Table>.tmdl` (TMDL), or
  `model.tables[*].measures[]` (TMSL).

See `references/dax-measures.md` for DAX patterns and `references/relationships.md`
for star-schema rules; both apply to suggestions exactly as they do to the
initial design.

## Rules

- Output **valid JSON only** for the spec — no trailing commas, no comments.
- Never invent columns that are not in the source schema; you may add
  **measures** (which are DAX, not stored columns).
- Preserve `source_schema` / `source_table` / `source_column` exactly as given
  so the generated Power Query partitions bind to the real tables.
- Prefer `storage_mode: directQuery` for Fabric SQL endpoints unless the user
  asks for `import` or `directLake`. Use `directLake` to load OneLake Delta
  data on demand via a shared connection expression (compatibility level 1604+);
  it gives Import-like performance without scheduled data refresh.
- Keep this file as the entry point; load `references/*.md` only when you need
  the detail.
- Follow the **Best Practice Analyzer** guidance in `references/best-practices.md`
  (Microsoft's official BPA rule set); the app audits models against the same
  rules.

## References

- `references/spec-schema.md` — the JSON contract you must emit.
- `references/suggestions-schema.md` — JSON contract for the suggestion mode.
- `references/type-mapping.md` — SQL → tabular data-type table.
- `references/dax-measures.md` — measure patterns and `formatString` cheatsheet.
- `references/best-practices.md` — Microsoft Best Practice Analyzer rules to
  apply when crafting the model.
- `references/relationships.md` — star-schema and role-playing guidance.
