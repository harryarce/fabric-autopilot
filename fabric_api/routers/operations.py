"""Operation status routes (publish/design result re-fetch)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from fabric_services.errors import NotFoundError

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("/{operation_id}")
def get_operation(operation_id: str, request: Request) -> dict:
    """Return the recorded result of a long-running create operation."""
    op = request.app.state.operations.get(operation_id)
    if op is None:
        raise NotFoundError(f"Operation '{operation_id}' was not found.")
    return {
        "id": op.id,
        "kind": op.kind,
        "status": op.status,
        "result": op.result,
        "createdAt": op.created_at,
    }
