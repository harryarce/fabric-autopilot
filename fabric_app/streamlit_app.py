"""Decoupled Streamlit frontend — talks only to the REST API.

Run with::

    FABRIC_API_BASE_URL=http://localhost:8000 streamlit run fabric_app/streamlit_app.py

Unlike the legacy ``app/streamlit_app.py`` (which calls Fabric/SQL directly),
this entrypoint contains **no business logic**: it renders state and delegates
every capability to :class:`fabric_app.api_client.FabricApiClient`. It is the
container image's web tier, configured solely by ``FABRIC_API_BASE_URL``.
"""

from __future__ import annotations

import json
import os
import sys

# Allow ``streamlit run fabric_app/streamlit_app.py`` to resolve packages
# regardless of the working directory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st

from fabric_app.api_client import ApiError, FabricApiClient

st.set_page_config(
    page_title="fabric-autopilot",
    page_icon="F",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner=False)
def _client(base_url: str, tenant_id: str | None) -> FabricApiClient:
    return FabricApiClient(base_url=base_url, tenant_id=tenant_id or None)


def _show_api_error(exc: ApiError) -> None:
    if exc.code == "api_unreachable":
        st.error(f"API unreachable: {exc.detail}")
    elif exc.is_fabric_access_denied:
        st.warning(
            "The platform identity is not yet enabled for Fabric. "
            f"Onboarding required: {exc.detail}"
        )
    elif exc.is_dependency_unavailable:
        st.info(f"Optional capability unavailable: {exc.detail}")
    else:
        st.error(f"{exc.title} ({exc.status}): {exc.detail}")


# ---------------------------------------------------------------------------
# Sidebar — connection + health badges
# ---------------------------------------------------------------------------

st.sidebar.title("fabric-autopilot")
base_url = st.sidebar.text_input(
    "API base URL",
    value=os.environ.get("FABRIC_API_BASE_URL", "http://localhost:8000"),
)
tenant_id = st.sidebar.text_input(
    "Tenant id", value=os.environ.get("FABRIC_DEFAULT_TENANT_ID", "")
)
client = _client(base_url, tenant_id)

with st.sidebar:
    st.markdown("---")
    st.caption("Service health")
    try:
        client.healthz()
        st.success("API: healthy")
    except ApiError as exc:
        st.error("API: unreachable")
        _show_api_error(exc)
        st.stop()

    try:
        fabric = client.fabric_health()
        if fabric.get("ok"):
            st.success(f"Fabric: {fabric.get('workspaceCount', 0)} workspaces")
        else:
            st.warning(f"Fabric: {fabric.get('message', 'not ready')}")
    except ApiError:
        st.warning("Fabric: access not configured")

    try:
        agent = client.agent_status()
        if agent.get("semanticModelDesign"):
            st.success("Agents: Foundry ready")
        else:
            st.info("Agents: deterministic fallback")
    except ApiError:
        st.info("Agents: status unavailable")


# ---------------------------------------------------------------------------
# Main — workflow tabs
# ---------------------------------------------------------------------------

st.title("fabric-autopilot")
st.caption(
    "Agentic semantic-model and report authoring over Microsoft Fabric. "
    "All actions run through the platform API."
)

explore_tab, model_tab, report_tab, ask_model_tab, model_chat_tab, artifact_tab, agent_tab = st.tabs(
    [
        "Explore",
        "Semantic Models",
        "Reports",
        "Ask Your Model",
        "Model Chat",
        "Artifacts",
        "Agent Studio",
    ]
)


def _select_workspace(key: str) -> dict | None:
    try:
        workspaces = client.list_workspaces()
    except ApiError as exc:
        _show_api_error(exc)
        return None
    if not workspaces:
        st.info("No workspaces are visible to the platform identity.")
        return None
    labels = {w["name"]: w for w in workspaces}
    choice = st.selectbox("Workspace", list(labels), key=key)
    return labels.get(choice)


def _select_sql_source(key: str, ws: dict | None) -> tuple[str | None, str | None]:
    """Discover the SQL connection (server, database) for a workspace.

    Mirrors the Explore tab: lists the workspace's SQL endpoints and lakehouse
    analytics endpoints and lets the user pick one, auto-filling the server FQDN
    and database name instead of typing them by hand. A "Enter manually" escape
    hatch keeps the previous free-text behaviour for endpoints the API cannot
    enumerate. Returns ``(server, database)`` — either may be ``None`` until the
    user makes a usable choice.
    """
    _MANUAL = "✏️ Enter manually"
    if not ws:
        st.info("Select a workspace to discover its SQL endpoints.")
        return None, None

    endpoints: list[dict] = []
    try:
        endpoints = client.list_sql_endpoints(ws["id"])
    except ApiError as exc:
        _show_api_error(exc)
    options: dict[str, dict | None] = {
        f"{e['name']} ({e['item_kind']})": e for e in endpoints
    }

    try:
        for lh in client.list_lakehouses(ws["id"]):
            if lh.get("sql_endpoint_server"):
                options[f"{lh['name']} (Lakehouse)"] = {
                    "server": lh["sql_endpoint_server"],
                    "database": lh.get("name"),
                }
    except ApiError:
        # Lakehouse discovery is best-effort; SQL endpoints already cover most
        # cases and the manual entry below is always available.
        pass

    options[_MANUAL] = None
    choice = st.selectbox(
        "Data source",
        list(options),
        key=f"{key}_source",
        help="Pick a SQL endpoint or lakehouse to auto-fill its server and database.",
    )
    selected = options.get(choice)

    if selected is None:
        # Manual override — preserve the original free-text inputs.
        server = st.text_input("SQL endpoint server (FQDN)", key=f"{key}_server")
        database = st.text_input("Database / lakehouse SQL name", key=f"{key}_database")
        return server or None, database or None

    server = selected.get("server")
    database = selected.get("database")
    st.caption(f"Server `{server}` · database `{database}`")
    return server or None, database or None


def _select_agent_source(key: str, ws: dict | None) -> dict | None:
    """Pick an Agent Studio data source, carrying the OneLake binding.

    A lakehouse selection returns its OneLake binding (lakehouse id, OneLake
    workspace + tables path, default schema) so the orchestrator can publish a
    Direct Lake on OneLake model — bound through the model's own identity, so it
    refreshes without a credential-less "default data connection". A SQL
    endpoint selection falls back to :func:`_select_sql_source`. Returns a
    source dict, or ``None`` until a usable choice is made.
    """
    if not ws:
        st.info("Select a workspace to discover its data sources.")
        return None

    kind = st.radio(
        "Data source type",
        options=["SQL endpoint", "Lakehouse (Direct Lake on OneLake)"],
        horizontal=True,
        key=f"{key}_source_kind",
        help=(
            "Lakehouses publish as Direct Lake on OneLake — refresh-free, with "
            "no data-source credentials to bind. SQL endpoints publish as "
            "import / DirectQuery / Direct Lake on SQL."
        ),
    )

    if kind.startswith("Lakehouse"):
        try:
            lakehouses = client.list_lakehouses(ws["id"])
        except ApiError as exc:
            _show_api_error(exc)
            return None
        if not lakehouses:
            st.info("No lakehouses in this workspace.")
            return None
        labels = {lh["name"]: lh for lh in lakehouses}
        choice = st.selectbox("Lakehouse", list(labels), key=f"{key}_lh")
        lh = labels[choice]
        server = lh.get("sql_endpoint_server")
        if not server:
            st.warning(
                "This lakehouse has no SQL analytics endpoint yet; schema is "
                "read through that endpoint, so it must be provisioned first."
            )
        st.caption(f"Server `{server}` · database `{lh.get('name')}`")
        return {
            "kind": "lakehouse",
            "server": server,
            "database": lh.get("name"),
            "name": lh.get("name"),
            "lakehouse_id": lh.get("id"),
            "lakehouse_name": lh.get("name"),
            "onelake_workspace_id": lh.get("workspace_id"),
            "onelake_tables_path": lh.get("onelake_tables_path"),
            "default_schema": lh.get("default_schema"),
        }

    server, database = _select_sql_source(key, ws)
    if not (server and database):
        return None
    return {"kind": "sql", "server": server, "database": database, "name": database}


# Schemas that hold engine/system metadata rather than user data. Hidden by
# default; the user can opt in to see them (mirrors the legacy app).
SYSTEM_SCHEMAS = {"sys", "information_schema", "queryinsights"}


def _is_system_object(schema: dict) -> bool:
    # Aggregation tables (``aggregate_*``) are user data, never engine/system
    # metadata, so keep them visible even when system schemas are hidden.
    if (schema.get("name") or "").casefold().startswith("aggregate_"):
        return False
    return (schema.get("schema") or "").casefold() in SYSTEM_SCHEMAS


