"""Insights page (foundation): browse what has been imported, generated and
audited locally.

This page deliberately does *not* call any AI service. It surfaces the
structured artifact store — the same store that a future natural-language /
narrative-insights layer (or an Azure Blob-backed deployment) will read from —
so the groundwork is visible today.
"""

from __future__ import annotations

import streamlit as st

from app.ui import artifact_store, page_intro


def render() -> None:
    page_intro(
        "Insights",
        "Everything imported, generated or audited is stored here for future "
        "natural-language insights.",
    )

    store = artifact_store()
    items = store.list_items()
    if not items:
        st.info(
            "No artifacts yet. Import a semantic model or report, or build a "
            "report, to populate the store."
        )
        return

    manifest = store.read_manifest()
    rows = []
    for ref in items:
        entry = manifest.get(ref.prefix, {})
        rows.append(
            {
                "Kind": ref.kind,
                "Name": ref.item_name,
                "Workspace": ref.workspace_name,
                "Source": entry.get("source", ""),
                "Format": entry.get("format", ""),
                "Fetched": entry.get("fetchedAt", ""),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.caption(
        "Roadmap: ground a natural-language Q&A / narrative-summary experience on "
        "these stored definitions and audit results, and swap the local store for "
        "an Azure Storage account without changing the pages above."
    )
