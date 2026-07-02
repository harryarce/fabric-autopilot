"""Ask-your-model page — natural language Q&A over a Fabric semantic model.

This page closes the loop on the platform's *agent-first* vision:

1. The user picks a Fabric workspace and a semantic model.
2. The page imports the model definition (using the existing artifact /
   ``model_service`` flow) so the NL→DAX translator can be grounded in the
   real table, column, and measure names.
3. The user types a question in natural language.
4. :func:`app.intelligence.nl_to_dax.nl_to_dax` turns it into a DAX query
   using deterministic, offline-safe heuristics.
5. The DAX is executed against the model through the **Power BI Modeling
   MCP** server's ``dax_query_operations`` tool (the connection itself is
   established with the same server's ``connection_operations`` tool).
6. The result table, the generated DAX, and the raw MCP tool transcript are
   displayed side by side so the user can audit every call.

No SQL endpoint, ODBC connection, or T-SQL is involved — the entire
pipeline goes through MCP operations against the semantic model.
"""

from __future__ import annotations

import html as _html
import json
from typing import Any

import pandas as pd
import streamlit as st

from app.artifacts import ArtifactRef
from app.fabric_client import FabricApiError
from app.intelligence import nl_to_dax
from app.intelligence.nl_to_dax import DaxTranslation, NlToDaxError
from app.intelligence.pbi_modeling_mcp import (
    PowerBiModelingMcp,
    PowerBiModelingMcpError,
    build_client,
)
from app.intelligence.tmdl_parser import TmdlParseError, parse_semantic_model
from app.ui import (
    artifact_store,
    fabric_client,
    page_intro,
    workspace_picker,
)

_STATE_CLIENT = "ask_pbi_mcp_client"
_STATE_CONNECTED = "ask_pbi_mcp_connected"
_STATE_HISTORY = "ask_pbi_mcp_history"
_STATE_LAST_TRANSLATION = "ask_pbi_mcp_last_translation"


@st.cache_data(ttl=120, show_spinner="Listing semantic models…")
def _list_models(workspace_id: str):
    return fabric_client().list_semantic_models(workspace_id)


def render() -> None:
    page_intro(
        "Ask Your Model",
        "Ask a question in natural language. The page converts it to DAX and "
        "runs it through the Power BI Modeling MCP server's "
        "`dax_query_operations` tool.",
    )

    workspace = workspace_picker(key="ask_ws")
    if workspace is None:
        return

    try:
        models = _list_models(workspace.id)
    except FabricApiError as exc:
        st.error(f"Could not list semantic models: {exc}")
        return
    if not models:
        st.info("This workspace has no semantic models.")
        return

    labels = {m.display_name: m for m in models}
    choice = st.selectbox("Semantic model", sorted(labels), key="ask_pick")
    model = labels[choice]

    ref = ArtifactRef(
        kind="semanticModels",
        workspace_id=workspace.id,
        item_id=model.id,
        workspace_name=workspace.name,
        item_name=model.display_name,
    )

    spec = _ensure_spec_loaded(ref, model.display_name)
    if spec is None:
        return

    _render_model_summary(spec)
    _render_connection_controls(workspace.name, model.display_name)
    _render_question_form(spec)
    _render_history()


# ---------------------------------------------------------------------------
# Model definition (grounding for NL→DAX)
# ---------------------------------------------------------------------------


def _ensure_spec_loaded(ref: ArtifactRef, display_name: str):
    """Make sure a parsed :class:`SemanticModelSpec` is available locally.

    If the model definition has not been imported yet we offer a one-click
    import — the NL→DAX translator needs the real table, column, and measure
    names to ground every query.
    """
    stored = artifact_store().load_definition(ref)
    if stored is None:
        st.warning(
            "The model definition has not been imported yet. Importing it "
            "lets the natural-language → DAX translator use real table and "
            "column names from your model."
        )
        if st.button("Import definition", type="primary", key="ask_import"):
            _import_model(ref)
            st.rerun()
        return None

    try:
        return parse_semantic_model(stored.files, name=display_name)
    except TmdlParseError as exc:
        st.error(f"Could not parse the model definition: {exc}")
        return None


def _import_model(ref: ArtifactRef) -> None:
    try:
        definition = fabric_client().get_semantic_model_definition(
            ref.workspace_id, ref.item_id, fmt="TMDL"
        )
    except FabricApiError as exc:
        st.error(f"Failed to fetch definition: {exc}")
        return
    artifact_store().save_definition(
        ref, definition.files, fmt=definition.format, source="imported"
    )
    st.toast(f"Imported '{ref.item_name}'.")


