"""Fabric SQL Explorer — a Streamlit UI.

Run with::

    streamlit run app/streamlit_app.py

The app walks the user through a guided flow:

1. Browse the Fabric workspaces they can access.
2. Select a workspace.
3. List its SQL analytics endpoints / warehouses.
4. Retrieve a connection string.
5. Open a token-authenticated connection.
6. Extract detailed table & view schemas.
"""

from __future__ import annotations

import html
import json
import os
import sys
from dataclasses import dataclass

# Allow ``streamlit run app/streamlit_app.py`` to resolve the ``app`` package
# regardless of the current working directory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pandas as pd
import streamlit as st

from app.auth import AuthError, get_token_provider
from app.exporters import EXPORT_FORMATS, export
from app.fabric_client import (
    FabricApiError,
    FabricClient,
    Lakehouse,
    LakehouseTable,
    SqlEndpoint,
    Workspace,
)
from app.sql_client import (
    SqlClientError,
    SqlEndpointClient,
    TableSchema,
    available_odbc_drivers,
)

# ---------------------------------------------------------------------------
# Page configuration & light theming
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Fabric SQL Explorer",
    page_icon="SQL",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        :root {
            --fd-primary: #0078d4;
            --fd-border: rgba(148, 163, 184, 0.28);
            --fd-surface: rgba(148, 163, 184, 0.08);
            --fd-muted: #64748b;
            --fd-teal-bg: rgba(20, 184, 166, 0.14);
            --fd-teal: #0f766e;
            --fd-amber-bg: rgba(245, 158, 11, 0.16);
            --fd-amber: #92400e;
            --fd-blue-bg: rgba(0, 120, 212, 0.13);
            --fd-green-bg: rgba(22, 163, 74, 0.14);
            --fd-green: #15803d;
            --fd-red-bg: rgba(220, 38, 38, 0.13);
            --fd-red: #b91c1c;
        }
        .fd-subtitle { color: var(--fd-muted); margin-top: -0.75rem; }
        .fd-workflow {
            border: 1px solid var(--fd-border);
            border-radius: 8px;
            padding: 0.75rem;
            margin: 0.75rem 0 1.25rem 0;
            background: var(--fd-surface);
        }
        .fd-workflow-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 0.5rem;
        }
        .fd-step {
            border: 1px solid var(--fd-border);
            border-radius: 8px;
            padding: 0.65rem 0.75rem;
            background: rgba(255,255,255,0.02);
            min-height: 72px;
        }
        .fd-step.active { border-color: var(--fd-primary); box-shadow: inset 0 0 0 1px var(--fd-primary); }
        .fd-step.done { background: var(--fd-green-bg); border-color: rgba(22, 163, 74, 0.28); }
        .fd-step.pending { opacity: 0.72; }
        .fd-step-index { color: var(--fd-muted); font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }
        .fd-step-title { font-size: 0.9rem; font-weight: 700; margin-top: 0.15rem; }
        .fd-step-note { color: var(--fd-muted); font-size: 0.76rem; margin-top: 0.15rem; }
        .fd-badge {
            display:inline-block;
            padding: 0.12rem 0.5rem;
            border-radius: 999px;
            font-size: 0.72rem;
            font-weight: 700;
            line-height: 1.35;
            border: 1px solid transparent;
            white-space: nowrap;
        }
        .fd-badge.blue { background: var(--fd-blue-bg); color: var(--fd-primary); border-color: rgba(0,120,212,0.22); }
        .fd-badge.green { background: var(--fd-green-bg); color: var(--fd-green); border-color: rgba(22,163,74,0.22); }
        .fd-badge.amber { background: var(--fd-amber-bg); color: var(--fd-amber); border-color: rgba(245,158,11,0.24); }
        .fd-badge.red { background: var(--fd-red-bg); color: var(--fd-red); border-color: rgba(220,38,38,0.22); }
        .fd-badge.teal { background: var(--fd-teal-bg); color: var(--fd-teal); border-color: rgba(20,184,166,0.24); }
        .fd-badge.neutral { background: rgba(100,116,139,0.12); color: var(--fd-muted); border-color: rgba(100,116,139,0.24); }
        .fd-card {
            border: 1px solid var(--fd-border);
            border-radius: 8px;
            padding: 0.8rem 0.95rem;
            background: rgba(255,255,255,0.02);
            margin: 0.5rem 0;
        }
        .fd-card-title { font-weight: 700; margin-bottom: 0.15rem; }
        .fd-card-meta { color: var(--fd-muted); font-size: 0.82rem; }
        div[data-testid="stMetricValue"] { font-size:1.35rem; }
        @media (max-width: 900px) {
            .fd-workflow-grid { grid-template-columns: 1fr; }
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Cached resources & data loaders
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _fabric_client() -> FabricClient:
    return FabricClient(get_token_provider())


@st.cache_resource(show_spinner=False)
def _sql_client() -> SqlEndpointClient:
    return SqlEndpointClient(get_token_provider())


@st.cache_data(ttl=300, show_spinner="Loading workspaces…")
def _load_workspaces() -> list[Workspace]:
    return _fabric_client().list_workspaces()


@st.cache_data(ttl=300, show_spinner="Discovering SQL endpoints…")
def _load_endpoints(workspace_id: str) -> list[SqlEndpoint]:
    return _fabric_client().list_sql_endpoints(workspace_id)


@st.cache_data(ttl=300, show_spinner="Discovering lakehouses…")
def _load_lakehouses(workspace_id: str) -> list[Lakehouse]:
    return _fabric_client().list_lakehouses(workspace_id)


@st.cache_data(ttl=300, show_spinner="Listing lakehouse tables…")
def _load_lakehouse_tables(
    workspace_id: str, lakehouse_id: str
) -> list[LakehouseTable]:
    return _fabric_client().list_lakehouse_tables(workspace_id, lakehouse_id)


@st.cache_data(ttl=120, show_spinner="Extracting schemas…")
def _load_schemas(server: str, database: str) -> list[TableSchema]:
    return _sql_client().get_schemas(server, database)


# ---------------------------------------------------------------------------
# Session-state helpers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "workspace": None,  # type: Workspace | None
    "source_kind": "sql",  # type: str  ("sql" | "lakehouse")
    "endpoint": None,  # type: SqlEndpoint | None
    "lakehouse": None,  # type: Lakehouse | None
    "lakehouse_tables": None,  # type: list[LakehouseTable] | None
    "schemas": None,  # type: list[TableSchema] | None
    "selected_objects": None,  # type: set[str] | None  (full_name values)
    "semantic_definition": None,  # type: SemanticModelDefinition | None
    "semantic_spec": None,  # type: SemanticModelSpec | None
    "semantic_suggestions": None,  # type: SemanticModelSuggestions | None
    "semantic_suggestions_status": None,  # type: dict | None
}