def _render_explore_selection(schemas: list[dict]) -> None:
    """Full-fidelity table selection: system filter, search, type filter, bulk
    select/clear, and metrics — matching the legacy Streamlit explorer."""
    # -- system-schema toggle (hidden by default) ------------------------
    system_count = sum(1 for s in schemas if _is_system_object(s))
    show_system = st.checkbox(
        f"Show system schemas ({', '.join(sorted(SYSTEM_SCHEMAS))})",
        value=False,
        help=(
            "Includes engine/metadata objects such as sys, INFORMATION_SCHEMA "
            "and queryinsights. Hidden by default."
        ),
        key="explore_show_system",
    )
    selected = set(st.session_state.get("explore_selected", set()))
    if not show_system:
        schemas = [s for s in schemas if not _is_system_object(s)]
        # Never keep hidden objects selected.
        visible_all = {f"{s['schema']}.{s['name']}" for s in schemas}
        if selected - visible_all:
            selected &= visible_all
            st.session_state["explore_selected"] = selected
        if system_count:
            st.caption(
                f"{system_count} system object(s) hidden and excluded from "
                "selection."
            )

    if not schemas:
        st.info(
            "All objects belong to system schemas. Tick the box above to show "
            "them."
        )
        return

    tables = [s for s in schemas if s["object_type"] == "TABLE"]
    views = [s for s in schemas if s["object_type"] == "VIEW"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tables", len(tables))
    c2.metric("Views", len(views))
    c3.metric("Columns", sum(len(s.get("columns", [])) for s in schemas))
    c4.metric("Selected", len(selected))

    st.markdown("#### Select tables to include in the semantic model")
    st.caption(
        "Choose the tables and views to include in the semantic model. Only "
        "selected objects are passed to the designer."
    )

    # -- search + object-type filters ------------------------------------
    filter_col, type_col = st.columns([2, 1])
    object_query = (
        filter_col.text_input(
            "Search objects",
            placeholder="Filter by schema, table, or view name…",
            key="explore_object_filter",
        )
        .strip()
        .casefold()
    )
    type_options = sorted({s["object_type"].title() for s in schemas})
    type_filter = type_col.multiselect(
        "Object type",
        options=type_options,
        default=type_options,
        key="explore_type_filter",
    )
    allowed_types = {t.upper() for t in type_filter}
    filtered = [
        s
        for s in schemas
        if (
            not object_query
            or object_query in f"{s['schema']}.{s['name']}".casefold()
        )
        and s["object_type"].upper() in allowed_types
    ]

    if not filtered:
        st.info("No objects match the current filter.")
        return

    visible_names = {f"{s['schema']}.{s['name']}" for s in filtered}

    # -- bulk select / clear ---------------------------------------------
    bcol1, bcol2, bcol3 = st.columns([1, 1, 4])
    if bcol1.button("Select visible", use_container_width=True, key="explore_select_all"):
        st.session_state["explore_selected"] = selected | visible_names
        st.rerun()
    if bcol2.button("Clear visible", use_container_width=True, key="explore_clear_all"):
        st.session_state["explore_selected"] = selected - visible_names
        st.rerun()
    bcol3.caption(
        f"Showing {len(filtered)} of {len(schemas)} object(s); "
        f"{len(selected)} selected."
    )

    grid = [
        {
            "Include": f"{s['schema']}.{s['name']}" in selected,
            "Object": f"{s['schema']}.{s['name']}",
            "Type": s["object_type"].title(),
            "Columns": len(s.get("columns", [])),
            "FKs": len(s.get("foreign_keys", [])),
        }
        for s in filtered
    ]
    edited = st.data_editor(
        grid,
        hide_index=True,
        use_container_width=True,
        height=320,
        disabled=["Object", "Type", "Columns", "FKs"],
        column_config={
            "Include": st.column_config.CheckboxColumn("Include", width="small"),
            "Object": st.column_config.TextColumn("Object", width="large"),
        },
        key="explore_selection_grid",
    )
    new_visible = {row["Object"] for row in edited if row["Include"]}
    merged = (selected - visible_names) | new_visible
    if merged != selected:
        st.session_state["explore_selected"] = merged
        st.rerun()

    st.divider()
    _render_object_inspector(schemas)


def _column_type_display(col: dict) -> str:
    """Reconstruct a friendly type string (varchar(255), decimal(18,2), …)."""
    data_type = col.get("data_type", "")
    t = data_type.lower()
    max_length = col.get("max_length")
    if max_length is not None and t in {
        "varchar",
        "nvarchar",
        "char",
        "nchar",
        "varbinary",
        "binary",
    }:
        length = "max" if max_length in (-1, None) else max_length
        return f"{data_type}({length})"
    precision = col.get("precision")
    if precision is not None and t in {"decimal", "numeric"}:
        return f"{data_type}({precision},{col.get('scale') or 0})"
    return data_type


def _render_object_inspector(schemas: list[dict]) -> None:
    """Inspect a single object's columns and foreign keys (legacy parity)."""
    st.markdown("#### Inspect an object")
    by_name = {f"{s['schema']}.{s['name']}": s for s in schemas}
    chosen = st.selectbox(
        "Object",
        options=list(by_name.keys()),
        label_visibility="collapsed",
        key="explore_inspect_object",
    )
    if not chosen:
        return
    schema = by_name[chosen]
    columns = schema.get("columns", [])
    fks = schema.get("foreign_keys", [])
    st.caption(
        f"{schema['object_type'].title()} · {len(columns)} column(s) · "
        f"{len(fks)} foreign key(s)"
    )
    st.dataframe(
        [
            {
                "#": col.get("ordinal"),
                "Column": col.get("name"),
                "Type": _column_type_display(col),
                "Nullable": "Yes" if col.get("is_nullable") else "No",
                "PK": "Yes" if col.get("is_primary_key") else "",
                "Default": col.get("default") or "",
            }
            for col in columns
        ],
        use_container_width=True,
        hide_index=True,
    )
    if fks:
        with st.expander(f"Foreign keys ({len(fks)})"):
            for fk in fks:
                ref = (
                    f"{fk.get('references_schema')}."
                    f"{fk.get('references_table')}."
                    f"{fk.get('references_column')}"
                )
                st.markdown(f"- `{fk.get('column')}` → `{ref}`")


# ---------------------------------------------------------------------------
# Audit, suggestions, and definition helpers (semantic-model parity)
# ---------------------------------------------------------------------------

_SEVERITY_ICON = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}


def _render_audit_reports(reports: dict) -> None:
    """Render the per-feature audit bundle as a structured health view.

    ``reports`` is the ``{feature: AuditReport.to_dict()}`` mapping the API
    returns. A merged ``health`` report (when present) is surfaced first as the
    headline score; every other feature gets its own expander.
    """
    if not reports:
        st.info("No audit findings.")
        return

    health = reports.get("health") or reports.get("semantic-model-health")
    if health:
        summary = health.get("summary", {})
        cols = st.columns(4)
        cols[0].metric("Health score", f"{health.get('score', 0)}/100")
        cols[1].metric("Errors", summary.get("errors", 0))
        cols[2].metric("Warnings", summary.get("warnings", 0))
        cols[3].metric("Info", summary.get("infos", 0))

    for feature, report in reports.items():
        if report is health:
            continue
        summary = report.get("summary", {})
        score = report.get("score", 0)
        title = feature.replace("semantic-model-", "").replace("-", " ").title()
        head = (
            f"{title} — {score}/100 "
            f"(❌ {summary.get('errors', 0)} · ⚠️ {summary.get('warnings', 0)} "
            f"· ℹ️ {summary.get('infos', 0)})"
        )
        findings = report.get("findings", [])
        with st.expander(head, expanded=bool(summary.get("errors"))):
            if not findings:
                st.success("No issues found.")
                continue
            for f in findings:
                icon = _SEVERITY_ICON.get(f.get("severity", "info"), "•")
                ref = f.get("object_ref")
                line = f"{icon} **{f.get('code')}** — {f.get('message')}"
                if ref:
                    line += f" (`{ref}`)"
                st.markdown(line)
                if f.get("recommendation"):
                    st.caption(f"↳ {f['recommendation']}")


def _suggestion_label(s: dict) -> str:
    """Build a human-friendly one-line label for a suggestion dict."""
    ref = s.get("object_ref") or ""
    field = s.get("field") or ""
    target = " · ".join(p for p in (ref, field) if p)
    proposed = s.get("proposed_value")
    if isinstance(proposed, str):
        proposed_txt = proposed
    elif proposed is None:
        proposed_txt = ""
    else:
        proposed_txt = str(proposed)
    if len(proposed_txt) > 80:
        proposed_txt = proposed_txt[:77] + "…"
    code = s.get("code") or s.get("kind") or "fix"
    label = f"**{code}**"
    if target:
        label += f" — `{target}`"
    if proposed_txt:
        label += f" → {proposed_txt}"
    return label


def _suggestion_selector(suggestions: list[dict], *, key_prefix: str) -> list[dict]:
    """Render a user-friendly checklist of suggestions with select/clear all.

    Returns the subset of ``suggestions`` (untouched dicts) the user has ticked.
    Checkbox state is keyed by each suggestion's stable ``id``.
    """
    if not suggestions:
        st.info("No applicable fixes were proposed.")
        return []

    sel_col, clr_col, info_col = st.columns([1, 1, 3])
    if sel_col.button("Select all", key=f"{key_prefix}_sel_all"):
        for s in suggestions:
            st.session_state[f"{key_prefix}_cb_{s.get('id')}"] = True
    if clr_col.button("Clear all", key=f"{key_prefix}_clr_all"):
        for s in suggestions:
            st.session_state[f"{key_prefix}_cb_{s.get('id')}"] = False

    # Group by category (kind) for readability.
    groups: dict[str, list[dict]] = {}
    for s in suggestions:
        groups.setdefault(s.get("kind", "other"), []).append(s)

    _kind_titles = {
        "model_usability": "Usability",
        "model_bpa": "Best Practice Analyzer",
        "model_copilot": "Copilot readiness",
        "model_ai": "AI-enhanced (descriptions & synonyms)",
        "report_theme": "Theme / formatting",
    }

    selected: list[dict] = []
    for kind, items in groups.items():
        title = _kind_titles.get(kind, kind.replace("_", " ").title())
        st.markdown(f"**{title}** ({len(items)})")
        for s in items:
            cb_key = f"{key_prefix}_cb_{s.get('id')}"
            if cb_key not in st.session_state:
                st.session_state[cb_key] = True
            checked = st.checkbox(
                _suggestion_label(s),
                key=cb_key,
                help=s.get("rationale") or None,
            )
            if s.get("current_value") not in (None, ""):
                st.caption(f"↳ current: {s.get('current_value')}")
            if checked:
                selected.append(s)

    info_col.caption(f"{len(selected)} of {len(suggestions)} selected")
    return selected


