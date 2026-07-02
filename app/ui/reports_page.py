"""Reports page: list/import/audit existing reports and build new ones.

The **Build** flow is semantic-model-first: the user picks a semantic model,
its definition is fetched and parsed, a grounded starter report is scaffolded
from the *real* fields and measures, audited for formatting/accessibility, and
only created in Fabric on an explicit opt-in.
"""

from __future__ import annotations

import streamlit as st

from app.artifacts import ArtifactRef
from app.fabric_client import FabricApiError
from app.intelligence.audit import (
    audit_report,
    audit_report_with_agent,
    load_brand_theme,
    propose_theme_remediation,
    report_agent_available,
)
from app.intelligence.report_builder import suggest_report
from app.intelligence.report_definition import build_report_definition, parse_report
from app.intelligence.report_design_agent import (
    report_design_available,
    suggest_report_with_agent,
)
from app.intelligence.suggestions import apply_report_suggestions
from app.intelligence.tmdl_parser import TmdlParseError, parse_semantic_model
from app.ui import (
    artifact_store,
    fabric_client,
    page_intro,
    render_audit_report,
    suggestion_selector,
    workspace_picker,
)


@st.cache_data(ttl=120, show_spinner="Listing reports…")
def _list_reports(workspace_id: str):
    return fabric_client().list_reports(workspace_id)


@st.cache_data(ttl=120, show_spinner="Listing semantic models…")
def _list_models(workspace_id: str):
    return fabric_client().list_semantic_models(workspace_id)


def render() -> None:
    page_intro(
        "Reports",
        "Audit existing report formatting, or build a new report grounded in a "
        "semantic model.",
    )

    workspace = workspace_picker(key="rpt_ws")
    if workspace is None:
        return

    tab_audit, tab_build = st.tabs(["Audit existing", "Build from model"])
    with tab_audit:
        _render_audit_existing(workspace)
    with tab_build:
        _render_build(workspace)


def _render_audit_existing(workspace) -> None:
    try:
        reports = _list_reports(workspace.id)
    except FabricApiError as exc:
        st.error(f"Could not list reports: {exc}")
        return
    if not reports:
        st.info("This workspace has no reports.")
        return

    labels = {r.display_name: r for r in reports}
    choice = st.selectbox("Report", sorted(labels), key="rpt_pick")
    report_item = labels[choice]

    # Step 1 — import (non-destructive read + local store).
    if st.button("Import definition", type="primary", key="rpt_import"):
        _import_report(workspace, report_item)

    imported = st.session_state.get("rpt_imported")
    if not imported or imported.get("report_id") != report_item.id:
        st.caption("Import the report to view its contents and run an audit.")
        return

    st.success(
        f"Imported '{imported['report_name']}' · "
        f"{len(imported['files'])} file(s)."
    )

    spec = imported["spec"]
    tab_contents, tab_audit = st.tabs(["Contents", "Audit"])
    with tab_contents:
        _render_report_contents(spec, imported["files"])
    with tab_audit:
        _render_report_audit(workspace, report_item, spec)


def _import_report(workspace, report_item) -> None:
    try:
        definition = fabric_client().get_report_definition(
            workspace.id, report_item.id
        )
    except FabricApiError as exc:
        st.error(f"Failed to fetch report definition: {exc}")
        return
    ref = ArtifactRef(
        kind="reports",
        workspace_id=workspace.id,
        item_id=report_item.id,
        workspace_name=workspace.name,
        item_name=report_item.display_name,
    )
    artifact_store().save_definition(
        ref, definition.files, source="imported",
        description=report_item.description,
    )
    try:
        spec = parse_report(definition.files, name=report_item.display_name)
    except Exception as exc:  # noqa: BLE001 - surface parse issues inline
        st.error(f"Imported, but could not parse the report: {exc}")
        return
    st.session_state["rpt_imported"] = {
        "report_id": report_item.id,
        "report_name": report_item.display_name,
        "files": definition.files,
        "spec": spec,
    }
    # A fresh import invalidates any previous audit result.
    st.session_state.pop("rpt_audit_result", None)
    st.toast(f"Imported '{report_item.display_name}'.")