def _init_state() -> None:
    for key, value in DEFAULTS.items():
        st.session_state.setdefault(key, value)


def _reset_from(level: str) -> None:
    """Clear downstream selections when an upstream choice changes."""
    if level in ("workspace",):
        st.session_state.endpoint = None
        st.session_state.lakehouse = None
        st.session_state.lakehouse_tables = None
        st.session_state.schemas = None
        st.session_state.selected_objects = None
    if level in ("source",):
        # Switching between SQL endpoint and lakehouse invalidates the
        # current data source and everything derived from it.
        st.session_state.endpoint = None
        st.session_state.lakehouse = None
        st.session_state.lakehouse_tables = None
        st.session_state.schemas = None
        st.session_state.selected_objects = None
    if level in ("endpoint", "lakehouse"):
        st.session_state.schemas = None
        st.session_state.selected_objects = None
    # Generated artifacts depend on the selection; always invalidate them.
    st.session_state.semantic_definition = None
    st.session_state.semantic_spec = None
    st.session_state.semantic_suggestions = None
    st.session_state.semantic_suggestions_status = None


# ---------------------------------------------------------------------------
# Active data source — unifies SQL endpoint and lakehouse handling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ActiveSource:
    """A normalized view over the selected SQL endpoint or lakehouse.

    Both source kinds ultimately read columns through a SQL analytics
    endpoint (pyodbc). The lakehouse additionally carries the OneLake binding
    needed to author a Direct Lake on OneLake semantic model.
    """

    kind: str  # "sql" | "lakehouse"
    name: str
    server: str
    database: str
    item_kind: str
    endpoint: SqlEndpoint | None = None
    lakehouse: Lakehouse | None = None

    @property
    def is_lakehouse(self) -> bool:
        return self.kind == "lakehouse"

    def as_export_endpoint(self) -> SqlEndpoint:
        """Return a ``SqlEndpoint`` view usable by the schema exporters."""
        if self.endpoint is not None:
            return self.endpoint
        return SqlEndpoint(
            id=(self.lakehouse.id if self.lakehouse else ""),
            name=self.name,
            item_kind=self.item_kind,
            server=self.server,
            database=self.database,
            workspace_id=(self.lakehouse.workspace_id if self.lakehouse else ""),
            raw={},
        )


def _active_source() -> _ActiveSource | None:
    """Resolve the currently selected data source from session state."""
    kind = st.session_state.get("source_kind", "sql")
    if kind == "lakehouse":
        lh: Lakehouse | None = st.session_state.get("lakehouse")
        if lh is None or not lh.sql_endpoint_server:
            return None
        return _ActiveSource(
            kind="lakehouse",
            name=lh.name,
            server=lh.sql_endpoint_server,
            database=lh.name,
            item_kind="Lakehouse",
            lakehouse=lh,
        )
    ep: SqlEndpoint | None = st.session_state.get("endpoint")
    if ep is None:
        return None
    return _ActiveSource(
        kind="sql",
        name=ep.name,
        server=ep.server,
        database=ep.database,
        item_kind=ep.item_kind,
        endpoint=ep,
    )


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def _badge(label: str, tone: str = "neutral") -> str:
    """Return a compact HTML badge for status/source/confidence metadata."""
    safe = html.escape(str(label))
    return f'<span class="fd-badge {tone}">{safe}</span>'


def _confidence_tone(confidence: float) -> str:
    if confidence >= 0.85:
        return "green"
    if confidence >= 0.7:
        return "amber"
    return "neutral"


def _source_tone(source: str) -> str:
    return {
        "agent": "blue",
        "deterministic": "neutral",
        "fk": "green",
        "pk-match": "teal",
        "suffix": "amber",
    }.get(source, "neutral")


def _workflow_steps() -> list[tuple[str, str, str, bool]]:
    """Return workflow steps as ``(title, note, state, completed)`` tuples."""
    has_workspace = st.session_state.workspace is not None
    has_source = _active_source() is not None
    has_schemas = st.session_state.schemas is not None
    has_objects = has_schemas and bool(st.session_state.selected_objects)
    has_model = st.session_state.semantic_definition is not None
    states = [has_workspace, has_source, has_objects, has_model, False]
    active_idx = next((i for i, done in enumerate(states) if not done), 4)
    data = [
        ("Workspace", "Choose a Fabric workspace"),
        ("Source", "Pick a SQL endpoint or lakehouse"),
        ("Objects", "Extract and select schema"),
        ("Model", "Generate and review"),
        ("Create", "Publish to Fabric"),
    ]
    steps: list[tuple[str, str, str, bool]] = []
    for idx, (title, note) in enumerate(data):
        completed = states[idx]
        if completed:
            state = "done"
        elif idx == active_idx:
            state = "active"
        else:
            state = "pending"
        steps.append((title, note, state, completed))
    return steps


