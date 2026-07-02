"""The agent orchestrator.

Coordinates the specialized team into an end-to-end flow: *schema → semantic
model → audit → report → audit*. It is **agent-first with deterministic
fallback at every layer**:

* The execution steps call the service layer, which is itself Foundry-first —
  so model design / report suggestion / audit use the agents when available and
  deterministic logic otherwise. This guarantees the orchestrator always
  produces a usable result, online or offline.
* When the Agent Framework + Foundry are reachable, a *manager* agent adds a
  natural-language plan and summary over the run (best-effort narration); any
  failure there is swallowed so it never degrades the structured outcome.

Both a streaming (:meth:`stream`) and a collected (:meth:`run`) API are exposed
over the *same* event schema so the REST SSE endpoint, the MCP tools, and the
Streamlit UI render identical transcripts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from fabric_services import ServiceContainer
from fabric_services.context import TenantContext

from .config import AgentTeamConfig
from .events import AgentEvent, OrchestrationResult
from .team import agents_available
from .tools import ToolKit


@dataclass
class OrchestrationRequest:
    """Inputs for an orchestration run.

    A run is *grounded* when ``server``/``database``/``model_name`` are present:
    the orchestrator then executes the full design pipeline. Without them it
    produces a plan/advisory response for the free-form ``objective``.
    """

    objective: str = ""
    server: Optional[str] = None
    database: Optional[str] = None
    model_name: Optional[str] = None
    workspace_id: Optional[str] = None
    storage_mode: str = "import"
    source_kind: str = "sql"
    lakehouse_id: Optional[str] = None
    lakehouse_name: Optional[str] = None
    onelake_workspace_id: Optional[str] = None
    onelake_tables_path: Optional[str] = None
    default_schema: Optional[str] = None
    direct_lake_mode: str = "auto"
    use_agent: bool = True
    include_report: bool = True
    extra_instructions: Optional[str] = None

    @property
    def grounded(self) -> bool:
        return bool(self.server and self.database and self.model_name)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrchestrationRequest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class AgentOrchestrator:
    """Run multi-step, agentic model + report authoring over the platform."""

    def __init__(
        self,
        container: ServiceContainer,
        tenant: TenantContext,
        config: AgentTeamConfig | None = None,
    ) -> None:
        self.container = container
        self.tenant = tenant
        self.config = config or AgentTeamConfig.from_env()
        self.toolkit = ToolKit(container, tenant)

    # -- public API -------------------------------------------------------

    def run(self, request: OrchestrationRequest) -> OrchestrationResult:
        """Execute the pipeline and return the collected transcript + artifacts."""
        result = OrchestrationResult(objective=request.objective)
        for event in self.stream(request):
            result.events.append(event)
            if event.kind == "artifact":
                result.artifacts.update(event.data)
            if event.status == "error":
                result.status = "error"
                result.error = event.text or result.error
            if event.data.get("usedAgent"):
                result.used_agent = True
        return result

    def stream(self, request: OrchestrationRequest) -> Iterator[AgentEvent]:
        """Yield orchestration events as the pipeline progresses."""
        yield self._plan_event(request)

        if not request.grounded:
            # No data source: respond with guidance only.
            yield AgentEvent(
                kind="message",
                agent=self.config.manager_name,
                text=(
                    "Provide a SQL endpoint (server + database) and a model "
                    "name to run the full design pipeline. I can still outline "
                    "an approach for the stated objective."
                ),
            )
            yield AgentEvent(kind="final", text="Advisory response complete.")
            return

        artifacts: dict[str, Any] = {}
        used_agent = False

        # 1) Extract schema -------------------------------------------------
        yield AgentEvent(
            kind="step", agent="architect", status="running",
            text=f"Extracting schema from {request.database}…",
        )
        try:
            schema = self.toolkit.extract_schema(request.server, request.database)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            yield AgentEvent(kind="step", agent="architect", status="error",
                             text=f"Schema extraction failed: {exc}")
            yield AgentEvent(kind="final", status="error", text="Run aborted.")
            return
        yield AgentEvent(
            kind="step", agent="architect", status="ok",
            text=f"Extracted {len(schema)} objects.",
            data={"objectCount": len(schema)},
        )

        # 2) Design model ---------------------------------------------------
        yield AgentEvent(kind="step", agent="architect", status="running",
                         text="Designing semantic model…")
        design = self.toolkit.design_model(
            server=request.server,  # type: ignore[arg-type]
            database=request.database,  # type: ignore[arg-type]
            model_name=request.model_name,  # type: ignore[arg-type]
            storage_mode=request.storage_mode,
            source_kind=request.source_kind,
            lakehouse_id=request.lakehouse_id,
            lakehouse_name=request.lakehouse_name,
            onelake_workspace_id=request.onelake_workspace_id,
            onelake_tables_path=request.onelake_tables_path,
            default_schema=request.default_schema,
            direct_lake_mode=request.direct_lake_mode,
            use_agent=request.use_agent,
            extra_instructions=request.extra_instructions,
        )
        model_spec = design["spec"]
        used_agent = used_agent or bool(design.get("usedAgent"))
        artifacts["model"] = model_spec
        yield AgentEvent(
            kind="artifact", agent="architect", text="Semantic model designed.",
            data={"model": model_spec, "usedAgent": design.get("usedAgent")},
        )

        # 3) Audit model ----------------------------------------------------
        yield AgentEvent(kind="step", agent="auditor", status="running",
                         text="Auditing semantic model…")
        model_audit = self.toolkit.audit_model(model_spec)
        artifacts["modelAudit"] = model_audit
        yield AgentEvent(kind="artifact", agent="auditor",
                         text="Model audit complete.",
                         data={"modelAudit": model_audit})

        if request.include_report:
            # 4) Suggest report --------------------------------------------
            yield AgentEvent(kind="step", agent="report", status="running",
                             text="Designing a starter report…")
            report = self.toolkit.suggest_report(
                model_spec,
                report_name=f"{request.model_name} Overview",
                use_agent=request.use_agent,
                extra_instructions=request.extra_instructions,
            )
            used_agent = used_agent or bool(report.get("usedAgent"))
            report_spec = report["spec"]
            artifacts["report"] = report_spec
            yield AgentEvent(kind="artifact", agent="report",
                             text="Report designed.",
                             data={"report": report_spec,
                                   "usedAgent": report.get("usedAgent")})

            # 5) Audit report ----------------------------------------------
            yield AgentEvent(kind="step", agent="auditor", status="running",
                             text="Auditing report…")
            report_audit = self.toolkit.audit_report(report_spec)
            artifacts["reportAudit"] = report_audit
            yield AgentEvent(kind="artifact", agent="auditor",
                             text="Report audit complete.",
                             data={"reportAudit": report_audit})

        # 6) Manager narration (best-effort) -------------------------------
        summary = self._narrate(request, artifacts)
        if summary:
            used_agent = True
            yield AgentEvent(kind="message", agent=self.config.manager_name,
                             text=summary, data={"usedAgent": True})

        yield AgentEvent(
            kind="final",
            agent=self.config.manager_name,
            text="Pipeline complete: model designed, audited, and a report drafted."
            if request.include_report
            else "Pipeline complete: model designed and audited.",
            data={"usedAgent": used_agent},
        )

    # -- internals --------------------------------------------------------

    def _plan_event(self, request: OrchestrationRequest) -> AgentEvent:
        if request.grounded:
            steps = [
                "extract schema",
                "design semantic model",
                "audit model",
            ]
            if request.include_report:
                steps += ["design report", "audit report"]
            text = "Plan: " + " → ".join(steps)
        else:
            text = f"Plan: advise on objective — {request.objective or '(unspecified)'}"
        return AgentEvent(kind="plan", agent=self.config.manager_name, text=text,
                          data={"grounded": request.grounded})

    def _narrate(
        self, request: OrchestrationRequest, artifacts: dict[str, Any]
    ) -> str | None:
        """Ask a manager agent to summarise the run (best-effort, optional)."""
        if not self.config.narrate or not agents_available():
            return None
        try:
            return asyncio.run(self._narrate_async(request, artifacts))
        except Exception:  # noqa: BLE001 - narration is non-essential
            return None

    async def _narrate_async(
        self, request: OrchestrationRequest, artifacts: dict[str, Any]
    ) -> str | None:
        import json
        import os

        from agent_framework import Agent
        from agent_framework.foundry import FoundryChatClient
        from azure.identity.aio import DefaultAzureCredential

        model = artifacts.get("model", {})
        tables = [t.get("name") for t in (model.get("tables") or [])]
        brief = {
            "objective": request.objective,
            "model": request.model_name,
            "tables": tables,
            "hasReport": "report" in artifacts,
        }
        client_id = os.environ.get("AZURE_CLIENT_ID") or None
        credential = DefaultAzureCredential(
            managed_identity_client_id=client_id,
            exclude_interactive_browser_credential=True,
        )
        async with credential:
            client = FoundryChatClient(
                project_endpoint=self.config.project_endpoint,
                model=self.config.model,
                credential=credential,
            )
            agent = Agent(
                name=self.config.manager_name,
                client=client,
                instructions=(
                    "You are the orchestration manager. In 3-5 sentences, "
                    "summarise what the team produced and recommend the next "
                    "best action (e.g. publish, refine measures, fix audit "
                    "findings). Be concrete and concise."
                ),
                description="Summarises an agentic model+report run.",
            )
            async with agent:
                result = await agent.run(
                    "Summarise this run:\n" + json.dumps(brief, indent=2)
                )
        text = getattr(result, "text", None) or str(result)
        return text.strip() or None