def _render_model_suggestions(
    source: dict, spec: dict, selected_tables: list[str]
) -> None:
    """Suggest additional relationships and measures, and apply the picks."""
    st.markdown("#### Suggest additions")
    st.caption(
        "Agent-assisted (with deterministic fallback) relationship and measure "
        "ideas that go beyond the foreign keys already in the model."
    )
    col_a, col_b = st.columns([1, 1])
    with col_a:
        use_agent_s = st.checkbox(
            "Use Foundry agent", value=True, key="suggest_agent"
        )
    with col_b:
        extra_s = st.text_input(
            "Extra requirements (optional)", value="", key="suggest_extra"
        )

    if st.button("Suggest additions", key="suggest_additions"):
        with st.spinner("Generating suggestions..."):
            try:
                result = client.suggest_semantic_model(
                    server=source["server"],
                    database=source["database"],
                    spec=spec,
                    selected_tables=selected_tables or None,
                    use_agent=use_agent_s,
                    extra_instructions=extra_s or None,
                )
                st.session_state["model_suggestions"] = result
            except ApiError as exc:
                _show_api_error(exc)
    if st.button("Clear suggestions", key="clear_suggestions"):
        st.session_state.pop("model_suggestions", None)

    result = st.session_state.get("model_suggestions")
    if not result:
        return

    status = result.get("status")
    contributed = result.get("agentContributed", 0)
    if status == "ok":
        st.success(
            f"Agent contributed {contributed} new suggestion(s) beyond the "
            "deterministic baseline."
        )
    elif status == "error":
        st.warning("Agent unavailable — showing deterministic suggestions.")
        detail = result.get("error")
        if detail:
            st.caption(detail)
    else:
        st.info("Showing deterministic suggestions.")

    suggestions = result.get("suggestions", {})
    rels = suggestions.get("relationships", [])
    measures = suggestions.get("measures", [])

    sources = sorted({s.get("source", "deterministic") for s in rels + measures})
    source_filter = st.multiselect(
        "Sources", options=sources, default=sources, key="suggest_source_filter"
    )
    min_conf = st.slider(
        "Minimum confidence", 0.0, 1.0, 0.0, 0.05, key="suggest_conf_filter"
    )

    def _keep(item: dict) -> bool:
        return (
            item.get("source", "deterministic") in source_filter
            and float(item.get("confidence", 0)) >= min_conf
        )

    rels = [r for r in rels if _keep(r)]
    measures = [m for m in measures if _keep(m)]

    rel_tab, measure_tab = st.tabs(
        [f"Relationships ({len(rels)})", f"Measures ({len(measures)})"]
    )

    chosen_rels: list[dict] = []
    with rel_tab:
        if not rels:
            st.caption("No relationship suggestions match the filters.")
        for i, r in enumerate(rels):
            rel = r.get("relationship", {})
            conf = float(r.get("confidence", 0))
            label = (
                f"{rel.get('from_table')}[{rel.get('from_column')}] → "
                f"{rel.get('to_table')}[{rel.get('to_column')}]"
            )
            if st.checkbox(
                label, value=conf >= 0.9, key=f"rel_pick_{i}"
            ):
                chosen_rels.append(r)
            st.caption(
                f"{r.get('source', 'deterministic')} · confidence {conf:.0%}"
                + (f" · {r.get('rationale')}" if r.get("rationale") else "")
            )

    chosen_measures: list[dict] = []
    with measure_tab:
        if not measures:
            st.caption("No measure suggestions match the filters.")
        for i, m in enumerate(measures):
            meas = m.get("measure", {})
            conf = float(m.get("confidence", 0))
            label = f"{m.get('table')}[{meas.get('name')}]"
            if st.checkbox(
                label, value=conf >= 0.85, key=f"measure_pick_{i}"
            ):
                chosen_measures.append(m)
            st.caption(
                f"{m.get('source', 'deterministic')} · confidence {conf:.0%}"
                + (f" · {m.get('rationale')}" if m.get("rationale") else "")
            )
            with st.expander("DAX", expanded=False):
                st.code(meas.get("expression", ""), language="dax")
                if meas.get("format_string"):
                    st.caption(f"Format: {meas['format_string']}")

    st.caption(
        f"{len(chosen_rels)} relationship(s) and {len(chosen_measures)} "
        "measure(s) selected."
    )
    if st.button(
        "Apply selected",
        key="apply_suggestions",
        disabled=not (chosen_rels or chosen_measures),
    ):
        with st.spinner("Applying suggestions..."):
            try:
                applied = client.apply_semantic_model_suggestions(
                    spec=spec,
                    relationships=chosen_rels,
                    measures=chosen_measures,
                )
                updated_spec = applied["spec"]
                st.session_state["model_spec"] = updated_spec
                # Drop only the suggestions that were just applied; keep the
                # rest (including filtered-out items) so the user can review
                # and apply more without regenerating from scratch.
                applied_rel_ids = {id(r) for r in chosen_rels}
                applied_meas_ids = {id(m) for m in chosen_measures}
                remaining_rels = [
                    r for r in suggestions.get("relationships", [])
                    if id(r) not in applied_rel_ids
                ]
                remaining_meas = [
                    m for m in suggestions.get("measures", [])
                    if id(m) not in applied_meas_ids
                ]
                if remaining_rels or remaining_meas:
                    result["suggestions"] = {
                        "relationships": remaining_rels,
                        "measures": remaining_meas,
                    }
                    st.session_state["model_suggestions"] = result
                else:
                    st.session_state.pop("model_suggestions", None)
                # Rebuild the definition right away so the updated model
                # contents are visible immediately (matches the original app).
                fmt = st.session_state.get("model_def_format", "TMDL")
                try:
                    st.session_state["model_definition"] = (
                        client.build_model_definition(updated_spec, fmt=fmt)
                    )
                except ApiError as exc:
                    st.session_state.pop("model_definition", None)
                    _show_api_error(exc)
                st.success(
                    f"Applied {len(chosen_rels)} relationship(s) and "
                    f"{len(chosen_measures)} measure(s)."
                )
                st.rerun()
            except ApiError as exc:
                _show_api_error(exc)


def _render_model_definition(spec: dict) -> None:
    """Preflight-validate, render, and expose the model definition contents."""
    st.markdown("#### Model contents")
    fmt = st.radio(
        "Definition format",
        options=["TMDL", "TMSL"],
        horizontal=True,
        key="model_def_format",
        help="TMDL is the human-readable Tabular Model Definition Language; "
        "TMSL emits a single model.bim JSON.",
    )

    # Preflight: surface consistency errors before rendering/publishing.
    try:
        validation = client.validate_semantic_model(spec)
    except ApiError as exc:
        _show_api_error(exc)
        validation = None
    if validation:
        errors = validation.get("errors", [])
        warnings = validation.get("warnings", [])
        if errors:
            st.error(f"Preflight found {len(errors)} blocking issue(s):")
            for e in errors:
                ref = e.get("object_ref")
                st.markdown(
                    f"- ❌ **{e.get('code')}** — {e.get('message')}"
                    + (f" (`{ref}`)" if ref else "")
                )
        if warnings:
            with st.expander(f"Preflight warnings ({len(warnings)})"):
                for w in warnings:
                    ref = w.get("object_ref")
                    st.markdown(
                        f"- ⚠️ **{w.get('code')}** — {w.get('message')}"
                        + (f" (`{ref}`)" if ref else "")
                    )
        if not errors and not warnings:
            st.success("Preflight checks passed.")

    if st.button("Build / refresh definition", key="build_definition"):
        with st.spinner("Rendering definition..."):
            try:
                st.session_state["model_definition"] = client.build_model_definition(
                    spec, fmt=fmt
                )
            except ApiError as exc:
                _show_api_error(exc)

    definition = st.session_state.get("model_definition")
    if not definition:
        return

    for w in definition.get("warnings", []):
        st.warning(w)

    metrics = definition.get("metrics", {})
    cols = st.columns(4)
    cols[0].metric("Tables", metrics.get("tables", 0))
    cols[1].metric("Relationships", metrics.get("relationships", 0))
    cols[2].metric("Measures", metrics.get("measures", 0))
    cols[3].metric("Files", metrics.get("files", 0))

    files = definition.get("files", {})
    with st.expander("Developer details", expanded=False):
        st.caption("Fabric REST definition payload (truncated)")
        payload_text = json.dumps(definition.get("definition", {}), indent=2)
        st.code(payload_text[:8000], language="json")
        st.caption("Generated definition files")
        for path, text in files.items():
            st.markdown(f"**{path}**")
            lang = "json" if path.endswith((".json", ".bim", ".pbism")) else "text"
            st.code(text[:6000], language=lang)

    if files:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, text in files.items():
                zf.writestr(path, text)
        st.download_button(
            "Download definition (.zip)",
            data=buffer.getvalue(),
            file_name=f"{spec.get('name', 'model')}.{definition.get('format', 'TMDL').lower()}.zip",
            mime="application/zip",
            key="download_definition",
        )


def _pick_existing_model(ws: dict, *, key_prefix: str) -> dict | None:
    """List models in ``ws``, let the user import one, and return its spec.

    Returns the parsed semantic-model spec (suitable for auditing or grounding
    a report) or ``None`` until a model has been imported. The imported spec is
    cached in session state keyed by ``key_prefix`` so it survives reruns.
    """
    try:
        models = client.list_semantic_models(ws["id"])
    except ApiError as exc:
        _show_api_error(exc)
        return None
    if not models:
        st.info("This workspace has no semantic models.")
        return None
    labels = {m.get("display_name", m.get("displayName", m.get("id"))): m for m in models}
    choice = st.selectbox("Semantic model", sorted(labels), key=f"{key_prefix}_pick")
    model = labels[choice]
    model_id = model.get("id")
    state_key = f"{key_prefix}_spec"
    id_key = f"{key_prefix}_model_id"
    if st.button("Import definition", key=f"{key_prefix}_import"):
        with st.spinner("Importing model definition from Fabric..."):
            try:
                result = client.import_semantic_model(
                    ws["id"],
                    model_id,
                    workspace_name=ws.get("name", ""),
                    item_name=choice,
                )
                st.session_state[state_key] = result["spec"]
                st.session_state[id_key] = model_id
            except ApiError as exc:
                _show_api_error(exc)
    if st.session_state.get(id_key) != model_id:
        st.caption("Import the definition to enable auditing / report grounding.")
        return None
    return st.session_state.get(state_key)


def _pick_existing_report(ws: dict, *, key_prefix: str) -> dict | None:
    """List reports in ``ws``, let the user import one, and return its spec."""
    try:
        reports = client.list_reports(ws["id"])
    except ApiError as exc:
        _show_api_error(exc)
        return None
    if not reports:
        st.info("This workspace has no reports.")
        return None
    labels = {r.get("display_name", r.get("displayName", r.get("id"))): r for r in reports}
    choice = st.selectbox("Report", sorted(labels), key=f"{key_prefix}_pick")
    report = labels[choice]
    report_id = report.get("id")
    state_key = f"{key_prefix}_spec"
    id_key = f"{key_prefix}_report_id"
    if st.button("Import definition", key=f"{key_prefix}_import"):
        with st.spinner("Importing report definition from Fabric..."):
            try:
                result = client.import_report(
                    ws["id"],
                    report_id,
                    workspace_name=ws.get("name", ""),
                    item_name=choice,
                )
                st.session_state[state_key] = result["spec"]
                st.session_state[id_key] = report_id
            except ApiError as exc:
                _show_api_error(exc)
    if st.session_state.get(id_key) != report_id:
        st.caption("Import the definition to enable auditing.")
        return None
    return st.session_state.get(state_key)


