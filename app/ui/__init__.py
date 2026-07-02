"""Reusable UI building blocks for the multipage Streamlit app.

These helpers are intentionally thin and Streamlit-specific; all real logic
lives in the deterministic, Streamlit-independent modules under
``app.intelligence`` and ``app.artifacts``. Keeping the pages declarative makes
the underlying functionality easy to lift into a customer-facing package.
"""

from __future__ import annotations

import streamlit as st

from app.artifacts import ArtifactStore, get_artifact_store
from app.auth import get_token_provider
from app.fabric_client import FabricClient, Workspace
from app.intelligence.audit.base import AuditReport
from app.intelligence.suggestions import SuggestionSpec


@st.cache_resource(show_spinner=False)
def fabric_client() -> FabricClient:
    """A process-wide Fabric client (fresh token acquired per request)."""
    return FabricClient(get_token_provider())


@st.cache_resource(show_spinner=False)
def artifact_store() -> ArtifactStore:
    """The configured artifact store (local by default)."""
    return get_artifact_store()


@st.cache_data(ttl=300, show_spinner="Loading workspaces…")
def load_workspaces() -> list[Workspace]:
    return fabric_client().list_workspaces()


def workspace_picker(key: str) -> Workspace | None:
    """Render a workspace selectbox and return the chosen workspace."""
    try:
        workspaces = load_workspaces()
    except Exception as exc:  # noqa: BLE001 - surface any auth/API error inline
        st.error(f"Could not load workspaces: {exc}")
        return None
    if not workspaces:
        st.info("No accessible workspaces found.")
        return None
    labels = {ws.name: ws for ws in workspaces}
    choice = st.selectbox("Workspace", sorted(labels), key=key)
    return labels.get(choice)


def render_audit_report(report: AuditReport) -> None:
    """Render an :class:`AuditReport` as a scorecard plus a findings table."""
    cols = st.columns(4)
    cols[0].metric("Score", f"{report.score}/100")
    cols[1].metric("Errors", len(report.errors))
    cols[2].metric("Warnings", len(report.warnings))
    cols[3].metric("Info", len(report.infos))

    if not report.findings:
        st.success("No issues found.")
        return

    import html as _html

    severity_colors = {
        "error": "#d13438",
        "warning": "#c19c00",
        "info": "#0078d4",
    }
    header_cells = "".join(
        f"<th style='text-align:left;padding:6px 10px;border-bottom:2px solid "
        f"rgba(128,128,128,0.4);white-space:nowrap;'>{col}</th>"
        for col in ("Severity", "Code", "Object", "Finding", "Recommendation")
    )
    body_rows = []
    for f in report.sorted_findings():
        sev = (f.severity or "").lower()
        color = severity_colors.get(sev, "inherit")
        cells = [
            f"<td style='padding:6px 10px;vertical-align:top;white-space:nowrap;"
            f"font-weight:600;color:{color};'>{_html.escape(f.severity or '')}</td>",
            f"<td style='padding:6px 10px;vertical-align:top;white-space:nowrap;'>"
            f"<code>{_html.escape(f.code or '')}</code></td>",
            f"<td style='padding:6px 10px;vertical-align:top;white-space:normal;'>"
            f"{_html.escape(f.object_ref or '')}</td>",
            f"<td style='padding:6px 10px;vertical-align:top;white-space:normal;'>"
            f"{_html.escape(f.message or '')}</td>",
            f"<td style='padding:6px 10px;vertical-align:top;white-space:normal;'>"
            f"{_html.escape(f.recommendation or '')}</td>",
        ]
        body_rows.append(
            "<tr style='border-bottom:1px solid rgba(128,128,128,0.2);'>"
            + "".join(cells)
            + "</tr>"
        )
    table_html = (
        "<table style='width:100%;border-collapse:collapse;table-layout:fixed;"
        "font-size:0.875rem;'>"
        "<colgroup>"
        "<col style='width:8%'><col style='width:16%'><col style='width:14%'>"
        "<col style='width:31%'><col style='width:31%'>"
        "</colgroup>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _suggestion_category(suggestion: SuggestionSpec) -> str:
    """Map a suggestion to a friendly, high-level grouping label."""
    code = (suggestion.code or "").upper()
    if code.startswith("SM_USAB"):
        return "Usability"
    if code.startswith("SM_COPILOT"):
        return "Copilot readiness"
    if code.startswith("RPT_FMT") or suggestion.kind in (
        "report_theme",
        "report_formatting",
    ):
        return "Formatting & accessibility"
    return "Other changes"


def _format_proposed(value) -> str:
    """Render a proposed value compactly for a checkbox label."""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    elif isinstance(value, bool):
        text = "Yes" if value else "No"
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= 90 else text[:87] + "…"


def _suggestion_label(suggestion: SuggestionSpec) -> str:
    """A concise, scannable one-line description of a single change."""
    field = suggestion.field.split(".")[-1].replace("_", " ")
    source = " · 🧠 AI" if suggestion.source == "agent" else ""
    return (
        f"`{suggestion.object_ref}` — set **{field}** → "
        f"{_format_proposed(suggestion.proposed_value)}{source}"
    )


def suggestion_selector(
    suggestions: list[SuggestionSpec], *, key_prefix: str
) -> list[SuggestionSpec]:
    """Render a friendly, selectable checklist of write-back suggestions.

    Provides *Select all* / *Clear all* controls, a live selected-count, and
    groups changes by category for scannability. Returns the subset the user
    has ticked.

    Checkbox state is keyed by the stable :attr:`SuggestionSpec.id`, so the
    caller MUST persist the suggestion list (e.g. in ``st.session_state``)
    across reruns — regenerating it on every run would reset the ids and lose
    the user's selection.
    """
    if not suggestions:
        st.info("No automated fixes are available for this audit.")
        return []

    # Seed each checkbox's state (default: selected) *before* the widgets are
    # instantiated, so the Select all / Clear all buttons can mutate it without
    # tripping Streamlit's "set after widget creation" guard.
    for s in suggestions:
        st.session_state.setdefault(f"{key_prefix}_cb_{s.id}", True)

    ctrl = st.columns([1, 1, 3])
    if ctrl[0].button("Select all", key=f"{key_prefix}_all", use_container_width=True):
        for s in suggestions:
            st.session_state[f"{key_prefix}_cb_{s.id}"] = True
        st.rerun()
    if ctrl[1].button("Clear all", key=f"{key_prefix}_none", use_container_width=True):
        for s in suggestions:
            st.session_state[f"{key_prefix}_cb_{s.id}"] = False
        st.rerun()
    selected_count = sum(
        1 for s in suggestions if st.session_state.get(f"{key_prefix}_cb_{s.id}")
    )
    ctrl[2].markdown(
        f"**{selected_count}** of **{len(suggestions)}** change(s) selected"
    )

    groups: dict[str, list[SuggestionSpec]] = {}
    for s in suggestions:
        groups.setdefault(_suggestion_category(s), []).append(s)

    selected: list[SuggestionSpec] = []
    for category, items in groups.items():
        with st.expander(f"{category} · {len(items)} change(s)", expanded=True):
            for s in items:
                if st.checkbox(
                    _suggestion_label(s),
                    key=f"{key_prefix}_cb_{s.id}",
                    help=s.rationale or None,
                ):
                    selected.append(s)
    return selected


def page_intro(title: str, subtitle: str) -> None:
    st.title(title)
    st.caption(subtitle)