def _render_model_summary(spec) -> None:
    """Compact snapshot of the model so users know what they can ask about."""
    cols = st.columns(3)
    cols[0].metric("Tables", len(spec.tables))
    cols[1].metric("Columns", sum(len(t.columns) for t in spec.tables))
    cols[2].metric("Measures", sum(len(t.measures) for t in spec.tables))
    with st.expander("Model surface (tables, columns, measures)", expanded=False):
        for table in spec.tables:
            measure_names = ", ".join(m.name for m in table.measures) or "—"
            column_names = ", ".join(c.name for c in table.columns) or "—"
            st.markdown(
                f"**{_html.escape(table.name)}** "
                f"<span style='color:#64748b'>· {len(table.columns)} cols · "
                f"{len(table.measures)} measures</span>",
                unsafe_allow_html=True,
            )
            st.caption(f"Columns: {column_names}")
            if table.measures:
                st.caption(f"Measures: {measure_names}")


# ---------------------------------------------------------------------------
# MCP connection lifecycle
# ---------------------------------------------------------------------------


def _get_client() -> PowerBiModelingMcp | None:
    """Return the cached :class:`PowerBiModelingMcp` for this Streamlit session.

    The client owns a background event loop and a long-running stdio
    subprocess, so we lazily build one per Streamlit session and keep it
    alive across reruns.
    """
    client = st.session_state.get(_STATE_CLIENT)
    if client is not None:
        return client
    try:
        client = build_client()
    except Exception as exc:  # noqa: BLE001 - surface auth/launch failures
        st.error(f"Could not start the Power BI Modeling MCP server: {exc}")
        return None
    st.session_state[_STATE_CLIENT] = client
    return client


def _render_connection_controls(workspace_name: str, model_name: str) -> None:
    st.subheader("MCP connection")
    connected_key = (
        st.session_state.get(_STATE_CONNECTED, {}).get("workspace"),
        st.session_state.get(_STATE_CONNECTED, {}).get("model"),
    )
    is_connected = connected_key == (workspace_name, model_name)

    col_status, col_action = st.columns([3, 1])
    with col_status:
        if is_connected:
            st.success(
                f"Connected to `{workspace_name}` / `{model_name}` via "
                "`connection_operations.ConnectFabric`."
            )
        else:
            st.info(
                "Not connected yet. The first query will trigger "
                "`connection_operations.ConnectFabric` automatically — "
                "or you can connect now."
            )
    with col_action:
        if st.button("Connect", key="ask_connect", disabled=is_connected):
            _connect(workspace_name, model_name)


def _connect(workspace_name: str, model_name: str) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        with st.spinner("Connecting Power BI Modeling MCP server to the model…"):
            call = client.connect_to_fabric_model(workspace_name, model_name)
    except PowerBiModelingMcpError as exc:
        st.error(str(exc))
        return
    st.session_state[_STATE_CONNECTED] = {
        "workspace": workspace_name,
        "model": model_name,
    }
    _append_history(
        question="(connect)",
        translation=None,
        calls=[call.to_dict()],
        columns=[],
        rows=[],
        error=None,
    )
    st.rerun()


# ---------------------------------------------------------------------------
# Question form
# ---------------------------------------------------------------------------


def _render_question_form(spec) -> None:
    st.subheader("Ask a question")

    examples = _example_questions(spec)
    if examples:
        st.caption("Try one of these:")
        cols = st.columns(min(3, len(examples)))
        for i, example in enumerate(examples):
            if cols[i % len(cols)].button(example, key=f"ask_example_{i}"):
                st.session_state["ask_question_text"] = example

    question = st.text_area(
        "Your question",
        key="ask_question_text",
        height=80,
        placeholder="e.g. Top 10 customers by total sales",
    )
    run_clicked = st.button(
        "Translate & run",
        type="primary",
        key="ask_run",
        disabled=not (question or "").strip(),
    )
    if not run_clicked:
        return

    workspace_name = st.session_state.get(_STATE_CONNECTED, {}).get("workspace")
    model_name = st.session_state.get(_STATE_CONNECTED, {}).get("model")
    current_workspace = st.session_state.get("ask_ws")
    current_model = st.session_state.get("ask_pick")
    needs_connect = (workspace_name, model_name) != (current_workspace, current_model)

    _handle_question(
        spec=spec,
        question=question.strip(),
        workspace_name=current_workspace,
        model_name=current_model,
        needs_connect=needs_connect,
    )