def _render_report_audit_outcome(result: dict) -> None:
    """Render a report-audit response (status banner + findings)."""
    status = result.get("status")
    count = result.get("agentFindingCount", 0)
    if status == "ok":
        st.success(f"Agent-enriched audit · {count} agent finding(s).")
    elif status == "deterministic":
        st.info("Deterministic audit (agent unavailable).")
    elif status == "error":
        st.warning(
            f"Audit fell back to deterministic. {result.get('error') or ''}"
        )
    report = result.get("report")
    if report:
        _render_audit_reports({report.get("feature", "report"): report})


def _render_definition_files(
    files: dict, *, key: str, download_name: str | None = None
) -> None:
    """Compact viewer for a set of definition files.

    Shows a file count, a single-file picker (one file at a time so the page
    stays compact) and a zip download of the whole definition.
    """
    if not files:
        st.info("No definition files stored.")
        return
    paths = sorted(files)
    st.caption(f"{len(paths)} file(s)")
    selected = st.selectbox("File", paths, key=f"{key}_file")
    text = files.get(selected, "")
    lang = (
        "json"
        if selected.endswith((".json", ".bim", ".pbism", ".pbir", ".platform"))
        else "text"
    )
    st.code(text[:12000], language=lang)

    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, body in files.items():
            zf.writestr(path, body)
    st.download_button(
        "Download all files (.zip)",
        data=buffer.getvalue(),
        file_name=f"{download_name or key}.zip",
        mime="application/zip",
        key=f"{key}_download",
    )


def _render_report_overview(report_spec: dict) -> None:
    """Show the report's semantic-model connection and ideated visuals."""
    dataset_name = report_spec.get("dataset_name")
    dataset_id = report_spec.get("dataset_id")
    pages = report_spec.get("pages", []) or []
    visual_count = sum(len(p.get("visuals", []) or []) for p in pages)

    cols = st.columns(3)
    cols[0].metric("Pages", len(pages))
    cols[1].metric("Visuals", visual_count)
    cols[2].metric("Connected model", dataset_name or dataset_id or "—")

    if dataset_name or dataset_id:
        st.caption(
            "Connected semantic model: "
            f"**{dataset_name or 'unnamed'}**"
            + (f" (`{dataset_id}`)" if dataset_id else "")
        )
    else:
        st.caption("No semantic-model binding recorded on this report yet.")

    for page in pages:
        title = page.get("display_name") or page.get("name") or "Page"
        visuals = page.get("visuals", []) or []
        with st.expander(f"{title} · {len(visuals)} visual(s)", expanded=False):
            if not visuals:
                st.caption("No visuals on this page.")
            for v in visuals:
                fields: list[str] = []
                for role_fields in (v.get("projections") or {}).values():
                    for proj in role_fields or []:
                        entity = proj.get("entity", "")
                        prop = proj.get("property", "")
                        ref = ".".join(p for p in (entity, prop) if p)
                        if ref:
                            fields.append(ref)
                label = v.get("title") or v.get("name") or "visual"
                vtype = v.get("visual_type", "?")
                field_text = ", ".join(fields) if fields else "—"
                st.markdown(f"- **{label}** · `{vtype}` → {field_text}")


with explore_tab:
    st.subheader("Schema explorer")
    ws = _select_workspace("explore_ws")
    if ws:
        source_kind = st.radio(
            "Data source type",
            options=["SQL endpoint", "Lakehouse (Direct Lake)"],
            horizontal=True,
            key="explore_source_kind",
            help=(
                "SQL endpoints support import / DirectQuery / Direct Lake. "
                "Lakehouses bind to OneLake to enable Direct Lake on OneLake."
            ),
        )
        is_lakehouse = source_kind.startswith("Lakehouse")

        if is_lakehouse:
            try:
                lakehouses = client.list_lakehouses(ws["id"])
            except ApiError as exc:
                lakehouses = []
                _show_api_error(exc)
            if lakehouses:
                lh_labels = {lh["name"]: lh for lh in lakehouses}
                lh_choice = st.selectbox(
                    "Lakehouse", list(lh_labels), key="explore_lh"
                )
                lh = lh_labels[lh_choice]
                server = lh.get("sql_endpoint_server")
                database = lh.get("name")
                if not server:
                    st.warning(
                        "This lakehouse has no SQL analytics endpoint yet. "
                        "Columns are read through that endpoint, so it must be "
                        "provisioned before extracting schema."
                    )
                source = {
                    "kind": "lakehouse",
                    "server": server,
                    "database": database,
                    "name": lh.get("name"),
                    "lakehouse_id": lh.get("id"),
                    "lakehouse_name": lh.get("name"),
                    "onelake_workspace_id": lh.get("workspace_id"),
                    "onelake_tables_path": lh.get("onelake_tables_path"),
                    "default_schema": lh.get("default_schema"),
                }
            else:
                st.info("No lakehouses in this workspace.")
                source = None
        else:
            try:
                endpoints = client.list_sql_endpoints(ws["id"])
            except ApiError as exc:
                endpoints = []
                _show_api_error(exc)
            if endpoints:
                ep_labels = {f"{e['name']} ({e['item_kind']})": e for e in endpoints}
                ep_choice = st.selectbox(
                    "SQL endpoint", list(ep_labels), key="explore_ep"
                )
                endpoint = ep_labels[ep_choice]
                source = {
                    "kind": "sql",
                    "server": endpoint["server"],
                    "database": endpoint["database"],
                    "name": endpoint["name"],
                }
            else:
                st.info("No SQL endpoints in this workspace.")
                source = None

        if source and source.get("server"):
            if st.button("Extract schema", key="explore_extract"):
                with st.spinner("Extracting schema..."):
                    try:
                        schemas = client.extract_schemas(
                            source["server"], source["database"]
                        )
                        st.session_state["explore_schemas"] = schemas
                        st.session_state["explore_source"] = source
                        # Default selection mirrors the legacy designer: every
                        # table for a lakehouse (views cannot join Direct Lake
                        # on OneLake), everything for a SQL endpoint.
                        if source["kind"] == "lakehouse":
                            default = {
                                f"{t['schema']}.{t['name']}"
                                for t in schemas
                                if t["object_type"] == "TABLE"
                            }
                        else:
                            default = {
                                f"{t['schema']}.{t['name']}" for t in schemas
                            }
                        st.session_state["explore_selected"] = default
                    except ApiError as exc:
                        _show_api_error(exc)

            schemas = st.session_state.get("explore_schemas")
            stored_source = st.session_state.get("explore_source")
            if schemas and stored_source == source:
                st.success(f"Extracted {len(schemas)} tables / views.")
                _render_explore_selection(schemas)


with model_tab:
    _model_design_sub, _model_audit_sub = st.tabs(
        ["Design new", "Audit existing"]
    )

with _model_design_sub:
    st.subheader("Design a semantic model (agentic)")
    source = st.session_state.get("explore_source")
    if not source:
        st.info("Extract a schema in the Explore tab first to enable design.")
    else:
        is_lakehouse = source.get("kind") == "lakehouse"
        selected_tables = sorted(st.session_state.get("explore_selected", set()))

        if is_lakehouse:
            st.caption(
                "Direct Lake on OneLake — lakehouse models bind to OneLake for "
                "refresh-free queries."
            )

        model_name = st.text_input("Model name", value=source.get("name") or "GeneratedModel")

        # Both lakehouse and SQL-endpoint sources default to Direct Lake — the
        # recommended Fabric storage mode for refresh-free queries over OneLake
        # / the SQL endpoint. Other modes remain available for import caching or
        # DirectQuery federation.
        if is_lakehouse:
            storage_options = ["directLake", "import"]
        else:
            storage_options = ["directLake", "import", "directQuery"]
        storage_mode = st.selectbox(
            "Storage mode",
            options=storage_options,
            help=(
                "Direct Lake binds the model to OneLake / the SQL endpoint for "
                "refresh-free queries (default); import caches data in the "
                "model; directQuery federates queries to the SQL endpoint."
            ),
        )

        # Direct Lake comes in two flavours: on OneLake (binds to the lakehouse
        # OneLake storage; tables only) and on SQL (binds to the SQL endpoint;
        # allows views with DirectQuery fallback).
        direct_lake_mode = "auto"
        if storage_mode == "directLake":
            has_onelake = bool(
                source.get("onelake_tables_path")
                or (source.get("onelake_workspace_id") and source.get("lakehouse_id"))
            )
            dl_options = (
                ["Direct Lake on OneLake (recommended)", "Direct Lake on SQL"]
                if has_onelake
                else ["Direct Lake on SQL"]
            )
            dl_label = st.radio(
                "Direct Lake type",
                options=dl_options,
                horizontal=True,
                help=(
                    "Direct Lake on OneLake reads Delta tables straight from "
                    "OneLake and never falls back to DirectQuery (preferred). "
                    "Direct Lake on SQL binds to the SQL analytics endpoint — "
                    "use it for SQL views or SQL-endpoint security."
                ),
            )
            direct_lake_mode = (
                "onelake" if dl_label.startswith("Direct Lake on OneLake") else "sql"
            )
            if not has_onelake:
                st.caption(
                    "This source has no OneLake binding, so only Direct Lake on "
                    "SQL is available. Pick a lakehouse source to use Direct Lake "
                    "on OneLake."
                )

        use_agent = st.checkbox("Use Foundry agent", value=True)
        instructions = st.text_area("Extra instructions (optional)", value="")

        if not selected_tables:
            st.warning("Select at least one table in the Explore tab to design a model.")

        if st.button("Design model", key="design", disabled=not selected_tables):
            with st.spinner("Designing model..."):
                try:
                    result = client.design_semantic_model(
                        server=source["server"],
                        database=source["database"],
                        model_name=model_name,
                        storage_mode=storage_mode,
                        source_kind=source.get("kind", "sql"),
                        use_agent=use_agent,
                        extra_instructions=instructions or None,
                        selected_tables=selected_tables,
                        lakehouse_id=source.get("lakehouse_id"),
                        lakehouse_name=source.get("lakehouse_name"),
                        onelake_workspace_id=source.get("onelake_workspace_id"),
                        onelake_tables_path=source.get("onelake_tables_path"),
                        default_schema=source.get("default_schema"),
                        direct_lake_mode=direct_lake_mode,
                    )
                    st.session_state["model_spec"] = result["spec"]
                    st.session_state.pop("model_suggestions", None)
                    st.session_state.pop("model_definition", None)
                    st.session_state.pop("model_audit", None)
                    # A freshly designed model is not published yet; drop any
                    # stale published id from a previous model.
                    st.session_state.pop("published_model_id", None)
                    st.session_state.pop("published_model_name", None)
                    st.session_state.pop("published_model_workspace", None)
                    badge = "agent" if result.get("usedAgent") else "deterministic"
                    st.success(
                        f"Model designed ({badge}) from {len(selected_tables)} "
                        "selected object(s)."
                    )
                except ApiError as exc:
                    _show_api_error(exc)
        spec = st.session_state.get("model_spec")

        if spec:
            st.markdown("#### Generated model")
            with st.expander("Model spec (JSON)", expanded=False):
                st.json(spec, expanded=False)

            _render_model_suggestions(source, spec, selected_tables)
            _render_model_definition(spec)

            st.markdown("#### Audit & publish")
            col1, col2 = st.columns(2)
            with col1:
                include_bpa = st.checkbox(
                    "Include Best Practice Analyzer", value=False, key="audit_bpa"
                )
                if st.button("Audit model", key="audit_model"):
                    with st.spinner("Auditing..."):
                        try:
                            reports = client.audit_semantic_model(
                                spec, include_bpa=include_bpa
                            )
                            st.session_state["model_audit"] = reports
                        except ApiError as exc:
                            _show_api_error(exc)
                audit = st.session_state.get("model_audit")
                if audit is not None:
                    _render_audit_reports(audit)
            with col2:
                ws_model = _select_workspace("publish_ws")
                display_name = st.text_input("Publish as", value=spec.get("name", "Model"))
                publish_fmt = st.radio(
                    "Publish format",
                    options=["TMDL", "TMSL"],
                    horizontal=True,
                    key="publish_format",
                )
                if ws_model and st.button("Publish model", key="publish_model"):
                    with st.spinner("Publishing to Fabric..."):
                        try:
                            created = client.publish_semantic_model(
                                workspace_id=ws_model["id"],
                                display_name=display_name,
                                spec=spec,
                                fmt=publish_fmt,
                            )
                            item = created.get("item", created)
                            new_id = item.get("id") if isinstance(item, dict) else None
                            if new_id:
                                # Remember the published model id so a report
                                # grounded on this session model can connect to
                                # it (byConnection) without re-entering the id.
                                st.session_state["published_model_id"] = new_id
                                st.session_state["published_model_name"] = spec.get(
                                    "name"
                                )
                                st.session_state["published_model_workspace"] = (
                                    ws_model["id"]
                                )
                            st.success(f"Published: {item}")
                        except ApiError as exc:
                            _show_api_error(exc)


