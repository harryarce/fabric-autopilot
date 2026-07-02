"""Client for the Microsoft **Power BI Modeling MCP** server.

This module is the bridge between fabric-autopilot and the
``@microsoft/powerbi-modeling-mcp`` server published by Microsoft
(https://github.com/microsoft/powerbi-modeling-mcp). The MCP server is
launched as a local stdio child process and we talk to it with the official
``mcp`` Python SDK. Two of its tools are central to natural-language Q&A
against a Power BI / Fabric semantic model:

* ``connection_operations`` — connect to a semantic model in a Fabric
  workspace (we use the ``ConnectFabric`` operation).
* ``dax_query_operations`` — execute or validate a DAX query against the
  currently connected model (we use ``Execute`` and ``Validate``).

The MCP server normally prompts an interactive sign-in. To keep the
Streamlit experience seamless we acquire a Power BI XMLA token via
:class:`app.auth.TokenProvider` and inject it through the
``PBI_MODELING_MCP_ACCESS_TOKEN`` environment variable, exactly as documented
in the server's README.

All public entry points are **synchronous** so they compose naturally with
Streamlit; an internal ``asyncio`` event loop drives the MCP session and the
subprocess is reused for the lifetime of the wrapper.

The module never speculates about responses: every call records the raw MCP
tool name, the JSON arguments, and the structured/text result so the UI can
display a faithful transcript that proves the queries went through the
**dax_query_operations** tool.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import auth as auth_module

logger = logging.getLogger("fabric.intelligence.pbi_modeling_mcp")


# Default launch command — matches the manual MCP-client recipe in the README:
# ``npx -y @microsoft/powerbi-modeling-mcp@latest --start``. ``--readonly``
# disables every write tool so an accidental NL question can never mutate the
# model, and ``--skipconfirmation`` keeps the elicitation prompts from
# blocking the stdio session (we already audited every call site).
# ``--authmode=interactive`` is the README default; we set it explicitly so
# the server unambiguously honours the ``PBI_MODELING_MCP_ACCESS_TOKEN`` env
# var instead of ever silently falling back to another auth path.
_DEFAULT_NPM_PACKAGE = "@microsoft/powerbi-modeling-mcp@latest"
_DEFAULT_ARGS_TAIL = (
    "--start",
    "--readonly",
    "--skipconfirmation",
    "--authmode=interactive",
)

# Cap the stderr tail we splice into error messages so we never dump megabytes
# of trace data into an HTTP response body.
_STDERR_TAIL_BYTES = 8 * 1024

# Env override knobs — let advanced users point at a manually-installed binary
# (``--manual-install`` in the README) without code changes.
_ENV_COMMAND = "PBI_MODELING_MCP_COMMAND"  # e.g. r"C:\\MCPServers\\...\\powerbi-modeling-mcp.exe"
_ENV_EXTRA_ARGS = "PBI_MODELING_MCP_ARGS"  # space-separated extra args
_ENV_TOKEN = "PBI_MODELING_MCP_ACCESS_TOKEN"


# Service-principal / user credential variables that the .NET server's Azure
# Identity chain (``EnvironmentCredential``) will pick up automatically. On a
# workstation these frequently belong to an unrelated app registration in a
# different tenant with NO Power BI access; if inherited by the server they
# make ``ConnectFabric`` fail with *"Authentication failed for all
# authenticators"* — overriding both the injected bearer token and interactive
# sign-in. Kept in lock-step with the Agent Studio's own sanitiser at
# ``fabric_api/agents/mcp_clients.py`` so both surfaces (Ask Your Model and
# Agent Studio) behave identically.
_AZURE_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_CLIENT_CERTIFICATE_PATH",
    "AZURE_CLIENT_CERTIFICATE_PASSWORD",
    "AZURE_USERNAME",
    "AZURE_PASSWORD",
)

# Managed-identity / Azure Arc discovery variables. The .NET server's
# ``DefaultAzureCredential`` finds a managed identity through THESE (not the
# ``AZURE_*`` vars above): App Service / Functions / Service Fabric expose
# ``IDENTITY_ENDPOINT``/``IDENTITY_HEADER``, Azure Arc adds ``IMDS_ENDPOINT``,
# Cloud Shell uses ``MSI_ENDPOINT``/``MSI_SECRET``, and AKS pod identity uses
# ``AZURE_POD_IDENTITY_AUTHORITY_HOST``. On a workstation an ambient Azure Arc
# managed identity often belongs to a principal with NO Power BI access and is
# flaky; if the server reaches for it, ``ConnectFabric`` fails intermittently
# with *"Authentication failed for all authenticators"* even though a valid
# bearer token was injected. When we hand the server an explicit token these
# are stripped so it authenticates with that token and nothing else.
_MANAGED_IDENTITY_ENV_VARS: tuple[str, ...] = (
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "IDENTITY_SERVER_THUMBPRINT",
    "IMDS_ENDPOINT",
    "MSI_ENDPOINT",
    "MSI_SECRET",
    "AZURE_POD_IDENTITY_AUTHORITY_HOST",
)


def _sanitize_subprocess_env(
    base: dict[str, str],
    *,
    access_token: str | None,
) -> dict[str, str]:
    """Return a copy of ``base`` safe to hand to the MCP subprocess.

    Always drops the ``AZURE_*`` service-principal / user variables so an
    ambient ``EnvironmentCredential`` in the server's identity chain cannot
    hijack authentication with the wrong tenant. When an explicit access
    token is being injected, also drops the managed-identity / Azure Arc
    discovery variables so the .NET server authenticates *only* with the
    token we just handed it — this is the same recipe the Agent Studio
    uses at ``fabric_api/agents/mcp_clients.py``.
    """
    stripped: set[str] = set(_AZURE_CREDENTIAL_ENV_VARS)
    if access_token:
        stripped.update(_MANAGED_IDENTITY_ENV_VARS)
    return {k: v for k, v in base.items() if k not in stripped}


class PowerBiModelingMcpError(RuntimeError):
    """Raised when the Power BI Modeling MCP server cannot be reached or
    returned an error result for a tool call."""


@dataclass
class McpCall:
    """One MCP tool invocation, captured for the UI transcript."""

    tool: str
    arguments: dict[str, Any]
    is_error: bool
    text: str  # the concatenated text content blocks
    structured: Any | None = None  # tool's ``structuredContent`` if any

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "is_error": self.is_error,
            "text": self.text,
            "structured": self.structured,
        }


@dataclass
class DaxExecutionResult:
    """Outcome of a ``dax_query_operations`` execute call."""

    dax: str
    columns: list[str]
    rows: list[list[Any]]
    raw: Any
    calls: list[McpCall] = field(default_factory=list)


@dataclass
class DaxValidationResult:
    """Outcome of a ``dax_query_operations`` validate call."""

    dax: str
    is_valid: bool
    message: str
    raw: Any
    calls: list[McpCall] = field(default_factory=list)


def _resolve_launch_command() -> tuple[str, list[str]]:
    """Build the (command, args) tuple used to spawn the MCP server.

    Honours ``PBI_MODELING_MCP_COMMAND`` (e.g. for a manual VSIX install) and
    falls back to launching the published npm package through ``npx``.
    """
    override = os.environ.get(_ENV_COMMAND, "").strip()
    extra_raw = os.environ.get(_ENV_EXTRA_ARGS, "").strip()
    extra = extra_raw.split() if extra_raw else []
    if override:
        # The override is the full executable path; --start is still required
        # so the server actually enters its MCP stdio loop.
        return override, ["--start", *extra]

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise PowerBiModelingMcpError(
            "Could not find 'npx' on PATH. Install Node.js "
            "(https://nodejs.org) or set the "
            f"{_ENV_COMMAND} environment variable to the full path of a "
            "manually-installed 'powerbi-modeling-mcp' executable."
        )
    args = ["-y", _DEFAULT_NPM_PACKAGE, *_DEFAULT_ARGS_TAIL, *extra]
    return npx, args


def _join_text(blocks: Iterable[Any]) -> str:
    """Concatenate ``text`` content blocks returned by an MCP tool call."""
    parts: list[str] = []
    for block in blocks or ():
        # ``mcp`` typed objects expose ``.text`` for text content; embedded
        # resources expose ``.resource``. Anything we don't recognise is
        # stringified so the transcript never silently drops information.
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
            continue
        resource = getattr(block, "resource", None)
        if resource is not None:
            inner = getattr(resource, "text", None) or getattr(resource, "blob", None)
            if inner is not None:
                parts.append(str(inner))
                continue
        parts.append(str(block))
    return "\n".join(parts)


def _parse_table_from_text(text: str) -> tuple[list[str], list[list[Any]]] | None:
    """Best-effort extraction of a tabular ``execute`` payload from text.

    The Power BI Modeling MCP server returns DAX results as JSON inside a
    text content block. We accept either an array of row-objects (the
    common case) or an explicit ``{"columns": [...], "rows": [...]}`` shape,
    and fall back to ``None`` so the caller can still show the raw payload.
    """
    if not text:
        return None
    text = text.strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return _table_from_object(data)


def _table_from_object(data: Any) -> tuple[list[str], list[list[Any]]] | None:
    if isinstance(data, dict):
        # Shape 1: {"columns": ["A", "B"], "rows": [[1, 2], ...]}
        cols = data.get("columns")
        rows = data.get("rows")
        if isinstance(cols, list) and isinstance(rows, list):
            return [str(c) for c in cols], [list(r) for r in rows]
        # Shape 2: {"results": [{"tables": [{"rows": [{...}, ...]}]}]} —
        # the executeQueries REST shape, surfaced verbatim by some builds.
        results = data.get("results")
        if isinstance(results, list) and results:
            tables = (results[0] or {}).get("tables")
            if isinstance(tables, list) and tables:
                inner_rows = (tables[0] or {}).get("rows")
                if isinstance(inner_rows, list):
                    return _table_from_object(inner_rows)
        # Shape 3: {"data": [...]} — common wrapper.
        if isinstance(data.get("data"), list):
            return _table_from_object(data["data"])
        return None
    if isinstance(data, list):
        if not data:
            return [], []
        if all(isinstance(r, dict) for r in data):
            columns: list[str] = []
            seen: set[str] = set()
            for row in data:
                for key in row.keys():
                    if key not in seen:
                        seen.add(key)
                        columns.append(str(key))
            rows = [[row.get(col) for col in columns] for row in data]
            return columns, rows
        if all(isinstance(r, list) for r in data):
            width = max((len(r) for r in data), default=0)
            columns = [f"Column{i + 1}" for i in range(width)]
            return columns, [list(r) for r in data]
    return None


def _result_is_error(result: Any) -> bool:
    """Read the ``isError`` flag of an MCP CallToolResult defensively."""
    flag = getattr(result, "isError", None)
    if flag is None and isinstance(result, dict):
        flag = result.get("isError")
    return bool(flag)


class PowerBiModelingMcp:
    """Synchronous wrapper around the Power BI Modeling MCP stdio server.

    Spawns the server on first use, keeps the connection open across many
    calls (one shared event loop on a background thread), and exposes the
    small surface used by the natural-language Q&A flow:

    * :meth:`connect_to_fabric_model` → ``connection_operations``.
    * :meth:`execute_dax`             → ``dax_query_operations`` (execute).
    * :meth:`validate_dax`            → ``dax_query_operations`` (validate).

    Every call also goes through :meth:`call_tool` so unit tests and the UI
    transcript can inspect exactly which MCP tools were invoked.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._access_token = access_token
        self._env_extra = dict(env or {})
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any | None = None  # mcp.ClientSession (lazy import)
        # Signals the MCP session's owner-task to exit. Created on the loop
        # thread inside :meth:`_lifetime` so anyio's cancel scopes stay in
        # the task that opened them.
        self._shutdown_event: asyncio.Event | None = None
        self._lifetime_future: concurrent.futures.Future | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._tool_names: list[str] | None = None
        # ``inputSchema`` per tool name, captured from ``list_tools()``. We
        # splice the relevant schema into error messages so a caller can
        # immediately see whether their argument names/types were wrong
        # instead of chasing an opaque "An error occurred invoking 'X'".
        self._tool_schemas: dict[str, Any] = {}
        # Real file (not a pipe/StringIO) that receives the MCP server's
        # stderr. ``stdio_client`` forwards ``errlog`` straight through to
        # ``subprocess.Popen(stderr=...)`` which requires a real fileno.
        # The tail is included in error messages and the file is removed on
        # :meth:`close`.
        self._stderr_file: Any | None = None
        self._stderr_path: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "PowerBiModelingMcp":
        self._ensure_started()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401 - context manager
        self.close()

    def close(self) -> None:
        """Tear down the MCP session and the background event loop.

        Signals the lifetime coroutine (which owns the ``stdio_client`` /
        ``ClientSession`` async contexts) to unwind on its own task, then
        stops the worker loop. Doing the tear-down inside the same task
        that entered the async contexts is required by anyio's structured
        concurrency — otherwise we hit *"Attempted to exit cancel scope
        in a different task than it was entered in"*.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            shutdown = self._shutdown_event
            lifetime = self._lifetime_future
        if loop is not None and not loop.is_closed():
            if shutdown is not None:
                try:
                    loop.call_soon_threadsafe(shutdown.set)
                except RuntimeError:  # pragma: no cover - loop already closed
                    pass
            if lifetime is not None:
                try:
                    lifetime.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    logger.warning(
                        "powerbi-modeling-mcp lifetime did not exit within "
                        "10s of shutdown signal; abandoning."
                    )
                except Exception:  # pragma: no cover - startup already failed
                    logger.debug(
                        "powerbi-modeling-mcp lifetime finished with error",
                        exc_info=True,
                    )
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:  # pragma: no cover - loop already closed
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _ensure_started(self) -> None:
        """Start the background loop and spawn the MCP server on demand."""
        with self._lock:
            if self._session is not None:
                return
            if self._closed:
                raise PowerBiModelingMcpError("Client was already closed.")

        loop = asyncio.new_event_loop()
        loop_started = threading.Event()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            loop_started.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_runner, name="pbi-modeling-mcp", daemon=True
        )
        thread.start()
        loop_started.wait(timeout=5)
        self._loop = loop
        self._thread = thread

        # The lifetime coroutine owns the AsyncExitStack / stdio_client /
        # ClientSession contexts for their full lifetime, so anyio's cancel
        # scopes are always entered *and* exited on the same task. The
        # ``ready`` future signals startup completion (or failure) back to
        # this thread.
        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._lifetime_future = asyncio.run_coroutine_threadsafe(
            self._lifetime(ready), loop
        )
        try:
            ready.result(timeout=60)
        except Exception as exc:
            # Startup failed; drain the lifetime task and tear the loop down
            # so a caller can retry from a clean slate.
            with self._lock:
                self._closed = True
            try:
                self._lifetime_future.result(timeout=5)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
            thread.join(timeout=5)
            raise PowerBiModelingMcpError(
                f"Failed to launch the Power BI Modeling MCP server: {exc}"
            ) from exc

    async def _lifetime(self, ready: concurrent.futures.Future) -> None:
        """Own the MCP session's async contexts from open to close.

        Runs on the worker loop. Opens the stdio subprocess and the
        ``ClientSession``, signals ``ready``, then parks on
        :attr:`_shutdown_event` until :meth:`close` sets it. Because both
        the ``async with`` entries and their exits happen on this single
        task, anyio's cancel scopes stay balanced.
        """
        # Imported lazily so the rest of the app keeps working when the
        # ``mcp`` optional extra is not installed.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._shutdown_event = asyncio.Event()
        try:
            command, args = _resolve_launch_command()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            return

        env: dict[str, str] = _sanitize_subprocess_env(
            dict(os.environ), access_token=self._access_token
        )
        if self._access_token:
            env[_ENV_TOKEN] = self._access_token
        env.update(self._env_extra)

        params = StdioServerParameters(command=command, args=args, env=env)
        logger.info(
            "Launching Power BI Modeling MCP server: %s %s",
            command,
            " ".join(args),
        )

        # Open a real-file stderr sink so we can splice tail bytes into
        # error messages. Using a tempfile (not a StringIO) is required
        # because the underlying ``subprocess`` needs a real fileno.
        try:
            self._stderr_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="w+",
                encoding="utf-8",
                errors="replace",
                prefix="pbi-modeling-mcp-stderr-",
                suffix=".log",
                delete=False,
            )
            self._stderr_path = self._stderr_file.name
            errlog: Any = self._stderr_file
        except OSError:  # pragma: no cover - fs full / permission denied
            errlog = sys.stderr

        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    try:
                        await session.initialize()
                        tools_resp = await session.list_tools()
                        self._tool_names = [t.name for t in tools_resp.tools]
                        self._tool_schemas = {
                            t.name: getattr(t, "inputSchema", None)
                            for t in tools_resp.tools
                        }
                        logger.info(
                            "powerbi-modeling-mcp connected; tools=%s",
                            self._tool_names,
                        )
                        # Log the schemas of the two tools we actually invoke
                        # at DEBUG so anyone hitting an arg-name issue can
                        # grep the app log rather than reading the source.
                        for critical in ("connection_operations", "dax_query_operations"):
                            schema = self._tool_schemas.get(critical)
                            if schema is not None:
                                logger.debug(
                                    "powerbi-modeling-mcp tool %s inputSchema=%s",
                                    critical,
                                    json.dumps(schema, default=str)[:1000],
                                )
                        self._session = session
                    except BaseException as exc:
                        if not ready.done():
                            ready.set_exception(exc)
                        return
                    if not ready.done():
                        ready.set_result(None)
                    # Park until close() signals shutdown.
                    await self._shutdown_event.wait()
        except BaseException as exc:  # pragma: no cover - subprocess errors
            if not ready.done():
                ready.set_exception(exc)
            else:
                # We were healthy but the subprocess died or teardown failed.
                # Log at debug — the caller has already moved on.
                logger.debug(
                    "powerbi-modeling-mcp lifetime exited with %r", exc
                )
        finally:
            self._session = None
            # Best-effort cleanup of the stderr sink. We close *and* unlink
            # so long-running processes don't leak temp files.
            stderr_file = self._stderr_file
            stderr_path = self._stderr_path
            self._stderr_file = None
            if stderr_file is not None:
                try:
                    stderr_file.close()
                except Exception:  # pragma: no cover - already closed
                    pass
            if stderr_path:
                try:
                    os.unlink(stderr_path)
                except OSError:  # pragma: no cover - already gone
                    pass
                self._stderr_path = None

    # ------------------------------------------------------------------
    # Tool catalogue
    # ------------------------------------------------------------------

    def available_tools(self) -> list[str]:
        """Return the MCP tool names exposed by the server (sorted)."""
        self._ensure_started()
        return sorted(self._tool_names or [])

    def tool_input_schema(self, name: str) -> Any | None:
        """Return the JSON-Schema for an MCP tool (or ``None`` if unknown).

        Captured once from ``list_tools()`` on session startup. Used mostly
        so error messages can show the expected parameter shape when a call
        fails with an argument-related error.
        """
        self._ensure_started()
        return self._tool_schemas.get(name)

    def _stderr_tail(self, max_bytes: int = _STDERR_TAIL_BYTES) -> str:
        """Return the last ``max_bytes`` of the MCP server's stderr log.

        The Power BI Modeling MCP server wraps most Fabric-side failures in
        a generic ``"An error occurred invoking 'X'."`` message. The
        actionable detail (XMLA HTTP status, model-not-found, permission
        error, tenant-setting error, capacity check) is written to stderr.
        Splicing the tail into the exception message makes those failures
        diagnosable without having to run ``dotnet-trace``.
        """
        path = self._stderr_path
        if not path:
            return ""
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                offset = max(0, size - max_bytes)
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return ""
        try:
            return data.decode("utf-8", errors="replace").strip()
        except Exception:  # pragma: no cover - defensive
            return ""

    # ------------------------------------------------------------------
    # Generic tool dispatch
    # ------------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCall:
        """Invoke an MCP tool by name and capture the response."""
        self._ensure_started()
        assert self._loop is not None and self._session is not None
        fut = asyncio.run_coroutine_threadsafe(
            self._async_call(name, arguments), self._loop
        )
        try:
            return fut.result(timeout=180)
        except Exception as exc:  # pragma: no cover - upstream
            # Some exceptions (notably ``concurrent.futures.TimeoutError``)
            # stringify to the empty string, which turns "tool X failed: "
            # into an unhelpful message. Fall back to ``repr(exc)`` so the
            # caller always sees the exception type at minimum.
            detail = str(exc) or repr(exc)
            raise PowerBiModelingMcpError(
                f"powerbi-modeling-mcp tool '{name}' failed: {detail}"
            ) from exc

    async def _async_call(self, name: str, arguments: dict[str, Any]) -> McpCall:
        assert self._session is not None
        result = await self._session.call_tool(name, arguments)
        text = _join_text(getattr(result, "content", None) or [])
        structured = getattr(result, "structuredContent", None)
        return McpCall(
            tool=name,
            arguments=arguments,
            is_error=_result_is_error(result),
            text=text,
            structured=structured,
        )

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _format_error_message(
        self,
        prefix: str,
        call: McpCall,
        *,
        context: str | None,
        hint: str,
        tool_name_for_schema: str | None,
    ) -> str:
        """Build a diagnostic error string for a failed MCP tool call.

        Combines: text (usually the generic MCP wrapper), structured
        content (when the server returned any), the tail of the MCP
        server's stderr log (where the real Fabric-side reason typically
        lives), and \u2014 as a last resort \u2014 the tool's inputSchema so
        the caller can see the expected argument shape.
        """
        parts: list[str] = [f"{prefix} failed"]
        if context:
            parts[0] = f"{parts[0]} for {context}"

        detail = (call.text or "").strip()
        if detail and detail != f"An error occurred invoking '{tool_name_for_schema}'.":
            parts.append(f": {detail}")
        elif detail:
            # The generic MCP wrapper \u2014 keep it, but don't pretend it's
            # useful on its own.
            parts.append(f": {detail}")
        else:
            parts.append(": (no text content returned by MCP server)")

        if call.structured is not None:
            try:
                structured_str = json.dumps(call.structured, default=str)
            except (TypeError, ValueError):
                structured_str = str(call.structured)
            if len(structured_str) > 800:
                structured_str = structured_str[:800] + "\u2026(truncated)"
            parts.append(f" | structured={structured_str}")

        stderr_tail = self._stderr_tail()
        if stderr_tail:
            # Only surface the *tail* of stderr so we don't dump the entire
            # subprocess log into an HTTP body.
            trimmed = stderr_tail[-2000:]
            parts.append(f" | server_stderr_tail=<<<{trimmed}>>>")

        if tool_name_for_schema:
            schema = self._tool_schemas.get(tool_name_for_schema)
            if schema is not None:
                try:
                    schema_str = json.dumps(schema, default=str)
                except (TypeError, ValueError):
                    schema_str = str(schema)
                if len(schema_str) > 600:
                    schema_str = schema_str[:600] + "\u2026(truncated)"
                parts.append(f" | tool_inputSchema={schema_str}")

        if hint:
            parts.append(f". Hint: {hint}")
        return "".join(parts)

    def connect_to_fabric_model(
        self,
        workspace: str,
        semantic_model: str,
    ) -> McpCall:
        """Connect the MCP session to a Fabric workspace semantic model.

        Uses the ``connection_operations`` tool with the ``ConnectFabric``
        operation — the same path the README's *ConnectToFabric* prompt
        drives. The Power BI Modeling MCP server wraps every action in a
        ``request`` object; the ``operation`` field selects the action
        (see the ``inputSchema`` printed by ``scripts/probe_pbi_mcp_schemas.py``).
        """
        call = self.call_tool(
            "connection_operations",
            {
                "request": {
                    "operation": "ConnectFabric",
                    "workspaceName": workspace,
                    "semanticModelName": semantic_model,
                }
            },
        )
        if call.is_error:
            raise PowerBiModelingMcpError(
                self._format_error_message(
                    "connection_operations.ConnectFabric",
                    call,
                    context=(
                        f"workspace={workspace!r} model={semantic_model!r}"
                    ),
                    hint=(
                        "Verify that (1) a Power BI XMLA access token is "
                        "available (PBI_MODELING_MCP_ACCESS_TOKEN or Azure "
                        "CLI / managed identity credentials with scope "
                        "https://analysis.windows.net/powerbi/api/.default), "
                        "(2) the workspace and semantic model names are "
                        "correct and the caller has 'Read' or higher "
                        "permission on them, (3) the workspace is on a "
                        "capacity that permits XMLA read (tenant setting "
                        "'Allow XMLA endpoints and Analyze in Excel with "
                        "on-premises datasets' must be enabled), and "
                        "(4) no ambient AZURE_* / IDENTITY_* env vars are "
                        "hijacking the server's identity chain (they are "
                        "stripped from the subprocess by default, matching "
                        "the Agent Studio's own MCP client)."
                    ),
                    tool_name_for_schema="connection_operations",
                )
            )
        return call

    # ------------------------------------------------------------------
    # DAX
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_dax_error_text(call: McpCall) -> str:
        """Return the clean server-side error text for a failed DAX call.

        The MCP server returns structured JSON of the shape
        ``{"message": "...", "operation": "Execute",
        "data": {"errorMessage": "...", ...}}``. The nested ``errorMessage``
        is the raw Analysis-Services diagnostic and the most useful thing
        to surface. Falls back to ``call.text`` when the shape differs.
        Also strips Microsoft's ``<oii>...</oii>`` PII-masking tags so
        object names read naturally.
        """
        raw: str | None = None
        structured = call.structured
        if isinstance(structured, dict):
            data = structured.get("data")
            if isinstance(data, dict):
                candidate = data.get("errorMessage")
                if isinstance(candidate, str) and candidate.strip():
                    raw = candidate
            if raw is None:
                candidate = structured.get("message")
                if isinstance(candidate, str) and candidate.strip():
                    raw = candidate
        if raw is None:
            raw = (call.text or "").strip()
        # Strip <oii>...</oii> PII masking that Fabric wraps object names in.
        cleaned = re.sub(r"</?oii>", "", raw)
        return cleaned.strip()

    @staticmethod
    def _dax_execute_hint(error_text: str) -> str:
        """Pick a targeted, actionable hint for well-known DAX-run errors.

        Falls back to the generic hint when nothing matches.
        """
        lowered = error_text.lower()
        if "is not refreshed" in lowered or "fallback to directquery is disabled" in lowered:
            return (
                "The semantic model contains an Import-mode table that has "
                "never been refreshed, so it has no data to query. Fix in "
                "the Fabric portal: open the semantic model → Refresh (or "
                "schedule a refresh). Alternatively, switch the table's "
                "storage mode to DirectQuery, or enable 'DirectQuery over "
                "Power BI datasets and Analysis Services' fallback on the "
                "model. See https://go.microsoft.com/fwlink/?linkid=2248855."
            )
        if "cannot find table" in lowered or "cannot find the column" in lowered:
            return (
                "The DAX query references a table or column that does not "
                "exist in the connected semantic model. Re-import the model "
                "schema (the cached TMDL may be stale) and try again."
            )
        if "permission" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
            return (
                "The caller does not have permission to query this semantic "
                "model. Ensure the identity has at least 'Build' on the "
                "dataset and 'Viewer' on the workspace."
            )
        if "timed out" in lowered or "timeout" in lowered:
            return (
                "The query exceeded the server-side timeout. Narrow the "
                "question (add a filter, request fewer rows) and retry."
            )
        return (
            "Confirm the semantic model is connected (a prior "
            "connection_operations.ConnectFabric call must have succeeded) "
            "and that the DAX query is valid."
        )

    def execute_dax(self, dax: str) -> DaxExecutionResult:
        """Run a DAX query via ``dax_query_operations`` (Execute)."""
        call = self.call_tool(
            "dax_query_operations",
            {"request": {"operation": "Execute", "query": dax}},
        )
        if call.is_error:
            error_text = self._extract_dax_error_text(call)
            raise PowerBiModelingMcpError(
                self._format_error_message(
                    "dax_query_operations.Execute",
                    call,
                    context=None,
                    hint=self._dax_execute_hint(error_text),
                    tool_name_for_schema="dax_query_operations",
                )
            )
        table = None
        if isinstance(call.structured, (dict, list)):
            table = _table_from_object(call.structured)
        if table is None:
            table = _parse_table_from_text(call.text)
        columns, rows = table if table is not None else ([], [])
        return DaxExecutionResult(
            dax=dax,
            columns=columns,
            rows=rows,
            raw=call.structured if call.structured is not None else call.text,
            calls=[call],
        )

    def validate_dax(self, dax: str) -> DaxValidationResult:
        """Validate a DAX query via ``dax_query_operations`` (Validate)."""
        call = self.call_tool(
            "dax_query_operations",
            {"request": {"operation": "Validate", "query": dax}},
        )
        is_valid = not call.is_error
        return DaxValidationResult(
            dax=dax,
            is_valid=is_valid,
            message=call.text,
            raw=call.structured if call.structured is not None else call.text,
            calls=[call],
        )


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def _is_headless_host() -> bool:
    """Auto-detect whether the current host has a usable interactive browser.

    Mirrors the Agent Studio's local-vs-headless split (see
    :class:`fabric_api.agents.config.PowerBiModelingMcpConfig` and
    :meth:`fabric_api.agents.modeling_chat.ModelingChatService._resolve_live_token`):

    * On a developer workstation Node's ``@microsoft/powerbi-modeling-mcp``
      can open the correct-user's browser sign-in — the ONE identity XMLA
      will accept for that workspace. Injecting a CLI-minted or MI-minted
      token here typically fails with *"Authentication failed for all
      authenticators"* because the token identity isn't the model owner.
    * In an Azure container (App Service / Container Apps / AKS) there is
      no browser, so we must inject a bearer token from the platform's
      managed identity via ``PBI_MODELING_MCP_ACCESS_TOKEN``.

    The env var ``POWERBI_MODELING_MCP_HEADLESS`` (``true``/``false``/``1``/
    ``0``/``yes``/``no``) forces a specific mode. When unset, we treat the
    presence of the Azure CLI on ``PATH`` as the signal that we are on a
    workstation and can rely on the server's interactive sign-in.
    """
    raw = os.environ.get("POWERBI_MODELING_MCP_HEADLESS", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # No explicit override: infer from environment. If ``az`` is available
    # (workstation), prefer interactive sign-in (headless=False). Otherwise
    # (Azure container image without az), fall back to token injection.
    return shutil.which("az") is None and shutil.which("az.cmd") is None


def build_client(
    *,
    use_fallback_token: bool = False,
    headless: bool | None = None,
) -> PowerBiModelingMcp:
    """Build a :class:`PowerBiModelingMcp` sized to its host environment.

    The Power BI Modeling MCP server accepts two authentication paths and
    the *right* one depends on whether the host has a browser:

    * **Workstation (headless=False, the default when ``az`` is installed):**
      no token is injected. The server runs with ``--authmode=interactive``
      and opens the correct-user's browser sign-in. This is the *only* path
      that reliably works locally — a CLI-minted bearer token is often
      rejected with *"Authentication failed for all authenticators"* because
      its identity is not the model owner. This mirrors the Agent Studio's
      local-mode behaviour (see
      :meth:`fabric_api.agents.modeling_chat.ModelingChatService._resolve_live_token`).
    * **Azure container (headless=True, the default when ``az`` is absent):**
      a Power BI / XMLA bearer token is acquired via the platform's
      :class:`~app.auth.TokenProvider` (managed identity in Azure, Azure CLI
      as a fallback) and injected through ``PBI_MODELING_MCP_ACCESS_TOKEN``.
      This is required because a container has no browser.

    ``headless`` overrides the auto-detection. ``use_fallback_token`` only
    applies when a token is actually acquired.
    """
    if headless is None:
        headless = _is_headless_host()
    if not headless:
        # Let the server sign the correct user in via its own browser flow.
        return PowerBiModelingMcp(access_token=None)
    provider = auth_module.get_token_provider()
    token = provider.powerbi_xmla_token(use_fallback=use_fallback_token)
    return PowerBiModelingMcp(access_token=token)
