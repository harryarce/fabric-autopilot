"""Contract tests for the FastAPI surface (``fabric_api``).

These exercise routing, dependency wiring, serialization (``to_jsonable``), and
problem+json error handling with a fake service container, so they run without
any Fabric / SQL / Foundry connectivity.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.fabric_client import Workspace
from app.sql_client import ColumnSchema, TableSchema
from fabric_api.dependencies import get_container
from fabric_api.main import create_app


def _fake_workspace() -> Workspace:
    return Workspace(id="ws-1", name="Demo Workspace", type="Workspace")


def _fake_schema() -> TableSchema:
    return TableSchema(
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


def _fake_dim_schema() -> TableSchema:
    return TableSchema(
        schema="dbo",
        name="Customer",
        object_type="TABLE",
        columns=[
            ColumnSchema(
                name="CustomerId",
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


class FakeProvisioningService:
    def list_workspaces(self):
        return [_fake_workspace()]


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

    def publish_remote_semantic_model_agent(self, description=None):
        return {"status": "Created", "agent_name": "semantic-model-builder"}


class FakeSchemaService:
    def extract_schemas(self, server, database):
        return [_fake_schema(), _fake_dim_schema()]

    @staticmethod
    def filter_schemas(schemas, selected_tables):
        if selected_tables is None:
            return schemas
        wanted = {name.casefold() for name in selected_tables}
        return [
            s for s in schemas if f"{s.schema}.{s.name}".casefold() in wanted
        ]


class _FakeSpec:
    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict:
        return {"name": self.name}


class FakeDesignResult:
    def __init__(self, spec, used_agent: bool) -> None:
        self.spec = spec
        self.used_agent = used_agent


class FakeModelService:
    """Captures design_model arguments for assertions."""

    last_call: dict = {}
    last_suggest: dict = {}
    last_apply: dict = {}

    def design_model(self, schemas, **kwargs):
        FakeModelService.last_call = {
            "schemas": list(schemas),
            "kwargs": kwargs,
        }
        return FakeDesignResult(_FakeSpec(kwargs.get("model_name", "M")), False)

    def validate_model(self, spec):
        from app.intelligence import (
            SemanticModelValidationIssue,
            SemanticModelValidationResult,
        )

        return SemanticModelValidationResult(
            issues=[
                SemanticModelValidationIssue(
                    severity="warning",
                    code="SM_NO_MEASURE",
                    message="Model has no measures.",
                )
            ]
        )

    def render_definition(self, spec, *, fmt="TMDL"):
        from app.intelligence import DefinitionFormat, SemanticModelDefinition
        from fabric_services.model_service import RenderResult

        definition = SemanticModelDefinition(
            format=DefinitionFormat(fmt.upper()),
            files={"definition/model.tmdl": "model Demo\n"},
        )
        return RenderResult(definition=definition, warnings=["Dropped 1 relationship(s)."])

    def suggest_additions(self, schemas, spec, *, use_agent=True, extra_instructions=None):
        from app.intelligence import (
            SemanticMeasure,
            SemanticModelSuggestions,
            SemanticRelationship,
            SuggestedMeasure,
            SuggestedRelationship,
        )
        from fabric_services.model_service import SuggestionResult

        FakeModelService.last_suggest = {
            "schemas": list(schemas),
            "use_agent": use_agent,
            "extra_instructions": extra_instructions,
        }
        suggestions = SemanticModelSuggestions(
            relationships=[
                SuggestedRelationship(
                    relationship=SemanticRelationship(
                        from_table="Sales",
                        from_column="CustomerId",
                        to_table="Customer",
                        to_column="CustomerId",
                    ),
                    rationale="FK match",
                    confidence=0.95,
                )
            ],
            measures=[
                SuggestedMeasure(
                    table="Sales",
                    measure=SemanticMeasure(name="Total", expression="SUM(Sales[Amount])"),
                    confidence=0.9,
                )
            ],
        )
        return SuggestionResult(
            suggestions=suggestions, status="deterministic", agent_contributed=0
        )

    def apply_model_suggestions(self, spec, *, relationships=None, measures=None):
        FakeModelService.last_apply = {
            "relationships": list(relationships or []),
            "measures": list(measures or []),
        }
        return _FakeSpec(getattr(spec, "name", "M"))

    # -- Phase 1 write-back + audit suggestions ------------------------------

    last_update: dict = {}
    last_audit_apply: dict = {}

    def update_model(self, workspace_id, model_id, spec, *, fmt="TMDL", item_name=""):
        FakeModelService.last_update = {
            "workspace_id": workspace_id,
            "model_id": model_id,
            "spec": spec,
            "fmt": fmt,
            "item_name": item_name,
        }
        return {"status": "Succeeded", "format": fmt, "fileCount": 2}

    def propose_usability_fixes(self, spec):
        from app.intelligence import SuggestionSpec

        return [
            SuggestionSpec(
                kind="model_usability",
                object_ref=getattr(spec, "name", "M"),
                field="description",
                proposed_value="auto",
                code="SM_USAB_MODEL_NO_DESCRIPTION",
            )
        ]

    def propose_copilot_prep(self, spec):
        from app.intelligence import SuggestionSpec

        return [
            SuggestionSpec(
                kind="model_copilot",
                object_ref=getattr(spec, "name", "M"),
                field="model.description",
                proposed_value="copilot",
                code="SM_COPILOT_MODEL_NO_DESCRIPTION",
            )
        ]

    def apply_audit_suggestions(self, spec, suggestions):
        from app.intelligence import SuggestionSpec

        FakeModelService.last_audit_apply = {
            "spec": spec,
            "suggestions": list(suggestions),
        }
        applied: list[SuggestionSpec] = []
        for s in suggestions:
            data = s.to_dict()
            data["status"] = "applied"
            applied.append(SuggestionSpec.from_dict(data))
        return _FakeSpec(getattr(spec, "name", "M")), applied


class FakeReportService:
    last_update: dict = {}
    last_audit_apply: dict = {}

    def update_report(self, workspace_id, report_id, spec, *, item_name=""):
        FakeReportService.last_update = {
            "workspace_id": workspace_id,
            "report_id": report_id,
            "spec": spec,
            "item_name": item_name,
        }
        return {"status": "Succeeded", "fileCount": 3}

    def generate_theme_remediation(self, spec):
        from app.intelligence import SuggestionSpec

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
        from app.intelligence import SuggestionSpec

        FakeReportService.last_audit_apply = {
            "spec": spec,
            "suggestions": list(suggestions),
        }
        applied: list[SuggestionSpec] = []
        for s in suggestions:
            data = s.to_dict()
            data["status"] = "applied"
            applied.append(SuggestionSpec.from_dict(data))
        return _FakeSpec(getattr(spec, "name", "R")), applied


class FakeArtifactService:
    """Stand-in artifact service for the artifacts router contract tests."""

    saved_suggestions: dict = {}
    status_patches: list = []

    def list_items(self, *, kind=None):
        from app.artifacts import ArtifactRef

        ref = ArtifactRef(
            kind="reports",
            workspace_id="ws-1",
            item_id="rep-1",
            workspace_name="Demo Workspace",
            item_name="Sales Report",
        )
        if kind and kind != ref.kind:
            return []
        return [ref]

    def read_manifest(self):
        return {"reports/ws-1/rep-1": {"source": "fabric", "format": "PBIR"}}

    def load_definition(self, ref):
        from fabric_services.errors import NotFoundError

        if ref.item_id != "rep-1":
            raise NotFoundError(f"No stored definition for {ref.item_id}.")
        return {
            "files": {"definition.pbir": "{}", "report.json": "{}"},
            "metadata": {"source": "fabric", "format": "PBIR"},
        }

    def save_suggestions(self, ref, suggestions):
        items = list(suggestions)
        FakeArtifactService.saved_suggestions[ref.prefix] = items
        return f"{ref.prefix}/suggestions.json"

    def load_suggestions(self, ref):
        return list(FakeArtifactService.saved_suggestions.get(ref.prefix, []))

    def update_suggestion_status(self, ref, suggestion_id, status):
        from app.intelligence import SuggestionSpec
        from fabric_services.errors import NotFoundError

        FakeArtifactService.status_patches.append(
            {"ref": ref.prefix, "id": suggestion_id, "status": status}
        )
        items = FakeArtifactService.saved_suggestions.get(ref.prefix, [])
        for s in items:
            if s.id == suggestion_id:
                data = s.to_dict()
                data["status"] = status
                return SuggestionSpec.from_dict(data)
        raise NotFoundError(f"Suggestion {suggestion_id} not found.")


class FakeContainer:
    """Minimal stand-in exposing only what the tested routes touch."""

    def provisioning_service(self):
        return FakeProvisioningService()

    def intelligence_service(self):
        return FakeIntelligenceService()

    def schema_service(self):
        return FakeSchemaService()

    def model_service(self, tenant):
        return FakeModelService()

    def report_service(self, tenant):
        return FakeReportService()

    def artifact_service(self, tenant):
        return FakeArtifactService()


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.app.dependency_overrides[get_container] = lambda: FakeContainer()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def test_healthz(self) -> None:
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)

    def test_correlation_id_header_echoed(self) -> None:
        resp = self.client.get("/healthz")
        self.assertIn("X-Correlation-Id", resp.headers)

    def test_list_workspaces(self) -> None:
        resp = self.client.get("/api/v1/workspaces")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body[0]["id"], "ws-1")
        self.assertEqual(body[0]["name"], "Demo Workspace")

    def test_extract_schemas(self) -> None:
        resp = self.client.post(
            "/api/v1/schemas/extract",
            json={"server": "srv", "database": "db"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body[0]["name"], "Sales")
        self.assertEqual(body[0]["columns"][0]["name"], "Id")

    def test_extract_schemas_validation_error(self) -> None:
        resp = self.client.post("/api/v1/schemas/extract", json={"server": "srv"})
        self.assertEqual(resp.status_code, 422)

    def test_unknown_operation_returns_problem_json(self) -> None:
        resp = self.client.get("/api/v1/operations/does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("application/problem+json", resp.headers.get("content-type", ""))
        body = resp.json()
        self.assertEqual(body["status"], 404)
        self.assertEqual(body["code"], "not_found")

    def test_agent_status(self) -> None:
        resp = self.client.get("/api/v1/agents/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("semanticModelDesign", body)
        self.assertIn("foundry", body)

    def test_list_artifacts(self) -> None:
        resp = self.client.get("/api/v1/artifacts")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body[0]["item_id"], "rep-1")
        self.assertEqual(body[0]["kind"], "reports")

    def test_load_artifact_definition(self) -> None:
        resp = self.client.get(
            "/api/v1/artifacts/definition",
            params={
                "kind": "reports",
                "workspace_id": "ws-1",
                "item_id": "rep-1",
                "workspace_name": "Demo Workspace",
                "item_name": "Sales Report",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("definition.pbir", body["files"])
        self.assertEqual(body["metadata"]["format"], "PBIR")

    def test_load_artifact_definition_missing_returns_404(self) -> None:
        resp = self.client.get(
            "/api/v1/artifacts/definition",
            params={
                "kind": "reports",
                "workspace_id": "ws-1",
                "item_id": "nope",
            },
        )
        self.assertEqual(resp.status_code, 404)

    def test_design_filters_selected_tables(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/design",
            json={
                "server": "srv",
                "database": "db",
                "model_name": "M",
                "selected_tables": ["dbo.Sales"],
            },
        )
        self.assertEqual(resp.status_code, 200)
        # Only the selected table reaches the model service.
        names = {s.name for s in FakeModelService.last_call["schemas"]}
        self.assertEqual(names, {"Sales"})

    def test_design_passes_direct_lake_binding(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/design",
            json={
                "server": "srv",
                "database": "db",
                "model_name": "M",
                "storage_mode": "directLake",
                "source_kind": "lakehouse",
                "direct_lake_mode": "onelake",
                "lakehouse_id": "lh-1",
                "onelake_tables_path": "Tables",
            },
        )
        self.assertEqual(resp.status_code, 200)
        kwargs = FakeModelService.last_call["kwargs"]
        self.assertEqual(kwargs["storage_mode"], "directLake")
        self.assertEqual(kwargs["source_kind"], "lakehouse")
        self.assertEqual(kwargs["direct_lake_mode"], "onelake")
        self.assertEqual(kwargs["lakehouse_id"], "lh-1")
        self.assertEqual(kwargs["onelake_tables_path"], "Tables")

    def test_openapi_served(self) -> None:
        resp = self.client.get("/openapi.json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/api/v1/workspaces", resp.json()["paths"])


    def test_validate_model(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/validate",
            json={"spec": {"name": "Demo", "tables": []}},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["warnings"]), 1)
        self.assertEqual(body["warnings"][0]["code"], "SM_NO_MEASURE")

    def test_build_definition_returns_warnings_and_metrics(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/build-definition",
            json={
                "name": "Demo",
                "tables": [
                    {"name": "Sales", "columns": [{"name": "Id"}]}
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["format"], "TMDL")
        self.assertIn("Dropped 1 relationship(s).", body["warnings"])
        self.assertEqual(body["metrics"]["tables"], 1)
        self.assertEqual(body["metrics"]["files"], 1)

    def test_suggest_additions(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/suggest",
            json={
                "server": "srv",
                "database": "db",
                "spec": {"name": "Demo", "tables": []},
                "selected_tables": ["dbo.Sales"],
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "deterministic")
        self.assertEqual(len(body["suggestions"]["relationships"]), 1)
        self.assertEqual(len(body["suggestions"]["measures"]), 1)
        # Selection filters the schema list reaching the service.
        names = {s.name for s in FakeModelService.last_suggest["schemas"]}
        self.assertEqual(names, {"Sales"})

    def test_apply_suggestions(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/apply-suggestions",
            json={
                "spec": {"name": "Demo", "tables": []},
                "relationships": [
                    {
                        "relationship": {
                            "from_table": "Sales",
                            "from_column": "CustomerId",
                            "to_table": "Customer",
                            "to_column": "CustomerId",
                        },
                        "confidence": 0.95,
                    }
                ],
                "measures": [
                    {
                        "table": "Sales",
                        "measure": {"name": "Total", "expression": "SUM(Sales[Amount])"},
                        "confidence": 0.9,
                    }
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["spec"]["name"], "Demo")
        self.assertEqual(len(FakeModelService.last_apply["relationships"]), 1)
        self.assertEqual(len(FakeModelService.last_apply["measures"]), 1)


class WriteBackAndAuditApiTests(unittest.TestCase):
    """Phase 1 — updateDefinition + audit-suggestion routes."""

    def setUp(self) -> None:
        self.app = create_app()
        self.app.dependency_overrides[get_container] = lambda: FakeContainer()
        self.client = TestClient(self.app)
        FakeArtifactService.saved_suggestions = {}
        FakeArtifactService.status_patches = []

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    # -- semantic models -----------------------------------------------------

    def test_update_semantic_model(self) -> None:
        resp = self.client.put(
            "/api/v1/semantic-models/ws-1/m-1",
            json={"spec": {"name": "Demo", "tables": []}, "format": "TMDL"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "Succeeded")
        self.assertEqual(body["fileCount"], 2)
        self.assertEqual(body["format"], "TMDL")
        self.assertIn("operationId", body)
        self.assertEqual(FakeModelService.last_update["workspace_id"], "ws-1")
        self.assertEqual(FakeModelService.last_update["model_id"], "m-1")

    def test_propose_usability_suggestions(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/suggestions/propose-usability",
            json={"spec": {"name": "Demo", "tables": []}},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["suggestions"]), 1)
        self.assertEqual(body["suggestions"][0]["kind"], "model_usability")

    def test_propose_copilot_suggestions(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/suggestions/propose-copilot",
            json={"spec": {"name": "Demo", "tables": []}},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["suggestions"][0]["kind"], "model_copilot")

    def test_apply_model_audit_suggestions(self) -> None:
        resp = self.client.post(
            "/api/v1/semantic-models/suggestions/apply",
            json={
                "spec": {"name": "Demo", "tables": []},
                "suggestions": [
                    {
                        "kind": "model_usability",
                        "object_ref": "Demo",
                        "field": "description",
                        "proposed_value": "hi",
                        "status": "accepted",
                    }
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["spec"]["name"], "Demo")
        self.assertEqual(body["suggestions"][0]["status"], "applied")

    def test_persist_and_load_model_suggestions(self) -> None:
        persist = self.client.post(
            "/api/v1/semantic-models/suggestions/persist",
            json={
                "workspace_id": "ws-1",
                "item_id": "m-1",
                "item_name": "DemoModel",
                "suggestions": [
                    {
                        "kind": "model_usability",
                        "object_ref": "Demo",
                        "field": "description",
                        "proposed_value": "hi",
                    }
                ],
            },
        )
        self.assertEqual(persist.status_code, 200)
        self.assertEqual(persist.json()["count"], 1)
        load = self.client.get(
            "/api/v1/semantic-models/suggestions/load",
            params={
                "workspace_id": "ws-1",
                "item_id": "m-1",
                "item_name": "DemoModel",
            },
        )
        self.assertEqual(load.status_code, 200)
        self.assertEqual(len(load.json()["suggestions"]), 1)

    def test_patch_model_suggestion_status(self) -> None:
        # Seed a suggestion first.
        self.client.post(
            "/api/v1/semantic-models/suggestions/persist",
            json={
                "workspace_id": "ws-1",
                "item_id": "m-1",
                "item_name": "DemoModel",
                "suggestions": [
                    {
                        "id": "s-1",
                        "kind": "model_usability",
                        "object_ref": "Demo",
                        "field": "description",
                        "proposed_value": "hi",
                    }
                ],
            },
        )
        resp = self.client.patch(
            "/api/v1/semantic-models/suggestions/status",
            json={
                "workspace_id": "ws-1",
                "item_id": "m-1",
                "item_name": "DemoModel",
                "suggestion_id": "s-1",
                "status": "accepted",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["suggestion"]["status"], "accepted")

    def test_patch_unknown_suggestion_returns_404(self) -> None:
        resp = self.client.patch(
            "/api/v1/semantic-models/suggestions/status",
            json={
                "workspace_id": "ws-1",
                "item_id": "m-1",
                "suggestion_id": "missing",
                "status": "accepted",
            },
        )
        self.assertEqual(resp.status_code, 404)

    # -- reports -------------------------------------------------------------

    def test_update_report(self) -> None:
        resp = self.client.put(
            "/api/v1/reports/ws-1/rep-1",
            json={"spec": {"name": "Demo", "pages": []}},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "Succeeded")
        self.assertEqual(body["fileCount"], 3)
        self.assertEqual(FakeReportService.last_update["report_id"], "rep-1")

    def test_propose_theme_suggestions(self) -> None:
        resp = self.client.post(
            "/api/v1/reports/suggestions/propose-theme",
            json={"spec": {"name": "Demo", "pages": []}},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["suggestions"][0]["kind"], "report_theme")

    def test_apply_report_audit_suggestions(self) -> None:
        resp = self.client.post(
            "/api/v1/reports/suggestions/apply",
            json={
                "spec": {"name": "Demo", "pages": []},
                "suggestions": [
                    {
                        "kind": "report_theme",
                        "object_ref": "theme:Brand",
                        "field": "theme.background",
                        "proposed_value": "#FFFFFF",
                        "status": "accepted",
                    }
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["suggestions"][0]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