def _render_workflow_header() -> None:
    """Render the guided workflow progress bar at the top of the app."""
    blocks = []
    for idx, (title, note, state, completed) in enumerate(_workflow_steps(), start=1):
        status = "Complete" if completed else ("Current" if state == "active" else "Pending")
        blocks.append(
            f'<div class="fd-step {state}">'
            f'<div class="fd-step-index">Step {idx} · {status}</div>'
            f'<div class="fd-step-title">{html.escape(title)}</div>'
            f'<div class="fd-step-note">{html.escape(note)}</div>'
            '</div>'
        )
    st.markdown(
        '<div class="fd-workflow"><div class="fd-workflow-grid">'
        + "".join(blocks)
        + "</div></div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Sidebar — selection (dropdowns), actions & environment
# ---------------------------------------------------------------------------


def _render_sidebar() -> None:
    with st.sidebar:
        st.title("Fabric SQL Explorer")
        st.caption("Connect to Fabric, inspect schema, and create a semantic model.")

        _sidebar_workspace_picker()
        _sidebar_source_picker()

        st.divider()
        st.markdown("#### Workspace actions")
        if st.button("Refresh Fabric metadata", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if st.button("Reset session", use_container_width=True):
            for key in DEFAULTS:
                st.session_state[key] = DEFAULTS[key]
            st.rerun()

        with st.expander("Environment"):
            drivers = available_odbc_drivers()
            if drivers:
                st.success("ODBC drivers detected")
                st.code("\n".join(drivers), language="text")
            else:
                st.warning(
                    "No ODBC drivers / pyodbc found. Schema extraction needs the "
                    "'ODBC Driver 18 for SQL Server'."
                )


def _sidebar_workspace_picker() -> None:
    st.markdown("#### Workspace")
    try:
        workspaces = _load_workspaces()
    except AuthError as exc:
        st.error("Not signed in.")
        st.caption(str(exc))
        st.info("Run `az login`, then **Refresh data**.")
        return
    except FabricApiError as exc:
        st.error(str(exc))
        return

    if not workspaces:
        st.info("No accessible workspaces.")
        return

    ws_filter = st.text_input(
        "Filter workspaces",
        placeholder="Type to filter…",
        label_visibility="collapsed",
    ).strip().casefold()
    filtered = [w for w in workspaces if ws_filter in w.name.casefold()] if ws_filter else workspaces
    if not filtered:
        st.caption("No match.")
        return

    options = {f"{w.name}": w for w in filtered}
    current = st.session_state.workspace
    current_label = current.name if current and current.name in options else None

    selected_label = st.selectbox(
        "Workspace",
        options=list(options.keys()),
        index=(list(options).index(current_label) if current_label else None),
        placeholder="Select a workspace…",
        label_visibility="collapsed",
    )
    if selected_label:
        chosen = options[selected_label]
        if not current or current.id != chosen.id:
            st.session_state.workspace = chosen
            _reset_from("workspace")
            st.rerun()


def _sidebar_source_picker() -> None:
    """Render the data-source kind toggle and the matching item picker."""
    ws = st.session_state.workspace
    if not ws:
        return

    st.markdown("#### Data source")
    labels = {"sql": "SQL endpoint", "lakehouse": "Lakehouse (Direct Lake)"}
    current_kind = st.session_state.get("source_kind", "sql")
    chosen_label = st.radio(
        "Data source kind",
        options=list(labels.values()),
        index=list(labels).index(current_kind),
        horizontal=True,
        label_visibility="collapsed",
        help=(
            "SQL endpoints support import / DirectQuery / Direct Lake. "
            "Lakehouses bind directly to OneLake to enable Direct Lake on "
            "OneLake — the storage mode required for the fastest refresh-free "
            "Power BI experience."
        ),
    )
    chosen_kind = next(k for k, v in labels.items() if v == chosen_label)
    if chosen_kind != current_kind:
        st.session_state.source_kind = chosen_kind
        _reset_from("source")
        st.rerun()

    if chosen_kind == "lakehouse":
        _sidebar_lakehouse_picker()
    else:
        _sidebar_endpoint_picker()


def _sidebar_endpoint_picker() -> None:
    ws = st.session_state.workspace
    if not ws:
        return

    st.markdown("#### SQL endpoint")
    try:
        endpoints = _load_endpoints(ws.id)
    except FabricApiError as exc:
        st.error(str(exc))
        return

    if not endpoints:
        st.info("No SQL endpoints, warehouses or lakehouses found.")
        return

    options = {f"{e.name}  ·  {e.item_kind}": e for e in endpoints}
    current = st.session_state.endpoint
    current_label = next(
        (lbl for lbl, e in options.items() if current and e.id == current.id), None
    )

    selected_label = st.selectbox(
        "Endpoint",
        options=list(options.keys()),
        index=(list(options).index(current_label) if current_label else 0),
        label_visibility="collapsed",
    )
    chosen = options[selected_label]
    if not current or current.id != chosen.id:
        st.session_state.endpoint = chosen
        _reset_from("endpoint")

    ep = st.session_state.endpoint
    endpoint_badge = _badge(ep.item_kind, "blue")
    st.markdown(
        f"Selected endpoint: **{ep.name}** {endpoint_badge}",
        unsafe_allow_html=True,
    )

    with st.expander("Connection details"):
        st.caption("For external tools. The app uses Azure AD token auth.")
        st.code(ep.connection_string, language="text")

    col1, col2 = st.columns(2)
    if col1.button("Test connection", use_container_width=True):
        _test_connection(ep)
    if col2.button("Extract schema", type="primary", use_container_width=True):
        _extract_schemas(ep)


def _test_connection(ep: SqlEndpoint) -> None:
    try:
        with st.spinner("Opening connection…"):
            version = _sql_client().test_connection(ep.server, ep.database)
        st.success("Connection succeeded.")
        st.caption(version)
    except (SqlClientError, AuthError) as exc:
        st.error(str(exc))


def _sidebar_lakehouse_picker() -> None:
    ws = st.session_state.workspace
    if not ws:
        return

    st.markdown("#### Lakehouse")
    try:
        lakehouses = _load_lakehouses(ws.id)
    except FabricApiError as exc:
        st.error(str(exc))
        return

    if not lakehouses:
        st.info("No lakehouses found in this workspace.")
        return

    options = {lh.name: lh for lh in lakehouses}
    current = st.session_state.lakehouse
    current_label = next(
        (lbl for lbl, lh in options.items() if current and lh.id == current.id), None
    )

    selected_label = st.selectbox(
        "Lakehouse",
        options=list(options.keys()),
        index=(list(options).index(current_label) if current_label else 0),
        label_visibility="collapsed",
    )
    chosen = options[selected_label]
    if not current or current.id != chosen.id:
        st.session_state.lakehouse = chosen
        st.session_state.lakehouse_tables = None
        _reset_from("lakehouse")

    lh = st.session_state.lakehouse
    schema_badge = _badge(
        "Schema-enabled" if lh.is_schema_enabled else "Default schema",
        "teal" if lh.is_schema_enabled else "neutral",
    )
    st.markdown(
        f"Selected lakehouse: **{lh.name}** {schema_badge}",
        unsafe_allow_html=True,
    )

    status = (lh.sql_endpoint_status or "Unknown").casefold()
    if not lh.sql_endpoint_server:
        st.warning(
            "This lakehouse has no SQL analytics endpoint yet. Columns are read "
            "through that endpoint, so it must be provisioned before extracting "
            "schema."
        )
    elif status not in ("success", "provisioned", "ready", ""):
        st.warning(
            f"SQL endpoint provisioning status: **{lh.sql_endpoint_status}**. "
            "Schema extraction may fail until it is ready."
        )

    with st.expander("OneLake & endpoint details"):
        st.caption("Direct Lake on OneLake binds the model to these paths.")
        details = {
            "Default schema": lh.default_schema or "dbo",
            "OneLake tables path": lh.onelake_tables_path or "—",
            "OneLake files path": lh.onelake_files_path or "—",
            "SQL endpoint server": lh.sql_endpoint_server or "—",
            "SQL endpoint status": lh.sql_endpoint_status or "—",
        }
        st.code(
            "\n".join(f"{k}: {v}" for k, v in details.items()),
            language="text",
        )

    if not lh.sql_endpoint_server:
        return

    col1, col2 = st.columns(2)
    if col1.button("Test connection", use_container_width=True, key="lh_test"):
        _test_connection(_lakehouse_endpoint(lh))
    if col2.button(
        "Extract schema", type="primary", use_container_width=True, key="lh_extract"
    ):
        _extract_schemas_lakehouse(lh)


def _lakehouse_endpoint(lh: Lakehouse) -> SqlEndpoint:
    """Build a ``SqlEndpoint`` view of a lakehouse's SQL analytics endpoint."""
    return SqlEndpoint(
        id=lh.sql_endpoint_id or lh.id,
        name=lh.name,
        item_kind="Lakehouse",
        server=lh.sql_endpoint_server,
        database=lh.name,
        workspace_id=lh.workspace_id,
        raw={},
    )


def _extract_schemas_lakehouse(lh: Lakehouse) -> None:
    """Extract columns via the lakehouse SQL endpoint and table metadata."""
    try:
        schemas = _load_schemas(lh.sql_endpoint_server, lh.name)
    except (SqlClientError, AuthError) as exc:
        st.session_state.schemas = None
        st.session_state.selected_objects = None
        st.error(str(exc))
        return

    # Best-effort: enrich with Managed/External table metadata from the
    # preview List Tables API. Failures here never block schema extraction.
    try:
        tables = _load_lakehouse_tables(lh.workspace_id, lh.id)
    except FabricApiError:
        tables = []
    st.session_state.lakehouse_tables = tables

    st.session_state.schemas = schemas
    # Default Direct Lake on OneLake selection: tables only (non-materialized
    # SQL views cannot participate in Direct Lake).
    st.session_state.selected_objects = {
        s.full_name for s in schemas if s.object_type == "TABLE"
    }
    view_count = sum(1 for s in schemas if s.object_type == "VIEW")
    msg = f"Extracted {len(schemas)} object(s)"
    if tables:
        msg += f" · {len(tables)} Delta table(s) discovered"
    if view_count:
        msg += f" · {view_count} view(s) excluded from Direct Lake"
    st.success(msg + ".")


def _extract_schemas(ep: SqlEndpoint) -> None:
    try:
        schemas = _load_schemas(ep.server, ep.database)
        st.session_state.schemas = schemas
        # Default selection: everything.
        st.session_state.selected_objects = {s.full_name for s in schemas}
        st.success(f"Extracted {len(schemas)} object(s).")
    except (SqlClientError, AuthError) as exc:
        st.session_state.schemas = None
        st.session_state.selected_objects = None
        st.error(str(exc))


# ---------------------------------------------------------------------------
# Main area — schemas, selection grid & export
# ---------------------------------------------------------------------------

# Schemas that hold engine/system metadata rather than user data. Hidden by
# default; the user can opt in to see them.
SYSTEM_SCHEMAS = {"sys", "information_schema", "queryinsights"}


def _is_system_object(schema: TableSchema) -> bool:
    # Aggregation tables (``aggregate_*``) are user data, never engine/system
    # metadata, so keep them visible even when system schemas are hidden.
    if schema.name.casefold().startswith("aggregate_"):
        return False
    return schema.schema.casefold() in SYSTEM_SCHEMAS


def _columns_dataframe(schema: TableSchema) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "#": col.ordinal,
                "Column": col.name,
                "Type": col.type_display,
                "Nullable": "Yes" if col.is_nullable else "No",
                "PK": "Yes" if col.is_primary_key else "",
                "Default": col.default or "",
            }
            for col in schema.columns
        ]
    )


