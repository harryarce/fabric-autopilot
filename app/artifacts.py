"""Structured artifact store for Fabric item definitions and audit outputs.

Every artifact the app fetches (imported), generates, or exports is persisted
through a single :class:`ArtifactStore` so that downstream tooling — natural
language insights, cross-artifact analysis, change tracking — has one
predictable place to read from.

Design goals
------------
* **Storage-agnostic.** The store is defined in terms of *keys* (forward-slash
  paths, blob-friendly) rather than OS filesystem paths. The concrete
  :class:`LocalArtifactStore` is provided today; a future
  ``AzureBlobArtifactStore`` only needs to implement the four byte-level
  primitives (:meth:`~ArtifactStore._read_bytes`,
  :meth:`~ArtifactStore._write_bytes`, :meth:`~ArtifactStore._list_keys`,
  :meth:`~ArtifactStore._exists`). All high-level verbs are shared.
* **Modular / shareable.** No dependency on Streamlit, the Fabric client, or
  the intelligence layer. This module can be lifted into a separate package and
  handed to a customer as-is.

Key layout (identical for a local directory or a blob prefix)::

    <workspace>/{semanticModels|reports}/<item>/
        definition/<part-path...>        # the decoded item definition files
        metadata.json                    # id, names, format, source, timestamps
        audit/<feature>-<timestamp>.json # audit/recommendation outputs
        audit/<feature>-<timestamp>.md
    manifest.json                        # index of every stored item (at root)
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

# Logical collection segment per item kind (matches the Fabric REST route).
ArtifactKind = Literal["semanticModels", "reports"]

# Where an artifact came from, recorded in metadata for later filtering.
ArtifactSource = Literal["imported", "generated", "exported"]

MANIFEST_KEY = "manifest.json"
METADATA_NAME = "metadata.json"
DEFINITION_DIR = "definition"
AUDIT_DIR = "audit"
SUGGESTIONS_NAME = "suggestions.json"


def sanitize_segment(value: str, *, fallback: str = "item") -> str:
    """Make ``value`` safe to use as a single key/path segment.

    Mirrors the conservative sanitisation used elsewhere in the app
    (alphanumerics, dash and underscore survive; everything else becomes ``_``)
    so keys are portable across local filesystems and blob storage.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (value or ""))
    return safe or fallback


@dataclass(frozen=True)
class ArtifactRef:
    """Identifies one stored item and resolves its key prefix.

    The combination of name + id keeps keys human-browsable while staying
    unique even when two items share a display name.
    """

    kind: ArtifactKind
    workspace_id: str
    item_id: str
    workspace_name: str = ""
    item_name: str = ""

    @property
    def workspace_segment(self) -> str:
        ws = sanitize_segment(self.workspace_name, fallback="workspace")
        return f"{ws}__{sanitize_segment(self.workspace_id, fallback='ws')}"

    @property
    def item_segment(self) -> str:
        name = sanitize_segment(self.item_name, fallback="item")
        return f"{name}__{sanitize_segment(self.item_id, fallback='id')}"

    @property
    def prefix(self) -> str:
        """The forward-slash key prefix under which this item is stored."""
        return f"{self.workspace_segment}/{self.kind}/{self.item_segment}"


