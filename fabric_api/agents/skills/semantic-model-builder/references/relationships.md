# Star-schema & relationship guidance

## Fact vs. dimension
- **Fact table**: many rows, mostly numeric measures + several foreign keys
  (e.g. `FactSales`, `Orders`, `Events`). Measures live here.
- **Dimension table**: descriptive attributes with a primary key referenced by
  facts (e.g. `Product`, `Customer`, `Date`, `Geography`).
- **Bridge / junction table** (composite PK that is two foreign keys, no extra
  attributes): keep it, but consider `is_hidden: true`.

## Turning foreign keys into relationships
For every foreign key `Fact.DimKey → Dim.DimKey`:
```json
{
  "from_table": "Fact", "from_column": "DimKey",
  "to_table": "Dim",   "to_column": "DimKey",
  "from_cardinality": "many", "to_cardinality": "one",
  "cross_filtering_behavior": "oneDirection", "is_active": true
}
```

## One active relationship per table pair
A tabular model allows only **one active** relationship between any two tables.
When a fact references the same dimension twice (e.g. `OrderDateKey` and
`ShipDateKey` both → `Date`), this is a **role-playing dimension**:
- Keep the first relationship active.
- Set `is_active: false` on the others. Surface them in DAX with
  `USERELATIONSHIP` inside measures when needed.

## Date table
If a dimension contains a continuous range of dates with one row per day, mark
it `"is_date_table": true`. This sets `dataCategory: Time` and enables
time-intelligence (`TOTALYTD`, `SAMEPERIODLASTYEAR`, …).

## Direction
Default to single-direction (`oneDirection`) cross-filtering. Use
`bothDirections` only for a deliberate many-to-many bridge — it can create
ambiguous filter paths.
