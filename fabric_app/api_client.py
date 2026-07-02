"""Typed HTTP client for the Fabric SaaS REST API.

This is the **only** way the Streamlit frontend reaches platform capabilities,
keeping the UI free of business logic, Fabric SDK calls, and SQL drivers. Every
method maps to a ``/api/v1`` route; non-2xx responses (RFC 7807 problem+json)
are raised as :class:`ApiError` with the structured ``code``/``detail`` so the
UI can render friendly, actionable error states.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator, Optional

import httpx

DEFAULT_TIMEOUT = 180.0


class ApiError(RuntimeError):
    """Raised when the API returns a non-success (problem+json) response."""

    def __init__(
        self,
        status: int,
        *,
        code: str = "error",
        title: str = "Error",
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail

    @property
    def is_dependency_unavailable(self) -> bool:
        return self.code == "dependency_unavailable"

    @property
    def is_fabric_access_denied(self) -> bool:
        return self.code == "fabric_access_denied"


class FabricApiClient:
    """A thin, typed wrapper over the Fabric SaaS REST API.

    Args:
        base_url: API root (e.g. ``http://localhost:8000``). Defaults to the
            ``FABRIC_API_BASE_URL`` env var, then ``http://localhost:8000``.
        tenant_id: optional tenant; sent as ``X-Tenant-Id`` for multi-tenancy.
        timeout: request timeout (seconds); generous to cover Fabric LROs.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        tenant_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("FABRIC_API_BASE_URL")
            or "http://localhost:8000"
        ).rstrip("/")
        self.tenant_id = tenant_id or os.environ.get("FABRIC_DEFAULT_TENANT_ID")
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.tenant_id:
            headers["X-Tenant-Id"] = self.tenant_id
        self._client = client or httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout
        )
        self._prefix = "/api/v1"

    # -- low-level ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # network / connection failure
            raise ApiError(
                503,
                code="api_unreachable",
                title="API unreachable",
                detail=f"Could not reach the API at {self.base_url}: {exc}",
            ) from exc
        if response.status_code >= 400:
            raise self._to_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    @staticmethod
    def _to_error(response: httpx.Response) -> ApiError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        return ApiError(
            response.status_code,
            code=body.get("code", "error"),
            title=body.get("title", "Error"),
            detail=body.get("detail") or response.text or None,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FabricApiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- health ------------------------------------------------------------

    def healthz(self) -> dict:
        return self._request("GET", "/healthz")

    def readyz(self) -> dict:
        return self._request("GET", "/readyz")

    def fabric_health(self) -> dict:
        """Provisioning preflight: is the platform identity Fabric-enabled?"""
        return self._request("GET", f"{self._prefix}/health/fabric")

    # -- discovery ---------------------------------------------------------

    def list_workspaces(self) -> list[dict]:
        return self._request("GET", f"{self._prefix}/workspaces")

    def list_sql_endpoints(self, workspace_id: str) -> list[dict]:
        return self._request(
            "GET", f"{self._prefix}/workspaces/{workspace_id}/datasources/sql-endpoints"
        )

    def list_lakehouses(self, workspace_id: str) -> list[dict]:
        return self._request(
            "GET", f"{self._prefix}/workspaces/{workspace_id}/datasources/lakehouses"
        )

    # -- schemas -----------------------------------------------------------

    def extract_schemas(self, server: str, database: str) -> list[dict]:
        return self._request(
            "POST",
            f"{self._prefix}/schemas/extract",
            json={"server": server, "database": database},
        )

    def export_schemas(
        self, fmt: str, server: str, database: str, endpoint_name: str = ""
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/schemas/export",
            json={
                "format": fmt,
                "server": server,
                "database": database,
                "endpoint_name": endpoint_name,
            },
        )

    # -- semantic models ---------------------------------------------------

    def list_semantic_models(self, workspace_id: str) -> list[dict]:
        return self._request(
            "GET", f"{self._prefix}/semantic-models/{workspace_id}"
        )

    def import_semantic_model(
        self,
        workspace_id: str,
        model_id: str,
        *,
        fmt: str = "TMDL",
        workspace_name: str = "",
        item_name: str = "",
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/{workspace_id}/{model_id}/import",
            params={
                "fmt": fmt,
                "workspace_name": workspace_name,
                "item_name": item_name,
            },
        )

    def design_semantic_model(
        self,
        *,
        server: str,
        database: str,
        model_name: str,
        storage_mode: str = "import",
        source_kind: str = "sql",
        use_agent: bool = True,
        extra_instructions: str | None = None,
        selected_tables: list[str] | None = None,
        lakehouse_id: str | None = None,
        lakehouse_name: str | None = None,
        onelake_workspace_id: str | None = None,
        onelake_tables_path: str | None = None,
        default_schema: str | None = None,
        direct_lake_mode: str = "auto",
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/design",
            json={
                "server": server,
                "database": database,
                "model_name": model_name,
                "storage_mode": storage_mode,
                "source_kind": source_kind,
                "use_agent": use_agent,
                "extra_instructions": extra_instructions,
                "selected_tables": selected_tables,
                "lakehouse_id": lakehouse_id,
                "lakehouse_name": lakehouse_name,
                "onelake_workspace_id": onelake_workspace_id,
                "onelake_tables_path": onelake_tables_path,
                "default_schema": default_schema,
                "direct_lake_mode": direct_lake_mode,
            },
        )

    def build_model_definition(self, spec: dict, *, fmt: str = "TMDL") -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/build-definition",
            params={"fmt": fmt},
            json=spec,
        )

    def validate_semantic_model(self, spec: dict) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/validate",
            json={"spec": spec},
        )

    def suggest_semantic_model(
        self,
        *,
        server: str,
        database: str,
        spec: dict,
        selected_tables: list[str] | None = None,
        use_agent: bool = True,
        extra_instructions: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/suggest",
            json={
                "server": server,
                "database": database,
                "spec": spec,
                "selected_tables": selected_tables,
                "use_agent": use_agent,
                "extra_instructions": extra_instructions,
            },
        )

    def apply_semantic_model_suggestions(
        self,
        *,
        spec: dict,
        relationships: list[dict] | None = None,
        measures: list[dict] | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/apply-suggestions",
            json={
                "spec": spec,
                "relationships": relationships or [],
                "measures": measures or [],
            },
        )

    def publish_semantic_model(
        self,
        *,
        workspace_id: str,
        display_name: str,
        spec: dict,
        fmt: str = "TMDL",
        description: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/publish",
            json={
                "workspace_id": workspace_id,
                "display_name": display_name,
                "spec": spec,
                "format": fmt,
                "description": description,
            },
        )

    def propose_model_usability_fixes(self, spec: dict) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/suggestions/propose-usability",
            json={"spec": spec},
        )

    def propose_model_copilot_fixes(self, spec: dict) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/suggestions/propose-copilot",
            json={"spec": spec},
        )

    def propose_model_bpa_fixes(self, spec: dict) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/suggestions/propose-bpa",
            json={"spec": spec},
        )

    def propose_model_ai_fixes(self, spec: dict) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/suggestions/propose-ai",
            json={"spec": spec},
        )

    def apply_model_audit_suggestions(
        self, *, spec: dict, suggestions: list[dict]
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/suggestions/apply",
            json={"spec": spec, "suggestions": suggestions},
        )

    def update_semantic_model(
        self,
        *,
        workspace_id: str,
        model_id: str,
        spec: dict,
        fmt: str = "TMDL",
        item_name: str = "",
        verify: bool = True,
    ) -> dict:
        return self._request(
            "PUT",
            f"{self._prefix}/semantic-models/{workspace_id}/{model_id}",
            json={
                "spec": spec,
                "format": fmt,
                "item_name": item_name,
                "verify": verify,
            },
        )

    def ask_semantic_model(
        self,
        *,
        workspace_id: str,
        workspace_name: str,
        model_id: str,
        model_name: str,
        question: str,
        top_n: int = 50,
        auto_import: bool = False,
        refresh: bool = False,
    ) -> dict:
        """Ask a natural-language question against a live semantic model.

        Server translates the question to DAX (grounded in the model's TMDL
        spec) and executes it via the Power BI Modeling MCP server's
        ``dax_query_operations`` tool. Returns the generated DAX, the result
        table, and the MCP tool transcript.

        The server requires the model definition to have been imported first
        (see :meth:`import_semantic_model`). Pass ``auto_import=True`` to let
        the server fetch it on the fly, or ``refresh=True`` to force a
        re-import when the cached copy is stale.
        """
        return self._request(
            "POST",
            f"{self._prefix}/semantic-models/ask",
            json={
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
                "model_id": model_id,
                "model_name": model_name,
                "question": question,
                "top_n": top_n,
                "auto_import": auto_import,
                "refresh": refresh,
            },
        )

    # -- reports -----------------------------------------------------------

    def list_reports(self, workspace_id: str) -> list[dict]:
        return self._request("GET", f"{self._prefix}/reports/{workspace_id}")

    def import_report(
        self,
        workspace_id: str,
        report_id: str,
        *,
        workspace_name: str = "",
        item_name: str = "",
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/reports/{workspace_id}/{report_id}/import",
            params={"workspace_name": workspace_name, "item_name": item_name},
        )

    def suggest_report(
        self,
        *,
        model: dict,
        report_name: str | None = None,
        dataset_id: str | None = None,
        dataset_name: str | None = None,
        use_agent: bool = True,
        extra_instructions: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/reports/suggest",
            json={
                "model": model,
                "report_name": report_name,
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "use_agent": use_agent,
                "extra_instructions": extra_instructions,
            },
        )

    def build_report_definition(self, spec: dict) -> dict:
        return self._request(
            "POST", f"{self._prefix}/reports/build-definition", json=spec
        )

    def publish_report(
        self,
        *,
        workspace_id: str,
        display_name: str,
        spec: dict,
        description: str | None = None,
        dataset_id: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/reports/publish",
            json={
                "workspace_id": workspace_id,
                "display_name": display_name,
                "spec": spec,
                "description": description,
                "dataset_id": dataset_id,
            },
        )

    def propose_report_theme_fixes(self, spec: dict) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/reports/suggestions/propose-theme",
            json={"spec": spec},
        )

    def apply_report_audit_suggestions(
        self, *, spec: dict, suggestions: list[dict]
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/reports/suggestions/apply",
            json={"spec": spec, "suggestions": suggestions},
        )

    def update_report(
        self,
        *,
        workspace_id: str,
        report_id: str,
        spec: dict,
        item_name: str = "",
        verify: bool = True,
    ) -> dict:
        return self._request(
            "PUT",
            f"{self._prefix}/reports/{workspace_id}/{report_id}",
            json={"spec": spec, "item_name": item_name, "verify": verify},
        )

    # -- audits ------------------------------------------------------------

    def audit_semantic_model(
        self, spec: dict, *, include_bpa: bool = False, features: Optional[list[str]] = None
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/audits/semantic-model",
            json={"spec": spec, "include_bpa": include_bpa, "features": features},
        )

    def audit_report(self, spec: dict, *, use_agent: bool = True) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/audits/report",
            json={"spec": spec, "use_agent": use_agent},
        )

    # -- artifacts ---------------------------------------------------------

    def list_artifacts(self, *, kind: str | None = None) -> list[dict]:
        params = {"kind": kind} if kind else None
        return self._request("GET", f"{self._prefix}/artifacts", params=params)

    def read_manifest(self) -> dict:
        return self._request("GET", f"{self._prefix}/artifacts/manifest")

    def load_artifact_definition(
        self,
        *,
        kind: str,
        workspace_id: str,
        item_id: str,
        workspace_name: str = "",
        item_name: str = "",
    ) -> dict:
        """Return ``{"files": {...}, "metadata": {...}}`` for one stored item."""
        return self._request(
            "GET",
            f"{self._prefix}/artifacts/definition",
            params={
                "kind": kind,
                "workspace_id": workspace_id,
                "item_id": item_id,
                "workspace_name": workspace_name,
                "item_name": item_name,
            },
        )

    # -- operations & agents ----------------------------------------------

    def get_operation(self, operation_id: str) -> dict:
        return self._request("GET", f"{self._prefix}/operations/{operation_id}")

    def agent_status(self) -> dict:
        return self._request("GET", f"{self._prefix}/agents/status")

    def publish_remote_agent(self, description: str | None = None) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/agents/semantic-model/publish-remote",
            json={"description": description},
        )

    # -- agent team / orchestration ---------------------------------------

    def agent_team_status(self) -> dict:
        return self._request("GET", f"{self._prefix}/agents/team")

    def model_chat(
        self,
        *,
        workspace_name: str,
        model_name: str,
        message: str,
        history: list[dict] | None = None,
        workspace_id: str | None = None,
        model_id: str | None = None,
    ) -> dict:
        """Run one natural-language editing turn against an existing model."""
        return self._request(
            "POST",
            f"{self._prefix}/agents/model-chat",
            json={
                "workspace_name": workspace_name,
                "model_name": model_name,
                "message": message,
                "history": history or [],
                "workspace_id": workspace_id,
                "model_id": model_id,
            },
        )

    def orchestrate(
        self,
        *,
        objective: str = "",
        server: str | None = None,
        database: str | None = None,
        model_name: str | None = None,
        workspace_id: str | None = None,
        storage_mode: str = "import",
        source_kind: str = "sql",
        lakehouse_id: str | None = None,
        lakehouse_name: str | None = None,
        onelake_workspace_id: str | None = None,
        onelake_tables_path: str | None = None,
        default_schema: str | None = None,
        direct_lake_mode: str = "auto",
        use_agent: bool = True,
        include_report: bool = True,
        extra_instructions: str | None = None,
    ) -> dict:
        """Run the agentic pipeline and return the collected transcript."""
        return self._request(
            "POST",
            f"{self._prefix}/agents/orchestrate",
            json={
                "objective": objective,
                "server": server,
                "database": database,
                "model_name": model_name,
                "workspace_id": workspace_id,
                "storage_mode": storage_mode,
                "source_kind": source_kind,
                "lakehouse_id": lakehouse_id,
                "lakehouse_name": lakehouse_name,
                "onelake_workspace_id": onelake_workspace_id,
                "onelake_tables_path": onelake_tables_path,
                "default_schema": default_schema,
                "direct_lake_mode": direct_lake_mode,
                "use_agent": use_agent,
                "include_report": include_report,
                "extra_instructions": extra_instructions,
            },
        )

    def orchestrate_stream(
        self,
        *,
        objective: str = "",
        server: str | None = None,
        database: str | None = None,
        model_name: str | None = None,
        workspace_id: str | None = None,
        storage_mode: str = "import",
        source_kind: str = "sql",
        lakehouse_id: str | None = None,
        lakehouse_name: str | None = None,
        onelake_workspace_id: str | None = None,
        onelake_tables_path: str | None = None,
        default_schema: str | None = None,
        direct_lake_mode: str = "auto",
        use_agent: bool = True,
        include_report: bool = True,
        extra_instructions: str | None = None,
    ) -> Iterator[dict]:
        """Run the agentic pipeline, yielding transcript events as they arrive.

        Consumes the API's Server-Sent Events endpoint (``stream=true``). The
        long-running pipeline (live schema extraction + LLM design/report)
        emits an event per step, so the connection stays active and never trips
        the blocking read timeout that a single-JSON ``orchestrate`` call hits.
        The read timeout is disabled for this request; a step may legitimately
        take minutes.
        """
        payload = {
            "objective": objective,
            "server": server,
            "database": database,
            "model_name": model_name,
            "workspace_id": workspace_id,
            "storage_mode": storage_mode,
            "source_kind": source_kind,
            "lakehouse_id": lakehouse_id,
            "lakehouse_name": lakehouse_name,
            "onelake_workspace_id": onelake_workspace_id,
            "onelake_tables_path": onelake_tables_path,
            "default_schema": default_schema,
            "direct_lake_mode": direct_lake_mode,
            "use_agent": use_agent,
            "include_report": include_report,
            "extra_instructions": extra_instructions,
        }
        # Disable the read timeout: a single step (LLM/Fabric) can run for
        # minutes. Connect/write/pool timeouts still guard against a dead API.
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        try:
            with self._client.stream(
                "POST",
                f"{self._prefix}/agents/orchestrate",
                params={"stream": "true"},
                json=payload,
                headers={"Accept": "text/event-stream"},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._to_error(response)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    frame = line[len("data:"):].strip()
                    if frame:
                        yield json.loads(frame)
        except httpx.HTTPError as exc:  # network / connection failure
            raise ApiError(
                503,
                code="api_unreachable",
                title="API unreachable",
                detail=f"Could not reach the API at {self.base_url}: {exc}",
            ) from exc

    def list_agent_workflows(self) -> list[dict]:
        return self._request("GET", f"{self._prefix}/agents/workflows")

    def create_publish_model_workflow(
        self,
        *,
        workspace_id: str,
        display_name: str,
        spec: dict,
        fmt: str = "TMDL",
        description: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/agents/workflows/publish-model",
            json={
                "workspace_id": workspace_id,
                "display_name": display_name,
                "spec": spec,
                "format": fmt,
                "description": description,
            },
        )

    def create_publish_report_workflow(
        self,
        *,
        workspace_id: str,
        display_name: str,
        spec: dict,
        description: str | None = None,
        dataset_id: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/agents/workflows/publish-report",
            json={
                "workspace_id": workspace_id,
                "display_name": display_name,
                "spec": spec,
                "description": description,
                "dataset_id": dataset_id,
            },
        )

    def create_publish_model_report_workflow(
        self,
        *,
        workspace_id: str,
        model_display_name: str,
        model_spec: dict,
        report_display_name: str,
        report_spec: dict,
        fmt: str = "TMDL",
        description: str | None = None,
    ) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/agents/workflows/publish-model-report",
            json={
                "workspace_id": workspace_id,
                "model_display_name": model_display_name,
                "model_spec": model_spec,
                "report_display_name": report_display_name,
                "report_spec": report_spec,
                "format": fmt,
                "description": description,
            },
        )

    def get_agent_workflow(self, workflow_id: str) -> dict:
        return self._request(
            "GET", f"{self._prefix}/agents/workflows/{workflow_id}"
        )

    def decide_agent_workflow(self, workflow_id: str, *, approve: bool) -> dict:
        return self._request(
            "POST",
            f"{self._prefix}/agents/workflows/{workflow_id}/decision",
            json={"approve": approve},
        )


_client: FabricApiClient | None = None


def get_api_client() -> FabricApiClient:
    """Return a process-wide API client (lazy, env-configured)."""
    global _client
    if _client is None:
        _client = FabricApiClient()
    return _client