def _example_questions(spec) -> list[str]:
    """A few NL prompts grounded in the actual spec, to seed exploration."""
    examples: list[str] = []
    if not spec.tables:
        return examples
    first = spec.tables[0]
    examples.append(f"How many rows are in {first.name}?")
    examples.append(f"Show top 10 rows from {first.name}")
    for table in spec.tables:
        if table.measures:
            measure = table.measures[0]
            group = next(
                (c.name for c in table.columns if not c.is_hidden),
                None,
            )
            if group:
                examples.append(f"Top 10 {group} by {measure.name}")
            else:
                examples.append(f"What is {measure.name}?")
            break
    return examples[:3]


# ---------------------------------------------------------------------------
# End-to-end NL → DAX → MCP execution
# ---------------------------------------------------------------------------


def _handle_question(
    *,
    spec,
    question: str,
    workspace_name: str,
    model_name: str,
    needs_connect: bool,
) -> None:
    try:
        translation = nl_to_dax(question, spec)
    except NlToDaxError as exc:
        st.error(str(exc))
        return
    st.session_state[_STATE_LAST_TRANSLATION] = translation

    client = _get_client()
    if client is None:
        return

    calls: list[dict[str, Any]] = []
    error: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []

    try:
        if needs_connect:
            with st.spinner("Connecting via `connection_operations`…"):
                connect_call = client.connect_to_fabric_model(
                    workspace_name, model_name
                )
            calls.append(connect_call.to_dict())
            st.session_state[_STATE_CONNECTED] = {
                "workspace": workspace_name,
                "model": model_name,
            }
        with st.spinner("Executing DAX via `dax_query_operations`…"):
            result = client.execute_dax(translation.dax)
        calls.extend(c.to_dict() for c in result.calls)
        columns = result.columns
        rows = result.rows
    except PowerBiModelingMcpError as exc:
        error = str(exc)

    _append_history(
        question=question,
        translation=translation,
        calls=calls,
        columns=columns,
        rows=rows,
        error=error,
    )
    st.rerun()


# ---------------------------------------------------------------------------
# History rendering
# ---------------------------------------------------------------------------


def _append_history(
    *,
    question: str,
    translation: DaxTranslation | None,
    calls: list[dict[str, Any]],
    columns: list[str],
    rows: list[list[Any]],
    error: str | None,
) -> None:
    history: list[dict[str, Any]] = st.session_state.setdefault(_STATE_HISTORY, [])
    history.insert(
        0,
        {
            "question": question,
            "translation": translation,
            "calls": calls,
            "columns": columns,
            "rows": rows,
            "error": error,
        },
    )
    # Cap the in-memory transcript so long sessions don't accumulate forever.
    del history[25:]


def _render_history() -> None:
    history: list[dict[str, Any]] = st.session_state.get(_STATE_HISTORY, [])
    if not history:
        return
    st.subheader("Results")
    for i, entry in enumerate(history):
        with st.expander(
            f"{i + 1}. {entry['question']}",
            expanded=i == 0,
        ):
            _render_entry(entry)


def _render_entry(entry: dict[str, Any]) -> None:
    translation: DaxTranslation | None = entry.get("translation")
    if translation is not None:
        cols = st.columns([3, 1])
        with cols[0]:
            st.markdown(f"**Intent:** `{translation.intent}`")
            st.caption(translation.explanation)
            if translation.warnings:
                for warn in translation.warnings:
                    st.warning(warn)
        with cols[1]:
            st.metric("Confidence", translation.confidence.title())
        st.code(translation.dax, language="dax")

    error = entry.get("error")
    if error:
        st.error(error)

    rows = entry.get("rows") or []
    columns = entry.get("columns") or []
    if rows:
        df = pd.DataFrame(rows, columns=columns or None)
        st.dataframe(df, use_container_width=True)
    elif not error and translation is not None:
        st.caption("Query returned no rows.")

    calls = entry.get("calls") or []
    if calls:
        with st.expander(
            f"MCP tool transcript ({len(calls)} call(s))", expanded=False
        ):
            for call in calls:
                _render_call(call)


def _render_call(call: dict[str, Any]) -> None:
    tool = call.get("tool", "?")
    is_error = call.get("is_error", False)
    badge = "🔴 error" if is_error else "🟢 ok"
    st.markdown(f"**`{tool}`** — {badge}")
    args = call.get("arguments") or {}
    st.caption("Arguments")
    st.code(json.dumps(args, indent=2, default=str), language="json")
    text = call.get("text") or ""
    if text:
        st.caption("Response (text)")
        # Truncate very large payloads so the transcript stays readable; the
        # full raw value is still kept in session state for debugging.
        preview = text if len(text) <= 4000 else text[:4000] + "\n…[truncated]"
        st.code(preview, language="json")
    structured = call.get("structured")
    if structured is not None:
        st.caption("Response (structured)")
        st.json(structured, expanded=False)
