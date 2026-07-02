"""Best Practice Analyzer (BPA) rule catalog.

Loads Microsoft's official **Best Practice Rules** for tabular / semantic models
(``BPARules.json`` from the
`microsoft/Analysis-Services <https://github.com/microsoft/Analysis-Services>`_
repository) and exposes them to the rest of the app:

* the deterministic :mod:`app.intelligence.audit.bpa` auditor evaluates the
  statically-checkable subset of these rules against a
  :class:`~app.intelligence.spec.SemanticModelSpec`, and
* the AI design agent (:mod:`app.intelligence.agent`) folds a compact checklist
  of the design-time rules into its instructions so freshly crafted models
  follow the same guidance.

The rule *metadata* (id, name, category, description, severity) is the single
source of truth — it always comes from the downloaded JSON, never hard-coded —
so refreshing ``BPARules.json`` keeps both consumers in sync.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# Bundled copy of microsoft/Analysis-Services/BestPracticeRules/BPARules.json.
BPA_RULES_PATH = Path(__file__).resolve().parent / "resources" / "BPARules.json"

# Tabular Editor severity (1 = info, 2 = warning, 3 = error) → our audit levels.
# Literal strings (not imported from ``audit.base``) to keep this module free of
# any dependency on the ``audit`` package and avoid an import cycle.
_SEVERITY_TO_LEVEL = {1: "info", 2: "warning", 3: "error"}

# The design-time rules we can meaningfully influence when crafting a model from
# a SQL schema. Used to build the agent checklist; metadata is pulled from the
# catalog by id, so these stay in sync with the downloaded JSON.
AUTHORING_RULE_IDS: tuple[str, ...] = (
    "PROVIDE_FORMAT_STRING_FOR_MEASURES",
    "NUMERIC_COLUMN_SUMMARIZE_BY",
    "AVOID_FLOATING_POINT_DATA_TYPES",
    "HIDE_FOREIGN_KEYS",
    "MARK_PRIMARY_KEYS",
    "RELATIONSHIP_COLUMNS_SAME_DATA_TYPE",
    "RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE",
    "MODEL_SHOULD_HAVE_A_DATE_TABLE",
    "DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE",
    "USE_THE_DIVIDE_FUNCTION_FOR_DIVISION",
    "AVOID_USING_THE_IFERROR_FUNCTION",
    "USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT",
    "AVOID_DUPLICATE_MEASURES",
    "OBJECTS_WITH_NO_DESCRIPTION",
    "FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED",
    "OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE",
    "ENSURE_TABLES_HAVE_RELATIONSHIPS",
    "MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION",
)


def severity_to_level(severity: int) -> str:
    """Map a BPA numeric severity onto an audit level (defaults to WARNING)."""
    return _SEVERITY_TO_LEVEL.get(int(severity or 0), "warning")


@lru_cache(maxsize=1)
def load_bpa_rules() -> tuple[dict[str, Any], ...]:
    """Return the BPA rule catalog as an immutable tuple of dicts.

    Reads the bundled ``BPARules.json``. The file is tolerated to carry a UTF-8
    BOM (it ships with one). Returns an empty tuple if the file is missing so
    the auditor and agent degrade gracefully rather than crash.
    """
    try:
        text = BPA_RULES_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ()
    return tuple(r for r in data if isinstance(r, dict) and r.get("ID"))


@lru_cache(maxsize=1)
def rules_by_id() -> dict[str, dict[str, Any]]:
    """Return a mapping of rule ID → rule dict."""
    return {r["ID"]: r for r in load_bpa_rules()}


def rule(rule_id: str) -> dict[str, Any] | None:
    """Look up a single rule by its BPA ID."""
    return rules_by_id().get(rule_id)


def finding_code(rule_id: str) -> str:
    """Return a stable, machine-readable finding code for a BPA rule."""
    safe = "".join(c if c.isalnum() else "_" for c in rule_id.upper())
    # Collapse runs of underscores for readability.
    while "__" in safe:
        safe = safe.replace("__", "_")
    return "BPA_" + safe.strip("_")


def best_practice_checklist(rule_ids: tuple[str, ...] = AUTHORING_RULE_IDS) -> str:
    """Build a compact, prompt-friendly checklist of BPA rule names.

    Only rules present in the catalog are emitted (so a refreshed JSON or a
    missing file never produces dangling bullets). Returns an empty string when
    no rules are available.
    """
    catalog = rules_by_id()
    lines: list[str] = []
    for rid in rule_ids:
        r = catalog.get(rid)
        if r is None:
            continue
        # Names already carry a "[Category] " prefix in the catalog.
        lines.append(f"- {r.get('Name', rid)}")
    return "\n".join(lines)