def _selected_schemas() -> list[TableSchema]:
    schemas = st.session_state.schemas or []
    selected = st.session_state.selected_objects or set()
    return [s for s in schemas if s.full_name in selected]


def _render_schemas() -> None:
    source = _active_source()
    schemas = st.session_state.schemas

    if not source:
        st.info(
            "Select a workspace and a data source (SQL endpoint or lakehouse) in "
            "the setup panel to begin."
        )
        return
    if schemas is None:
        st.info("Use **Extract schema** in the setup panel to load tables and views.")
        return
    if not schemas:
        st.warning("No tables or views found in this data source.")
        return

    system_count = sum(1 for s in schemas if _is_system_object(s))
    show_system = st.checkbox(
        f"Show system schemas ({', '.join(sorted(SYSTEM_SCHEMAS))})",
        value=False,
        help="Includes engine/metadata objects such as sys, INFORMATION_SCHEMA "
        "and queryinsights. Hidden by default.",
    )
    if not show_system:
        schemas = [s for s in schemas if not _is_system_object(s)]
        # Never keep hidden objects selected for export.
        if st.session_state.selected_objects:
            visible = {s.full_name for s in schemas}
            st.session_state.selected_objects &= visible
        if system_count:
            st.caption(f"{system_count} system object(s) hidden and excluded from selection.")

    if not schemas:
        st.info("All objects belong to system schemas. Tick the box above to show them.")
        return

    tables = [s for s in schemas if s.object_type == "TABLE"]
    views = [s for s in schemas if s.object_type == "VIEW"]
    selected = st.session_state.selected_objects or set()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tables", len(tables))
    c2.metric("Views", len(views))
    c3.metric("Columns", sum(len(s.columns) for s in schemas))
    c4.metric("Selected", len(selected))

    _render_selection_grid(schemas)
    st.divider()
    _render_object_detail(schemas)
    st.divider()
    export_tab, model_tab = st.tabs(["Export Schema", "Semantic Model"])
    with export_tab:
        _render_export(source.as_export_endpoint())
    with model_tab:
        _render_intelligence(source)