@dataclass(frozen=True)
class StoredArtifact:
    """A loaded artifact: its definition files plus stored metadata."""

    ref: ArtifactRef
    files: dict[str, str]
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def format(self) -> str | None:
        value = self.metadata.get("format")
        return value if isinstance(value, str) else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class ArtifactStore(ABC):
    """Abstract artifact store. Subclasses implement only byte-level I/O.

    The high-level verbs (:meth:`save_definition`, :meth:`load_definition`,
    :meth:`save_audit`, :meth:`list_items`, :meth:`read_manifest`) are fully
    implemented here in terms of the four abstract primitives, so a new backend
    is a small, well-bounded amount of code.
    """

    # -- primitives a backend must implement ------------------------------

    @abstractmethod
    def _read_bytes(self, key: str) -> bytes | None:
        """Return the bytes stored at ``key`` or ``None`` if absent."""

    @abstractmethod
    def _write_bytes(self, key: str, data: bytes) -> None:
        """Write ``data`` to ``key``, creating any intermediate structure."""

    @abstractmethod
    def _list_keys(self, prefix: str) -> Iterable[str]:
        """Yield every key whose path starts with ``prefix``."""

    @abstractmethod
    def _exists(self, key: str) -> bool:
        """Return whether ``key`` currently exists."""

    # -- text helpers -----------------------------------------------------

    def _read_text(self, key: str) -> str | None:
        data = self._read_bytes(key)
        return None if data is None else data.decode("utf-8", errors="replace")

    def _write_text(self, key: str, text: str) -> None:
        self._write_bytes(key, text.encode("utf-8"))

    # -- definitions ------------------------------------------------------

    def save_definition(
        self,
        ref: ArtifactRef,
        files: dict[str, str],
        *,
        fmt: str | None = None,
        source: ArtifactSource = "imported",
        description: str | None = None,
        etag: str | None = None,
        extra_metadata: dict[str, object] | None = None,
    ) -> ArtifactRef:
        """Persist an item's definition files plus metadata, update manifest."""
        for rel_path, text in files.items():
            self._write_text(f"{ref.prefix}/{DEFINITION_DIR}/{rel_path}", text)

        metadata: dict[str, object] = {
            "id": ref.item_id,
            "displayName": ref.item_name,
            "workspaceId": ref.workspace_id,
            "workspaceName": ref.workspace_name,
            "kind": ref.kind,
            "format": fmt,
            "source": source,
            "description": description,
            "etag": etag,
            "fetchedAt": _utc_now_iso(),
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        self._write_text(
            f"{ref.prefix}/{METADATA_NAME}",
            json.dumps(metadata, indent=2, ensure_ascii=False),
        )
        self._upsert_manifest_entry(ref, metadata)
        return ref

    def load_definition(self, ref: ArtifactRef) -> StoredArtifact | None:
        """Load a previously stored definition, or ``None`` if not present."""
        definition_prefix = f"{ref.prefix}/{DEFINITION_DIR}/"
        files: dict[str, str] = {}
        for key in self._list_keys(definition_prefix):
            rel = key[len(definition_prefix):]
            if not rel:
                continue
            text = self._read_text(key)
            if text is not None:
                files[rel] = text
        if not files:
            return None
        metadata_raw = self._read_text(f"{ref.prefix}/{METADATA_NAME}")
        metadata: dict[str, object] = {}
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except json.JSONDecodeError:
                metadata = {}
        return StoredArtifact(ref=ref, files=files, metadata=metadata)

    # -- audits -----------------------------------------------------------

    def save_audit(
        self,
        ref: ArtifactRef,
        feature: str,
        content: str,
        *,
        ext: str = "json",
    ) -> str:
        """Persist an audit/recommendation output; returns its key."""
        feature_slug = sanitize_segment(feature, fallback="audit")
        key = f"{ref.prefix}/{AUDIT_DIR}/{feature_slug}-{_timestamp_slug()}.{ext}"
        self._write_text(key, content)
        return key

    # -- suggestions ------------------------------------------------------

    def save_suggestions(self, ref: ArtifactRef, content: str) -> str:
        """Persist the suggestion bundle for an item; returns its key.

        The bundle lives at a fixed key (``<prefix>/suggestions.json``) so it
        survives across review sessions and is overwritten each time the user
        regenerates or accepts/skips a suggestion.
        """
        key = f"{ref.prefix}/{SUGGESTIONS_NAME}"
        self._write_text(key, content)
        return key

    def load_suggestions(self, ref: ArtifactRef) -> str | None:
        """Return the raw JSON suggestion bundle for ``ref`` (or ``None``)."""
        return self._read_text(f"{ref.prefix}/{SUGGESTIONS_NAME}")

    # -- manifest ---------------------------------------------------------

    def read_manifest(self) -> dict[str, dict[str, object]]:
        """Return the manifest mapping ``item prefix -> metadata`` (may be empty)."""
        raw = self._read_text(MANIFEST_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def list_items(self, *, kind: ArtifactKind | None = None) -> list[ArtifactRef]:
        """List stored items from the manifest, optionally filtered by kind."""
        refs: list[ArtifactRef] = []
        for entry in self.read_manifest().values():
            entry_kind = entry.get("kind")
            if kind is not None and entry_kind != kind:
                continue
            if entry_kind not in ("semanticModels", "reports"):
                continue
            refs.append(
                ArtifactRef(
                    kind=entry_kind,  # type: ignore[arg-type]
                    workspace_id=str(entry.get("workspaceId", "")),
                    item_id=str(entry.get("id", "")),
                    workspace_name=str(entry.get("workspaceName", "")),
                    item_name=str(entry.get("displayName", "")),
                )
            )
        refs.sort(key=lambda r: (r.kind, r.item_name.casefold()))
        return refs

    def _upsert_manifest_entry(
        self, ref: ArtifactRef, metadata: dict[str, object]
    ) -> None:
        manifest = self.read_manifest()
        manifest[ref.prefix] = {**metadata, "prefix": ref.prefix}
        self._write_text(
            MANIFEST_KEY, json.dumps(manifest, indent=2, ensure_ascii=False)
        )


class LocalArtifactStore(ArtifactStore):
    """Filesystem-backed artifact store rooted at a local directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        # Keys are forward-slash; resolve to a platform path under root.
        return self._root.joinpath(*key.split("/"))

    def _read_bytes(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def _write_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _list_keys(self, prefix: str) -> Iterable[str]:
        base = self._path(prefix)
        # ``prefix`` may name a directory or a partial path; scan its directory.
        search_dir = base if base.is_dir() else base.parent
        if not search_dir.is_dir():
            return []
        keys: list[str] = []
        for path in search_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self._root).as_posix()
            if rel.startswith(prefix):
                keys.append(rel)
        return keys

    def _exists(self, key: str) -> bool:
        return self._path(key).exists()


class BlobArtifactStore(ArtifactStore):
    """Azure Blob Storage-backed artifact store.

    Implements only the four byte-level primitives over a single container;
    every blob name is a forward-slash key, so the layout is byte-for-byte
    identical to :class:`LocalArtifactStore`. Authentication uses
    :class:`~azure.identity.DefaultAzureCredential`, i.e. the service's Managed
    Identity in Azure (no connection strings or account keys).

    An optional ``prefix`` namespaces all keys under a virtual folder; the
    service layer uses this to isolate tenants (``tenants/<tenant_id>/``).
    """

    def __init__(
        self,
        account_url: str,
        container: str,
        *,
        prefix: str = "",
        credential: object | None = None,
    ) -> None:
        # Imported lazily so the local store has no hard dependency on the
        # azure-storage-blob package.
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient

        cred = credential
        if cred is None:
            client_id = os.environ.get("AZURE_CLIENT_ID") or None
            cred = DefaultAzureCredential(
                managed_identity_client_id=client_id,
                exclude_interactive_browser_credential=True,
            )
        self._container = ContainerClient(
            account_url=account_url, container_name=container, credential=cred
        )
        self._prefix = prefix.strip("/")
        try:  # Idempotent: create the container if it does not exist yet.
            self._container.create_container()
        except Exception:  # pragma: no cover - already exists / no perms
            pass

    def _blob_name(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def _read_bytes(self, key: str) -> bytes | None:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._container.get_blob_client(self._blob_name(key))
        try:
            return blob.download_blob().readall()
        except ResourceNotFoundError:
            return None

    def _write_bytes(self, key: str, data: bytes) -> None:
        blob = self._container.get_blob_client(self._blob_name(key))
        blob.upload_blob(data, overwrite=True)

    def _list_keys(self, prefix: str) -> Iterable[str]:
        full_prefix = self._blob_name(prefix)
        strip = len(self._prefix) + 1 if self._prefix else 0
        keys: list[str] = []
        for blob in self._container.list_blobs(name_starts_with=full_prefix):
            name = blob.name[strip:] if strip else blob.name
            keys.append(name)
        return keys

    def _exists(self, key: str) -> bool:
        blob = self._container.get_blob_client(self._blob_name(key))
        return blob.exists()


def default_artifact_root() -> Path:
    """Resolve the default local artifact root (``<repo>/artifacts``)."""
    env_root = os.environ.get("FABRIC_ARTIFACT_ROOT")
    if env_root:
        return Path(env_root)
    # app/artifacts.py -> app -> repo root
    return Path(__file__).resolve().parents[1] / "artifacts"


def get_artifact_store(*, prefix: str = "") -> ArtifactStore:
    """Return the configured artifact store (local by default).

    Selection is driven by ``FABRIC_ARTIFACT_STORE``:

    * ``local`` (default) — :class:`LocalArtifactStore` under
      ``FABRIC_ARTIFACT_ROOT`` (or ``<repo>/artifacts``).
    * ``blob`` — :class:`BlobArtifactStore`, configured via
      ``FABRIC_BLOB_ACCOUNT_URL`` and ``FABRIC_BLOB_CONTAINER``
      (default container ``artifacts``), authenticated with Managed Identity.

    ``prefix`` namespaces all keys (used for per-tenant isolation). The factory
    is the single seam callers use — they never construct a concrete store.
    """
    backend = os.environ.get("FABRIC_ARTIFACT_STORE", "local").strip().lower()
    if backend in ("", "local"):
        store = LocalArtifactStore(default_artifact_root())
        # Local store has no native prefixing; fold it into the root.
        if prefix:
            return LocalArtifactStore(default_artifact_root() / prefix)
        return store
    if backend == "blob":
        account_url = os.environ.get("FABRIC_BLOB_ACCOUNT_URL", "").strip()
        if not account_url:
            raise ValueError(
                "FABRIC_ARTIFACT_STORE=blob requires FABRIC_BLOB_ACCOUNT_URL "
                "(e.g. https://<account>.blob.core.windows.net)."
            )
        container = os.environ.get("FABRIC_BLOB_CONTAINER", "artifacts").strip()
        return BlobArtifactStore(account_url, container, prefix=prefix)
    raise ValueError(
        f"Unsupported artifact store backend '{backend}'. "
        "Set FABRIC_ARTIFACT_STORE=local or FABRIC_ARTIFACT_STORE=blob."
    )
