"""Tests for the agent-facing MCP server (``fabric_mcp``).

Exercise tool registration and a couple of tools against a fake service
container, so they run without Fabric / SQL / Foundry connectivity.
"""

from __future__ import annotations

import asyncio
import unittest

from app.fabric_client import Workspace
from app.sql_client import ColumnSchema, TableSchema
from fabric_mcp import mcp
from fabric_mcp import server as mcp_server


class FakeProvisioningService:
    def list_workspaces(self):
        return [Workspace(id="ws-1", name="Demo Workspace", type="Workspace")]


class FakeIntelligenceService:
    def status(self):
        return {
            "semanticModelDesign": False,
            "reportDesign": False,
            "reportAudit": False,
            "foundry": {
                "projectEndpoint": "https://example/api/projects/p",
                "model": "gpt-4o",
                "agentName": "semantic-model-builder",
            },
        }


class FakeSchemaService:
    def extract_schemas(self, server, database):
        return [
            TableSchema(
                schema="dbo",
                name="Sales",
                object_type="TABLE",
                columns=[
                    ColumnSchema(
                        name="Id",
                        ordinal=1,
                        data_type="int",
                        max_length=None,
                        precision=10,
                        scale=0,
                        is_nullable=False,
                        default=None,
                        is_primary_key=True,
                    )
                ],
            )
        ]


class FakeContainer:
    def provisioning_service(self):
        return FakeProvisioningService()

    def intelligence_service(self):
        return FakeIntelligenceService()

    def schema_service(self):
        return FakeSchemaService()


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        mcp_server._container = FakeContainer()  # type: ignore[assignment]

    def tearDown(self) -> None:
        mcp_server._container = None

    def test_tools_registered(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        self.assertIn("design_semantic_model", names)
        self.assertIn("publish_semantic_model", names)
        self.assertIn("suggest_report", names)
        self.assertIn("list_workspaces", names)
        # Phase 1 write-back + audit-suggestion tools.
        for name in (
            "update_semantic_model",
            "update_report",
            "propose_model_usability_fixes",
            "propose_model_copilot_fixes",
            "apply_model_audit_suggestions",
            "propose_report_theme_fixes",
            "apply_report_audit_suggestions",
        ):
            self.assertIn(name, names)
        # 16 baseline + 7 new = at least 23.
        self.assertGreaterEqual(len(tools), 23)

    def test_list_workspaces_tool(self) -> None:
        result = mcp_server.list_workspaces()
        self.assertEqual(result[0]["id"], "ws-1")
        self.assertEqual(result[0]["name"], "Demo Workspace")

    def test_extract_schema_tool(self) -> None:
        result = mcp_server.extract_schema("srv", "db")
        self.assertEqual(result[0]["name"], "Sales")
        self.assertEqual(result[0]["columns"][0]["name"], "Id")

    def test_agent_status_tool(self) -> None:
        status = mcp_server.agent_status()
        self.assertIn("semanticModelDesign", status)
        self.assertIn("foundry", status)
        self.assertIn("projectEndpoint", status["foundry"])


class McpWriteBackTests(unittest.TestCase):
    """Phase 1 — exercise the write-back / audit-suggestion MCP tools."""

    def setUp(self) -> None:
        # Container stub with just the model + report services we need.
        class _FakeModelService:
            def update_model(self_inner, workspace_id, model_id, spec, *, fmt="TMDL", item_name=""):
                self.last_model_update = {
                    "workspace_id": workspace_id,
                    "model_id": model_id,
                    "fmt": fmt,
                }
                return {"status": "Succeeded", "format": fmt, "fileCount": 2}

            def propose_usability_fixes(self_inner, spec):
                from app.intelligence import SuggestionSpec

                return [
                    SuggestionSpec(
                        kind="model_usability",
                        object_ref=spec.name,
                        field="description",
                        proposed_value="auto",
                    )
                ]

            def propose_copilot_prep(self_inner, spec):
                from app.intelligence import SuggestionSpec

                return [
                    SuggestionSpec(
                        kind="model_copilot",
                        object_ref=spec.name,
                        field="model.description",
                        proposed_value="copilot",
                    )
                ]

            def apply_audit_suggestions(self_inner, spec, suggestions):
                from app.intelligence import SuggestionSpec

                applied = []
                for s in suggestions:
                    data = s.to_dict()
                    data["status"] = "applied"
                    applied.append(SuggestionSpec.from_dict(data))
                return spec, applied

        class _FakeReportService:
            def update_report(self_inner, workspace_id, report_id, spec, *, item_name=""):
                self.last_report_update = {
                    "workspace_id": workspace_id,
                    "report_id": report_id,
                }
                return {"status": "Succeeded", "fileCount": 3}

            def generate_theme_remediation(self_inner, spec):
                from app.intelligence import SuggestionSpec

                return [
                    SuggestionSpec(
                        kind="report_theme",
                        object_ref="theme:Brand",
                        field="theme.background",
                        proposed_value="#FFFFFF",
                    )
                ]

            def apply_audit_suggestions(self_inner, spec, suggestions):
                from app.intelligence import SuggestionSpec

                applied = []
                for s in suggestions:
                    data = s.to_dict()
                    data["status"] = "applied"
                    applied.append(SuggestionSpec.from_dict(data))
                return spec, applied

        class _Container:
            def model_service(self_inner, tenant):
                return _FakeModelService()

            def report_service(self_inner, tenant):
                return _FakeReportService()

        mcp_server._container = _Container()  # type: ignore[assignment]

    def tearDown(self) -> None:
        mcp_server._container = None

    def _model_spec(self) -> dict:
        return {"name": "Demo", "tables": []}

    def _report_spec(self) -> dict:
        return {"name": "DemoReport", "pages": []}

    def test_update_semantic_model_tool(self) -> None:
        out = mcp_server.update_semantic_model("ws-1", "m-1", self._model_spec())
        self.assertEqual(out["status"], "Succeeded")
        self.assertEqual(self.last_model_update["model_id"], "m-1")

    def test_propose_model_usability_fixes_tool(self) -> None:
        out = mcp_server.propose_model_usability_fixes(self._model_spec())
        self.assertEqual(len(out["suggestions"]), 1)
        self.assertEqual(out["suggestions"][0]["kind"], "model_usability")

    def test_apply_model_audit_suggestions_tool(self) -> None:
        out = mcp_server.apply_model_audit_suggestions(
            self._model_spec(),
            [
                {
                    "kind": "model_usability",
                    "object_ref": "Demo",
                    "field": "description",
                    "proposed_value": "hi",
                    "status": "accepted",
                }
            ],
        )
        self.assertEqual(out["suggestions"][0]["status"], "applied")
        self.assertEqual(out["spec"]["name"], "Demo")

    def test_update_report_tool(self) -> None:
        out = mcp_server.update_report("ws-1", "rep-1", self._report_spec())
        self.assertEqual(out["status"], "Succeeded")
        self.assertEqual(self.last_report_update["report_id"], "rep-1")

    def test_propose_report_theme_fixes_tool(self) -> None:
        out = mcp_server.propose_report_theme_fixes(self._report_spec())
        self.assertEqual(out["suggestions"][0]["kind"], "report_theme")

    def test_apply_report_audit_suggestions_tool(self) -> None:
        out = mcp_server.apply_report_audit_suggestions(
            self._report_spec(),
            [
                {
                    "kind": "report_theme",
                    "object_ref": "theme:Brand",
                    "field": "theme.background",
                    "proposed_value": "#FFFFFF",
                    "status": "accepted",
                }
            ],
        )
        self.assertEqual(out["suggestions"][0]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