def _render_selection_grid(schemas: list[TableSchema]) -> None:
    st.markdown("#### Select objects")
    st.caption("Choose the tables and views to export or include in the semantic model.")

    selected = st.session_state.selected_objects or set()
    filter_col, type_col = st.columns([2, 1])
    object_query = filter_col.text_input(
        "Search objects",
        placeholder="Filter by schema, table, or view name…",
        key="object_filter",
    ).strip().casefold()
    type_options = sorted({s.object_type.title() for s in schemas})
    type_filter = type_col.multiselect(
        "Object type",
        options=type_options,
        default=type_options,
        key="object_type_filter",
    )
    allowed_types = {t.upper() for t in type_filter}
    filtered = [
        s for s in schemas
        if (not object_query or object_query in s.full_name.casefold())
        and s.object_type.upper() in allowed_types
    ]

    if not filtered:
        st.info("No objects match the current filter.")
        return

    grid = pd.DataFrame(
        [
            {
                "Export": s.full_name in selected,
                "Object": s.full_name,
                "Type": s.object_type.title(),
                "Columns": len(s.columns),
                "FKs": len(s.foreign_keys),
            }
            for s in filtered
        ]
    )

    bcol1, bcol2, bcol3 = st.columns([1, 1, 4])
    visible_names = {s.full_name for s in filtered}
    if bcol1.button("Select visible", use_container_width=True):
        st.session_state.selected_objects = selected | visible_names
        st.rerun()
    if bcol2.button("Clear visible", use_container_width=True):
        st.session_state.selected_objects = selected - visible_names
        st.rerun()
    bcol3.caption(
        f"Showing {len(filtered)} of {len(schemas)} object(s); "
        f"{len(selected)} selected."
    )

    edited = st.data_editor(
        grid,
        hide_index=True,
        use_container_width=True,
        height=320,
        disabled=["Object", "Type", "Columns", "FKs"],
        column_config={
            "Export": st.column_config.CheckboxColumn("Export", width="small"),
            "Object": st.column_config.TextColumn("Object", width="large"),
        },
        key="selection_grid",
    )
    new_selection = set(edited.loc[edited["Export"], "Object"].tolist())
    merged_selection = (selected - visible_names) | new_selection
    if merged_selection != selected:
        st.session_state.selected_objects = merged_selection
        st.rerun()


def _render_object_detail(schemas: list[TableSchema]) -> None:
    st.markdown("#### Inspect an object")
    by_name = {s.full_name: s for s in schemas}
    chosen_name = st.selectbox(
        "Object",
        options=list(by_name.keys()),
        label_visibility="collapsed",
    )
    if not chosen_name:
        return
    schema = by_name[chosen_name]

    st.caption(
        f"{schema.object_type.title()} · {len(schema.columns)} column(s) · "
        f"{len(schema.foreign_keys)} foreign key(s)"
    )
    st.dataframe(
        _columns_dataframe(schema),
        use_container_width=True,
        hide_index=True,
    )
    if schema.foreign_keys:
        with st.expander(f"Foreign keys ({len(schema.foreign_keys)})"):
            for fk in schema.foreign_keys:
                st.markdown(f"- `{fk.column}` → `{fk.references_full}`")


def _render_export(ep: SqlEndpoint) -> None:
    st.markdown("#### Export selected objects")
    selected = _selected_schemas()
    if not selected:
        st.info("Tick at least one object above to enable export.")
        return

    col1, col2 = st.columns([2, 3])
    fmt = col1.selectbox("Format", options=EXPORT_FORMATS)
    col2.caption(
        "**Markdown** is recommended for LLM agents (compact & readable). "
        "**JSON** for programmatic use. **SQL DDL** when reasoning in SQL."
    )

    result = export(fmt, ep, selected)
    st.download_button(
        f"⬇️ Download {result.file_name}  ({len(selected)} object(s))",
        data=result.content,
        file_name=result.file_name,
        type="primary",
        use_container_width=True,
    )
    with st.expander("Preview", expanded=False):
        st.code(result.content, language=result.language)


# ---------------------------------------------------------------------------
# Intelligence layer — semantic model generation
# ---------------------------------------------------------------------------


def _build_definition_with_feedback(spec, fmt_value: str):
    """Build a definition and surface any dropped-relationship warnings.

    ``build_definition`` removes relationships that reference unknown
    tables/columns (which would make Fabric reject the dataset) and emits a
    warning. We capture that warning and show it in the UI so the user knows
    why an edge they expected is missing instead of it vanishing silently.
    """
    import warnings as _warnings

    from app.intelligence import DefinitionFormat, build_definition

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        definition = build_definition(spec, DefinitionFormat(fmt_value))
    for w in caught:
        if "Dropped" in str(w.message) and "relationship" in str(w.message):
            st.warning(str(w.message))
    return definition


def _render_semantic_preflight(spec) -> bool:
    """Validate the semantic-model IR and render actionable findings.

    Returns True when rendering/deployment can continue. Blocking consistency
    errors are shown before TMDL/TMSL generation, which avoids opaque Fabric
    import failures later in the flow.
    """
    from app.intelligence import validate_semantic_model_spec

    result = validate_semantic_model_spec(spec)
    if result.errors:
        st.error(
            "The semantic model has consistency errors that must be fixed before "
            "the definition can be generated."
        )
        for issue in result.errors:
            prefix = f"`{issue.object_ref}`: " if issue.object_ref else ""
            st.markdown(f"- **{issue.code}** - {prefix}{issue.message}")
        return False
    if result.warnings:
        with st.expander(
            f"Preflight warnings ({len(result.warnings)})",
            expanded=True,
        ):
            for issue in result.warnings:
                prefix = f"`{issue.object_ref}`: " if issue.object_ref else ""
                st.markdown(f"- **{issue.code}** - {prefix}{issue.message}")
    return True


