"""Tests for the frontend API client (``fabric_app.api_client``).

Use an httpx ``MockTransport`` so no server is needed; verify request routing,
header propagation, response parsing, and problem+json -> ApiError mapping.
"""

from __future__ import annotations

import unittest

import httpx

from fabric_app.api_client import ApiError, FabricApiClient


def _make_client(handler) -> FabricApiClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(
        base_url="http://test",
        headers={"Accept": "application/json", "X-Tenant-Id": "acme"},
        transport=transport,
    )
    client = FabricApiClient(base_url="http://test", tenant_id="acme", client=inner)
    return client


class ApiClientTests(unittest.TestCase):
    def test_list_workspaces(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/workspaces")
            self.assertEqual(request.headers.get("X-Tenant-Id"), "acme")
            return httpx.Response(200, json=[{"id": "ws-1", "name": "Demo"}])

        with _make_client(handler) as client:
            result = client.list_workspaces()
        self.assertEqual(result[0]["id"], "ws-1")

    def test_design_semantic_model_posts_body(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.read().decode()
            return httpx.Response(200, json={"spec": {"name": "M"}, "usedAgent": True})

        with _make_client(handler) as client:
            result = client.design_semantic_model(
                server="srv", database="db", model_name="M"
            )
        self.assertEqual(captured["path"], "/api/v1/semantic-models/design")
        self.assertIn("model_name", captured["body"])
        self.assertTrue(result["usedAgent"])

    def test_problem_json_raises_api_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={
                    "title": "Not Found",
                    "status": 404,
                    "code": "not_found",
                    "detail": "Missing",
                },
            )

        with _make_client(handler) as client:
            with self.assertRaises(ApiError) as ctx:
                client.get_operation("missing")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.detail, "Missing")

    def test_dependency_unavailable_flag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503,
                json={"title": "x", "status": 503, "code": "dependency_unavailable"},
            )

        with _make_client(handler) as client:
            with self.assertRaises(ApiError) as ctx:
                client.publish_remote_agent("desc")
        self.assertTrue(ctx.exception.is_dependency_unavailable)

    def test_network_failure_maps_to_api_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with _make_client(handler) as client:
            with self.assertRaises(ApiError) as ctx:
                client.healthz()
        self.assertEqual(ctx.exception.code, "api_unreachable")

    def test_orchestrate_stream_yields_sse_events(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["query"] = request.url.params.get("stream")
            captured["accept"] = request.headers.get("Accept")
            body = (
                'data: {"kind": "plan", "agent": "manager"}\n\n'
                'data: {"kind": "artifact", "data": {"model": {"name": "M"}, '
                '"usedAgent": true}}\n\n'
                'data: {"kind": "final", "text": "done"}\n\n'
            )
            return httpx.Response(
                200,
                content=body.encode(),
                headers={"content-type": "text/event-stream"},
            )

        with _make_client(handler) as client:
            events = list(client.orchestrate_stream(objective="x", server="s", database="d"))

        self.assertEqual(captured["path"], "/api/v1/agents/orchestrate")
        self.assertEqual(captured["query"], "true")
        self.assertEqual(captured["accept"], "text/event-stream")
        self.assertEqual([e["kind"] for e in events], ["plan", "artifact", "final"])
        self.assertEqual(events[1]["data"]["model"]["name"], "M")

    def test_orchestrate_stream_network_failure_maps_to_api_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        with _make_client(handler) as client:
            with self.assertRaises(ApiError) as ctx:
                list(client.orchestrate_stream(objective="x"))
        self.assertEqual(ctx.exception.code, "api_unreachable")

    def test_suggest_semantic_model_posts_body(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.read().decode()
            return httpx.Response(
                200,
                json={
                    "suggestions": {"relationships": [], "measures": []},
                    "status": "deterministic",
                    "agentContributed": 0,
                    "error": None,
                },
            )

        with _make_client(handler) as client:
            result = client.suggest_semantic_model(
                server="srv",
                database="db",
                spec={"name": "M"},
                selected_tables=["dbo.Sales"],
            )
        self.assertEqual(captured["path"], "/api/v1/semantic-models/suggest")
        self.assertIn("selected_tables", captured["body"])
        self.assertEqual(result["status"], "deterministic")

    def test_apply_suggestions_posts_body(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.read().decode()
            return httpx.Response(200, json={"spec": {"name": "M"}})

        with _make_client(handler) as client:
            result = client.apply_semantic_model_suggestions(
                spec={"name": "M"},
                relationships=[{"relationship": {}}],
                measures=[],
            )
        self.assertEqual(captured["path"], "/api/v1/semantic-models/apply-suggestions")
        self.assertIn("relationships", captured["body"])
        self.assertEqual(result["spec"]["name"], "M")

    def test_validate_semantic_model(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/semantic-models/validate")
            return httpx.Response(
                200, json={"ok": True, "errors": [], "warnings": [], "issues": []}
            )

        with _make_client(handler) as client:
            result = client.validate_semantic_model({"name": "M"})
        self.assertTrue(result["ok"])

    def test_create_publish_model_report_workflow_posts_body(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = request.read().decode()
            return httpx.Response(
                200, json={"id": "wf-1", "kind": "publish-model-report", "state": "pending"}
            )

        with _make_client(handler) as client:
            result = client.create_publish_model_report_workflow(
                workspace_id="ws-1",
                model_display_name="M",
                model_spec={"name": "M"},
                report_display_name="R",
                report_spec={"name": "R"},
            )
        self.assertEqual(
            captured["path"], "/api/v1/agents/workflows/publish-model-report"
        )
        self.assertIn("model_spec", captured["body"])
        self.assertIn("report_spec", captured["body"])
        self.assertEqual(result["kind"], "publish-model-report")


if __name__ == "__main__":
    unittest.main()
