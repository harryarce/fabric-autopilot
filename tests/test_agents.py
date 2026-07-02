"""End-to-end tests for the agentic orchestration layer (``fabric_api.agents``).

These exercise the full multi-agent pipeline, the approval-gated publish
workflows, the MCP orchestration tools, and the FastAPI agent routes against
fake services — so they run **offline**, with no Fabric / SQL / Foundry
connectivity. The deterministic fallback path is what executes here, which is
exactly the behaviour every deployment relies on when Foundry is unavailable.
"""

from __future__ import annotations

import asyncio
import os
import unittest

from app.fabric_client import Workspace
from app.intelligence import (
    SemanticModelSpec,
    SuggestionSpec,
    spec_from_schemas,
    suggest_report,
)
from app.sql_client import ColumnSchema, TableSchema
from fabric_api.agents import (
    AgentOrchestrator,
    AgentTeamConfig,
    OrchestrationRequest,
    PublishWorkflowStore,
    ToolKit,
    capability_status,
    get_workflow_store,
    resolve_workflow,
)
from fabric_services.context import TenantContext


def setUpModule() -> None:
    # Never let the manager-agent narration reach Foundry during tests.
    os.environ["FABRIC_AGENT_NARRATE"] = "0"


# ---------------------------------------------------------------------------
# Fakes — deterministic, no network
# ---------------------------------------------------------------------------


def _fact() -> TableSchema:
    return TableSchema(
        schema="dbo",
        name="Sales",
        object_type="TABLE",
        columns=[
            ColumnSchema(
                name="Id", ordinal=1, data_type="int", max_length=None,
                precision=10, scale=0, is_nullable=False, default=None,
                is_primary_key=True,
            ),
            ColumnSchema(
                name="Amount", ordinal=2, data_type="decimal", max_length=None,
                precision=18, scale=2, is_nullable=False, default=None,
                is_primary_key=False,
            ),
        ],
    )


def _dim() -> TableSchema:
    return TableSchema(
        schema="dbo",
        name="Customer",
        object_type="TABLE",
        columns=[
            ColumnSchema(
                name="CustomerId", ordinal=1, data_type="int", max_length=None,
                precision=10, scale=0, is_nullable=False, default=None,
                is_primary_key=True,
            ),
        ],
    )


class _DesignResult:
    def __init__(self, spec: SemanticModelSpec, used_agent: bool) -> None:
        self.spec = spec
        self.used_agent = used_agent


class _SuggestResult:
    def __init__(self, spec, status, used_agent, agent_visual_count, error) -> None:
        self.spec = spec
        self.status = status
        self.used_agent = used_agent
        self.agent_visual_count = agent_visual_count
        self.error = error


class _ReportAuditResult:
    def __init__(self, report, status, agent_finding_count, error) -> None:
        self.report = report
        self.status = status
        self.agent_finding_count = agent_finding_count
        self.error = error


class FakeSchemaService:
    def extract_schemas(self, server, database):
        return [_fact(), _dim()]


class FakeModelService:
    def __init__(self) -> None:
        self.published: dict | None = None

    def design_model(self, schemas, **kwargs):
        spec = spec_from_schemas(
            schemas,
            model_name=kwargs.get("model_name", "Model"),
            source_server=kwargs.get("source_server"),
            source_database=kwargs.get("source_database"),
            storage_mode=kwargs.get("storage_mode", "import"),
            source_kind=kwargs.get("source_kind", "sql"),
        )
        return _DesignResult(spec, used_agent=False)

    def generate_dax_measure(self, spec, intent, *, sample_rows=None):
        class _Gen:
            def to_dict(self_inner):
                return {"table": "Sales", "measure": {"name": intent[:20]}}

        return _Gen()

    def publish_model(self, workspace_id, display_name, spec, *, fmt="TMDL", description=None):
        self.published = {
            "workspace_id": workspace_id,
            "display_name": display_name,
            "fmt": fmt,
        }
        return {
            "id": "model-123",
            "display_name": display_name,
            "workspace_id": workspace_id,
            "type": "SemanticModel",
            "status": "Created",
            "web_url": f"https://app.fabric.microsoft.com/groups/{workspace_id}/datasets/model-123",
        }


