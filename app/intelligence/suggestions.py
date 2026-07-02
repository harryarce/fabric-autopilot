"""Suggestion IR — write-back proposals for semantic models and reports.

A :class:`SuggestionSpec` is a small, serialisable record describing one
proposed mutation to either a :class:`~app.intelligence.spec.SemanticModelSpec`
or a :class:`~app.intelligence.report_spec.ReportSpec`. Suggestions are produced
by the audit/remediation engines (:mod:`app.intelligence.audit.remediation`),
persisted alongside the artifact (so the user can resume an audit-review
workflow across sessions), and finally **applied** to a spec — yielding a new
spec that the service layer can hand back to Fabric via the ``updateDefinition``
endpoint.

Design goals
------------
* **Auditable**. Each suggestion remembers its provenance (``source``,
  ``rationale``, ``confidence``) and lifecycle (``status``: ``proposed`` →
  ``accepted`` | ``skipped`` → ``applied`` | ``failed``). The user is always in
  control: nothing mutates a published artifact unless a suggestion is both
  ``accepted`` and explicitly applied.
* **Format-agnostic**. The same dataclass covers both model and report fixes by
  encoding the change as ``(object_ref, field, proposed_value)``. The apply
  step dispatches on ``kind`` to the right spec mutator.
* **Deterministic by default**. The audit engine generates suggestions from
  pure-Python rules; the agent layer may enrich the bundle later by appending
  more suggestions with ``source="agent"``.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal

from .report_spec import ReportSpec, ReportTheme
from .spec import SemanticModelSpec

# Lifecycle states. ``proposed`` is the initial state; ``accepted`` and
# ``skipped`` are user choices; ``applied`` / ``failed`` reflect the outcome of
# the actual write-back attempt.
SuggestionStatus = Literal["proposed", "accepted", "skipped", "applied", "failed"]

# What the suggestion targets. Used to dispatch the apply step and to filter
# the UI's "Generate Fixes" tabs.
SuggestionKind = Literal[
    "model_usability",
    "model_copilot",
    "report_theme",
    "report_formatting",
]

_TERMINAL_STATUSES = {"applied", "skipped", "failed"}


@dataclass
class SuggestionSpec:
    """One write-back proposal targeting a single field on one object.

    Attributes:
        id: Stable, unique id (UUID4 by default) so the UI/API can address an
            individual suggestion even after the bundle is reloaded.
        kind: Which audit family produced this suggestion (and therefore which
            apply path it routes through).
        object_ref: A human-readable path to the target object, mirroring the
            ``AuditFinding.object_ref`` convention. Examples:
            ``"Sales"`` (table), ``"Sales[Amount]"`` (column),
            ``"Sales[Total Sales]"`` (measure), ``"page-1/visual-1"`` (visual),
            ``"theme:FabricDevAI"`` (the report theme).
        field: The property on the target object to mutate, in dotted form.
            Examples: ``"description"``, ``"format_string"``, ``"is_hidden"``,
            ``"display_folder"``, ``"data_category"``, ``"is_date_table"``,
            ``"title"``, ``"theme.foreground"``, ``"theme.background"``,
            ``"theme.data_colors"``.
        current_value: The value the audit observed (informational). May be
            ``None`` when the field is currently unset.
        proposed_value: The value the audit recommends. JSON-serialisable.
        rationale: One-line human explanation, surfaced in the review UI.
        confidence: ``[0, 1]`` heuristic confidence (1.0 for deterministic rules
            with no ambiguity).
        source: Provenance tag — ``deterministic``, ``agent``, or any custom
            label used by future remediation engines.
        code: The originating audit rule code (e.g. ``SM_USAB_NO_DESCRIPTION``)
            so the UI can group suggestions by rule.
        status: Current lifecycle state — see :data:`SuggestionStatus`.
        error: Populated when ``status == "failed"`` with the apply error
            message; ``None`` otherwise.
    """

    kind: SuggestionKind
    object_ref: str
    field: str
    proposed_value: Any
    rationale: str = ""
    current_value: Any = None
    confidence: float = 1.0
    source: str = "deterministic"
    code: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: SuggestionStatus = "proposed"
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None or k == "current_value"}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuggestionSpec":
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            kind=data["kind"],
            object_ref=data["object_ref"],
            field=data["field"],
            proposed_value=data.get("proposed_value"),
            current_value=data.get("current_value"),
            rationale=data.get("rationale", ""),
            confidence=float(data.get("confidence", 1.0)),
            source=data.get("source", "deterministic"),
            code=data.get("code", ""),
            status=data.get("status", "proposed"),
            error=data.get("error"),
        )


def suggestions_to_dict(suggestions: Iterable[SuggestionSpec]) -> dict[str, Any]:
    """Serialise a suggestion bundle for persistence."""
    return {"suggestions": [s.to_dict() for s in suggestions]}


def suggestions_from_dict(data: dict[str, Any]) -> list[SuggestionSpec]:
    """Rebuild a suggestion bundle from persisted JSON."""
    return [SuggestionSpec.from_dict(s) for s in (data or {}).get("suggestions", [])]


# ---------------------------------------------------------------------------
# Apply — semantic-model side
# ---------------------------------------------------------------------------


def _parse_object_ref(ref: str) -> tuple[str, str | None]:
    """Split ``"Table[Column]"`` into ``("Table", "Column")``.

    A bare ``"Table"`` returns ``("Table", None)``. Bracket-escapes (``]]``)
    are honoured so a column whose name contains ``]`` round-trips correctly.
    """
    if "[" not in ref:
        return ref, None
    head, _, tail = ref.partition("[")
    if not tail.endswith("]"):
        return ref, None
    inner = tail[:-1].replace("]]", "]")
    return head, inner


def _set_table_field(spec: SemanticModelSpec, table_name: str, field_name: str, value: Any) -> bool:
    table = spec.table(table_name)
    if table is None:
        return False
    if not hasattr(table, field_name):
        return False
    setattr(table, field_name, value)
    return True


def _set_column_field(
    spec: SemanticModelSpec, table_name: str, column_name: str, field_name: str, value: Any
) -> bool:
    table = spec.table(table_name)
    if table is None:
        return False
    column = table.column(column_name)
    if column is None:
        return False
    if not hasattr(column, field_name):
        return False
    setattr(column, field_name, value)
    return True


def _set_measure_field(
    spec: SemanticModelSpec, table_name: str, measure_name: str, field_name: str, value: Any
) -> bool:
    table = spec.table(table_name)
    if table is None:
        return False
    measure = next((m for m in table.measures if m.name == measure_name), None)
    if measure is None:
        return False
    if not hasattr(measure, field_name):
        return False
    setattr(measure, field_name, value)
    return True


def _parse_relationship_ref(ref: str) -> tuple[str, str, str, str] | None:
    """Split ``"From[col] -> To[col]"`` into the four relationship endpoints.

    Returns ``(from_table, from_column, to_table, to_column)`` or ``None`` when
    the reference is not a relationship reference.
    """
    if "->" not in ref:
        return None
    left, _, right = ref.partition("->")
    from_table, from_col = _parse_object_ref(left.strip())
    to_table, to_col = _parse_object_ref(right.strip())
    if from_col is None or to_col is None:
        return None
    return from_table, from_col, to_table, to_col


def _set_relationship_field(
    spec: SemanticModelSpec, ref: str, field_name: str, value: Any
) -> bool:
    parsed = _parse_relationship_ref(ref)
    if parsed is None:
        return False
    from_table, from_col, to_table, to_col = parsed
    rel = next(
        (
            r
            for r in spec.relationships
            if r.from_table == from_table
            and r.from_column == from_col
            and r.to_table == to_table
            and r.to_column == to_col
        ),
        None,
    )
    if rel is None or not hasattr(rel, field_name):
        return False
    setattr(rel, field_name, value)
    return True


def apply_model_suggestion(spec: SemanticModelSpec, suggestion: SuggestionSpec) -> bool:
    """Mutate ``spec`` in place to satisfy ``suggestion``; return success."""
    if suggestion.kind not in (
        "model_usability",
        "model_copilot",
        "model_bpa",
        "model_ai",
    ):
        return False

    field_name = suggestion.field
    if field_name == "model.description":
        spec.description = suggestion.proposed_value
        return True

    # Relationship-targeted fixes (e.g. cross-filter direction) use a
    # ``From[col] -> To[col]`` reference shape.
    if "->" in suggestion.object_ref:
        return _set_relationship_field(
            spec, suggestion.object_ref, field_name, suggestion.proposed_value
        )

    table_name, child = _parse_object_ref(suggestion.object_ref)
    if child is None:
        return _set_table_field(spec, table_name, field_name, suggestion.proposed_value)

    # Measures and columns share the ``Table[Name]`` reference shape. Try the
    # measure first when the field is measure-only; otherwise fall back to
    # column. This matches how the audit emits ``object_ref``: measure rules
    # only target existing measures, column rules only target existing columns.
    measure_only_fields = {"expression", "display_folder"}
    if field_name in measure_only_fields:
        return _set_measure_field(spec, table_name, child, field_name, suggestion.proposed_value)

    # Try measure first (description/format_string can be on either), then column.
    if _set_measure_field(spec, table_name, child, field_name, suggestion.proposed_value):
        return True
    return _set_column_field(spec, table_name, child, field_name, suggestion.proposed_value)


def apply_model_suggestions(
    spec: SemanticModelSpec, suggestions: Iterable[SuggestionSpec]
) -> tuple[SemanticModelSpec, list[SuggestionSpec]]:
    """Apply accepted suggestions to ``spec``; return ``(new_spec, updated_list)``.

    The input spec is round-tripped through ``to_dict``/``from_dict`` so the
    caller's instance is not mutated. Each suggestion's status is updated to
    ``applied`` (success) or ``failed`` (target missing). Suggestions not in
    ``accepted`` state are left untouched.
    """
    new_spec = SemanticModelSpec.from_dict(spec.to_dict())
    updated: list[SuggestionSpec] = []
    for suggestion in suggestions:
        if suggestion.status != "accepted":
            updated.append(suggestion)
            continue
        ok = apply_model_suggestion(new_spec, suggestion)
        suggestion.status = "applied" if ok else "failed"
        if not ok:
            suggestion.error = (
                f"Could not locate target '{suggestion.object_ref}' for field "
                f"'{suggestion.field}'."
            )
        updated.append(suggestion)
    return new_spec, updated


# ---------------------------------------------------------------------------
# Apply — report side
# ---------------------------------------------------------------------------


def _set_theme_field(theme: ReportTheme, field_name: str, value: Any) -> bool:
    # field_name is in dotted form: ``theme.foreground``, ``theme.background``,
    # ``theme.data_colors``, etc.
    _, _, attr = field_name.partition(".")
    if not attr or not hasattr(theme, attr):
        return False
    setattr(theme, attr, value)
    return True


def _set_visual_field(spec: ReportSpec, ref: str, field_name: str, value: Any) -> bool:
    # ``ref`` is ``"<page>/<visual>"``.
    page_name, _, visual_name = ref.partition("/")
    if not visual_name:
        return False
    page = spec.page(page_name)
    if page is None:
        return False
    visual = next((v for v in page.visuals if v.name == visual_name), None)
    if visual is None:
        return False
    if not hasattr(visual, field_name):
        return False
    setattr(visual, field_name, value)
    return True


def apply_report_suggestion(spec: ReportSpec, suggestion: SuggestionSpec) -> bool:
    """Mutate ``spec`` in place to satisfy ``suggestion``; return success."""
    if suggestion.kind not in ("report_theme", "report_formatting"):
        return False

    field_name = suggestion.field
    if field_name.startswith("theme."):
        if spec.theme is None:
            spec.theme = ReportTheme()
        return _set_theme_field(spec.theme, field_name, suggestion.proposed_value)

    if field_name == "report.description":
        spec.description = suggestion.proposed_value
        return True

    # Visual-level field. ``object_ref`` is ``"<page>/<visual>"``.
    return _set_visual_field(spec, suggestion.object_ref, field_name, suggestion.proposed_value)


def apply_report_suggestions(
    spec: ReportSpec, suggestions: Iterable[SuggestionSpec]
) -> tuple[ReportSpec, list[SuggestionSpec]]:
    """Apply accepted suggestions to ``spec``; return ``(new_spec, updated_list)``."""
    new_spec = ReportSpec.from_dict(spec.to_dict())
    updated: list[SuggestionSpec] = []
    for suggestion in suggestions:
        if suggestion.status != "accepted":
            updated.append(suggestion)
            continue
        ok = apply_report_suggestion(new_spec, suggestion)
        suggestion.status = "applied" if ok else "failed"
        if not ok:
            suggestion.error = (
                f"Could not locate target '{suggestion.object_ref}' for field "
                f"'{suggestion.field}'."
            )
        updated.append(suggestion)
    return new_spec, updated


__all__ = [
    "SuggestionSpec",
    "SuggestionStatus",
    "SuggestionKind",
    "suggestions_to_dict",
    "suggestions_from_dict",
    "apply_model_suggestion",
    "apply_model_suggestions",
    "apply_report_suggestion",
    "apply_report_suggestions",
]
