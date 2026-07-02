"""Lifecycle tests for :class:`PowerBiModelingMcp`.

These tests use stub replacements for the ``mcp`` SDK so we can exercise the
real open → call → close flow of :class:`PowerBiModelingMcp` without spawning
a Node.js subprocess. The primary regression they guard against is the anyio
error *"Attempted to exit cancel scope in a different task than it was entered
in"* — which the old implementation triggered by opening the ``stdio_client``
async context on the worker task and calling ``stack.aclose()`` from a
different task during shutdown.
"""

from __future__ import annotations

import sys
import types
import unittest
from contextlib import asynccontextmanager
from unittest import mock

# ---------------------------------------------------------------------------
# Minimal ``mcp`` stub package. Registered globally at import time so that
# ``from mcp import ClientSession, StdioServerParameters`` (used lazily inside
# :meth:`PowerBiModelingMcp._lifetime`) resolves to our fakes.
# ---------------------------------------------------------------------------


class _StubTool:
    def __init__(self, name: str, input_schema: dict | None = None) -> None:
        self.name = name
        self.inputSchema = input_schema or {
            "type": "object",
            "properties": {"request": {"type": "object"}},
            "required": ["request"],
        }


class _StubToolsResp:
    def __init__(self, tools: list[_StubTool]) -> None:
        self.tools = tools


class _StubCallResult:
    def __init__(
        self,
        text: str,
        *,
        is_error: bool = False,
        structured: object | None = None,
    ) -> None:
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.structuredContent = structured
        self.isError = is_error


class _StubClientSession:
    # Class-level so tests can inspect calls even after the session's
    # context manager has closed (the wrapper doesn't hand the session
    # instance back to the caller).
    last_calls: list[tuple[str, dict]] = []
    # Test-controlled overrides: a mapping ``tool_name -> _StubCallResult``
    # that, when set, replaces the default happy-path response. Use to
    # drive error-path diagnostics tests without touching the real MCP SDK.
    response_overrides: dict[str, "_StubCallResult"] = {}

    def __init__(self, read, write) -> None:  # noqa: ARG002 - fake args
        self._read = read
        self._write = write

    async def __aenter__(self) -> "_StubClientSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _StubToolsResp:
        return _StubToolsResp(
            [
                _StubTool("connection_operations"),
                _StubTool("dax_query_operations"),
            ]
        )

    async def call_tool(self, name: str, arguments: dict) -> _StubCallResult:
        _StubClientSession.last_calls.append((name, arguments))
        override = _StubClientSession.response_overrides.get(name)
        if override is not None:
            return override
        if name == "connection_operations":
            return _StubCallResult(text="connected")
        if name == "dax_query_operations":
            return _StubCallResult(text="EVALUATE\nColumn1\n1")
        return _StubCallResult(text=f"unknown tool {name}", is_error=True)


class _StubStdioParams:
    # Class-level so tests can inspect the env that _lifetime built for the
    # (fake) subprocess even after the stdio_client context exits.
    last_params: "_StubStdioParams | None" = None

    def __init__(self, command: str, args: list[str], env: dict) -> None:
        self.command = command
        self.args = args
        self.env = env
        _StubStdioParams.last_params = self


@asynccontextmanager
async def _stub_stdio_client(params, errlog=None):  # noqa: ARG001 - fake args
    yield (object(), object())


def _install_mcp_stub() -> None:
    mcp_pkg = types.ModuleType("mcp")
    mcp_pkg.ClientSession = _StubClientSession
    mcp_pkg.StdioServerParameters = _StubStdioParams
    client_pkg = types.ModuleType("mcp.client")
    stdio_pkg = types.ModuleType("mcp.client.stdio")
    stdio_pkg.stdio_client = _stub_stdio_client
    client_pkg.stdio = stdio_pkg
    mcp_pkg.client = client_pkg
    sys.modules["mcp"] = mcp_pkg
    sys.modules["mcp.client"] = client_pkg
    sys.modules["mcp.client.stdio"] = stdio_pkg


_install_mcp_stub()

# Import after the stub is in place so lazy ``from mcp import ...`` inside
# ``_lifetime`` resolves to our stubs.
from app.intelligence.pbi_modeling_mcp import (  # noqa: E402
    PowerBiModelingMcp,
    PowerBiModelingMcpError,
    _resolve_launch_command,
    build_client,
)


class PowerBiModelingMcpLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        # Skip when the real mcp SDK actually shipped an incompatible signature
        # — the stubs above only cover the methods we exercise.
        try:
            _resolve_launch_command()
        except Exception as exc:  # pragma: no cover - env dependent
            self.skipTest(f"launch command not resolvable: {exc}")

    def test_open_call_close_does_not_raise(self) -> None:
        """The full open → call → close cycle completes cleanly.

        This exercises the same code path that produced *"Attempted to exit
        cancel scope in a different task than it was entered in"* in the old
        implementation, and would fail if the AsyncExitStack were closed on
        a task other than the one that opened it.
        """
        client = PowerBiModelingMcp()
        try:
            client._ensure_started()
            self.assertIn("connection_operations", client.available_tools())
            call = client.call_tool("dax_query_operations", {"query": "EVALUATE 1"})
            self.assertFalse(call.is_error)
        finally:
            # If the lifetime task had a cross-task cancel scope error, this
            # would surface as a WARNING/traceback and the timeout would fire.
            client.close()

    def test_context_manager_open_and_close(self) -> None:
        with PowerBiModelingMcp() as client:
            client.connect_to_fabric_model(
                workspace="ws", semantic_model="model"
            )
        # Reaching this line means __exit__ shut the lifetime task down
        # cleanly on its own task.

    def test_close_is_idempotent(self) -> None:
        client = PowerBiModelingMcp()
        client._ensure_started()
        client.close()
        client.close()  # second call is a no-op and must not raise.

    def test_connect_and_execute_use_request_wrapper(self) -> None:
        """Every ``connection_operations`` / ``dax_query_operations`` call
        must send its arguments inside a ``request`` object with an
        ``operation`` field. Sending a flat ``{action, ...}`` payload (as
        the initial implementation did) makes the server reject every call
        with the opaque ``"An error occurred invoking 'X'."`` wrapper.
        """
        _StubClientSession.last_calls = []
        with PowerBiModelingMcp() as client:
            client.connect_to_fabric_model(
                workspace="ws-A", semantic_model="model-1"
            )
            client.execute_dax("EVALUATE ROW(\"x\", 1)")
            client.validate_dax("EVALUATE ROW(\"x\", 1)")

        by_tool: dict[str, list[dict]] = {}
        for name, args in _StubClientSession.last_calls:
            by_tool.setdefault(name, []).append(args)

        self.assertEqual(len(by_tool.get("connection_operations", [])), 1)
        connect_args = by_tool["connection_operations"][0]
        self.assertEqual(
            connect_args,
            {
                "request": {
                    "operation": "ConnectFabric",
                    "workspaceName": "ws-A",
                    "semanticModelName": "model-1",
                }
            },
        )

        dax_calls = by_tool.get("dax_query_operations", [])
        self.assertEqual(len(dax_calls), 2)
        self.assertEqual(
            dax_calls[0],
            {"request": {"operation": "Execute", "query": "EVALUATE ROW(\"x\", 1)"}},
        )
        self.assertEqual(
            dax_calls[1],
            {"request": {"operation": "Validate", "query": "EVALUATE ROW(\"x\", 1)"}},
        )

    def test_connect_error_surfaces_structured_content_and_schema(self) -> None:
        """When ``connection_operations`` returns ``isError=True``, the
        raised :class:`PowerBiModelingMcpError` must include the server's
        structured payload *and* the tool's ``inputSchema`` so the caller
        can immediately see the actual Fabric-side reason (e.g. workspace
        not found) instead of only the opaque MCP wrapper string.
        """
        _StubClientSession.last_calls = []
        _StubClientSession.response_overrides = {
            "connection_operations": _StubCallResult(
                text="An error occurred invoking 'connection_operations'.",
                is_error=True,
                structured={
                    "message": "Workspace 'Missing' not found",
                    "operation": "ConnectFabric",
                },
            )
        }
        try:
            with PowerBiModelingMcp() as client:
                with self.assertRaises(PowerBiModelingMcpError) as ctx:
                    client.connect_to_fabric_model(
                        workspace="Missing", semantic_model="model-1"
                    )
        finally:
            _StubClientSession.response_overrides = {}

        msg = str(ctx.exception)
        # Server-provided structured content must be spliced in.
        self.assertIn("Workspace 'Missing' not found", msg)
        # The tool inputSchema must be spliced in so a caller can eyeball
        # the expected argument shape.
        self.assertIn("tool_inputSchema=", msg)
        # And the actionable hint must be present.
        self.assertIn("XMLA", msg)

    def test_subprocess_env_strips_ambient_azure_credentials(self) -> None:
        """The MCP subprocess must not inherit ambient ``AZURE_*`` /
        ``IDENTITY_*`` env vars — they hijack the .NET server's identity
        chain and produce *"Authentication failed for all authenticators"*
        even when a valid bearer token is injected. Locks in the parity
        with :mod:`fabric_api.agents.mcp_clients`.
        """
        import os as _os

        _StubStdioParams.last_params = None
        overrides = {
            "AZURE_CLIENT_ID": "hijacker-sp",
            "AZURE_TENANT_ID": "wrong-tenant",
            "AZURE_CLIENT_SECRET": "leaked",
            "IDENTITY_ENDPOINT": "http://169.254.169.254/",
            "IMDS_ENDPOINT": "http://169.254.169.254/metadata",
            "MSI_ENDPOINT": "http://127.0.0.1:41288/msi",
            # Unrelated vars must still be forwarded.
            "PATH": _os.environ.get("PATH", ""),
            "SOME_UNRELATED_VAR": "keep-me",
        }
        previous = {k: _os.environ.get(k) for k in overrides}
        try:
            for k, v in overrides.items():
                _os.environ[k] = v

            with PowerBiModelingMcp(access_token="fake-xmla-token") as _:
                pass

            params = _StubStdioParams.last_params
            self.assertIsNotNone(params, "stdio_client was never invoked")
            env = params.env
            # Ambient service-principal / user vars must be stripped.
            for var in (
                "AZURE_CLIENT_ID",
                "AZURE_TENANT_ID",
                "AZURE_CLIENT_SECRET",
            ):
                self.assertNotIn(
                    var, env, f"{var} leaked into MCP subprocess env"
                )
            # With an explicit token, MI / Azure Arc discovery vars must
            # also be stripped so the server can't fall back to a flaky
            # managed identity.
            for var in ("IDENTITY_ENDPOINT", "IMDS_ENDPOINT", "MSI_ENDPOINT"):
                self.assertNotIn(
                    var, env, f"{var} leaked into MCP subprocess env"
                )
            # The bearer token must be injected under the documented name.
            self.assertEqual(
                env.get("PBI_MODELING_MCP_ACCESS_TOKEN"), "fake-xmla-token"
            )
            # Unrelated vars pass through.
            self.assertEqual(env.get("SOME_UNRELATED_VAR"), "keep-me")
        finally:
            for k, v in previous.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v


