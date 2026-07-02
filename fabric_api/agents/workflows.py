"""Human-in-the-loop publish workflows.

Designing and auditing are safe to run autonomously; **publishing** to Fabric
is side-effecting and therefore gated behind explicit human approval. A workflow
is created in a ``pending`` state, returns an approval token, and only performs
the Fabric write when an operator approves it. Rejection discards it.

The pending store is an in-process, thread-safe map — sufficient for the
single-replica API today and trivially swappable for a durable store later. The
actual Fabric write is delegated to :class:`~fabric_api.agents.tools.ToolKit`,
so this module owns *only* the approval state machine, never business logic.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fabric_services import ServiceContainer
from fabric_services.context import TenantContext

from .tools import ToolKit

WorkflowKind = Literal["publish-model", "publish-report", "publish-model-report"]
WorkflowState = Literal["pending", "approved", "rejected", "failed"]


@dataclass
class PublishWorkflow:
    """A pending side-effecting action awaiting approval."""

    id: str
    kind: WorkflowKind
    tenant_id: str
    payload: dict[str, Any]
    state: WorkflowState = "pending"
    result: dict[str, Any] | None = None
    error: str | None = None
    created_ts: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "tenantId": self.tenant_id,
            "state": self.state,
            "payload": _redact(self.payload),
            "result": self.result,
            "error": self.error,
            "createdTs": self.created_ts,
        }


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a payload for display: drop the bulky spec body."""
    bulky = {"spec", "model_spec", "report_spec"}
    out = {k: v for k, v in payload.items() if k not in bulky}
    for key in ("spec", "model_spec", "report_spec"):
        spec = payload.get(key)
        if isinstance(spec, dict):
            summary_key = "specSummary" if key == "spec" else f"{_camel(key)}Summary"
            out[summary_key] = {
                "name": spec.get("name"),
                "tableCount": len(spec.get("tables") or []),
                "pageCount": len(spec.get("pages") or []),
            }
    return out


def _camel(snake: str) -> str:
    """``model_spec`` -> ``modelSpec`` (for display keys only)."""
    head, *rest = snake.split("_")
    return head + "".join(part.title() for part in rest)


def _summary_entry(item: dict[str, Any] | None) -> dict[str, Any]:
    """Project a published ``CreatedItem`` dict into a compact summary row."""
    item = item or {}
    return {
        "kind": item.get("type"),
        "id": item.get("id"),
        "displayName": item.get("display_name"),
        "workspaceId": item.get("workspace_id"),
        "status": item.get("status"),
        "webUrl": item.get("web_url"),
    }


def _publish_model_and_report(
    toolkit: ToolKit, payload: dict[str, Any]
) -> dict[str, Any]:
    """Publish the model, then bind & publish the report to it.

    A single approval drives both writes. The report is bound to the freshly
    published model via its dataset (semantic-model) id so Fabric accepts the
    ``byConnection`` reference.
    """
    workspace_id = payload["workspace_id"]
    description = payload.get("description")
    model = toolkit.publish_model(
        workspace_id=workspace_id,
        display_name=payload["model_display_name"],
        spec=payload["model_spec"],
        fmt=payload.get("fmt", "TMDL"),
        description=description,
    )
    report = toolkit.publish_report(
        workspace_id=workspace_id,
        display_name=payload["report_display_name"],
        spec=payload["report_spec"],
        description=description,
        dataset_id=model.get("id"),
    )
    return {
        "model": model,
        "report": report,
        "published": [_summary_entry(model), _summary_entry(report)],
    }



class PublishWorkflowStore:
    """Thread-safe registry of pending publish workflows."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, PublishWorkflow] = {}

    def create(
        self, kind: WorkflowKind, tenant: TenantContext, payload: dict[str, Any]
    ) -> PublishWorkflow:
        wf = PublishWorkflow(
            id=uuid.uuid4().hex,
            kind=kind,
            tenant_id=tenant.tenant_id,
            payload=payload,
        )
        with self._lock:
            self._items[wf.id] = wf
        return wf

    def get(self, workflow_id: str) -> PublishWorkflow | None:
        with self._lock:
            return self._items.get(workflow_id)

    def list(self, *, tenant: TenantContext | None = None) -> list[PublishWorkflow]:
        with self._lock:
            items = list(self._items.values())
        if tenant is not None:
            items = [w for w in items if w.tenant_id == tenant.tenant_id]
        return sorted(items, key=lambda w: w.created_ts, reverse=True)


# Process-wide store shared by the API router.
_STORE = PublishWorkflowStore()


def get_workflow_store() -> PublishWorkflowStore:
    """Return the process-wide publish-workflow store."""
    return _STORE


def resolve_workflow(
    workflow: PublishWorkflow,
    *,
    approve: bool,
    container: ServiceContainer,
    tenant: TenantContext,
) -> PublishWorkflow:
    """Approve (and execute) or reject a pending workflow.

    On approval the Fabric write runs via the toolkit and the result/error is
    recorded; on rejection the workflow is simply marked ``rejected``. Already
    resolved workflows are returned unchanged.
    """
    if workflow.state != "pending":
        return workflow
    if not approve:
        workflow.state = "rejected"
        return workflow

    toolkit = ToolKit(container, tenant)
    try:
        if workflow.kind == "publish-model":
            workflow.result = toolkit.publish_model(**workflow.payload)
        elif workflow.kind == "publish-report":
            workflow.result = toolkit.publish_report(**workflow.payload)
        elif workflow.kind == "publish-model-report":
            workflow.result = _publish_model_and_report(toolkit, workflow.payload)
        else:  # pragma: no cover - guarded by typing
            raise ValueError(f"Unknown workflow kind: {workflow.kind}")
        workflow.state = "approved"
    except Exception as exc:  # noqa: BLE001 - surface as workflow failure
        workflow.state = "failed"
        workflow.error = str(exc)
    return workflow
