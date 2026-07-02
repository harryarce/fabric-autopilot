"""Artifact service — browse and read the tenant's stored artifacts.

Thin orchestration over the tenant-scoped :class:`ArtifactStore`: lists stored
items from the manifest and loads individual definitions / audits. This is the
logic behind the Streamlit *Insights* page.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from app.artifacts import ArtifactKind, ArtifactRef, ArtifactStore
from app.intelligence import (
    SuggestionSpec,
    suggestions_from_dict,
    suggestions_to_dict,
)

from .context import TenantContext
from .errors import NotFoundError


class ArtifactService:
    """Read-side access to the artifact store for the current tenant."""

    def __init__(self, store: ArtifactStore, tenant: TenantContext) -> None:
        self._store = store
        self._tenant = tenant

    def list_items(self, *, kind: ArtifactKind | None = None) -> list[ArtifactRef]:
        """List stored items, optionally filtered by kind."""
        return self._store.list_items(kind=kind)

    def read_manifest(self) -> dict[str, dict[str, object]]:
        """Return the full artifact manifest index."""
        return self._store.read_manifest()

    def load_definition(self, ref: ArtifactRef) -> dict[str, Any]:
        """Load a stored definition's files + metadata for ``ref``."""
        stored = self._store.load_definition(ref)
        if stored is None:
            raise NotFoundError(
                f"No stored definition found for {ref.kind} item {ref.item_id}."
            )
        return {"files": stored.files, "metadata": stored.metadata}

    def save_audit(
        self, ref: ArtifactRef, feature: str, content: str, *, ext: str = "json"
    ) -> str:
        """Persist an audit output next to the item; returns its storage key."""
        return self._store.save_audit(ref, feature, content, ext=ext)

    # -- suggestion bundle ----------------------------------------------

    def save_suggestions(
        self, ref: ArtifactRef, suggestions: Iterable[SuggestionSpec]
    ) -> str:
        """Persist a suggestion bundle for ``ref``; returns the storage key."""
        payload = json.dumps(
            suggestions_to_dict(list(suggestions)), indent=2, ensure_ascii=False
        )
        return self._store.save_suggestions(ref, payload)

    def load_suggestions(self, ref: ArtifactRef) -> list[SuggestionSpec]:
        """Return the persisted suggestion bundle for ``ref`` (empty when none)."""
        raw = self._store.load_suggestions(ref)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return suggestions_from_dict(data)

    def update_suggestion_status(
        self, ref: ArtifactRef, suggestion_id: str, status: str
    ) -> SuggestionSpec:
        """Update one suggestion's status (``accepted`` | ``skipped`` | ...).

        Raises :class:`NotFoundError` when the suggestion cannot be found.
        """
        suggestions = self.load_suggestions(ref)
        target = next((s for s in suggestions if s.id == suggestion_id), None)
        if target is None:
            raise NotFoundError(
                f"Suggestion '{suggestion_id}' not found for {ref.kind} {ref.item_id}."
            )
        target.status = status  # type: ignore[assignment]
        self.save_suggestions(ref, suggestions)
        return target
