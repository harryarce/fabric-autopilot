"""JSON serialization for MCP tool results.

Self-contained (no FastAPI/pydantic dependency) so the agent-facing MCP layer
stays lightweight. Mirrors ``fabric_api.models.to_jsonable``: it prefers a
domain object's own ``to_dict()`` and otherwise unwraps dataclasses, enums, and
containers into JSON-ready structures.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/enums/containers to JSON-able data."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, type):
        return to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.repr
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value
