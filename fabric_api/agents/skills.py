"""Skill loading for the agent team.

Agents are equipped with **skills** — procedural playbooks the model consults
on demand — via the Microsoft Agent Framework's :class:`SkillsProvider`. The
team's overlay skills live alongside this package in :mod:`fabric_api.agents`'s
``skills/`` directory and encode this platform's conventions for semantic-model
authoring and report design, plus a bridge note for the official Power BI
Modeling MCP tools.

Everything here is **lazy and optional**: if the Agent Framework is not
installed (or the skills dir is empty) the loaders return ``None`` and callers
degrade to deterministic behaviour.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

_SKILLS_DIR = Path(__file__).parent / "skills"


def skills_dir() -> Path:
    """Absolute path to the team's overlay skills directory."""
    return _SKILLS_DIR


def _subprocess_script_runner(skill: Any, script: Any, args: list[str]) -> str:
    """Run a file-based skill script in a subprocess and return its stdout.

    Mirrors the ``script_runner`` contract used by ``app.intelligence.agent`` so
    behaviour is identical across the platform.
    """
    import subprocess
    import sys

    script_path = Path(script.full_path)
    completed = subprocess.run(  # noqa: S603 - trusted, bundled scripts only
        [sys.executable, str(script_path), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(script_path.parent),
    )
    if completed.returncode != 0:
        return (
            f"Script failed (exit {completed.returncode}).\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return completed.stdout


def load_skills_provider() -> Any | None:
    """Build a ``SkillsProvider`` over the overlay skills, or ``None``.

    Returns ``None`` when the Agent Framework is unavailable or no skills are
    present, so the caller can omit ``context_providers`` cleanly.
    """
    if not _SKILLS_DIR.exists() or not any(_SKILLS_DIR.iterdir()):
        return None
    try:
        from agent_framework import SkillsProvider
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    warnings.filterwarnings("ignore", message=r"\[SKILLS\].*")
    try:
        return SkillsProvider.from_paths(
            skill_paths=str(_SKILLS_DIR),
            script_runner=_subprocess_script_runner,
        )
    except Exception:  # noqa: BLE001 - never let skill loading break a run
        return None