with _model_audit_sub:
    st.subheader("Audit an existing semantic model")
    st.caption(
        "List the semantic models in a workspace, import a model's definition "
        "from Fabric, and run the deterministic (optionally BPA-enriched) audit."
    )
    ws_audit = _select_workspace("audit_existing_ws")
    if ws_audit:
        existing_spec = _pick_existing_model(ws_audit, key_prefix="audit_existing")
        if existing_spec:
            with st.expander("Model spec (JSON)", expanded=False):
                st.json(existing_spec, expanded=False)
            include_bpa_existing = st.checkbox(
                "Include Best Practice Analyzer",
                value=True,
                key="audit_existing_bpa",
            )
            if st.button("Audit model", key="audit_existing_model"):
                with st.spinner("Auditing..."):
                    try:
                        reports = client.audit_semantic_model(
                            existing_spec, include_bpa=include_bpa_existing
                        )
                        st.session_state["audit_existing_result"] = reports
                    except ApiError as exc:
                        _show_api_error(exc)
            existing_audit = st.session_state.get("audit_existing_result")
            if existing_audit is not None:
                _render_audit_reports(existing_audit)

                st.divider()
                st.markdown("#### Apply fixes")
                st.caption(
                    "Propose deterministic write-back fixes for usability, "
                    "Best Practice Analyzer and Copilot-readiness, choose which "
                    "to apply, and update the model definition in Fabric."
                )
                use_ai_fixes = st.checkbox(
                    "Include AI-enhanced fixes (descriptions & Q&A synonyms)",
                    value=False,
                    key="audit_existing_ai",
                    help=(
                        "Uses the Foundry agent to author business-friendly "
                        "descriptions and synonyms for objects that lack them. "
                        "Safe, additive fields only — never renames or rewrites DAX."
                    ),
                )
                if st.button("Suggest fixes", key="audit_existing_suggest"):
                    with st.spinner("Proposing fixes..."):
                        try:
                            usability = client.propose_model_usability_fixes(
                                existing_spec
                            ).get("suggestions", [])
                            bpa = client.propose_model_bpa_fixes(
                                existing_spec
                            ).get("suggestions", [])
                            copilot = client.propose_model_copilot_fixes(
                                existing_spec
                            ).get("suggestions", [])
                            ai_fixes: list[dict] = []
                            if use_ai_fixes:
                                with st.spinner("Asking the agent for descriptions..."):
                                    ai_resp = client.propose_model_ai_fixes(
                                        existing_spec
                                    )
                                ai_fixes = ai_resp.get("suggestions", [])
                                ai_status = ai_resp.get("status")
                                if ai_status == "unavailable":
                                    st.info(ai_resp.get("error") or "AI fixes unavailable.")
                                elif ai_status == "error":
                                    st.warning(
                                        "AI fixes unavailable: "
                                        + (ai_resp.get("error") or "agent error")
                                    )
                                elif not ai_fixes:
                                    st.caption("The agent proposed no new descriptions.")
                            # Deduplicate by (object_ref, field) so usability /
                            # BPA / Copilot / AI overlaps surface once. Usability
                            # wins ties, then BPA, then Copilot, then AI.
                            merged: dict[tuple[str, str], dict] = {}
                            for s in usability + bpa + copilot + ai_fixes:
                                key = (s.get("object_ref"), s.get("field"))
                                merged.setdefault(key, s)
                            st.session_state["audit_existing_fixes"] = list(
                                merged.values()
                            )
                        except ApiError as exc:
                            _show_api_error(exc)
                proposed = st.session_state.get("audit_existing_fixes")
                if proposed is not None:
                    selected = _suggestion_selector(
                        proposed, key_prefix="audit_existing_fix"
                    )
                    model_id = st.session_state.get("audit_existing_model_id")
                    if st.button(
                        f"Apply {len(selected)} change(s) & update model",
                        key="audit_existing_apply",
                        disabled=not selected or not model_id,
                    ):
                        with st.spinner("Applying fixes and updating model..."):
                            try:
                                accepted = [
                                    {**s, "status": "accepted"} for s in selected
                                ]
                                applied = client.apply_model_audit_suggestions(
                                    spec=existing_spec, suggestions=accepted
                                )
                                new_spec = applied["spec"]
                                result = client.update_semantic_model(
                                    workspace_id=ws_audit["id"],
                                    model_id=model_id,
                                    spec=new_spec,
                                    item_name=st.session_state.get(
                                        "audit_existing_pick", ""
                                    ),
                                )
                                st.session_state["audit_existing_spec"] = new_spec
                                st.session_state.pop("audit_existing_fixes", None)
                                st.success(
                                    f"Updated model in Fabric "
                                    f"({result.get('status')})."
                                )
                                verification = result.get("verification")
                                if verification:
                                    with st.expander(
                                        "Verification", expanded=False
                                    ):
                                        st.json(verification, expanded=False)
                                # Re-run the audit so the health view refreshes.
                                try:
                                    st.session_state[
                                        "audit_existing_result"
                                    ] = client.audit_semantic_model(
                                        new_spec,
                                        include_bpa=include_bpa_existing,
                                    )
                                except ApiError:
                                    pass
                                st.rerun()
                            except ApiError as exc:
                                _show_api_error(exc)


with report_tab:
    _report_build_sub, _report_audit_sub = st.tabs(
        ["Build from model", "Audit existing"]
    )