def _render_intelligence(source: _ActiveSource) -> None:
    st.markdown("#### Build a semantic model")
    selected = _selected_schemas()
    if not selected:
        st.info("Tick at least one object above to design a semantic model.")
        return

    is_lakehouse = source.is_lakehouse
    if is_lakehouse:
        st.markdown(
            _badge("Direct Lake on OneLake", "teal")
            + " &nbsp; Lakehouse models bind to OneLake for refresh-free queries.",
            unsafe_allow_html=True,
        )

    # Deterministic engine is always available; the agent is optional.
    from app.intelligence import DefinitionFormat, spec_from_schemas
    from app.intelligence import agent as intel_agent

    col1, col2, col3 = st.columns([2, 1, 1])
    model_name = col1.text_input("Model name", value=source.name or "SemanticModel")
    # TMDL is the recommended Fabric format and the default here.
    fmt_options = [DefinitionFormat.TMDL.value, DefinitionFormat.TMSL.value]
    fmt_label = col2.radio(
        "Definition format",
        options=fmt_options,
        index=0,
        horizontal=True,
        help="TMDL is the recommended Fabric format (prioritized). TMSL emits model.bim.",
    )
    # Lakehouse sources default to Direct Lake on OneLake; SQL endpoints
    # default to DirectQuery.
    storage_options = ["directLake", "import"] if is_lakehouse else [
        "directQuery",
        "import",
        "directLake",
    ]
    storage_mode = col3.selectbox(
        "Storage mode",
        options=storage_options,
        help=(
            "Direct Lake on OneLake binds the model to the lakehouse OneLake "
            "tables for refresh-free queries; import caches data in the model. "
            "directQuery federates queries to the SQL endpoint (SQL sources "
            "only)."
        ),
    )

    # Direct Lake comes in two flavours (Microsoft Fabric):
    #   * Direct Lake on OneLake  — binds to the lakehouse OneLake storage,
    #     reads Delta tables directly, never falls back to DirectQuery. Needs an
    #     OneLake binding (lakehouse). Recommended where available.
    #   * Direct Lake on SQL      — binds to the SQL analytics endpoint, allows
    #     SQL views (queries fall back to DirectQuery) and SQL-endpoint security.
    direct_lake_mode = "auto"
    if storage_mode == "directLake":
        _lh = source.lakehouse
        has_onelake = bool(
            _lh
            and (
                _lh.onelake_tables_path
                or (_lh.workspace_id and _lh.id)
            )
        )
        dl_options = (
            ["Direct Lake on OneLake (recommended)", "Direct Lake on SQL"]
            if has_onelake
            else ["Direct Lake on SQL"]
        )
        dl_label = st.radio(
            "Direct Lake type",
            options=dl_options,
            index=0,
            horizontal=True,
            help=(
                "Direct Lake on OneLake reads Delta tables straight from OneLake "
                "and never falls back to DirectQuery (preferred). Direct Lake on "
                "SQL binds to the SQL analytics endpoint — use it for SQL views "
                "or SQL-endpoint security; view queries fall back to DirectQuery."
            ),
        )
        direct_lake_mode = "onelake" if dl_label.startswith("Direct Lake on OneLake") else "sql"
        if not has_onelake:
            st.caption(
                "This source has no OneLake binding, so only Direct Lake on SQL "
                "is available. Pick a lakehouse source to use Direct Lake on "
                "OneLake."
            )

    # Direct Lake on OneLake cannot read non-materialized SQL views — drop them
    # so the generated model stays valid. Direct Lake on SQL keeps views but
    # warns that view queries fall back to DirectQuery.
    if storage_mode == "directLake":
        views = [s for s in selected if s.object_type == "VIEW"]
        if views and direct_lake_mode == "onelake":
            selected = [s for s in selected if s.object_type == "TABLE"]
            st.caption(
                f"{len(views)} view(s) excluded — Direct Lake on OneLake reads "
                "Delta tables only."
            )
            if not selected:
                st.info(
                    "Only views were selected. Direct Lake on OneLake needs at "
                    "least one table; select a table or switch to Direct Lake on "
                    "SQL."
                )
                return
        elif views and direct_lake_mode == "sql":
            st.caption(
                f"{len(views)} view(s) included — Direct Lake on SQL falls back "
                "to DirectQuery for view queries."
            )


    agent_ready = intel_agent.is_available()
    agent_tone = "green" if agent_ready else "amber"
    agent_status = "Agent ready" if agent_ready else "Agent unavailable"
    st.markdown(_badge(agent_status, agent_tone), unsafe_allow_html=True)
    use_agent = st.toggle(
        "Use Foundry agent for semantic design",
        value=agent_ready,
        disabled=not agent_ready,
        help=(
            "Requires the Microsoft Agent Framework and an `az login` session. "
            "When off, a deterministic star-schema mapping is used."
        ),
    )
    if not agent_ready:
        st.caption(
            "Install `agent-framework agent-framework-foundry` and run `az login` "
            "to enable AI-assisted design and remote agents."
        )
    extra = ""
    if use_agent:
        with st.expander("Advanced agent instructions", expanded=False):
            extra = st.text_area(
                "Extra requirements for the agent",
                placeholder="e.g. Add YoY measures, treat OrderDate as the date table…",
                height=80,
            )

    if st.button("Generate semantic model", type="primary", use_container_width=True):
        # Common source binding for both the deterministic and agent engines.
        lh = source.lakehouse
        binding = dict(
            source_server=source.server,
            source_database=source.database,
            storage_mode=storage_mode,
            source_kind=source.kind,
            lakehouse_id=(lh.id if lh else None),
            lakehouse_name=(lh.name if lh else None),
            onelake_workspace_id=(lh.workspace_id if lh else None),
            onelake_tables_path=(lh.onelake_tables_path if lh else None),
            default_schema=(lh.default_schema if lh else None),
            direct_lake_mode=direct_lake_mode,
        )
        with st.spinner("Designing semantic model…"):
            try:
                if use_agent and agent_ready:
                    intel = intel_agent.SemanticModelIntelligence()
                    spec = intel.design_spec_sync(
                        selected,
                        model_name=model_name,
                        extra_instructions=extra.strip() or None,
                        **binding,
                    )
                else:
                    spec = spec_from_schemas(
                        selected,
                        model_name=model_name,
                        **binding,
                    )
                if not _render_semantic_preflight(spec):
                    return
                definition = _build_definition_with_feedback(spec, fmt_label)
            except Exception as exc:  # noqa: BLE001 - surface to the user
                st.error(f"Could not generate the semantic model: {exc}")
                return
        st.session_state.semantic_definition = definition
        st.session_state.semantic_spec = spec

    definition = st.session_state.get("semantic_definition")
    spec = st.session_state.get("semantic_spec")
    if not definition or not spec:
        return

    measures = sum(len(t.measures) for t in spec.tables)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Tables", len(spec.tables))
    m2.metric("Relationships", len(spec.relationships))
    m3.metric("Measures", measures)
    m4.metric("Files", len(definition.files))

    _render_suggestions(source, spec, fmt_label, use_agent and agent_ready)

    # Re-read after a possible spec/definition refresh inside the suggestion panel.
    definition = st.session_state.get("semantic_definition")
    spec = st.session_state.get("semantic_spec")

    st.download_button(
        f"Download definition ({definition.format.value}, .zip)",
        data=definition.to_zip_bytes(),
        file_name=f"{spec.name}.SemanticModel.zip",
        type="primary",
        use_container_width=True,
    )
    with st.expander("Developer details", expanded=False):
        st.markdown("**Fabric REST payload**")
        st.code(json.dumps(definition.definition_payload(), indent=2)[:8000], language="json")
        st.markdown("**Generated definition files**")
        for path, text in definition.files.items():
            st.markdown(f"**{path}**")
            lang = "json" if path.endswith((".json", ".pbism", ".bim")) else "text"
            st.code(text[:6000], language=lang)

    _render_create_in_fabric(definition, spec)

    if agent_ready:
        st.markdown("##### Remote agent")
        st.caption(
            "Publish this designer as a reusable hosted **Foundry prompt agent** so "
            "it can be invoked outside this app."
        )
        if st.button("Publish remote agent to Foundry", use_container_width=True):
            with st.spinner("Publishing remote agent…"):
                try:
                    intel = intel_agent.SemanticModelIntelligence()
                    info = intel.publish_remote_agent_sync()
                except Exception as exc:  # noqa: BLE001 - surface to the user
                    st.error(f"Could not publish the remote agent: {exc}")
                else:
                    st.success(
                        f"Published agent **{info['agent_name']}** "
                        f"(version {info.get('version')})."
                    )