def _render_report_contents(spec, files: dict[str, str]) -> None:
    pages = spec.pages
    visual_count = sum(len(p.visuals) for p in pages)
    cols = st.columns(3)
    cols[0].metric("Pages", len(pages))
    cols[1].metric("Visuals", visual_count)
    cols[2].metric("Theme", spec.theme.name if spec.theme else "—")

    if spec.dataset_name or spec.dataset_id:
        st.caption(
            f"Bound dataset: {spec.dataset_name or spec.dataset_id}"
        )

    st.markdown("**Pages & visuals**")
    for page in pages:
        with st.expander(f"{page.display_name or page.name} ({len(page.visuals)} visuals)"):
            if not page.visuals:
                st.caption("No visuals on this page.")
                continue
            for visual in page.visuals:
                fields = ", ".join(f.query_ref for f in visual.all_fields())
                st.markdown(
                    f"- **{visual.title or visual.name}** · `{visual.visual_type}`"
                    + (f" → {fields}" if fields else "")
                )

    with st.expander("Raw definition files"):
        for path in sorted(files):
            st.markdown(f"`{path}`")
            st.code(files[path])


def _render_report_audit(workspace, report_item, spec) -> None:
    st.caption(
        "Report audits start with deterministic, rules-based checks "
        "(formatting, titles, overlap and WCAG colour-contrast) \u2014 these are "
        "the authoritative baseline. You can optionally ask the AI reviewer to "
        "add grounded, judgement-based findings (chart-type fit, title clarity, "
        "layout storytelling, domain wording) that rules cannot express."
    )

    agent_ready = report_agent_available()
    use_agent = st.checkbox(
        "Add AI design findings (requires Foundry)",
        value=agent_ready,
        key="rpt_use_agent",
        disabled=not agent_ready,
        help=(
            "Runs the deterministic audit first, then merges grounded findings "
            "from a Microsoft Foundry model. Falls back to the deterministic "
            "result if the model is unavailable or errors."
        ),
    )
    if not agent_ready:
        st.caption(
            "\u2139\ufe0f AI enrichment is unavailable \u2014 install "
            "`agent-framework` and `agent-framework-foundry` to enable it."
        )

    if st.button("Run audit", type="primary", key="rpt_run_audit"):
        if use_agent and agent_ready:
            with st.spinner("Running deterministic audit + AI review\u2026"):
                outcome = audit_report_with_agent(spec)
            audit = outcome.report
            if outcome.status == "ok":
                st.success(
                    f"AI review added {outcome.agent_finding_count} "
                    "grounded finding(s) on top of the deterministic baseline."
                )
            elif outcome.status == "error":
                st.warning(
                    "AI review unavailable \u2014 showing the deterministic "
                    f"baseline. Details: {outcome.error}"
                )
        else:
            audit = audit_report(spec)
        ref = ArtifactRef(
            kind="reports",
            workspace_id=workspace.id,
            item_id=report_item.id,
            workspace_name=workspace.name,
            item_name=report_item.display_name,
        )
        artifact_store().save_audit(ref, audit.feature, audit.to_json(), ext="json")
        artifact_store().save_audit(ref, audit.feature, audit.to_markdown(), ext="md")
        st.session_state["rpt_audit_result"] = audit

    audit = st.session_state.get("rpt_audit_result")
    if audit is not None:
        render_audit_report(audit)
        _render_report_fixes(workspace, report_item, spec)


def _render_report_fixes(workspace, report_item, spec) -> None:
    """Let the user pick formatting/theme fixes and write them back to Fabric."""
    st.divider()
    st.subheader("Apply fixes")
    st.caption(
        "Generate concrete formatting and accessibility changes (branded WCAG-AA "
        "theme, visual titles), choose the ones you want, and write them back to "
        "the report definition in Fabric."
    )

    state_key = f"rpt_fixes_{report_item.id}"
    if st.button("Suggest fixes", key="rpt_suggest_fixes"):
        st.session_state[state_key] = propose_theme_remediation(
            spec, brand_theme=load_brand_theme()
        )
        for k in list(st.session_state.keys()):
            if k.startswith("rpt_sel_cb_"):
                del st.session_state[k]

    suggestions = st.session_state.get(state_key)
    if suggestions is None:
        st.caption("Click **Suggest fixes** to see proposed changes.")
        return
    if not suggestions:
        st.success("The audit found nothing to auto-fix. ✨")
        return

    selected = suggestion_selector(suggestions, key_prefix="rpt_sel")

    if st.button(
        f"Apply {len(selected)} change(s) & update report",
        type="primary",
        key="rpt_apply_fixes",
        disabled=not selected,
    ):
        _apply_report_fixes(workspace, report_item, spec, suggestions, selected, state_key)