with _report_build_sub:
    st.subheader("Suggest a report (agentic)")
    st.caption(
        "Ground the report on a semantic model — either the model you just "
        "designed in this session, or an existing model imported from a "
        "workspace."
    )

    # Base semantic model selection.
    designed_spec = st.session_state.get("model_spec")
    base_options: list[str] = []
    if designed_spec:
        base_options.append("Designed model (this session)")
    base_options.append("Existing model from a workspace")
    base_choice = st.radio(
        "Base semantic model",
        options=base_options,
        key="report_base_source",
        horizontal=True,
    )

    base_spec: dict | None = None
    if base_choice.startswith("Designed"):
        base_spec = designed_spec
        if base_spec:
            st.caption(
                f"Grounding on the designed model "
                f"**{base_spec.get('name', 'model')}**."
            )
            published_id = st.session_state.get("published_model_id")
            published_matches = published_id and (
                st.session_state.get("published_model_name")
                == base_spec.get("name")
            )
            if published_matches:
                st.success(
                    f"This model is published — the report will connect to "
                    f"it (`{published_id}`)."
                )
            else:
                st.warning(
                    "This model has not been published yet. Publish it from "
                    "the **Semantic model** tab first so the report can "
                    "connect to it; otherwise Fabric will reject the report."
                )
    else:
        ws_report = _select_workspace("report_base_ws")
        if ws_report:
            base_spec = _pick_existing_model(ws_report, key_prefix="report_base")

    if not base_spec:
        st.info("Select or import a base semantic model to ground the report.")
    else:
        report_name = st.text_input("Report name", value="GeneratedReport")
        use_agent_r = st.checkbox(
            "Use Foundry agent", value=True, key="report_agent"
        )
        # Bind the report to the semantic model: an existing model carries a
        # Fabric dataset id; a model designed this session gets its id once it
        # is published to Fabric (captured in session state at publish time).
        if base_choice.startswith("Designed"):
            dataset_id = (
                st.session_state.get("published_model_id")
                if st.session_state.get("published_model_name")
                == base_spec.get("name")
                else None
            )
        else:
            dataset_id = st.session_state.get("report_base_model_id")
        dataset_name = base_spec.get("name")

        if st.button("Suggest report", key="suggest"):
            with st.spinner("Designing report..."):
                try:
                    result = client.suggest_report(
                        model=base_spec,
                        report_name=report_name,
                        dataset_id=dataset_id,
                        dataset_name=dataset_name,
                        use_agent=use_agent_r,
                    )
                    st.session_state["report_spec"] = result["spec"]
                    st.success(f"Report suggested ({result.get('status')}).")
                except ApiError as exc:
                    _show_api_error(exc)
        report_spec = st.session_state.get("report_spec")
        if report_spec:
            _render_report_overview(report_spec)
            with st.expander("Report spec (JSON)", expanded=False):
                st.json(report_spec, expanded=False)

            st.markdown("#### Report definition")
            if st.button("Build report definition", key="build_report_def"):
                with st.spinner("Rendering report definition..."):
                    try:
                        st.session_state["report_definition"] = (
                            client.build_report_definition(report_spec)
                        )
                    except ApiError as exc:
                        _show_api_error(exc)
            report_def = st.session_state.get("report_definition")
            if report_def:
                _render_definition_files(
                    report_def.get("files", {}),
                    key="report_files",
                    download_name=report_spec.get("name", "report"),
                )

            if st.button("Audit report", key="audit_report"):
                with st.spinner("Auditing report..."):
                    try:
                        result = client.audit_report(report_spec)
                        st.session_state["report_audit"] = result
                    except ApiError as exc:
                        _show_api_error(exc)
            report_audit = st.session_state.get("report_audit")
            if report_audit is not None:
                _render_report_audit_outcome(report_audit)

            st.divider()
            st.markdown("#### Publish to Fabric")
            st.caption(
                "Create the report as a new item in a workspace, connected to "
                "its semantic model."
            )
            ws_publish = _select_workspace("report_publish_ws")
            if ws_publish:
                st.warning(
                    "Publishing creates a new report item in the selected "
                    "workspace."
                )
                spec_dataset_id = report_spec.get("dataset_id")
                if spec_dataset_id:
                    st.caption(
                        f"The report will connect to semantic model "
                        f"`{spec_dataset_id}`."
                    )
                    bind_dataset_id = spec_dataset_id
                else:
                    st.info(
                        "This report is not bound to a published semantic "
                        "model yet. Fabric only accepts reports that connect "
                        "to an existing semantic model by id. Publish the "
                        "semantic model first, then paste its id below."
                    )
                    bind_dataset_id = st.text_input(
                        "Semantic model id to connect to",
                        key="report_publish_dataset_id",
                        help="The Fabric item id of the published semantic "
                        "model this report should connect to.",
                    ).strip()
                confirm = st.checkbox(
                    "I want to create this report in Fabric.",
                    key="report_publish_confirm",
                )
                if st.button(
                    "Publish report to Fabric",
                    key="publish_report",
                    disabled=not confirm or not bind_dataset_id,
                ):
                    with st.spinner("Publishing report to Fabric..."):
                        try:
                            created = client.publish_report(
                                workspace_id=ws_publish["id"],
                                display_name=report_spec.get("name", report_name),
                                spec=report_spec,
                                description="Generated by fabric-autopilot",
                                dataset_id=bind_dataset_id or None,
                            )
                            st.success("Report published to Fabric.")
                            st.json(created, expanded=False)
                        except ApiError as exc:
                            _show_api_error(exc)


with _report_audit_sub:
    st.subheader("Audit an existing report")
    st.caption(
        "List the reports in a workspace, import a report's definition from "
        "Fabric, and run the formatting / accessibility audit."
    )
    ws_report_audit = _select_workspace("report_audit_ws")
    if ws_report_audit:
        existing_report_spec = _pick_existing_report(
            ws_report_audit, key_prefix="report_audit"
        )
        if existing_report_spec:
            with st.expander("Report spec (JSON)", expanded=False):
                st.json(existing_report_spec, expanded=False)
            if st.button("Audit report", key="audit_existing_report"):
                with st.spinner("Auditing report..."):
                    try:
                        result = client.audit_report(existing_report_spec)
                        st.session_state["report_audit_existing"] = result
                    except ApiError as exc:
                        _show_api_error(exc)
            existing_report_audit = st.session_state.get("report_audit_existing")
            if existing_report_audit is not None:
                _render_report_audit_outcome(existing_report_audit)

                st.divider()
                st.markdown("#### Apply fixes")
                st.caption(
                    "Propose theme / formatting remediations, choose which to "
                    "apply, and update the report definition in Fabric."
                )
                if st.button("Suggest fixes", key="report_audit_suggest"):
                    with st.spinner("Proposing fixes..."):
                        try:
                            st.session_state["report_audit_fixes"] = (
                                client.propose_report_theme_fixes(
                                    existing_report_spec
                                ).get("suggestions", [])
                            )
                        except ApiError as exc:
                            _show_api_error(exc)
                proposed_report = st.session_state.get("report_audit_fixes")
                if proposed_report is not None:
                    selected_report = _suggestion_selector(
                        proposed_report, key_prefix="report_audit_fix"
                    )
                    report_id = st.session_state.get("report_audit_report_id")
                    if st.button(
                        f"Apply {len(selected_report)} change(s) & update report",
                        key="report_audit_apply",
                        disabled=not selected_report or not report_id,
                    ):
                        with st.spinner("Applying fixes and updating report..."):
                            try:
                                accepted = [
                                    {**s, "status": "accepted"}
                                    for s in selected_report
                                ]
                                applied = client.apply_report_audit_suggestions(
                                    spec=existing_report_spec,
                                    suggestions=accepted,
                                )
                                new_spec = applied["spec"]
                                result = client.update_report(
                                    workspace_id=ws_report_audit["id"],
                                    report_id=report_id,
                                    spec=new_spec,
                                    item_name=st.session_state.get(
                                        "report_audit_pick", ""
                                    ),
                                )
                                st.session_state["report_audit_spec"] = new_spec
                                st.session_state.pop("report_audit_fixes", None)
                                st.success(
                                    f"Updated report in Fabric "
                                    f"({result.get('status')})."
                                )
                                verification = result.get("verification")
                                if verification:
                                    with st.expander(
                                        "Verification", expanded=False
                                    ):
                                        st.json(verification, expanded=False)
                                try:
                                    st.session_state[
                                        "report_audit_existing"
                                    ] = client.audit_report(new_spec)
                                except ApiError:
                                    pass
                                st.rerun()
                            except ApiError as exc:
                                _show_api_error(exc)


with artifact_tab:
    st.subheader("Stored artifacts")
    kind = st.selectbox("Kind", ["", "semanticModels", "reports"], key="artifact_kind")
    if st.button("Refresh artifacts", key="refresh_artifacts"):
        try:
            items = client.list_artifacts(kind=kind or None)
            st.session_state["artifacts"] = items
        except ApiError as exc:
            _show_api_error(exc)
    items = st.session_state.get("artifacts")
    if items is not None:
        if items:
            st.dataframe(items, use_container_width=True)

            labels = {
                f"{it.get('kind')} · "
                f"{it.get('item_name') or it.get('item_id')} "
                f"({it.get('workspace_name') or it.get('workspace_id')})": it
                for it in items
            }
            picked_label = st.selectbox(
                "View definition files", sorted(labels), key="artifact_pick"
            )
            picked = labels[picked_label]
            if st.button("Load files", key="artifact_load"):
                try:
                    st.session_state["artifact_definition"] = (
                        client.load_artifact_definition(
                            kind=picked.get("kind"),
                            workspace_id=picked.get("workspace_id"),
                            item_id=picked.get("item_id"),
                            workspace_name=picked.get("workspace_name", ""),
                            item_name=picked.get("item_name", ""),
                        )
                    )
                    st.session_state["artifact_definition_key"] = picked_label
                except ApiError as exc:
                    _show_api_error(exc)
            definition = st.session_state.get("artifact_definition")
            if (
                definition is not None
                and st.session_state.get("artifact_definition_key") == picked_label
            ):
                metadata = definition.get("metadata", {})
                if metadata:
                    with st.expander("Metadata", expanded=False):
                        st.json(metadata, expanded=False)
                _render_definition_files(
                    definition.get("files", {}),
                    key="artifact_files",
                    download_name=(
                        picked.get("item_name")
                        or picked.get("item_id")
                        or "artifact"
                    ),
                )
        else:
            st.info("No artifacts stored for this tenant yet.")


# ---------------------------------------------------------------------------
# Agent Studio — run the multi-agent orchestration pipeline end to end
# ---------------------------------------------------------------------------

_AGENT_ICON = {
    "plan": "🧭",
    "step": "⚙️",
    "message": "💬",
    "artifact": "📦",
    "approval": "🔐",
    "final": "✅",
}


def _render_agent_event(event: dict) -> None:
    kind = event.get("kind", "step")
    agent = event.get("agent", "orchestrator")
    icon = _AGENT_ICON.get(kind, "•")
    status = event.get("status", "ok")
    text = event.get("text", "")
    flag = " ⚠️" if status == "error" else ""
    st.markdown(f"{icon} **{agent}** · _{kind}_{flag}  \n{text}")


def _published_objects(result: dict | None) -> list[dict]:
    """Normalize a publish-workflow result into summary rows for display.

    Handles the combined model+report result (``{"published": [...]}``) and a
    single-item result (a serialized ``CreatedItem`` with snake_case keys).
    """
    if not isinstance(result, dict):
        return []
    published = result.get("published")
    if isinstance(published, list):
        return published
    return [
        {
            "kind": result.get("type"),
            "id": result.get("id"),
            "displayName": result.get("display_name"),
            "workspaceId": result.get("workspace_id"),
            "status": result.get("status"),
            "webUrl": result.get("web_url"),
        }
    ]


def _render_published_summary(result: dict | None) -> None:
    """Render a published-objects summary with a shareable link per object."""
    objects = _published_objects(result)
    if not objects:
        return
    st.markdown("#### Published objects")
    for obj in objects:
        name = obj.get("displayName") or obj.get("id") or "(item)"
        kind = obj.get("kind") or "Item"
        status = obj.get("status") or "Created"
        url = obj.get("webUrl")
        if url:
            st.markdown(f"- **{kind}** — [{name}]({url}) · `{status}`")
        else:
            st.markdown(f"- **{kind}** — {name} · `{status}`")



def _select_model_for_chat(ws: dict, *, key: str) -> dict | None:
    """List semantic models in ``ws`` and let the user pick one (no import).

    The Power BI Modeling MCP server connects by *name*, so the chat surface
    only needs the model's display name (and ids for context); it does not
    import the definition the way the Semantic Models tab does.
    """
    try:
        models = client.list_semantic_models(ws["id"])
    except ApiError as exc:
        _show_api_error(exc)
        return None
    if not models:
        st.info("This workspace has no semantic models.")
        return None
    labels = {
        m.get("display_name", m.get("displayName", m.get("id"))): m for m in models
    }
    choice = st.selectbox("Semantic model", sorted(labels), key=key)
    return labels.get(choice)