class BuildClientHeadlessRoutingTests(unittest.TestCase):
    """``build_client`` picks the *right* auth path per host.

    Mirrors the Agent Studio's local-vs-headless strategy (documented in
    :meth:`fabric_api.agents.modeling_chat.ModelingChatService._resolve_live_token`):

    * Local workstation → no token, server does interactive browser sign-in.
    * Headless Azure container → inject XMLA token from managed identity.

    Locking the routing down means a well-intentioned refactor cannot silently
    reintroduce the *"Authentication failed for all authenticators"* bug that
    happens when a CLI-minted token is injected on a workstation.
    """

    def setUp(self) -> None:
        import os as _os

        self._env_backup = {
            k: _os.environ.get(k)
            for k in ("POWERBI_MODELING_MCP_HEADLESS",)
        }
        _os.environ.pop("POWERBI_MODELING_MCP_HEADLESS", None)

    def tearDown(self) -> None:
        import os as _os

        for k, v in self._env_backup.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    def test_headless_false_omits_token_injection(self) -> None:
        """``headless=False`` returns a client with ``access_token=None`` and
        never touches the platform's :class:`~app.auth.TokenProvider`."""
        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.auth_module.get_token_provider"
        ) as get_provider:
            client = build_client(headless=False)
            try:
                self.assertIsNone(client._access_token)
                get_provider.assert_not_called()
            finally:
                client.close()

    def test_headless_true_injects_xmla_token(self) -> None:
        """``headless=True`` mints an XMLA token via the provider and hands
        it to the client (for injection into the subprocess env)."""
        fake_provider = mock.Mock()
        fake_provider.powerbi_xmla_token.return_value = "fake-xmla-token"
        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.auth_module.get_token_provider",
            return_value=fake_provider,
        ):
            client = build_client(headless=True)
            try:
                self.assertEqual(client._access_token, "fake-xmla-token")
                fake_provider.powerbi_xmla_token.assert_called_once_with(
                    use_fallback=False
                )
            finally:
                client.close()

    def test_env_override_forces_headless(self) -> None:
        """``POWERBI_MODELING_MCP_HEADLESS=true`` forces the token path even
        when ``az`` is on PATH."""
        import os as _os

        fake_provider = mock.Mock()
        fake_provider.powerbi_xmla_token.return_value = "env-forced-token"
        _os.environ["POWERBI_MODELING_MCP_HEADLESS"] = "true"
        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.auth_module.get_token_provider",
            return_value=fake_provider,
        ):
            client = build_client()
            try:
                self.assertEqual(client._access_token, "env-forced-token")
            finally:
                client.close()

    def test_env_override_forces_interactive(self) -> None:
        """``POWERBI_MODELING_MCP_HEADLESS=false`` forces the interactive
        path even when ``az`` is absent."""
        import os as _os

        _os.environ["POWERBI_MODELING_MCP_HEADLESS"] = "false"
        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.auth_module.get_token_provider"
        ) as get_provider:
            client = build_client()
            try:
                self.assertIsNone(client._access_token)
                get_provider.assert_not_called()
            finally:
                client.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
