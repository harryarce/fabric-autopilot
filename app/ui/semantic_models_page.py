"""Semantic Models page: list, import, audit and export semantic models.

Read → audit → (export) pipeline. Write-back (updateDefinition) is intentionally
not exposed here yet; all operations are non-destructive.
"""

from __future__ import annotations

import streamlit as st

from app.artifacts import ArtifactRef
from app.fabric_client import FabricApiError
from app.intelligence.audit import (
    audit_semantic_model,
    propose_copilot_prep_fixes,
    propose_usability_fixes,
)
from app.intelligence.definition import DefinitionFormat, build_definition
from app.intelligence.suggestions import apply_model_suggestions
from app.intelligence.tmdl_parser import TmdlParseError, parse_semantic_model
from app.ui import (
    artifact_store,
    fabric_client,
    page_intro,
    render_audit_report,
    suggestion_selector,
    workspace_picker,
)


@st.cache_data(ttl=120, show_spinner="Listing semantic models…")
def _list_models(workspace_id: str):
    return fabric_client().list_semantic_models(workspace_id)


def render() -> None:
    page_intro(
        "Semantic Models",
        "Import a model's definition, run deterministic audits, and export it.",
    )

    workspace = workspace_picker(key="sm_ws")
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

    labels = {f"{m.display_name}": m for m in models}
    choice = st.selectbox("Semantic model", sorted(labels), key="sm_pick")
    model = labels[choice]

    fmt = st.radio(
        "Definition format", ["TMDL", "TMSL"], horizontal=True, key="sm_fmt"
    )

    if st.button("Import definition", type="primary", key="sm_import"):
        _import_model(workspace, model, fmt)

    ref = ArtifactRef(
        kind="semanticModels",
        workspace_id=workspace.id,
        item_id=model.id,
        workspace_name=workspace.name,
        item_name=model.display_name,
    )
    stored = artifact_store().load_definition(ref)
    if stored is None:
        st.caption("Import the definition to enable audit and export.")
        return

    st.success(
        f"Definition available locally · format {stored.format} · "
        f"{len(stored.files)} part(s)."
    )

    try:
        spec = parse_semantic_model(stored.files, name=model.display_name)
    except TmdlParseError as exc:
        st.error(f"Could not parse the definition: {exc}")
        return

    tab_audit, tab_export, tab_files = st.tabs(["Audit", "Export", "Files"])

    with tab_audit:
        _render_audit(ref, spec)
    with tab_export:
        _render_export(spec, model.display_name)
    with tab_files:
        for path in sorted(stored.files):
            with st.expander(path):
                st.code(stored.files[path])


def _import_model(workspace, model, fmt: str) -> None:
    try:
        definition = fabric_client().get_semantic_model_definition(
            workspace.id, model.id, fmt=fmt
        )
    except FabricApiError as exc:
        st.error(f"Failed to fetch definition: {exc}")
        return
    ref = ArtifactRef(
        kind="semanticModels",
        workspace_id=workspace.id,
        item_id=model.id,
        workspace_name=workspace.name,
        item_name=model.display_name,
    )
    artifact_store().save_definition(
        ref,
        definition.files,
        fmt=definition.format,
        source="imported",
        description=model.description,
    )
    st.toast(f"Imported '{model.display_name}'.")


def _render_audit(ref: ArtifactRef, spec) -> None:
    apply_bpa = st.toggle(
        "Apply Best Practice Analyzer (BPA) rules",
        value=True,
        key="sm_apply_bpa",
        help="Run Microsoft's Best Practice Analyzer rule set "
        "(BPARules.json) in addition to the core review.",
    )
    reports = audit_semantic_model(spec, include_bpa=apply_bpa)
    health = reports["semantic-model-health"]
    st.subheader("Health overview")
    render_audit_report(health)

    feature_labels = {
        "semantic-model-usability": "Usability",
        "semantic-model-design": "Design",
        "semantic-model-copilot-prep": "Copilot readiness",
        "semantic-model-dax": "DAX",
        "semantic-model-bpa": "Best Practice Analyzer",
    }
    for feature, label in feature_labels.items():
        report = reports.get(feature)
        if report is None:
            continue
        with st.expander(f"{label} — score {report.score}/100"):
            render_audit_report(report)

    if st.button("Save audit to artifact store", key="sm_save_audit"):
        for feature, report in reports.items():
            artifact_store().save_audit(ref, feature, report.to_json(), ext="json")
            artifact_store().save_audit(ref, feature, report.to_markdown(), ext="md")
        st.toast("Audit reports saved.")

    _render_fixes(ref, spec)