# ---------------------------------------------------------------------------
# Suggestion picker — relationships and measures the user can opt in to
# ---------------------------------------------------------------------------


def _render_suggestions_status(status: dict | None) -> None:
    """Surface what the intelligence layer actually did on the last run.

    Makes the deterministic fallback visible: if the agent errored, the user
    sees a warning (and the deterministic baseline is shown instead); if it
    ran but added nothing, that is stated explicitly so ``(agent)`` is never
    misleading.
    """
    if not status:
        return
    kind = status.get("status")
    if kind == "error":
        st.warning(
            "The Foundry agent could not be used, so deterministic "
            "suggestions are shown instead.\n\n"
            f"Reason: {status.get('error') or 'unknown error'}"
        )
    elif kind == "ok":
        contributed = status.get("agent_contributed", 0)
        if contributed:
            st.success(
                f"The agent contributed {contributed} new "
                f"suggestion{'s' if contributed != 1 else ''} "
                "(tagged *agent*); the rest come from the deterministic engine."
            )
        else:
            st.info(
                "The agent ran successfully but did not add anything beyond "
                "the deterministic baseline."
            )
    # "deterministic" → agent was never invoked; no note needed.


def _suggestion_badges(source: str, confidence: float) -> str:
    """Return source and confidence badges for a suggestion row."""
    return " ".join(
        [
            _badge(source, _source_tone(source)),
            _badge(f"{confidence:.0%}", _confidence_tone(confidence)),
        ]
    )


def _render_relationship_suggestion(sr) -> bool:
    """Render one relationship suggestion and return whether it is selected."""
    r = sr.relationship
    cols = st.columns([0.08, 0.62, 0.3])
    picked = cols[0].checkbox(
        "Select relationship suggestion",
        value=sr.confidence >= 0.9,
        key=f"sug_rel_{sr.key}",
        help=sr.rationale or None,
        label_visibility="collapsed",
    )
    cols[1].markdown(
        f"**`{r.from_table}.{r.from_column}` -> `{r.to_table}.{r.to_column}`**"
    )
    cols[2].markdown(
        _suggestion_badges(sr.source, sr.confidence), unsafe_allow_html=True
    )
    if sr.rationale:
        st.caption(sr.rationale)
    return picked


def _render_measure_suggestion(sm) -> bool:
    """Render one measure suggestion and return whether it is selected."""
    cols = st.columns([0.08, 0.52, 0.2, 0.2])
    picked = cols[0].checkbox(
        "Select measure suggestion",
        value=sm.confidence >= 0.85,
        key=f"sug_meas_{sm.key}",
        help=sm.rationale or None,
        label_visibility="collapsed",
    )
    cols[1].markdown(f"**`{sm.table}.{sm.measure.name}`**")
    cols[2].markdown(_badge(sm.source, _source_tone(sm.source)), unsafe_allow_html=True)
    cols[3].markdown(
        _badge(f"{sm.confidence:.0%}", _confidence_tone(sm.confidence)),
        unsafe_allow_html=True,
    )
    if sm.rationale:
        st.caption(sm.rationale)
    with st.expander(f"DAX: {sm.measure.name}", expanded=False):
        st.code(sm.measure.expression, language="dax")
        if sm.measure.format_string:
            st.caption(f"Format string: `{sm.measure.format_string}`")
    return picked


