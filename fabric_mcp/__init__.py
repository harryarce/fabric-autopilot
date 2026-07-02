"""Fabric SaaS MCP server package.

Exposes the platform's service layer to AI agents as typed MCP tools. The
server is agent-first: agentic design / suggestion / audit are enabled by
default with deterministic fallback.
"""

from __future__ import annotations

from .server import main, mcp

__all__ = ["mcp", "main"]
