# DAX measure patterns

Every measure `expression` is **DAX** (Data Analysis Expressions). Add measures
to the most relevant **fact** table. Reference columns with the **model**
(friendly) names you assigned: `'Table'[Column]`.

## DAX syntax rules (must follow)

Per the [DAX syntax reference](https://learn.microsoft.com/dax/dax-syntax-reference):

- A measure expression is a **scalar** DAX expression (the part *after* the
  `=`; do not include the `=` itself in `expression`).
- **Column reference** = fully-qualified `'Table'[Column]`.
  - Table name is wrapped in **single quotes**; a literal `'` inside the name is
    doubled: `O'Brien` → `'O''Brien'`.
  - Column name is wrapped in **square brackets**; a literal `]` inside the name
    is doubled: `Amount]` → `[Amount]]]`.
- **Measure reference** = `[Measure Name]` (brackets, no table qualifier
  needed).
- **Operators**: arithmetic `+ - * / ^`, comparison `= > < >= <= <>`, text
  concatenation `&`, logic `&& ||`.
- Functions always need parentheses, even with no arguments: `PI()`.
- A table-returning function must be wrapped so the measure resolves to a
  scalar (e.g. `COUNTROWS(...)`, `SUMX(...)`).

## Core aggregations
```dax
Total Sales       = SUM('Sales'[Sales Amount])
Order Count       = DISTINCTCOUNT('Sales'[Order Number])
Quantity Sold     = SUM('Sales'[Quantity])
Average Price     = DIVIDE([Total Sales], [Quantity Sold])
Row Count         = COUNTROWS('Sales')
```

## Ratios (always guard division)
```dax
Margin %          = DIVIDE([Total Profit], [Total Sales])
```

## Time intelligence (requires a marked date table)
```dax
Sales YTD         = TOTALYTD([Total Sales], 'Date'[Date])
Sales LY          = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))
Sales YoY %       = DIVIDE([Total Sales] - [Sales LY], [Sales LY])
```

## Role-playing dimension (inactive relationship)
```dax
Sales by Ship Date =
    CALCULATE([Total Sales], USERELATIONSHIP('Sales'[ShipDateKey], 'Date'[DateKey]))
```

## formatString to pair with measures
| Measure kind | formatString |
|--------------|--------------|
| Currency | `\$#,0.00` |
| Count / integer | `#,0` |
| Percentage | `0.0%` |
| Decimal | `#,0.00` |

Group related measures with `display_folder` (e.g. `"Sales"`, `"Profitability"`).
