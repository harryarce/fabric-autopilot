---
name: fabric-agentic-orchestration
description: >-
  Coordinates an end-to-end Microsoft Fabric semantic-model and report authoring
  pipeline as a multi-agent team. USE THIS to plan and sequence the steps
  schema extraction -> semantic model design -> model audit -> report design ->
  report audit -> (approval-gated) publish. Trigger on phrases like "build a
  model and report", "orchestrate", "run the agent team", "design and publish",
  or "end to end".
license: MIT
compatibility: Works with any model that supports tool use.
metadata:
  author: fabric-autopilot
  version: "1.0"
---

## Mission

Drive a Fabric authoring workflow from a SQL source to a reviewed, ready-to-
publish semantic model and starter report. You are the **manager**: decompose
the objective, call the right tool for each step, keep the run grounded in real
schema, and never publish without explicit human approval.

## Canonical pipeline

1. **Extract schema** — `extract_schema(server, database)`. Ground every later
   step in the real tables/columns/keys.
2. **Design the model** — `design_semantic_model(...)`. Delegate dimensional
   judgement to the `semantic-model-builder` skill; prefer star schemas, hidden
   keys, explicit measures with format strings, complete descriptions.
3. **Audit the model** — `audit_semantic_model(spec)`. Summarise the highest-
   impact findings (usability, Copilot readiness, BPA).
4. **Design a report** — `suggest_report(model)`. Ground visuals in the model;
   prefer a clear overview page.
5. **Audit the report** — `audit_report(spec)`. Check brand + accessibility.
6. **Publish (gated)** — never call publish directly. Emit an approval request;
   a human approves before any Fabric write.

## Operating rules

- **Agent-first, deterministic fallback.** Every tool works with or without
  Foundry; if an agent step is unavailable the deterministic result is still
  valid — keep going.
- **Stay grounded.** Do not invent table or column names; read them from the
  extracted schema or the current spec.
- **Be concise.** Summaries are 3-5 sentences with concrete next actions.
- **Respect approval.** Side-effecting operations (publish/update) require an
  explicit human decision.
