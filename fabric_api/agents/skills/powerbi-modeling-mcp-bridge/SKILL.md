---
name: powerbi-modeling-mcp-bridge
description: >-
  Routes live Tabular Object Model editing to the official
  @microsoft/powerbi-modeling-mcp server when it is enabled. USE THIS when the
  task requires operating directly on a running semantic model — adding or
  editing measures, relationships, calculation groups, or row-level security —
  rather than generating a TMDL/TMSL definition from scratch. Trigger on phrases
  like "edit the live model", "add a measure to the dataset", "update RLS", or
  "modify the deployed model".
license: MIT
compatibility: Requires the Power BI Modeling MCP tools to be attached.
metadata:
  author: fabric-autopilot
  version: "1.0"
---

## Mission

Use the official Power BI Modeling MCP tools (exposed under the
`powerbi-modeling` tool when enabled) for **live** model edits, and the
platform's own tools for schema -> spec -> definition -> publish.

## When to use which surface

- **Platform tools** (`design_semantic_model`, `audit_semantic_model`,
  `build_model_definition`): green-field design from a SQL schema, auditing, and
  rendering a deployable TMDL/TMSL definition.
- **Power BI Modeling MCP** (`powerbi-modeling` tool): connect to a running
  model and apply incremental TOM edits — `create_measure`, `update_measure`,
  `create_relationship`, `create_calculation_group`, `create_security_role`,
  `run_dax_query`, etc.

## Rules

- The bridge is **opt-in**. If the `powerbi-modeling` tool is not attached, stay
  on the platform tools and note that live editing is unavailable.
- Default to **read-only** unless the environment explicitly enables writes.
- Validate DAX with a `run_dax_query` before committing a measure when possible.
- Live edits to a deployed model are side-effecting — surface them for approval
  just like a publish.

## References — Microsoft Fabric guidance (official)

Installed from `microsoft/skills-for-fabric`. Consult these for authoritative
semantic-model design judgment when authoring measures, relationships, naming,
and storage choices — whether you go through the platform tools or live TOM
edits:

- `references/msfabric-modeling-guidelines.md` — dimensional modeling: tables,
  columns, relationships, date tables, hierarchies, performance, maintenance.
- `references/msfabric-dax-guidelines.md` — DAX coding patterns, variables,
  `DIVIDE`, and query syntax for measures.
- `references/msfabric-naming-conventions.md` — business-friendly naming rules
  (readable casing, no `FACT_`/`DIM_`, spell out words).
- `references/msfabric-semantic-model-ai-readiness.md` — descriptions, synonyms
  and metadata that make the model accurate for AI / Q&A.
- `references/msfabric-direct-lake-guidelines.md` — Direct Lake partitions and
  storage rules.
- `references/msfabric-semantic-model-consumption.md` — read-only DAX query and
  metadata discovery via `ExecuteQuery` / `INFO.VIEW.*` functions; use when
  validating measures or inspecting a live model.
- `references/msfabric-discovery-queries.md` — the INFO-function query catalog
  (scope estimation, projection/filtering, dependency discovery) that backs the
  consumption guidance.
- `references/msfabric-dax-perf-decision-guide.md` — performance-first decision
  guide for writing measures: when to use variables, iterators vs. aggregators,
  context transition, and filter-argument patterns. Use this whenever a measure
  could be slow or is on a hot path.
- `references/msfabric-dax-perf-patterns.md` — concrete high-performance DAX
  patterns (and the slow anti-patterns they replace) for common calculations.
  The DAX specialist and auditor should prefer these formulations.
