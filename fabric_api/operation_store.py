"""Lightweight operation registry for publish/design results.

Fabric long-running operations are already polled to completion inside
:class:`app.fabric_client.FabricClient`, so the API's create endpoints return a
terminal result synchronously. This registry records each completed operation
under a generated id so a client can re-fetch the result idempotently via
``GET /api/v1/operations/{id}`` within the process lifetime.

Note: the store is in-process (per replica). A durable, cross-replica
implementation (Azure Storage Queue + Table, or Durable Functions) is the
documented production upgrade path when fire-and-forget async is required.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Operation:
    id: str
    kind: str
    status: str  # "Succeeded" | "Created" | "Accepted" | "Failed"
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


class OperationStore:
    """Thread-safe in-memory registry of completed operations."""

    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}
        self._lock = threading.Lock()

    def record(self, kind: str, status: str, result: dict[str, Any]) -> Operation:
        op = Operation(
            id=str(uuid.uuid4()),
            kind=kind,
            status=status,
            result=result,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._ops[op.id] = op
        return op

    def get(self, operation_id: str) -> Operation | None:
        with self._lock:
            return self._ops.get(operation_id)