class FakeReportService:
    def __init__(self) -> None:
        self.published: dict | None = None

    def suggest_report(self, model_spec, *, report_name=None, dataset_id=None,
                       dataset_name=None, use_agent=True, extra_instructions=None):
        spec = suggest_report(model_spec, report_name=report_name)
        return _SuggestResult(spec, "deterministic", False, 0, None)

    def generate_theme_remediation(self, spec):
        return [
            SuggestionSpec(
                kind="report_theme",
                object_ref="theme:Brand",
                field="theme.background",
                proposed_value="#FFFFFF",
                code="RPT_FMT_THEME_REMEDIATION",
            )
        ]

    def apply_audit_suggestions(self, spec, suggestions):
        applied = []
        for s in suggestions:
            data = s.to_dict()
            data["status"] = "applied"
            applied.append(SuggestionSpec.from_dict(data))
        return spec, applied

    def publish_report(self, workspace_id, display_name, spec, *, description=None,
                       dataset_id=None):
        self.published = {
            "workspace_id": workspace_id,
            "display_name": display_name,
            "dataset_id": dataset_id,
        }
        return {
            "id": "report-123",
            "display_name": display_name,
            "workspace_id": workspace_id,
            "type": "Report",
            "status": "Created",
            "web_url": f"https://app.fabric.microsoft.com/groups/{workspace_id}/reports/report-123",
        }


class FakeAuditService:
    def audit_semantic_model(self, spec, *, features=None, include_bpa=False):
        return {"merged": {"score": 100, "findings": []}}

    def audit_report(self, spec, *, use_agent=True):
        return _ReportAuditResult({"score": 95, "findings": []}, "deterministic", 0, None)


class FakeProvisioningService:
    def list_workspaces(self):
        return [Workspace(id="ws-1", name="Demo", type="Workspace")]


class _NoopAuditLog:
    def log(self, **kwargs):
        return None


class FakeContainer:
    def __init__(self) -> None:
        self._model = FakeModelService()
        self._report = FakeReportService()

    def schema_service(self):
        return FakeSchemaService()

    def model_service(self, tenant=None):
        return self._model

    def report_service(self, tenant=None):
        return self._report

    def audit_service(self):
        return FakeAuditService()

    def provisioning_service(self):
        return FakeProvisioningService()

    def audit_log_service(self, tenant=None):
        return _NoopAuditLog()


def _config() -> AgentTeamConfig:
    cfg = AgentTeamConfig.from_env()
    cfg.narrate = False
    return cfg


def _grounded_request() -> OrchestrationRequest:
    return OrchestrationRequest(
        objective="Design a model and report",
        server="srv.fabric.example",
        database="WideWorld",
        model_name="Sales Analytics",
        include_report=True,
        use_agent=False,
    )


# ---------------------------------------------------------------------------
# Capability + request shape
# ---------------------------------------------------------------------------


class CapabilityTests(unittest.TestCase):
    def test_capability_status_shape(self) -> None:
        status = capability_status()
        self.assertIn("mode", status)
        self.assertIn(status["mode"], {"foundry", "deterministic"})
        self.assertEqual(
            status["roles"], ["architect", "dax", "auditor", "report"]
        )
        self.assertIn("fabric-agentic-orchestration", status["skills"])
        self.assertIn("powerBiModelingMcp", status)

    def test_request_grounded_flag(self) -> None:
        self.assertTrue(_grounded_request().grounded)
        self.assertFalse(OrchestrationRequest(objective="hi").grounded)

    def test_request_from_dict_ignores_unknown_keys(self) -> None:
        req = OrchestrationRequest.from_dict(
            {"objective": "x", "server": "s", "bogus": 1}
        )
        self.assertEqual(req.objective, "x")
        self.assertEqual(req.server, "s")


