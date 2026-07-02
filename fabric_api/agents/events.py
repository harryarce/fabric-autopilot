"""Shared event schema for agentic runs.

The API (SSE), the MCP tools, and the Streamlit UI all render the *same*
transcript, so the event shape is defined once here. Events are plain
dataclasses with a stable ``to_dict`` projection (mirroring the platform's
``to_jsonable`` convention) so they serialise cleanly to JSON / SSE frames.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class AgentEvent:
    """A single step in an orchestration transcript.

    ``kind`` is one of:

    * ``"plan"``      — the manager's high-level plan for the objective.
    * ``"step"``      — a tool/agent step started (``status="running"``) or
                        finished (``status="ok"`` / ``"error"``).
    * ``"message"``   — narrative text from an agent.
    * ``"artifact"``  — a produced spec/definition (payload in ``data``).
    * ``"approval"``  — a side-effecting action is awaiting human approval.
    * ``"final"``     — terminal summary of the run.
    """

    kind: str
    agent: str = "orchestrator"
    text: str = ""
    status: str = "ok"
    data: dict[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=_now_ms)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "agent": self.agent,
            "status": self.status,
            "ts": self.ts,
        }
        if self.text:
            out["text"] = self.text
        if self.data:
            out["data"] = self.data
        return out


@dataclass
class OrchestrationResult:
    """The collected outcome of a (non-streaming) orchestration run."""

    objective: str
    events: list[AgentEvent] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    used_agent: bool = False
    status: str = "ok"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "status": self.status,
            "usedAgent": self.used_agent,
            "error": self.error,
            "events": [e.to_dict() for e in self.events],
            "artifacts": self.artifacts,
        }
