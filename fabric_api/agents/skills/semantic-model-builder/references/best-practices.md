# Best Practice Analyzer (BPA) rules

Apply Microsoft's official **Best Practice Rules** for tabular / semantic models
when designing. The full catalog ships with the app as
`app/intelligence/resources/BPARules.json` (source:
[microsoft/Analysis-Services](https://github.com/microsoft/Analysis-Services/blob/master/BestPracticeRules/BPARules.json))
and the app audits every model against the statically-checkable subset.

The rules below are the ones you can satisfy at **design time**, straight from a
SQL schema. Honor them in the JSON spec you emit.

## Formatting

- **Provide a format string for every visible measure** (`format_string`), e.g.
  `"$#,0"` for currency, `"#,0"` for whole numbers, `"#,0.0%"` for percentages.
- **Do not summarize numeric columns** — set `summarize_by: "none"` on integer,
  decimal and double columns so report authors use measures instead of implicit
  aggregation.
- **Hide foreign keys** — surrogate/foreign-key columns should be `is_hidden:
  true`.
- **Mark primary keys** — the column on the "one" side of a relationship should
  be `is_key: true` (except on a date table).
- **First letter of objects should be capitalized**; never start or end a name
  with a space.

## Data types

- **Avoid the Double floating-point type** where Int64 or Decimal will do.
- **Relationship columns should share a data type**, and ideally both be Int64.

## Modeling

- **The model should have a date table**, and tables named like `Date`/`Calendar`
  should be marked with `is_date_table: true`.
- **Every table should participate in a relationship** (no island tables).
- **Many-to-many relationships should be single-direction**, not bidirectional.

## DAX

- **Use `DIVIDE()` instead of `/`** to avoid divide-by-zero errors.
- **Avoid `IFERROR`**; prefer `DIVIDE()` or structured handling.
- **Use `TREATAS()` instead of `INTERSECT()`** for virtual relationships.
- **Avoid duplicate measures** (two measures with the same expression).

## Documentation

- **Add a description to every visible object** (table, column, measure) so the
  model is self-documenting and Copilot/Q&A friendly.

> Rules that depend on runtime VertiPaq statistics (cardinality, row counts,
> referential-integrity violations) cannot be judged from a static spec and are
> evaluated only against a deployed model.