with ask_model_tab:
    st.subheader("Ask Your Model")
    st.caption(
        "Ask a natural-language question against a Fabric semantic model. "
        "The platform translates it to DAX (grounded in the model's TMDL "
        "spec) and executes it through the official Power BI Modeling MCP "
        "server's `dax_query_operations` tool. Read-only — no model changes."
    )

    _ask_mcp_live = False
    try:
        _ask_team = client.agent_team_status()
        _ask_mcp = _ask_team.get("powerBiModelingMcp", {})
        _ask_mcp_live = bool(_ask_mcp.get("enabled"))
        _ask_badge = (
            "🟢 DAX execution on" if _ask_mcp_live else "⚪ DAX execution off"
        )
        st.caption(
            f"Power BI Modeling MCP: {_ask_badge} · transport "
            f"`{_ask_mcp.get('transport', 'stdio')}`"
        )
    except ApiError as exc:
        _show_api_error(exc)

    if not _ask_mcp_live:
        st.info(
            "DAX execution via the Power BI Modeling MCP server is not "
            "active in this environment. You can still translate questions "
            "to DAX, but the query will not be executed."
        )

    ask_ws = _select_workspace("ask_model_ws")
    ask_model = (
        _select_model_for_chat(ask_ws, key="ask_model_model") if ask_ws else None
    )

    ask_model_name = (
        ask_model.get(
            "display_name", ask_model.get("displayName", ask_model.get("id"))
        )
        if ask_model
        else None
    )
    ask_target = f"{ask_ws['id'] if ask_ws else ''}::{ask_model_name or ''}"
    if st.session_state.get("ask_model_target") != ask_target:
        st.session_state["ask_model_target"] = ask_target
        st.session_state["ask_model_history"] = []

    ask_history: list[dict] = st.session_state.setdefault("ask_model_history", [])

    if ask_model:
        head = st.columns([4, 1])
        head[0].markdown(
            f"**Connected target:** `{ask_model_name}` in `{ask_ws['name']}`"
        )
        if head[1].button("Clear history", key="ask_model_clear"):
            st.session_state["ask_model_history"] = []
            st.rerun()

        with st.form("ask_model_form", clear_on_submit=False):
            question = st.text_area(
                "Question",
                key="ask_model_question",
                placeholder=(
                    "e.g. top 10 customers by total sales · how many orders · "
                    "average revenue by country"
                ),
                height=80,
            )
            row = st.columns([1, 1, 1])
            top_n = row[0].number_input(
                "Default row cap (top-N / browse)",
                min_value=1,
                max_value=10000,
                value=50,
                step=10,
                key="ask_model_topn",
            )
            auto_import = row[1].checkbox(
                "Auto-import if needed",
                value=False,
                key="ask_model_auto_import",
                help=(
                    "If the model definition hasn't been imported yet, fetch "
                    "it from Fabric on the fly. Otherwise the API asks you "
                    "to import it explicitly first — so DAX is grounded in "
                    "a definition you've reviewed."
                ),
            )
            refresh = row[2].checkbox(
                "Refresh definition",
                value=False,
                key="ask_model_refresh",
                help=(
                    "Re-import the model definition from Fabric even if a "
                    "cached copy exists. Use this after the model changed."
                ),
            )
            submitted = st.form_submit_button("Ask", type="primary")

        if submitted and question.strip():
            with st.spinner("Translating to DAX and running query…"):
                try:
                    resp = client.ask_semantic_model(
                        workspace_id=ask_ws["id"],
                        workspace_name=ask_ws["name"],
                        model_id=ask_model.get("id"),
                        model_name=ask_model_name,
                        question=question.strip(),
                        top_n=int(top_n),
                        auto_import=bool(auto_import),
                        refresh=bool(refresh),
                    )
                except ApiError as exc:
                    _show_api_error(exc)
                    # When the API tells us the model definition is missing,
                    # offer a one-click way to import it and retry.
                    if "has not been imported" in (exc.detail or ""):
                        st.session_state["ask_model_needs_import"] = True
                    resp = None
            if resp is not None:
                st.session_state.pop("ask_model_needs_import", None)
                if resp.get("imported_now"):
                    st.info(
                        "The model definition was imported from Fabric just "
                        "now — subsequent questions will reuse the cached "
                        "spec."
                    )
                ask_history.insert(0, resp)
                # Cap history so the page stays light.
                st.session_state["ask_model_history"] = ask_history[:25]
                st.rerun()

        if st.session_state.get("ask_model_needs_import"):
            st.warning(
                "This model has not been imported yet. Import it once so "
                "DAX is built against a definition you've reviewed."
            )
            if st.button("Import model now", key="ask_model_import_now"):
                with st.spinner("Importing model definition from Fabric…"):
                    try:
                        client.import_semantic_model(
                            workspace_id=ask_ws["id"],
                            model_id=ask_model.get("id"),
                            workspace_name=ask_ws["name"],
                            item_name=ask_model_name or "",
                        )
                        st.session_state.pop("ask_model_needs_import", None)
                        st.success(
                            "Model imported. Ask your question again to run it."
                        )
                    except ApiError as exc:
                        _show_api_error(exc)

        if not ask_history:
            st.caption("Ask a question to see the generated DAX and results here.")
        else:
            for idx, turn in enumerate(ask_history):
                tr = turn.get("translation", {})
                with st.expander(
                    f"❓ {turn.get('question', '')[:90]}",
                    expanded=(idx == 0),
                ):
                    cols = st.columns([3, 1])
                    cols[0].markdown(
                        f"**Intent:** `{tr.get('intent', '?')}` · "
                        f"**Confidence:** {tr.get('confidence', '?')}"
                    )
                    cols[1].markdown(
                        "**Executed:** "
                        + ("✅" if turn.get("executed") else "⚠️")
                    )
                    if tr.get("explanation"):
                        st.caption(tr["explanation"])
                    if tr.get("warnings"):
                        for warn in tr["warnings"]:
                            st.warning(warn)

                    st.markdown("**Generated DAX**")
                    st.code(tr.get("dax", ""), language="dax")

                    if turn.get("executed"):
                        columns = turn.get("columns") or []
                        rows = turn.get("rows") or []
                        if rows:
                            try:
                                import pandas as pd  # type: ignore

                                df = pd.DataFrame(rows, columns=columns or None)
                                st.dataframe(df, use_container_width=True)
                            except Exception:  # pragma: no cover - render fallback
                                st.write({"columns": columns, "rows": rows})
                        else:
                            st.info("Query returned no rows.")
                    elif turn.get("error"):
                        st.error(f"Execution failed: {turn['error']}")

                    calls = turn.get("calls") or []
                    if calls:
                        st.markdown("**MCP tool transcript**")
                        for call in calls:
                            st.markdown(
                                f"- **{call.get('tool', '?')}**"
                                + (" · ❌ error" if call.get("is_error") else "")
                            )
                            st.code(
                                json.dumps(
                                    call.get("arguments", {}), indent=2, default=str
                                ),
                                language="json",
                            )
                            text = call.get("text") or ""
                            if text:
                                st.text(
                                    text[:4000]
                                    + ("…" if len(text) > 4000 else "")
                                )
                            structured = call.get("structured")
                            if structured is not None:
                                st.json(structured)
    else:
        st.caption("Select a workspace and semantic model to start asking.")


with model_chat_tab:
    st.subheader("Model Chat")
    st.caption(
        "Edit an existing semantic model in natural language. Pick a workspace "
        "and model, then describe the change — new measures, columns, "
        "relationships, renames, descriptions, RLS, calculation groups, "
        "translations, and more. Requests are applied to the live model in "
        "Fabric through the official Power BI Modeling MCP server."
    )

    _mcp_live = False
    try:
        _team = client.agent_team_status()
        _mcp = _team.get("powerBiModelingMcp", {})
        _mcp_live = bool(_mcp.get("enabled"))
        _badge = "🟢 live editing on" if _mcp_live else "⚪ live editing off"
        st.caption(
            f"Power BI Modeling MCP: {_badge} · transport "
            f"`{_mcp.get('transport', 'stdio')}`"
        )
    except ApiError as exc:
        _show_api_error(exc)

    if not _mcp_live:
        st.info(
            "Live model editing is not active in this environment. You can "
            "still chat to preview what would happen — responses will explain "
            "the prerequisites needed to apply changes."
        )

    mc_ws = _select_workspace("model_chat_ws")
    mc_model = (
        _select_model_for_chat(mc_ws, key="model_chat_model") if mc_ws else None
    )

    mc_model_name = (
        mc_model.get(
            "display_name", mc_model.get("displayName", mc_model.get("id"))
        )
        if mc_model
        else None
    )
    target = f"{mc_ws['id'] if mc_ws else ''}::{mc_model_name or ''}"
    if st.session_state.get("model_chat_target") != target:
        st.session_state["model_chat_target"] = target
        st.session_state["model_chat_history"] = []

    history: list[dict] = st.session_state.setdefault("model_chat_history", [])

    if mc_model:
        head = st.columns([4, 1])
        head[0].markdown(
            f"**Connected target:** `{mc_model_name}` in `{mc_ws['name']}`"
        )
        if head[1].button("Clear chat", key="model_chat_clear"):
            st.session_state["model_chat_history"] = []
            st.rerun()

        for turn in history:
            with st.chat_message(
                "user" if turn.get("role") == "user" else "assistant"
            ):
                st.markdown(turn.get("content", ""))

        prompt = st.chat_input(
            "Describe a change to this model…", key="model_chat_input"
        )
        if prompt:
            prior = list(history)
            history.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("Working on the model…"):
                    try:
                        resp = client.model_chat(
                            workspace_name=mc_ws["name"],
                            model_name=mc_model_name,
                            message=prompt,
                            history=prior,
                            workspace_id=mc_ws.get("id"),
                            model_id=mc_model.get("id"),
                        )
                    except ApiError as exc:
                        _show_api_error(exc)
                        resp = None
                if resp is not None:
                    reply = resp.get("reply", "")
                    st.markdown(reply)
                    history.append({"role": "assistant", "content": reply})
                    if resp.get("usedAgent"):
                        st.caption("✅ Applied via the Power BI Modeling MCP server.")
                    elif resp.get("requires"):
                        st.caption(
                            "ℹ️ Preview only — needs: "
                            + ", ".join(resp["requires"])
                        )
            st.rerun()
    else:
        st.caption("Select a workspace and semantic model to start chatting.")