def _render_suggestions(
    source: _ActiveSource, spec, fmt_label: str, use_agent: bool
) -> None:
    """Show suggested relationships + measures and let the user accept any subset.

    Suggestions come from the deterministic engine by default; the Foundry
    agent is invoked when ``use_agent`` is true. Accepted items are merged
    into the active spec via :func:`apply_suggestions` and the on-disk
    definition is re-rendered so the user sees the change immediately.
    """
    from app.intelligence import agent as intel_agent
    from app.intelligence import (
        apply_suggestions,
        suggest_from_schemas,
    )

    st.markdown("##### Review suggested additions")
    st.caption(
        "Generate optional relationships and measures, review the rationale, "
        "then apply only the items you want in the semantic model."
    )

    selected = _selected_schemas()
    suggest_cols = st.columns([3, 2])
    if suggest_cols[0].button(
        "Suggest additions" + (" (agent)" if use_agent else " (deterministic)"),
        use_container_width=True,
    ):
        with st.spinner("Asking the intelligence layer…"):
            try:
                if use_agent and intel_agent.is_available():
                    intel = intel_agent.SemanticModelIntelligence()
                    outcome = intel.design_suggestions_detailed_sync(selected, spec)
                    bundle = outcome.suggestions
                    status = {
                        "status": outcome.status,
                        "error": outcome.error,
                        "agent_contributed": outcome.agent_contributed,
                    }
                else:
                    bundle = suggest_from_schemas(selected, spec=spec)
                    status = {"status": "deterministic", "error": None,
                              "agent_contributed": 0}
            except Exception as exc:  # noqa: BLE001 - surface to the user
                st.error(f"Could not produce suggestions: {exc}")
                return
        st.session_state.semantic_suggestions = bundle
        st.session_state.semantic_suggestions_status = status

    if suggest_cols[1].button("Clear suggestions", use_container_width=True):
        st.session_state.semantic_suggestions = None
        st.session_state.semantic_suggestions_status = None
        return

    _render_suggestions_status(
        st.session_state.get("semantic_suggestions_status")
    )

    bundle = st.session_state.get("semantic_suggestions")
    if not bundle or (not bundle.relationships and not bundle.measures):
        if bundle is not None:
            st.info("No additional relationships or measures were suggested.")
        return

    all_sources = sorted({s.source for s in bundle.relationships + bundle.measures})
    fcol1, fcol2 = st.columns([2, 1])
    selected_sources = fcol1.multiselect(
        "Suggestion sources",
        options=all_sources,
        default=all_sources,
        key="suggestion_source_filter",
    )
    min_confidence = fcol2.slider(
        "Minimum confidence",
        min_value=0,
        max_value=100,
        value=0,
        step=5,
        format="%d%%",
        key="suggestion_confidence_filter",
    ) / 100

    filtered_relationships = [
        s for s in bundle.relationships
        if s.source in selected_sources and s.confidence >= min_confidence
    ]
    filtered_measures = [
        s for s in bundle.measures
        if s.source in selected_sources and s.confidence >= min_confidence
    ]

    rel_tab, measure_tab = st.tabs(
        [
            f"Relationships ({len(filtered_relationships)})",
            f"Measures ({len(filtered_measures)})",
        ]
    )

    rel_picks: dict[str, bool] = {}
    measure_picks: dict[str, bool] = {}

    with rel_tab:
        if not filtered_relationships:
            st.caption("No relationship suggestions match the current filters.")
        else:
            st.caption("High-confidence relationship suggestions are preselected.")
            for sr in filtered_relationships:
                rel_picks[sr.key] = _render_relationship_suggestion(sr)

    with measure_tab:
        if not filtered_measures:
            st.caption("No measure suggestions match the current filters.")
        else:
            st.caption("High-confidence measure suggestions are preselected.")
            for sm in filtered_measures:
                measure_picks[sm.key] = _render_measure_suggestion(sm)

    accepted_rels = [s for s in filtered_relationships if rel_picks.get(s.key)]
    accepted_measures = [s for s in filtered_measures if measure_picks.get(s.key)]

    st.caption(
        f"Selected: {len(accepted_rels)} relationship(s), "
        f"{len(accepted_measures)} measure(s)."
    )

    if st.button(
        "Apply selected to semantic model",
        type="primary",
        use_container_width=True,
        disabled=not (accepted_rels or accepted_measures),
    ):
        updated_spec = apply_suggestions(
            spec,
            relationships=accepted_rels,
            measures=accepted_measures,
        )
        if not _render_semantic_preflight(updated_spec):
            return
        try:
            updated_definition = _build_definition_with_feedback(
                updated_spec, fmt_label
            )
        except Exception as exc:  # noqa: BLE001 - surface to the user
            st.error(f"Could not rebuild the definition: {exc}")
            return
        st.session_state.semantic_spec = updated_spec
        st.session_state.semantic_definition = updated_definition
        # Drop suggestions that have just been merged in so they don't re-show.
        applied_rel_keys = {s.key for s in accepted_rels}
        applied_meas_keys = {s.key for s in accepted_measures}
        st.session_state.semantic_suggestions = type(bundle)(
            relationships=[s for s in bundle.relationships if s.key not in applied_rel_keys],
            measures=[s for s in bundle.measures if s.key not in applied_meas_keys],
        )
        st.success(
            f"Added {len(accepted_rels)} relationship(s) and "
            f"{len(accepted_measures)} measure(s) to **{updated_spec.name}**."
        )
        st.rerun()


def _render_create_in_fabric(definition, spec) -> None:
    """Submit the generated definition to Fabric to create the semantic model."""
    workspace = st.session_state.workspace
    if workspace is None:
        return

    st.markdown("##### Create in Microsoft Fabric")
    st.caption(
        f"Target workspace: **{workspace.name}**. The model will be created in "
        "the same workspace as the selected data source."
    )
    if getattr(spec, "source_kind", "sql") == "lakehouse":
        st.info(
            "Direct Lake on OneLake requires the semantic model to live in the "
            "same workspace and region as the lakehouse. Creating it here in "
            f"**{workspace.name}** keeps that binding valid."
        )

    tables = len(spec.tables)
    relationships = len(spec.relationships)
    measures = sum(len(t.measures) for t in spec.tables)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tables", tables)
    c2.metric("Relationships", relationships)
    c3.metric("Measures", measures)
    c4.metric("Files", len(definition.files))

    target_name = st.text_input(
        "Semantic model name",
        value=spec.name,
        key="fabric_model_name",
        help="The display name of the semantic model item created in Fabric.",
    )
    submit = st.button(
        "Create semantic model in Fabric",
        type="primary",
        use_container_width=True,
        disabled=not target_name.strip(),
    )
    if not submit:
        return

    with st.spinner(f"Creating '{target_name}' in {workspace.name}…"):
        try:
            created = _fabric_client().create_semantic_model(
                workspace_id=workspace.id,
                display_name=target_name.strip(),
                definition=definition.definition_payload(),
                description=spec.description,
            )
        except FabricApiError as exc:
            st.error(f"Fabric rejected the request: {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001 - surface to the user
            st.error(f"Could not create the semantic model: {exc}")
            return

    if created.succeeded:
        detail = f" (id `{created.id}`)" if created.id else ""
        st.success(
            f"Created semantic model **{created.display_name}** in "
            f"**{workspace.name}**{detail}."
        )
    else:
        st.warning(f"Fabric returned status: {created.status}.")


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------


def main() -> None:
    _init_state()
    _render_sidebar()

    source = _active_source()
    st.title("Fabric SQL Explorer")
    if source:
        label = "Lakehouse" if source.is_lakehouse else "Endpoint"
        src_name = html.escape(source.name)
        src_server = html.escape(source.server)
        src_database = html.escape(source.database)
        st.markdown(
            f'<p class="fd-subtitle">{label} <strong>{src_name}</strong> · '
            f'<code>{src_server}</code> / <code>{src_database}</code></p>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<p class="fd-subtitle">Authenticated with Azure CLI. No passwords are stored.</p>',
            unsafe_allow_html=True,
        )

    _render_workflow_header()
    _render_schemas()


def _run() -> None:
    """Multipage entry point.

    Each page is a thin Streamlit view over the deterministic, network/AI-free
    cores in ``app.intelligence`` and ``app.artifacts``. The schema-export and
    semantic-model authoring flow remains the original guided experience; the
    new pages add the read → audit → (opt-in) write pipeline.
    """
    from app.ui import ask_model_page, insights_page, reports_page, semantic_models_page

    pages = [
        st.Page(
            main,
            title="Schema Explorer",
            icon=":material/table_view:",
            url_path="schema-explorer",
            default=True,
        ),
        st.Page(
            semantic_models_page.render,
            title="Semantic Models",
            icon=":material/dataset:",
            url_path="semantic-models",
        ),
        st.Page(
            ask_model_page.render,
            title="Ask Your Model",
            icon=":material/question_answer:",
            url_path="ask-model",
        ),
        st.Page(
            reports_page.render,
            title="Reports",
            icon=":material/assessment:",
            url_path="reports",
        ),
        st.Page(
            insights_page.render,
            title="Insights",
            icon=":material/insights:",
            url_path="insights",
        ),
    ]
    st.navigation(pages).run()


_run()
