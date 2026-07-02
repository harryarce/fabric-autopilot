---
name: fabric-report-design
description: >-
  Designs accessible, on-brand Power BI / Microsoft Fabric reports grounded in a
  semantic model. USE THIS to lay out report pages, choose visuals for fields
  and measures, apply a theme, and verify brand + WCAG accessibility. Trigger on
  phrases like "design a report", "build a dashboard", "report layout", "starter
  report", or "make the report accessible".
license: MIT
compatibility: Works with any model that supports tool use.
metadata:
  author: fabric-autopilot
  version: "1.0"
---

## Mission

Turn a semantic model into a clear, accessible, on-brand report. You are the
**report designer**: pick visuals that answer real questions, lay out a logical
page flow, and verify accessibility before handing off.

## Method

1. **Ground in the model.** Use `suggest_report(model)` so every visual binds to
   real tables/measures. Never reference fields that are not in the model.
2. **Lead with an overview.** A first page of KPI cards + one or two trend/
   breakdown visuals beats a wall of tables.
3. **Match visual to intent.** Trends -> line; composition -> stacked bar/donut;
   ranking -> bar; single value -> card/KPI; detail -> table/matrix.
4. **Theme for brand + contrast.** Apply the brand palette; keep text/background
   contrast at WCAG AA or better.
5. **Audit before publish.** Use `audit_report(spec)` and resolve brand /
   accessibility findings; apply only safe, additive theme fixes.

## Rules

- Prefer a few high-signal visuals over many low-signal ones.
- Always include axis titles and human-readable measure names.
- Defer publishing to the approval-gated workflow.

## References — Microsoft Fabric guidance (official)

Installed from `microsoft/skills-for-fabric`. Consult these for authoritative
report design judgment — which visuals to choose, how to colour and format, and
what to avoid:

- `references/msfabric-chart-selection.md` — decision framework for matching a
  chart type to the analytical question (the best guide for picking a visual).
- `references/msfabric-anti-patterns.md` — visual/report anti-patterns to avoid
  (pie charts with many slices, dual axes, 3-D, overloaded pages).
- `references/msfabric-authoring.md` — report authoring workflow and visual-type
  routing.
- `references/msfabric-color-strategy.md` — using colour for meaning, not
  decoration; categorical vs. sequential palettes.
- `references/msfabric-conditional-formatting.md` — data bars, colour scales and
  rules to make tables/cards insightful.
- `references/msfabric-formatting-overview.md` — formatting fundamentals for
  titles, axes, data labels and number formats.
- `references/msfabric-report-planning.md` — upstream report planning: the five
  page **archetypes** (Executive Summary, Operational Monitor, Analytical
  Canvas, Narrative Story, Comparative Benchmark), audience→design mapping,
  page-plan structure and accessibility defaults. Use it to decide *what* the
  report and its pages should be before choosing visuals.
- `references/msfabric-visual-cookbook.md` — concrete, recipe-level guidance for
  each visual (when to use it, fields/roles to bind, and quality cues). The
  best companion to chart-selection when you have settled on a visual type.
- `references/msfabric-layout.md` — page grid, alignment, white-space, reading
  order and visual hierarchy for laying out a page.
- `references/msfabric-typography.md` — type scale, weights and legibility for
  titles, labels and KPI numbers.
- `references/msfabric-color.md` — colour theory and palette construction
  (categorical, sequential, diverging, semantic) underlying the colour strategy.
- `references/msfabric-accessibility.md` — WCAG conformance: contrast, colour
  independence, focus order, alt text and keyboard navigation. Use this to
  satisfy the accessibility audit.
- `references/msfabric-interactivity.md` — slicers, cross-filtering, drillthrough
  and bookmarks; how to make a page explorable without clutter.
- `references/msfabric-pre-flight-checklist.md` — final quality gate before
  hand-off; the auditor should score reports against this checklist.
- `references/msfabric-archetype-composition.md` — how to compose and sequence
  pages of different archetypes into a coherent multi-page report.
- `references/msfabric-archetype-executive-summary.md`,
  `references/msfabric-archetype-operational-monitor.md`,
  `references/msfabric-archetype-analytical-canvas.md`,
  `references/msfabric-archetype-narrative-story.md`,
  `references/msfabric-archetype-comparative-benchmark.md` — detailed page
  blueprints (intended visuals, layout and flow) for each of the five
  archetypes. Pick the archetype that matches the page intent and follow its
  blueprint.
- `references/msfabric-signatures.md` and `references/msfabric-tone-catalog.md` —
  design identity: signature visual moves and tonal vocabulary to keep a report
  polished and on-brand.