# ---------------------------------------------------------------------------
# Orchestrator pipeline
# ---------------------------------------------------------------------------


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.container = FakeContainer()
        self.orch = AgentOrchestrator(
            self.container, TenantContext.default(), config=_config()
        )

    def test_grounded_run_produces_full_pipeline(self) -> None:
        result = self.orch.run(_grounded_request())
        self.assertEqual(result.status, "ok")
        kinds = [e.kind for e in result.events]
        self.assertEqual(kinds[0], "plan")
        self.assertEqual(kinds[-1], "final")
        self.assertIn("artifact", kinds)
        # All four artifacts produced.
        self.assertIn("model", result.artifacts)
        self.assertIn("modelAudit", result.artifacts)
        self.assertIn("report", result.artifacts)
        self.assertIn("reportAudit", result.artifacts)
        # Model spec round-trips through the IR.
        spec = SemanticModelSpec.from_dict(result.artifacts["model"])
        self.assertEqual(spec.name, "Sales Analytics")

    def test_stream_yields_ordered_events(self) -> None:
        events = list(self.orch.stream(_grounded_request()))
        self.assertEqual(events[0].kind, "plan")
        self.assertEqual(events[-1].kind, "final")
        running = [e for e in events if e.status == "running"]
        self.assertTrue(running, "expected at least one running step")

    def test_model_only_run_skips_report(self) -> None:
        req = _grounded_request()
        req.include_report = False
        result = self.orch.run(req)
        self.assertIn("model", result.artifacts)
        self.assertNotIn("report", result.artifacts)

    def test_ungrounded_run_is_advisory(self) -> None:
        result = self.orch.run(OrchestrationRequest(objective="What next?"))
        kinds = [e.kind for e in result.events]
        self.assertIn("message", kinds)
        self.assertEqual(kinds[-1], "final")
        self.assertEqual(result.artifacts, {})


# ---------------------------------------------------------------------------
# ToolKit
# ---------------------------------------------------------------------------


class ToolKitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.container = FakeContainer()
        self.toolkit = ToolKit(self.container, TenantContext.default())

    def test_extract_and_design(self) -> None:
        schema = self.toolkit.extract_schema("s", "d")
        self.assertEqual(len(schema), 2)
        design = self.toolkit.design_model(
            server="s", database="d", model_name="M"
        )
        self.assertIn("spec", design)
        self.assertFalse(design["usedAgent"])

    def test_audit_model_roundtrips(self) -> None:
        design = self.toolkit.design_model(server="s", database="d", model_name="M")
        audit = self.toolkit.audit_model(design["spec"])
        self.assertIn("merged", audit)

    def test_publish_model_records_call(self) -> None:
        design = self.toolkit.design_model(server="s", database="d", model_name="M")
        created = self.toolkit.publish_model(
            workspace_id="ws-1", display_name="M", spec=design["spec"]
        )
        self.assertEqual(created["id"], "model-123")
        self.assertEqual(self.container._model.published["display_name"], "M")


