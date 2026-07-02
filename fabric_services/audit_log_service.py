"""Audit log service: append-only JSONL trail of platform actions.

Each event is a single JSON document (one line) written under the tenant's
artifact-store prefix at ``audit/yyyy/mm/dd/events.jsonl``. The format is
chosen so events can be replayed or piped into a log-analytics pipeline (App
Insights, Log Analytics, an external SIEM) without re-shaping.

The service is intentionally process-local and best-effort: failures to log
must never fail the operation being audited.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.artifacts import ArtifactStore

from .context import TenantContext

logger = logging.getLogger(__name__)


@dataclass
class AuditEvent:
    """One audited platform action."""

    action: str
    tenant_id: str
    actor_id: str | None = None
    correlation_id: str | None = None
    target_kind: str | None = None
    target_id: str | None = None
    status: str = "ok"
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "correlation_id": self.correlation_id,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "status": self.status,
            "details": self.details,
        }


class AuditLogService:
    """Append-only JSONL audit log scoped to the current tenant prefix."""

    def __init__(self, artifact_store: ArtifactStore, tenant: TenantContext) -> None:
        self._store = artifact_store
        self._tenant = tenant

    @staticmethod
    def _date_key(now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        return f"audit/{now.year:04d}/{now.month:02d}/{now.day:02d}/events.jsonl"

    def log(
        self,
        action: str,
        *,
        actor_id: str | None = None,
        correlation_id: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> AuditEvent | None:
        """Append a single event. Returns ``None`` on best-effort failure."""
        event = AuditEvent(
            action=action,
            tenant_id=self._tenant.tenant_id,
            actor_id=actor_id or self._tenant.user_id,
            correlation_id=correlation_id or self._tenant.correlation_id,
            target_kind=target_kind,
            target_id=target_id,
            status=status,
            details=details or {},
        )
        try:
            self._append_line(json.dumps(event.to_dict(), default=str))
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("audit log append failed: %s", exc)
            return None
        return event

    def list_events(self, *, since: str | None = None) -> list[AuditEvent]:
        """Read back the current day's events (test/diagnostic helper)."""
        key = self._date_key()
        raw = self._store._read_text(key)  # noqa: SLF001 — internal helper
        if not raw:
            return []
        out: list[AuditEvent] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and doc.get("timestamp", "") < since:
                continue
            out.append(
                AuditEvent(
                    action=doc.get("action", ""),
                    tenant_id=doc.get("tenant_id", ""),
                    actor_id=doc.get("actor_id"),
                    correlation_id=doc.get("correlation_id"),
                    target_kind=doc.get("target_kind"),
                    target_id=doc.get("target_id"),
                    status=doc.get("status", "ok"),
                    details=doc.get("details", {}) or {},
                    timestamp=doc.get(
                        "timestamp", datetime.now(timezone.utc).isoformat()
                    ),
                )
            )
        return out

    # -- internals -------------------------------------------------------

    def _append_line(self, line: str) -> None:
        """Read-modify-write the day's JSONL file."""
        key = self._date_key()
        # Throttle storage round-trips a touch under high write rates by
        # batching at the call site if needed; the platform's expected audit
        # volume is low enough that the simple RMW is fine.
        existing = self._store._read_text(key) or ""  # noqa: SLF001
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self._store._write_text(key, existing + line + "\n")  # noqa: SLF001
        # Tiny sleep to nudge deterministic ordering when tests append a burst.
        # No-op in production where one event/sec is typical.
        _ = time
