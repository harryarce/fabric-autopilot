"""Contract tests for the ``POST /api/v1/semantic-models/ask`` endpoint."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from fastapi.testclient import TestClient

from app.intelligence.spec import (
    SemanticColumn,
    SemanticModelSpec,
    SemanticTable,
)
from fabric_api.dependencies import get_container
from fabric_api.main import create_app


def _spec() -> SemanticModelSpec:
    return SemanticModelSpec(
        name="Sales",
        tables=[
            SemanticTable(
                name="Sales",
                source_schema="dbo",
                source_table="FactSales",
                columns=[
                    SemanticColumn(
                        name="SalesAmount",
                        source_column="SalesAmount",
                        data_type="decimal",
                        summarize_by="sum",
                    ),
                ],
            )
        ],
    )


class _StubModelService:
    """Captures ``import_model`` arguments and returns a tiny spec."""

    last_import: dict = {}
    import_count: int = 0
    cached: bool = True  # simulate "definition already imported" by default

    def import_model(self, workspace_id, model_id, *, fmt, workspace_name, item_name):
        _StubModelService.last_import = {
            "workspace_id": workspace_id,
            "model_id": model_id,
            "fmt": fmt,
            "workspace_name": workspace_name,
            "item_name": item_name,
        }
        _StubModelService.import_count += 1
        return {"spec": _spec(), "files": {}, "format": fmt}

    def get_cached_spec(
        self, workspace_id, model_id, *, workspace_name="", item_name=""
    ):
        return _spec() if _StubModelService.cached else None


class _StubContainer:
    def model_service(self, tenant):
        return _StubModelService()


class _StubExecution:
    def __init__(self, dax: str) -> None:
        self.dax = dax
        self.columns = ["Sales[SalesAmount]"]
        self.rows = [[12345.0]]
        self.raw = None
        self.calls = []


class _StubMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def connect_to_fabric_model(self, *, workspace, semantic_model):
        self.calls.append(
            ("connection_operations", {"workspace": workspace, "model": semantic_model})
        )

    def execute_dax(self, dax):
        self.calls.append(("dax_query_operations", {"query": dax}))
        return _StubExecution(dax)


@contextmanager
def _stub_build_client():
    yield _StubMcpClient()


class AskModelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.app.dependency_overrides[get_container] = lambda: _StubContainer()
        self.client = TestClient(self.app)
        _StubModelService.cached = True
        _StubModelService.import_count = 0
        _StubModelService.last_import = {}

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def test_ask_endpoint_returns_dax_and_executes_via_mcp(self) -> None:
        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.build_client",
            side_effect=_stub_build_client,
        ):
            resp = self.client.post(
                "/api/v1/semantic-models/ask",
                json={
                    "workspace_id": "ws-1",
                    "workspace_name": "Demo Workspace",
                    "model_id": "m-1",
                    "model_name": "Sales",
                    "question": "how many sales",
                    "top_n": 10,
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["executed"])
        self.assertIn("dax", body["translation"])
        self.assertIn("explanation", body["translation"])
        self.assertIn("intent", body["translation"])
        self.assertTrue(body["translation"]["dax"].lstrip().upper().startswith("EVALUATE"))
        self.assertEqual(body["columns"], ["Sales[SalesAmount]"])
        self.assertEqual(body["rows"], [[12345.0]])
        # A cached spec was available, so we did not re-import from Fabric.
        self.assertFalse(body["imported_now"])
        self.assertEqual(_StubModelService.import_count, 0)

    def test_ask_endpoint_reports_mcp_error_without_raising(self) -> None:
        from app.intelligence.pbi_modeling_mcp import PowerBiModelingMcpError

        def _raise(*_a, **_kw):
            raise PowerBiModelingMcpError("npx not available")

        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.build_client", side_effect=_raise
        ):
            resp = self.client.post(
                "/api/v1/semantic-models/ask",
                json={
                    "workspace_id": "ws-1",
                    "workspace_name": "Demo Workspace",
                    "model_id": "m-1",
                    "model_name": "Sales",
                    "question": "how many sales",
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["executed"])
        self.assertIn("npx", body["error"])
        # DAX is still returned so the UI can display the translation.
        self.assertTrue(body["translation"]["dax"].lstrip().upper().startswith("EVALUATE"))

    def test_ask_endpoint_returns_422_for_unrelated_question(self) -> None:
        """Questions that don't reference the model return a helpful 422."""
        resp = self.client.post(
            "/api/v1/semantic-models/ask",
            json={
                "workspace_id": "ws-1",
                "workspace_name": "Demo Workspace",
                "model_id": "m-1",
                "model_name": "Sales",
                # No tokens in this question map to any table/column/measure.
                "question": "what is the weather today",
            },
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        body = resp.json()
        self.assertEqual(body.get("code"), "validation_error")
        # The detail lists at least one table name so the caller can rephrase.
        self.assertIn("Sales", body.get("detail", ""))

    def test_ask_endpoint_requires_import_when_not_cached(self) -> None:
        """If the model has never been imported, the endpoint asks the user
        to import it first rather than silently reaching out to Fabric."""
        _StubModelService.cached = False
        resp = self.client.post(
            "/api/v1/semantic-models/ask",
            json={
                "workspace_id": "ws-1",
                "workspace_name": "Demo Workspace",
                "model_id": "m-1",
                "model_name": "Sales",
                "question": "how many sales",
            },
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        body = resp.json()
        self.assertEqual(body.get("code"), "validation_error")
        detail = body.get("detail", "")
        self.assertIn("has not been imported", detail)
        # The hint points the caller at the import endpoint.
        self.assertIn("/import", detail)
        self.assertIn("auto_import", detail)
        # We did NOT reach out to Fabric.
        self.assertEqual(_StubModelService.import_count, 0)

    def test_ask_endpoint_auto_import_fetches_definition_on_the_fly(self) -> None:
        """With ``auto_import`` the endpoint imports the model transparently."""
        _StubModelService.cached = False
        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.build_client",
            side_effect=_stub_build_client,
        ):
            resp = self.client.post(
                "/api/v1/semantic-models/ask",
                json={
                    "workspace_id": "ws-1",
                    "workspace_name": "Demo Workspace",
                    "model_id": "m-1",
                    "model_name": "Sales",
                    "question": "how many sales",
                    "auto_import": True,
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["executed"])
        self.assertTrue(body["imported_now"])
        self.assertEqual(_StubModelService.import_count, 1)
        self.assertEqual(_StubModelService.last_import["workspace_id"], "ws-1")

    def test_ask_endpoint_refresh_forces_reimport_even_when_cached(self) -> None:
        """``refresh=True`` re-imports the model even if a cached copy exists."""
        _StubModelService.cached = True
        with mock.patch(
            "app.intelligence.pbi_modeling_mcp.build_client",
            side_effect=_stub_build_client,
        ):
            resp = self.client.post(
                "/api/v1/semantic-models/ask",
                json={
                    "workspace_id": "ws-1",
                    "workspace_name": "Demo Workspace",
                    "model_id": "m-1",
                    "model_name": "Sales",
                    "question": "how many sales",
                    "refresh": True,
                },
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["imported_now"])
        self.assertEqual(_StubModelService.import_count, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