def _apply_report_fixes(
    workspace, report_item, spec, suggestions, selected, state_key
) -> None:
    selected_ids = {s.id for s in selected}
    for s in suggestions:
        s.status = "accepted" if s.id in selected_ids else "proposed"

    new_spec, updated = apply_report_suggestions(spec, suggestions)
    applied = [s for s in updated if s.status == "applied"]
    failed = [s for s in updated if s.status == "failed"]
    if not applied:
        st.error("None of the selected changes could be applied.")
        return

    try:
        definition = build_report_definition(new_spec)
        with st.spinner("Updating the report definition in Fabric…"):
            fabric_client().update_report_definition(
                workspace.id, report_item.id, definition.definition_payload()
            )
    except Exception as exc:  # noqa: BLE001 - surface upstream errors inline
        st.error(f"Failed to update the report in Fabric: {exc}")
        return

    ref = ArtifactRef(
        kind="reports",
        workspace_id=workspace.id,
        item_id=report_item.id,
        workspace_name=workspace.name,
        item_name=report_item.display_name,
    )
    artifact_store().save_definition(ref, definition.files, source="exported")

    # Refresh the in-session view so Contents / re-audit reflect the change.
    imported = st.session_state.get("rpt_imported")
    if imported and imported.get("report_id") == report_item.id:
        imported["files"] = definition.files
        imported["spec"] = new_spec
    st.session_state.pop(state_key, None)
    st.session_state.pop("rpt_audit_result", None)
    for k in list(st.session_state.keys()):
        if k.startswith("rpt_sel_cb_"):
            del st.session_state[k]

    msg = f"Applied {len(applied)} change(s) and updated the report in Fabric."
    if failed:
        msg += f" {len(failed)} could not be located and were skipped."
    st.success(msg)
    st.toast("Report updated.")


def _render_build(workspace) -> None:
    try:
        models = _list_models(workspace.id)
    except FabricApiError as exc:
        st.error(f"Could not list semantic models: {exc}")
        return
    if not models:
        st.info("This workspace has no semantic models to build on.")
        return

    labels = {m.display_name: m for m in models}
    choice = st.selectbox("Semantic model", sorted(labels), key="rpt_model_pick")
    model = labels[choice]
    report_name = st.text_input(
        "Report name", value=f"{model.display_name} Report", key="rpt_name"
    )

    st.caption(
        "The AI designer reads the model's tables, measures and relationships to "
        "propose insightful visuals as a starting point; deterministic code then "
        "grounds and lays them out into a valid report. Without AI, a sensible "
        "deterministic starter is built instead."
    )
    agent_ready = report_design_available()
    use_agent = st.checkbox(
        "Design visuals with AI (requires Foundry)",
        value=agent_ready,
        key="rpt_build_use_agent",
        disabled=not agent_ready,
        help=(
            "Asks a Microsoft Foundry model to suggest the most insightful "
            "visuals for this model. Falls back to the deterministic starter if "
            "the model is unavailable or errors."
        ),
    )
    if not agent_ready:
        st.caption(
            "\u2139\ufe0f AI design is unavailable \u2014 install "
            "`agent-framework` and `agent-framework-foundry` to enable it."
        )

    if st.button("Generate starter report", type="primary", key="rpt_build"):
        _build_report(workspace, model, report_name, use_agent and agent_ready)

    state = st.session_state.get("rpt_build_state")
    if not state or state.get("model_id") != model.id:
        return

    st.success("Starter report generated from the model's real fields.")
    ai_status = state.get("ai_status")
    if ai_status:
        if ai_status["status"] == "ok":
            st.info(
                f"🧠 AI designed {ai_status['count']} grounded visual(s) "
                "for this report."
            )
        elif ai_status["status"] == "error":
            st.warning(
                "AI design unavailable — showing the deterministic starter. "
                f"Details: {ai_status['error']}"
            )
    audit = state["audit"]
    render_audit_report(audit)

    with st.expander("Visuals"):
        for visual in state["visuals"]:
            st.markdown(
                f"- **{visual['title']}** · `{visual['type']}` → "
                f"{', '.join(visual['fields'])}"
            )

    rationale = state.get("rationale") or []
    if rationale:
        with st.expander("Design rationale"):
            for item in rationale:
                badge = "🧠 AI" if item["source"] == "agent" else "⚙️ rule"
                st.markdown(
                    f"- **{item['title']}** · `{item['type']}` · {badge}"
                    + (f"  \n  {item['rationale']}" if item["rationale"] else "")
                )

    st.download_button(
        "Download report (.pbix)",
        data=state["zip_bytes"],
        file_name=f"{report_name}.pbix",
        mime="application/zip",
        key="rpt_download",
    )

    st.divider()
    st.warning(
        "Creating the report writes a new item to the Fabric workspace.",
        icon="⚠️",
    )
    confirm = st.checkbox("I want to create this report in Fabric.", key="rpt_confirm")
    if st.button("Create report in Fabric", disabled=not confirm, key="rpt_create"):
        _create_report(workspace, report_name, state)


