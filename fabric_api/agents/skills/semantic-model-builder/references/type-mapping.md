# SQL → tabular data-type mapping

Tabular (TOM) models expose a small set of data types. Map SQL Server / Fabric
SQL types as follows. Unknown types fall back to `string`.

| SQL type | Tabular `data_type` |
|----------|---------------------|
| `bit` | `boolean` |
| `tinyint`, `smallint`, `int`, `bigint` | `int64` |
| `decimal`, `numeric`, `money`, `smallmoney` | `decimal` |
| `float`, `real` | `double` |
| `date`, `datetime`, `datetime2`, `smalldatetime`, `datetimeoffset`, `time` | `dateTime` |
| `char`, `nchar`, `varchar`, `nvarchar`, `text`, `ntext`, `uniqueidentifier`, `xml` | `string` |
| `binary`, `varbinary`, `image` | `binary` |

## summarizeBy guidance

- **Keys** (primary keys, surrogate keys, foreign keys used only for joins):
  `summarize_by: none`, and set `is_hidden: true` + `is_key: true` on the
  table's own primary key.
- **Numeric facts** (amounts, quantities): `summarize_by: sum`.
- **Numeric attributes that should not aggregate** (year, age, rating, a status
  code stored as int): `summarize_by: none`.
- **Everything else** (strings, dates, booleans): `summarize_by: none`.

## formatString cheatsheet

| Intent | formatString |
|--------|--------------|
| Currency | `\$#,0.00` |
| Whole number w/ thousands | `#,0` |
| Percentage | `0.0%` |
| Short date | `yyyy-mm-dd` |
| Decimal (2dp) | `#,0.00` |