with agent_tab:
    st.subheader("Agent Studio")
    st.caption(
        "Run the multi-agent pipeline end to end: schema → semantic model → "
        "audit → report → audit. Agent-first with deterministic fallback; "
        "publishing is gated behind explicit approval."
    )

    try:
        team = client.agent_team_status()
    except ApiError as exc:
        team = None
        _show_api_error(exc)

    if team:
        mode = team.get("mode", "deterministic")
        cols = st.columns(3)
        cols[0].metric("Mode", "Foundry" if mode == "foundry" else "Deterministic")
        cols[1].metric("Roles", len(team.get("roles", [])))
        cols[2].metric(
            "Live modeling MCP",
            "on" if team.get("powerBiModelingMcp", {}).get("enabled") else "off",
        )
        with st.expander("Team capabilities", expanded=False):
            st.json(team, expanded=False)

    st.markdown("#### Objective")
    objective = st.text_input(
        "What should the team accomplish?",
        value="Design a semantic model and a starter report.",
        key="agent_objective",
    )

    ws = _select_workspace("agent_workspace")
    source = _select_agent_source("agent", ws)
    server = (source or {}).get("server")
    database = (source or {}).get("database")
    is_lakehouse = bool(source and source.get("kind") == "lakehouse")

    if is_lakehouse:
        storage_mode = "directLake"
        # Direct Lake on SQL is the proven-reliable binding for lakehouse
        # sources: the partitions carry schemaName "dbo" (extracted from the SQL
        # analytics endpoint, which always surfaces lakehouse tables under dbo),
        # which resolves correctly against Sql.Database. The OneLake binding
        # (AzureStorage.DataLake) instead resolves to /Tables/dbo/<table>, a
        # path the model cannot read when the lakehouse is not schema-enabled —
        # producing "source tables do not exist or access was denied" on
        # refresh. Default to SQL; OneLake remains available for schema-enabled
        # lakehouses.
        dl_label = st.radio(
            "Direct Lake type",
            options=["Direct Lake on SQL (recommended)", "Direct Lake on OneLake"],
            horizontal=True,
            key="agent_dl_mode",
            help=(
                "Direct Lake on SQL binds to the lakehouse SQL analytics "
                "endpoint and resolves the dbo schema reliably (recommended). "
                "Direct Lake on OneLake reads Delta tables straight from "
                "OneLake — only use it when the lakehouse is schema-enabled, "
                "otherwise refresh fails with 'source tables do not exist'."
            ),
        )
        direct_lake_mode = (
            "onelake" if dl_label.startswith("Direct Lake on OneLake") else "sql"
        )
        if direct_lake_mode == "sql":
            st.caption(
                "Direct Lake on SQL — binds to the lakehouse SQL analytics "
                "endpoint; the dbo-qualified tables resolve the same way as a "
                "working manually-built model."
            )
        else:
            st.caption(
                "Direct Lake on OneLake — binds to OneLake via the model's own "
                "identity. Requires a schema-enabled lakehouse so the "
                "dbo-qualified tables exist at /Tables/dbo/."
            )
    else:
        # Mirror the Semantic Models designer so the source connection is built
        # the same way: default to Direct Lake (refresh-free) rather than
        # import. An import model publishes Sql.Database partitions that fail to
        # refresh without a bound data-source credential — the exact "source
        # tables do not exist or access was denied" error seen on agent models.
        storage_mode = st.selectbox(
            "Storage mode",
            options=["directLake", "import", "directQuery"],
            key="agent_storage_mode",
            help=(
                "Direct Lake binds the model to the SQL analytics endpoint for "
                "refresh-free queries (default); import caches data and needs a "
                "bound data-source credential to refresh; directQuery federates "
                "queries to the endpoint. Pick a lakehouse source above to "
                "publish Direct Lake on OneLake."
            ),
        )
        # SQL-endpoint sources carry no OneLake binding, so Direct Lake here
        # resolves to Direct Lake on SQL (matches the designer for SQL sources).
        direct_lake_mode = "sql" if storage_mode == "directLake" else "auto"

    model_name = st.text_input(
        "Model name", value="Analytics Model", key="agent_model_name"
    )
    col_a, col_b = st.columns(2)
    include_report = col_a.checkbox("Also design a report", value=True, key="agent_report")
    use_agent = col_b.checkbox("Prefer Foundry agents", value=True, key="agent_use_agent")
    extra = st.text_area(
        "Additional requirements (optional)", key="agent_extra", height=80
    )

    if st.button("Run agent team", type="primary", key="agent_run"):
        progress = st.empty()
        events: list[dict] = []
        artifacts: dict = {}
        used_agent = False
        status = "ok"
        error: str | None = None
        try:
            with st.spinner("Orchestrating the agent team…"):
                # Stream Server-Sent Events so long-running steps (live schema
                # extraction + LLM design) report progress and never trip the
                # blocking read timeout a single-JSON call would hit.
                for event in client.orchestrate_stream(
                    objective=objective,
                    server=server or None,
                    database=database or None,
                    model_name=model_name or None,
                    workspace_id=(ws or {}).get("id"),
                    storage_mode=storage_mode,
                    source_kind=(source or {}).get("kind", "sql"),
                    lakehouse_id=(source or {}).get("lakehouse_id"),
                    lakehouse_name=(source or {}).get("lakehouse_name"),
                    onelake_workspace_id=(source or {}).get("onelake_workspace_id"),
                    onelake_tables_path=(source or {}).get("onelake_tables_path"),
                    default_schema=(source or {}).get("default_schema"),
                    direct_lake_mode=direct_lake_mode,
                    use_agent=use_agent,
                    include_report=include_report,
                    extra_instructions=extra or None,
                ):
                    events.append(event)
                    data = event.get("data") or {}
                    if event.get("kind") == "artifact":
                        artifacts.update(data)
                    if data.get("usedAgent"):
                        used_agent = True
                    if event.get("status") == "error":
                        status = "error"
                        error = event.get("text") or error
                    label = event.get("text") or event.get("kind", "working")
                    progress.markdown(f"⏳ {label}")
            progress.empty()
            st.session_state["agent_result"] = {
                "objective": objective,
                "status": status,
                "usedAgent": used_agent,
                "error": error,
                "events": events,
                "artifacts": artifacts,
            }
        except ApiError as exc:
            progress.empty()
            _show_api_error(exc)

    result = st.session_state.get("agent_result")
    if result:
        badge = "Foundry agents" if result.get("usedAgent") else "Deterministic"
        st.success(f"Run complete · {badge}")
        st.markdown("#### Transcript")
        for event in result.get("events", []):
            _render_agent_event(event)

        artifacts = result.get("artifacts", {})
        if artifacts:
            st.markdown("#### Artifacts")
            model_spec = artifacts.get("model")
            report_spec = artifacts.get("report")
            with st.expander("Semantic model spec", expanded=False):
                st.json(model_spec, expanded=False)
            if artifacts.get("modelAudit"):
                with st.expander("Model audit", expanded=False):
                    st.json(artifacts["modelAudit"], expanded=False)
            if report_spec:
                with st.expander("Report spec", expanded=False):
                    st.json(report_spec, expanded=False)
            if artifacts.get("reportAudit"):
                with st.expander("Report audit", expanded=False):
                    st.json(artifacts["reportAudit"], expanded=False)

            # -- approval-gated publish ----------------------------------
            if model_spec and ws:
                has_report = bool(report_spec)
                st.markdown("#### Publish (requires approval)")
                if has_report:
                    st.caption(
                        "One approval publishes the semantic model, then binds "
                        "and publishes the report to it."
                    )
                publish_name = st.text_input(
                    "Model display name", value=model_name, key="agent_publish_name"
                )
                report_publish_name = None
                if has_report:
                    default_report_name = (
                        report_spec.get("name")
                        or f"{publish_name or model_name} Report"
                    )
                    report_publish_name = st.text_input(
                        "Report display name",
                        value=default_report_name,
                        key="agent_publish_report_name",
                    )
                stage_label = (
                    "Stage model + report publish"
                    if has_report
                    else "Stage model publish"
                )
                if st.button(stage_label, key="agent_stage_publish"):
                    try:
                        if has_report:
                            wf = client.create_publish_model_report_workflow(
                                workspace_id=ws["id"],
                                model_display_name=publish_name or model_name,
                                model_spec=model_spec,
                                report_display_name=(
                                    report_publish_name
                                    or f"{publish_name or model_name} Report"
                                ),
                                report_spec=report_spec,
                            )
                        else:
                            wf = client.create_publish_model_workflow(
                                workspace_id=ws["id"],
                                display_name=publish_name or model_name,
                                spec=model_spec,
                            )
                        st.session_state["agent_workflow"] = wf
                    except ApiError as exc:
                        _show_api_error(exc)

    workflow = st.session_state.get("agent_workflow")
    if workflow:
        kind_label = (
            "model + report"
            if workflow.get("kind") == "publish-model-report"
            else "model"
        )
        st.info(
            f"Workflow `{workflow['id']}` ({kind_label}) · "
            f"state: **{workflow['state']}**"
        )
        if workflow["state"] == "pending":
            approve_col, reject_col = st.columns(2)
            approve_label = (
                "Approve & publish both"
                if workflow.get("kind") == "publish-model-report"
                else "Approve & publish"
            )
            if approve_col.button(approve_label, type="primary", key="agent_approve"):
                try:
                    with st.spinner("Publishing to Fabric… this can take a moment."):
                        st.session_state["agent_workflow"] = (
                            client.decide_agent_workflow(
                                workflow["id"], approve=True
                            )
                        )
                    st.rerun()
                except ApiError as exc:
                    _show_api_error(exc)
            if reject_col.button("Reject", key="agent_reject"):
                try:
                    with st.spinner("Discarding workflow…"):
                        st.session_state["agent_workflow"] = (
                            client.decide_agent_workflow(
                                workflow["id"], approve=False
                            )
                        )
                    st.rerun()
                except ApiError as exc:
                    _show_api_error(exc)
        elif workflow["state"] == "approved":
            st.success("Published to Fabric.")
            _render_published_summary(workflow.get("result"))
            with st.expander("Raw publish result", expanded=False):
                st.json(workflow.get("result"), expanded=False)
        elif workflow["state"] == "failed":
            st.error(f"Publish failed: {workflow.get('error')}")

