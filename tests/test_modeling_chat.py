"""Tests for the natural-language model-editing chat surface.

These are hermetic: the live Foundry + Power BI Modeling MCP path is never
exercised. We force the deterministic advisory fallback either by constructing a
config with the MCP bridge disabled or by setting
``POWERBI_MODELING_MCP_ENABLED=0`` in the environment, so nothing reaches the
network.
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from fabric_api.agents.config import AgentTeamConfig, PowerBiModelingMcpConfig
from fabric_api.agents.mcp_clients import build_powerbi_modeling_tool
from fabric_api.agents.modeling_chat import ModelingChatService
from fabric_api.main import create_app


def _disabled_config() -> AgentTeamConfig:
    """A config whose PBI Modeling MCP bridge is off (forces fallback)."""
    return AgentTeamConfig(
        powerbi_modeling_mcp=PowerBiModelingMcpConfig(enabled=False)
    )


class ConfigDefaultTests(unittest.TestCase):
    def test_mcp_enabled_by_default(self) -> None:
        cfg = PowerBiModelingMcpConfig()
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.skip_confirmation)
        self.assertTrue(cfg.readonly)
        # Headless (cloud token injection) is OFF by default so local dev keeps
        # the interactive browser sign-in. The cloud opts in via bicep.
        self.assertFalse(cfg.headless)

    def test_from_env_reads_headless(self) -> None:
        import os

        with mock.patch.dict(
            "os.environ", {"POWERBI_MODELING_MCP_HEADLESS": "true"}, clear=False
        ):
            cfg = PowerBiModelingMcpConfig.from_env()
        self.assertTrue(cfg.headless)
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("POWERBI_MODELING_MCP_HEADLESS", None)
            cfg = PowerBiModelingMcpConfig.from_env()
        self.assertFalse(cfg.headless)

    def test_from_env_defaults_enabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            for key in (
                "POWERBI_MODELING_MCP_ENABLED",
                "POWERBI_MODELING_MCP_SKIP_CONFIRMATION",
            ):
                import os

                os.environ.pop(key, None)
            cfg = PowerBiModelingMcpConfig.from_env()
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.skip_confirmation)

    def test_build_tool_none_when_disabled(self) -> None:
        cfg = PowerBiModelingMcpConfig(enabled=False)
        self.assertIsNone(
            build_powerbi_modeling_tool(cfg, readonly=False, skip_confirmation=True)
        )

    def test_build_tool_sanitizes_env_and_sets_authmode(self) -> None:
        """The stdio subprocess must not inherit ambient AZURE_* credentials
        (they hijack the server's own auth), and the token + authmode flow."""
        cfg = PowerBiModelingMcpConfig(enabled=True)
        with mock.patch.dict(
            "os.environ",
            {
                "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
                "AZURE_TENANT_ID": "contoso",
                "AZURE_CLIENT_SECRET": "secret",
                "IDENTITY_ENDPOINT": "http://localhost:40342/metadata/identity",
                "IMDS_ENDPOINT": "http://localhost:40342",
            },
            clear=False,
        ):
            tool = build_powerbi_modeling_tool(
                cfg,
                readonly=True,
                skip_confirmation=True,
                access_token="fake-token",
            )
        if tool is None:  # agent_framework unavailable in this environment
            self.skipTest("agent_framework MCP tool not available")
        env = tool.env or {}
        for leaked in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET"):
            self.assertNotIn(leaked, env)
        # With a token injected the server must use it alone: managed-identity /
        # Azure Arc discovery vars are stripped so a flaky Arc MI cannot hijack.
        for leaked in ("IDENTITY_ENDPOINT", "IMDS_ENDPOINT"):
            self.assertNotIn(leaked, env)
        self.assertEqual(env.get("PBI_MODELING_MCP_ACCESS_TOKEN"), "fake-token")
        self.assertIn("--authmode=interactive", tool.args)


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCred:
    """Async credential test double: yields a token or raises a chosen error."""

    def __init__(self, token: str | None = None, error: Exception | None = None):
        self._token = token
        self._error = error

    async def get_token(self, *_scopes: str) -> _FakeToken:
        if self._error is not None:
            raise self._error
        assert self._token is not None
        return _FakeToken(self._token)

    async def close(self) -> None:  # pragma: no cover - trivial
        return None


def _jwt(claims: dict[str, object]) -> str:
    import base64
    import json

    def _seg(obj: object) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_seg({'alg': 'none'})}.{_seg(claims)}.sig"


class TokenAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    def test_token_identity_decodes_claims(self) -> None:
        token = _jwt(
            {
                "aud": "https://analysis.windows.net/powerbi/api",
                "appid": "04b07795-aaaa",
                "upn": "admin@contoso.com",
                "tid": "tenant-1",
                "secret": "should-not-appear",
            }
        )
        ident = ModelingChatService._token_identity(token)
        self.assertEqual(ident["upn"], "admin@contoso.com")
        self.assertEqual(ident["tid"], "tenant-1")
        self.assertNotIn("secret", ident)

    def test_token_identity_opaque_token_is_safe(self) -> None:
        self.assertEqual(ModelingChatService._token_identity("not-a-jwt"), {})

    async def test_cli_token_preferred(self) -> None:
        cli_token = _jwt({"upn": "admin@contoso.com"})
        with mock.patch(
            "azure.identity.aio.AzureCliCredential",
            return_value=_FakeCred(token=cli_token),
        ):
            got = await ModelingChatService._acquire_powerbi_token(
                _FakeCred(token=_jwt({"appid": "managed-identity"}))
            )
        self.assertEqual(got, cli_token)

    async def test_no_mi_fallback_when_cli_present_but_scope_fails(self) -> None:
        """If az is signed in but cannot mint a Power BI token, return None —
        never inject the (wrong-identity) managed-identity token."""
        with mock.patch(
            "azure.identity.aio.AzureCliCredential",
            return_value=_FakeCred(error=RuntimeError("scope not consented")),
        ):
            got = await ModelingChatService._acquire_powerbi_token(
                _FakeCred(token=_jwt({"appid": "arc-managed-identity"}))
            )
        self.assertIsNone(got)

    async def test_mi_fallback_when_cli_unavailable(self) -> None:
        """When az is absent (e.g. in Azure) fall back to the app identity."""
        from azure.identity import CredentialUnavailableError

        mi_token = _jwt({"appid": "app-managed-identity"})
        with mock.patch(
            "azure.identity.aio.AzureCliCredential",
            return_value=_FakeCred(error=CredentialUnavailableError("no az")),
        ):
            got = await ModelingChatService._acquire_powerbi_token(
                _FakeCred(token=mi_token)
            )
        self.assertEqual(got, mi_token)


class ResolveLiveTokenTests(unittest.IsolatedAsyncioTestCase):
    """The headless decision: inject a token in the cloud, stay interactive
    locally, and fail loudly (never a doomed browser) when headless has no
    token."""

    @staticmethod
    def _service(*, headless: bool) -> ModelingChatService:
        return ModelingChatService(
            config=AgentTeamConfig(
                powerbi_modeling_mcp=PowerBiModelingMcpConfig(
                    enabled=True, headless=headless
                )
            )
        )

    async def test_interactive_returns_none_without_acquiring(self) -> None:
        service = self._service(headless=False)
        with mock.patch.object(
            ModelingChatService,
            "_acquire_powerbi_token",
            new=mock.AsyncMock(side_effect=AssertionError("must not acquire")),
        ):
            token = await service._resolve_live_token(_FakeCred(token="x"))
        self.assertIsNone(token)

    async def test_headless_injects_acquired_token(self) -> None:
        service = self._service(headless=True)
        with mock.patch.object(
            ModelingChatService,
            "_acquire_powerbi_token",
            new=mock.AsyncMock(return_value="bearer-123"),
        ):
            token = await service._resolve_live_token(_FakeCred(token="x"))
        self.assertEqual(token, "bearer-123")

    async def test_headless_without_token_raises_actionable_error(self) -> None:
        service = self._service(headless=True)
        with mock.patch.object(
            ModelingChatService,
            "_acquire_powerbi_token",
            new=mock.AsyncMock(return_value=None),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await service._resolve_live_token(_FakeCred(token="x"))
        msg = str(ctx.exception)
        self.assertIn("managed identity", msg)
        self.assertIn("Fabric workspace", msg)


class ModelingChatFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ModelingChatService(config=_disabled_config())

    def test_missing_mcp_returns_advisory(self) -> None:
        result = self.service.chat(
            workspace_name="Demo Workspace",
            model_name="Sales Model",
            message="Add a YoY growth measure to the Sales table",
            workspace_id="ws-1",
            model_id="m-1",
        )
        self.assertFalse(result["available"])
        self.assertFalse(result["usedAgent"])
        self.assertIn("powerbi-modeling-mcp-enabled", result["requires"])
        self.assertIn("Sales Model", result["reply"])
        self.assertIn("YoY growth", result["reply"])
        self.assertEqual(result["connection"]["workspace"], "Demo Workspace")
        self.assertEqual(result["connection"]["model"], "Sales Model")
        self.assertEqual(result["connection"]["modelId"], "m-1")

    def test_missing_message(self) -> None:
        result = self.service.chat(
            workspace_name="Demo Workspace",
            model_name="Sales Model",
            message="   ",
        )
        self.assertIn("message", result["requires"])
        self.assertFalse(result["available"])

    def test_missing_workspace_and_model(self) -> None:
        result = self.service.chat(
            workspace_name="",
            model_name="",
            message="Rename the Amount column to Revenue",
        )
        self.assertIn("workspace-and-model", result["requires"])
        self.assertFalse(result["available"])

    def test_chat_never_raises(self) -> None:
        # Even with odd inputs the surface returns a dict, never an exception.
        result = self.service.chat(
            workspace_name="W", model_name="M", message="x"
        )
        self.assertIsInstance(result, dict)
        self.assertIn("reply", result)


class FakeContainer:
    """Minimal container; the model-chat route does not touch it."""


class ModelChatApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_model_chat_route_shape(self) -> None:
        # Force the fast, hermetic fallback so no network call is attempted.
        with mock.patch.dict(
            "os.environ", {"POWERBI_MODELING_MCP_ENABLED": "0"}, clear=False
        ):
            resp = self.client.post(
                "/api/v1/agents/model-chat",
                json={
                    "workspace_name": "Demo Workspace",
                    "model_name": "Sales Model",
                    "message": "Add a measure Total Sales = SUM(Sales[Amount])",
                    "workspace_id": "ws-1",
                    "model_id": "m-1",
                    "history": [],
                },
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("reply", "usedAgent", "available", "requires", "connection"):
            self.assertIn(key, body)
        self.assertFalse(body["available"])
        self.assertIn("powerbi-modeling-mcp-enabled", body["requires"])

    def test_model_chat_validation_error(self) -> None:
        resp = self.client.post(
            "/api/v1/agents/model-chat",
            json={"workspace_name": "W"},  # missing model_name + message
        )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