def _render_fixes(ref: ArtifactRef, spec) -> None:
    """Let the user pick deterministic fixes and write them back to Fabric."""
    st.divider()
    st.subheader("Apply fixes")
    st.caption(
        "Generate concrete, one-field changes from the audit, choose the ones "
        "you want, and write them back to the model's definition in Fabric."
    )

    state_key = f"sm_fixes_{ref.item_id}"
    if st.button("Suggest fixes", key="sm_suggest_fixes"):
        suggestions = propose_usability_fixes(spec) + propose_copilot_prep_fixes(spec)
        st.session_state[state_key] = suggestions
        # Reset any stale checkbox state from a previous run.
        for k in list(st.session_state.keys()):
            if k.startswith("sm_sel_cb_"):
                del st.session_state[k]

    suggestions = st.session_state.get(state_key)
    if suggestions is None:
        st.caption("Click **Suggest fixes** to see proposed changes.")
        return
    if not suggestions:
        st.success("The audit found nothing to auto-fix. ✨")
        return

    selected = suggestion_selector(suggestions, key_prefix="sm_sel")

    if st.button(
        f"Apply {len(selected)} change(s) & update model",
        type="primary",
        key="sm_apply_fixes",
        disabled=not selected,
    ):
        _apply_fixes(ref, spec, suggestions, selected, state_key)


def _apply_fixes(ref: ArtifactRef, spec, suggestions, selected, state_key) -> None:
    selected_ids = {s.id for s in selected}
    for s in suggestions:
        s.status = "accepted" if s.id in selected_ids else "proposed"

    new_spec, updated = apply_model_suggestions(spec, suggestions)
    applied = [s for s in updated if s.status == "applied"]
    failed = [s for s in updated if s.status == "failed"]
    if not applied:
        st.error("None of the selected changes could be applied.")
        return

    try:
        definition = build_definition(new_spec, DefinitionFormat.TMDL)
        with st.spinner("Updating the model definition in Fabric…"):
            fabric_client().update_semantic_model_definition(
                ref.workspace_id, ref.item_id, definition.definition_payload()
            )
    except Exception as exc:  # noqa: BLE001 - surface upstream errors inline
        st.error(f"Failed to update the model in Fabric: {exc}")
        return

    artifact_store().save_definition(
        ref, definition.files, fmt=definition.format.value, source="exported"
    )
    st.session_state.pop(state_key, None)
    for k in list(st.session_state.keys()):
        if k.startswith("sm_sel_cb_"):
            del st.session_state[k]
    msg = f"Applied {len(applied)} change(s) and updated the model in Fabric."
    if failed:
        msg += f" {len(failed)} could not be located and were skipped."
    st.success(msg)
    st.toast("Model updated.")


def _render_export(spec, display_name: str) -> None:
    fmt_label = st.radio(
        "Export format",
        ["TMDL", "TMSL"],
        horizontal=True,
        key="sm_export_fmt",
    )
    fmt = DefinitionFormat.TMDL if fmt_label == "TMDL" else DefinitionFormat.TMSL
    definition = build_definition(spec, fmt)
    st.download_button(
        "Download definition (.zip)",
        data=definition.to_zip_bytes(),
        file_name=f"{display_name}.{fmt_label.lower()}.zip",
        mime="application/zip",
        key="sm_download",
    )