# ---------------------------------------------------------------------------
# Publish workflows (human-in-the-loop)
# ---------------------------------------------------------------------------


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.container = FakeContainer()
        self.tenant = TenantContext.default()
        self.store = PublishWorkflowStore()

    def _spec(self) -> dict:
        return spec_from_schemas([_fact(), _dim()], model_name="M").to_dict()

    def test_create_is_pending(self) -> None:
        wf = self.store.create(
            "publish-model", self.tenant,
            {"workspace_id": "ws-1", "display_name": "M", "spec": self._spec()},
        )
        self.assertEqual(wf.state, "pending")
        self.assertIsNone(wf.result)
        # Bulky spec is redacted in the projection.
        self.assertNotIn("spec", wf.to_dict()["payload"])
        self.assertIn("specSummary", wf.to_dict()["payload"])

    def test_approve_publishes(self) -> None:
        wf = self.store.create(
            "publish-model", self.tenant,
            {"workspace_id": "ws-1", "display_name": "M", "spec": self._spec(),
             "fmt": "TMDL", "description": None},
        )
        resolved = resolve_workflow(
            wf, approve=True, container=self.container, tenant=self.tenant
        )
        self.assertEqual(resolved.state, "approved")
        self.assertEqual(resolved.result["id"], "model-123")

    def test_reject_does_not_publish(self) -> None:
        wf = self.store.create(
            "publish-model", self.tenant,
            {"workspace_id": "ws-1", "display_name": "M", "spec": self._spec()},
        )
        resolved = resolve_workflow(
            wf, approve=False, container=self.container, tenant=self.tenant
        )
        self.assertEqual(resolved.state, "rejected")
        self.assertIsNone(self.container._model.published)

    def test_global_store_singleton(self) -> None:
        self.assertIs(get_workflow_store(), get_workflow_store())

    def _combined_payload(self) -> dict:
        model_spec = spec_from_schemas([_fact(), _dim()], model_name="M")
        report_spec = suggest_report(model_spec, report_name="Demo").to_dict()
        return {
            "workspace_id": "ws-1",
            "model_display_name": "M",
            "model_spec": model_spec.to_dict(),
            "report_display_name": "Demo",
            "report_spec": report_spec,
            "fmt": "TMDL",
            "description": None,
        }

    def test_combined_redacts_both_specs(self) -> None:
        wf = self.store.create(
            "publish-model-report", self.tenant, self._combined_payload()
        )
        payload = wf.to_dict()["payload"]
        self.assertNotIn("model_spec", payload)
        self.assertNotIn("report_spec", payload)
        self.assertIn("modelSpecSummary", payload)
        self.assertIn("reportSpecSummary", payload)

    def test_combined_approve_publishes_model_then_report(self) -> None:
        wf = self.store.create(
            "publish-model-report", self.tenant, self._combined_payload()
        )
        resolved = resolve_workflow(
            wf, approve=True, container=self.container, tenant=self.tenant
        )
        self.assertEqual(resolved.state, "approved")
        result = resolved.result
        self.assertEqual(result["model"]["id"], "model-123")
        self.assertEqual(result["report"]["id"], "report-123")
        # The report is bound to the freshly published model's dataset id.
        self.assertEqual(self.container._report.published["dataset_id"], "model-123")
        # Summary carries one row per object, each with a shareable link.
        published = result["published"]
        self.assertEqual(len(published), 2)
        self.assertEqual({p["kind"] for p in published}, {"SemanticModel", "Report"})
        self.assertTrue(all(p["webUrl"] for p in published))

    def test_combined_reject_publishes_nothing(self) -> None:
        wf = self.store.create(
            "publish-model-report", self.tenant, self._combined_payload()
        )
        resolved = resolve_workflow(
            wf, approve=False, container=self.container, tenant=self.tenant
        )
        self.assertEqual(resolved.state, "rejected")
        self.assertIsNone(self.container._model.published)
        self.assertIsNone(self.container._report.published)


# ---------------------------------------------------------------------------
# MCP orchestration tools
# ---------------------------------------------------------------------------


class McpOrchestrationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from fabric_mcp import server as mcp_server

        self.mcp_server = mcp_server
        self.container = FakeContainer()
        mcp_server._container = self.container  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.mcp_server._container = None

    def test_orchestration_tools_registered(self) -> None:
        from fabric_mcp import mcp

        names = {t.name for t in asyncio.run(mcp.list_tools())}
        for name in (
            "agent_team_status",
            "run_agent_team",
            "design_and_publish_model",
            "improve_report",
        ):
            self.assertIn(name, names)

    def test_run_agent_team_grounded(self) -> None:
        out = self.mcp_server.run_agent_team(
            objective="Build it",
            server="s",
            database="d",
            model_name="Sales",
            include_report=True,
        )
        self.assertEqual(out["status"], "ok")
        self.assertIn("model", out["artifacts"])
        self.assertIn("report", out["artifacts"])

    def test_design_and_publish_requires_approval_by_default(self) -> None:
        out = self.mcp_server.design_and_publish_model(
            server="s", database="d", model_name="Sales", workspace_id="ws-1"
        )
        self.assertTrue(out["approvalRequired"])
        self.assertIsNone(out["published"])
        self.assertIsNone(self.container._model.published)

    def test_design_and_publish_auto_approve(self) -> None:
        out = self.mcp_server.design_and_publish_model(
            server="s", database="d", model_name="Sales", workspace_id="ws-1",
            auto_approve=True,
        )
        self.assertFalse(out["approvalRequired"])
        self.assertEqual(out["published"]["id"], "model-123")

    def test_improve_report_applies_theme_fixes(self) -> None:
        model = spec_from_schemas([_fact(), _dim()], model_name="M")
        report = suggest_report(model, report_name="Demo").to_dict()
        out = self.mcp_server.improve_report(report)
        self.assertIn("spec", out)
        self.assertIn("auditBefore", out)
        self.assertIn("auditAfter", out)
        self.assertTrue(out["applied"])


# ---------------------------------------------------------------------------
# FastAPI agent routes
# ---------------------------------------------------------------------------


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from fabric_api.dependencies import get_container
        from fabric_api.main import create_app

        self.app = create_app()
        self.container = FakeContainer()
        self.app.state.container = self.container
        self.app.dependency_overrides[get_container] = lambda: self.container
        self.client = TestClient(self.app)

    def test_team_status(self) -> None:
        resp = self.client.get("/api/v1/agents/team")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("mode", resp.json())

    def test_orchestrate_grounded(self) -> None:
        resp = self.client.post(
            "/api/v1/agents/orchestrate",
            json={
                "objective": "Build",
                "server": "s",
                "database": "d",
                "model_name": "Sales",
                "include_report": True,
                "use_agent": False,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("model", body["artifacts"])
        self.assertEqual(body["events"][-1]["kind"], "final")

    def test_publish_model_workflow_lifecycle(self) -> None:
        spec = spec_from_schemas([_fact(), _dim()], model_name="M").to_dict()
        create = self.client.post(
            "/api/v1/agents/workflows/publish-model",
            json={"workspace_id": "ws-1", "display_name": "M", "spec": spec},
        )
        self.assertEqual(create.status_code, 200)
        wf = create.json()
        self.assertEqual(wf["state"], "pending")

        decide = self.client.post(
            f"/api/v1/agents/workflows/{wf['id']}/decision",
            json={"approve": True},
        )
        self.assertEqual(decide.status_code, 200)
        self.assertEqual(decide.json()["state"], "approved")
        self.assertEqual(decide.json()["result"]["id"], "model-123")

    def test_unknown_workflow_is_404(self) -> None:
        resp = self.client.get("/api/v1/agents/workflows/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_publish_model_report_workflow_lifecycle(self) -> None:
        model_spec = spec_from_schemas([_fact(), _dim()], model_name="M")
        report_spec = suggest_report(model_spec, report_name="Demo").to_dict()
        create = self.client.post(
            "/api/v1/agents/workflows/publish-model-report",
            json={
                "workspace_id": "ws-1",
                "model_display_name": "M",
                "model_spec": model_spec.to_dict(),
                "report_display_name": "Demo",
                "report_spec": report_spec,
            },
        )
        self.assertEqual(create.status_code, 200)
        wf = create.json()
        self.assertEqual(wf["state"], "pending")
        self.assertEqual(wf["kind"], "publish-model-report")

        decide = self.client.post(
            f"/api/v1/agents/workflows/{wf['id']}/decision",
            json={"approve": True},
        )
        self.assertEqual(decide.status_code, 200)
        body = decide.json()
        self.assertEqual(body["state"], "approved")
        published = body["result"]["published"]
        self.assertEqual(len(published), 2)
        self.assertTrue(all(p["webUrl"] for p in published))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