def _build_report(workspace, model, report_name: str, use_agent: bool) -> None:
    try:
        definition = fabric_client().get_semantic_model_definition(
            workspace.id, model.id, fmt="TMDL"
        )
        spec = parse_semantic_model(definition.files, name=model.display_name)
    except (FabricApiError, TmdlParseError) as exc:
        st.error(f"Could not load the semantic model: {exc}")
        return

    ai_status: dict | None = None
    rationale: list[dict] = []
    if use_agent:
        with st.spinner("Designing visuals with AI…"):
            outcome = suggest_report_with_agent(
                spec,
                report_name=report_name,
                dataset_id=model.id,
                dataset_name=model.display_name,
            )
        report_spec = outcome.report
        ai_status = {
            "status": outcome.status,
            "count": outcome.agent_visual_count,
            "error": outcome.error,
        }
        rationale = [
            {
                "title": s.title,
                "type": s.visual_type,
                "source": s.source,
                "rationale": s.rationale,
            }
            for s in outcome.suggestions
        ]
    else:
        report_spec = suggest_report(
            spec, report_name=report_name, dataset_id=model.id, dataset_name=model.display_name
        )
    report_def = build_report_definition(report_spec)
    audit = audit_report(report_spec)

    # Persist the generated report locally (source = generated).
    ref = ArtifactRef(
        kind="reports",
        workspace_id=workspace.id,
        item_id=f"generated-{model.id}",
        workspace_name=workspace.name,
        item_name=report_name,
    )
    artifact_store().save_definition(
        ref, report_def.files, source="generated",
        description=report_spec.description,
        extra_metadata={"basedOnSemanticModelId": model.id},
    )

    st.session_state["rpt_build_state"] = {
        "model_id": model.id,
        "audit": audit,
        "ai_status": ai_status,
        "rationale": rationale,
        "zip_bytes": report_def.to_zip_bytes(),
        "definition_payload": report_def.definition_payload(),
        "visuals": [
            {
                "title": v.title or v.name,
                "type": v.visual_type,
                "fields": [f.query_ref for f in v.all_fields()],
            }
            for v in report_spec.all_visuals()
        ],
    }


def _create_report(workspace, report_name: str, state: dict) -> None:
    try:
        created = fabric_client().create_report(
            workspace.id,
            report_name,
            state["definition_payload"],
            description="Generated by Fabric Dev AI from a semantic model.",
        )
    except FabricApiError as exc:
        st.error(f"Create failed: {exc}")
        return
    if created.succeeded:
        st.success(f"Created report '{created.display_name}' (id {created.id}).")
    else:
        st.warning(f"Create returned status '{created.status}'.")
