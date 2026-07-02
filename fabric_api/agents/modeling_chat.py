"""Natural-language editing of an existing semantic model.

This is the first *live* consumer of the official Power BI Modeling MCP server
(``@microsoft/powerbi-modeling-mcp``). The user picks a workspace and a semantic
model in that workspace, then drives modelling changes — new measures, columns,
relationships, renames, descriptions, RLS, calculation groups, translations,
etc. — entirely through chat.

A single Foundry-backed agent is equipped with:

* the Power BI Modeling MCP tool (read-write, headless / skip-confirmation), and
* the ``powerbi-modeling-mcp-bridge`` skill and its Microsoft Fabric references
  (modeling, DAX, naming, performance) so edits follow best practice.

Each turn first asks the agent to connect to the chosen model
(``Connect to semantic model '<model>' in Fabric Workspace '<workspace>'``) and
then to carry out the request. The whole path is **best-effort with graceful
fallback**: when the Agent Framework, Foundry, or the MCP server cannot be
reached, :meth:`ModelingChatService.chat` returns a deterministic advisory
instead of raising, so the UI stays usable everywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
from dataclasses import dataclass
from typing import Any

from .config import AgentTeamConfig
from .mcp_clients import build_powerbi_modeling_tool
from .skills import load_skills_provider
from .team import agents_available

_SYSTEM_INSTRUCTIONS = (
    "You are a Power BI semantic-modeling copilot. You operate on a single, "
    "already-identified semantic model using the Power BI Modeling MCP tools "
    "(connection, model, table, column, measure, relationship, calculation "
    "group, RLS, perspective and DAX-query operations). Workflow for every "
    "turn: (1) FIRST establish a live connection by CALLING the actual "
    "connection tool — `connection_operations` (or `database_operations` with "
    "its connect action) — passing the Fabric workspace name and semantic "
    "model name; do NOT rely on any `ConnectToFabric` prompt, which only "
    "returns guidance and does not open a connection. Every later operation "
    "(table/column/measure/etc.) reuses this connection, so it must succeed "
    "before you do anything else. If an operation reports 'no last used "
    "connection available', call the connection tool again and retry. (2) when "
    "unsure of exact object names, inspect the model first — never invent "
    "names; (3) make the smallest correct change that satisfies the request; "
    "(4) reply in 1-4 sentences describing exactly what you changed (or would "
    "change) and any follow-up the user should consider. Follow the `powerbi-"
    "modeling-mcp-bridge` skill and its Microsoft Fabric references for naming "
    "conventions, DAX correctness and performance, and modelling best "
    "practices. Treat edits as deliberate: if a request is ambiguous or "
    "destructive, ask a brief clarifying question instead of guessing."
)


@contextlib.contextmanager
def _quiet_mcp_banner_logs():
    """Silence benign 'Invalid JSON' spam from the MCP stdio reader.

    The ``@microsoft/powerbi-modeling-mcp`` server prints human-readable banner
    lines (``Detected platform: ...``, ``Using ... version: ...``) to *stdout*,
    which the MCP stdio transport reserves for JSON-RPC. The reader logs each
    such line at ERROR level and then skips it — harmless, but very noisy. We
    raise those two loggers to CRITICAL for the duration of a live call and
    restore them afterwards so real errors elsewhere are unaffected.
    """
    names = ("mcp.client.stdio", "agent_framework._mcp")
    loggers = [logging.getLogger(name) for name in names]
    previous = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for lg, level in zip(loggers, previous):
            lg.setLevel(level)


@dataclass
class ModelingChatResult:
    """Outcome of one natural-language modelling turn."""

    reply: str
    used_agent: bool
    available: bool
    requires: list[str]
    connection: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "usedAgent": self.used_agent,
            "available": self.available,
            "requires": self.requires,
            "connection": self.connection,
        }


class ModelingChatService:
    """Drive live, natural-language edits against a chosen semantic model."""

    def __init__(self, config: AgentTeamConfig | None = None) -> None:
        self.config = config or AgentTeamConfig.from_env()

    # -- public API -------------------------------------------------------

    def chat(
        self,
        *,
        workspace_name: str,
        model_name: str,
        message: str,
        history: list[dict[str, str]] | None = None,
        workspace_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one turn and return a JSON-able result (never raises)."""
        history = history or []
        connection = {
            "workspace": workspace_name,
            "model": model_name,
            "workspaceId": workspace_id,
            "modelId": model_id,
        }

        missing = self._missing_prereqs(workspace_name, model_name, message)
        if missing:
            return ModelingChatResult(
                reply=self._fallback_reply(
                    workspace_name, model_name, message, missing
                ),
                used_agent=False,
                available=False,
                requires=missing,
                connection=connection,
            ).to_dict()

        try:
            reply = self._run_sync(
                self._chat_async(workspace_name, model_name, message, history)
            )
            return ModelingChatResult(
                reply=reply,
                used_agent=True,
                available=True,
                requires=[],
                connection=connection,
            ).to_dict()
        except Exception as exc:  # noqa: BLE001 - degrade, never break the UI
            return ModelingChatResult(
                reply=(
                    "The live modelling agent could not complete the request "
                    f"({type(exc).__name__}: {exc}). No changes were made to "
                    f"'{model_name}'. Verify the Power BI Modeling MCP server "
                    "can launch (Node/npx) and that the platform identity can "
                    "reach the model's XMLA endpoint."
                ),
                used_agent=False,
                available=False,
                requires=["live-agent"],
                connection=connection,
            ).to_dict()

    # -- readiness (no network) ------------------------------------------

    def _missing_prereqs(
        self, workspace_name: str, model_name: str, message: str
    ) -> list[str]:
        missing: list[str] = []
        if not (workspace_name and model_name):
            missing.append("workspace-and-model")
        if not (message and message.strip()):
            missing.append("message")
        if not agents_available():
            missing.append("agent-framework")
        if not self.config.powerbi_modeling_mcp.enabled:
            missing.append("powerbi-modeling-mcp-enabled")
        elif (
            build_powerbi_modeling_tool(
                self.config.powerbi_modeling_mcp,
                readonly=False,
                skip_confirmation=True,
            )
            is None
        ):
            missing.append("powerbi-modeling-mcp-tool")
        return missing

    def _fallback_reply(
        self,
        workspace_name: str,
        model_name: str,
        message: str,
        missing: list[str],
    ) -> str:
        reasons = {
            "workspace-and-model": "select a workspace and a semantic model",
            "message": "enter a request",
            "agent-framework": (
                "install the Microsoft Agent Framework (the 'agents' extra)"
            ),
            "powerbi-modeling-mcp-enabled": (
                "enable the Power BI Modeling MCP "
                "(POWERBI_MODELING_MCP_ENABLED=true)"
            ),
            "powerbi-modeling-mcp-tool": (
                "make the Power BI Modeling MCP server launchable "
                "(Node/npx for stdio, or set POWERBI_MODELING_MCP_URL)"
            ),
        }
        steps = "; ".join(reasons[m] for m in missing if m in reasons)
        target = (
            f"semantic model '{model_name}' in workspace '{workspace_name}'"
            if workspace_name and model_name
            else "the selected semantic model"
        )
        request_echo = (
            f' Your request — "{message.strip()}" — was not applied.'
            if message and message.strip()
            else ""
        )
        return (
            "Live model editing is not active in this environment, so no "
            f"changes were made to {target}.{request_echo} To enable it, "
            f"{steps}. Once active, this chat connects to the model through the "
            "official Power BI Modeling MCP server and applies your requested "
            "changes (measures, columns, relationships, renames, descriptions, "
            "RLS, calculation groups, translations, and more)."
        )

    # -- live path --------------------------------------------------------

    @staticmethod
    def _run_sync(coro: Any) -> Any:
        """Run ``coro`` on a private loop and tear it down cleanly.

        ``asyncio.run`` closes the loop the instant the coroutine returns, but
        the httpx/azure HTTP clients used by Foundry and Azure Identity finalize
        their connections slightly later. On Windows the Proactor loop is
        required (the MCP server is a subprocess), and those late finalizers
        schedule a ``call_soon`` on the now-closed loop, surfacing a harmless
        but noisy ``RuntimeError('Event loop is closed')`` /
        ``Task exception was never retrieved``. Draining pending callbacks and
        async generators — and forcing finalizers via ``gc.collect`` — before
        closing the loop lets those closes complete on a live loop.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            try:
                # Let finalizers (e.g. httpx.AsyncClient.__del__) schedule their
                # aclose tasks while the loop is still open, then drain them.
                gc.collect()
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(asyncio.sleep(0.1))
            except Exception:  # noqa: BLE001 - teardown best-effort
                pass
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    @staticmethod
    def _token_identity(token: str) -> dict[str, str]:
        """Decode the non-sensitive identity claims from a JWT access token.

        Returns ``aud``/``appid``/``upn``/``tid``/``oid`` (whichever are
        present) so logs can show *which* identity a token belongs to without
        leaking the signature or any secret material. Never raises.
        """
        import base64
        import json

        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception:  # noqa: BLE001 - opaque/non-JWT token
            return {}
        keys = ("aud", "appid", "azp", "upn", "unique_name", "tid", "oid")
        return {k: str(claims[k]) for k in keys if claims.get(k) is not None}

    @staticmethod
    async def _acquire_powerbi_token(credential: Any) -> str | None:
        """Acquire a Power BI / XMLA bearer token for the headless MCP server.

        The Modeling MCP server (``--authmode=interactive`` by default) cannot
        sign in without a browser, so we supply a token via
        ``PBI_MODELING_MCP_ACCESS_TOKEN``. The scope defaults to the Power BI
        service audience and is overridable via ``POWERBI_MODELING_MCP_SCOPE``.

        Acquisition is deliberately split between dev and Azure:

        1. Prefer the **Azure CLI** sign-in — the interactive user who actually
           owns the model. When ``az`` is present (a workstation) this is the
           only acceptable source: if the CLI *is* signed in but cannot mint a
           Power BI token we return ``None`` rather than falling back to a
           managed identity. On this kind of box the ambient credential chain
           (e.g. an Azure Arc managed identity such as ``e600146b``) mints a
           token for the WRONG identity that XMLA rejects with "Authentication
           failed for all authenticators"; returning ``None`` instead lets the
           headless server fall back to its own interactive sign-in for the
           correct user.
        2. Only when ``az`` is **unavailable** (e.g. in Azure, where the CLI is
           absent) fall back to the provided credential — the app's managed
           identity — which is the legitimate identity there.

        Returns ``None`` when no acceptable token can be minted, so the run
        either uses the server's interactive sign-in (dev) or surfaces a clear
        authentication error (Azure) rather than injecting a wrong identity.
        """
        log = logging.getLogger(__name__)
        scope = (
            os.environ.get("POWERBI_MODELING_MCP_SCOPE")
            or "https://analysis.windows.net/powerbi/api/.default"
        )

        async def _mint(cred: Any) -> str:
            token = await cred.get_token(scope)
            return token.token

        try:
            from azure.identity import CredentialUnavailableError
            from azure.identity.aio import AzureCliCredential
        except Exception:  # noqa: BLE001 - azure-identity missing
            AzureCliCredential = None  # type: ignore[assignment]
            CredentialUnavailableError = Exception  # type: ignore[assignment]

        cli_available = True
        if AzureCliCredential is not None:
            cli = AzureCliCredential()
            try:
                token = await _mint(cli)
                log.info(
                    "Power BI token acquired via Azure CLI %s",
                    ModelingChatService._token_identity(token),
                )
                return token
            except CredentialUnavailableError as exc:
                # ``az`` is not installed or not signed in — we are likely in
                # Azure; allow the managed-identity fallback below.
                cli_available = False
                log.info("Azure CLI credential unavailable (%s)", exc)
            except Exception as exc:  # noqa: BLE001 - az present but scope failed
                # The CLI is signed in but could not mint a Power BI token. Do
                # NOT inject a managed-identity token here (wrong identity on a
                # dev box); let the server sign the correct user in instead.
                log.warning(
                    "Azure CLI could not mint a Power BI token (%s); not "
                    "falling back to managed identity on a CLI host.",
                    exc,
                )
                return None
            finally:
                with contextlib.suppress(Exception):
                    await cli.close()

        if cli_available:
            return None

        try:
            token = await _mint(credential)
            log.info(
                "Power BI token acquired via provided credential %s",
                ModelingChatService._token_identity(token),
            )
            return token
        except Exception as exc:  # noqa: BLE001 - surface a clear auth failure
            log.warning("Could not acquire a Power BI token: %s", exc)
            return None

    async def _resolve_live_token(self, credential: Any) -> str | None:
        """Decide whether to inject a bearer token for the Modeling MCP server.

        - Local/dev (``headless`` is False): return ``None`` so the server uses
          its own interactive browser sign-in. This keeps the original,
          working developer behavior unchanged.
        - Headless hosts (the Azure containers, ``headless`` is True): acquire a
          Power BI / XMLA token from the platform identity and inject it. There
          is no browser to fall back to, so a missing token is a hard error
          with an actionable message instead of a doomed interactive sign-in.
        """
        if not self.config.powerbi_modeling_mcp.headless:
            return None
        token = await self._acquire_powerbi_token(credential)
        if not token:
            raise RuntimeError(
                "Headless Power BI Modeling MCP is enabled but no XMLA access "
                "token could be acquired for the platform identity. Grant the "
                "app's managed identity Member/Contributor (build & write) "
                "access on the target Fabric workspace, or run locally with "
                "interactive auth (POWERBI_MODELING_MCP_HEADLESS=false)."
            )
        return token

    async def _chat_async(
        self,
        workspace_name: str,
        model_name: str,
        message: str,
        history: list[dict[str, str]],
    ) -> str:
        from agent_framework import Agent
        from agent_framework.foundry import FoundryChatClient
        from azure.identity.aio import DefaultAzureCredential

        skills_provider = load_skills_provider()
        context_providers = [skills_provider] if skills_provider else None

        connect = (
            "First, open a live connection by calling the connection tool "
            "(`connection_operations`, or `database_operations` with its connect "
            "action) for the semantic model "
            f"'{model_name}' in the Fabric workspace '{workspace_name}'. Do not "
            "treat the `ConnectToFabric` prompt as the connection — you must "
            "invoke the connection tool so the following operations reuse it. "
            "Only after the connection succeeds, carry out the request."
        )
        transcript = self._render_history(history)
        prompt = connect + "\n\n"
        if transcript:
            prompt += f"Conversation so far:\n{transcript}\n\n"
        prompt += f"User request: {message.strip()}"

        client_id = os.environ.get("AZURE_CLIENT_ID") or None
        credential = DefaultAzureCredential(
            managed_identity_client_id=client_id,
            exclude_interactive_browser_credential=True,
        )
        with _quiet_mcp_banner_logs():
            async with credential:
                # Authentication strategy is selected by ``headless``:
                #   * Local/dev (headless False): no token is injected; the MCP
                #     server runs with ``--authmode=interactive`` and opens its
                #     own browser sign-in for the user who owns the model. This
                #     is the original, working developer behavior.
                #   * Azure containers (headless True): there is no browser, so
                #     a Power BI / XMLA bearer token is minted from the platform
                #     managed identity and injected via
                #     ``PBI_MODELING_MCP_ACCESS_TOKEN``. A missing token raises a
                #     clear, actionable error (see ``_resolve_live_token``).
                access_token = await self._resolve_live_token(credential)
                tool = build_powerbi_modeling_tool(
                    self.config.powerbi_modeling_mcp,
                    readonly=False,
                    skip_confirmation=True,
                    access_token=access_token,
                )
                if tool is None:  # pragma: no cover - guarded by _missing_prereqs
                    raise RuntimeError("Power BI Modeling MCP tool is unavailable.")

                chat_client = FoundryChatClient(
                    project_endpoint=self.config.project_endpoint,
                    model=self.config.model,
                    credential=credential,
                )
                # Scope the MCP server connection to THIS event loop so its
                # stdio subprocess starts and stops on a live loop. Without an
                # explicit ``async with tool`` the connection lifecycle is not
                # bound to the running loop and teardown races the loop close,
                # surfacing ``RuntimeError: Event loop is closed``.
                async with tool:
                    agent = Agent(
                        name="semantic-model-copilot",
                        client=chat_client,
                        instructions=_SYSTEM_INSTRUCTIONS,
                        description=(
                            "Edits an existing semantic model via natural language."
                        ),
                        tools=[tool],
                        context_providers=context_providers,
                    )
                    async with agent:
                        result = await agent.run(prompt)
        text = getattr(result, "text", None) or str(result)
        return text.strip() or "Done."

    @staticmethod
    def _render_history(history: list[dict[str, str]]) -> str:
        lines: list[str] = []
        for turn in history[-12:]:
            role = (turn.get("role") or "").lower()
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            speaker = "User" if role in {"user", "human"} else "Copilot"
            lines.append(f"{speaker}: {content}")
        return "\n".join(lines)
