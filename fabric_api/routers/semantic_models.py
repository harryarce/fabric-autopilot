"""Semantic model lifecycle routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.artifacts import ArtifactRef
from app.intelligence import SemanticModelSpec, SuggestionSpec
from app.intelligence.spec import SuggestedMeasure, SuggestedRelationship
from fabric_services import ServiceContainer
from fabric_services.context import TenantContext
from fabric_services.errors import ValidationError

from ..dependencies import get_container, get_tenant
from ..models import (
    ApplyAuditSuggestionsRequest,
    ApplySuggestionsRequest,
    AskModelRequest,
    DesignModelRequest,
    GenerateDaxMeasureRequest,
    PersistSuggestionsRequest,
    ProposeModelSuggestionsRequest,
    PublishModelRequest,
    SuggestModelRequest,
    SuggestionStatusRequest,
    UpdateModelRequest,
    ValidateModelRequest,
    to_jsonable,
)

router = APIRouter(prefix="/semantic-models", tags=["semantic-models"])


@router.get("/{workspace_id}")
def list_models(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> list[dict]:
    """List semantic models in a workspace."""
    models = container.model_service(tenant).list_models(workspace_id)
    return [to_jsonable(m) for m in models]


@router.post("/{workspace_id}/{model_id}/import")
def import_model(
    workspace_id: str,
    model_id: str,
    fmt: str = Query("TMDL", description="TMDL | TMSL"),
    workspace_name: str = Query(""),
    item_name: str = Query(""),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Fetch, persist, and parse a model definition from Fabric."""
    result = container.model_service(tenant).import_model(
        workspace_id,
        model_id,
        fmt=fmt,
        workspace_name=workspace_name,
        item_name=item_name,
    )
    return {
        "spec": to_jsonable(result["spec"]),
        "files": result["files"],
        "format": result["format"],
    }


@router.post("/design")
def design_model(
    body: DesignModelRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Design a semantic model from a SQL schema (agent-first)."""
    service = container.schema_service()
    schemas = service.extract_schemas(body.server, body.database)

    # Honour an explicit table/view selection (schema.name). When omitted, every
    # extracted object is used — preserving the previous behaviour.
    schemas = service.filter_schemas(schemas, body.selected_tables)

    result = container.model_service(tenant).design_model(
        schemas,
        model_name=body.model_name,
        source_server=body.server,
        source_database=body.database,
        storage_mode=body.storage_mode,
        source_kind=body.source_kind,
        use_agent=body.use_agent,
        extra_instructions=body.extra_instructions,
        lakehouse_id=body.lakehouse_id,
        lakehouse_name=body.lakehouse_name,
        onelake_workspace_id=body.onelake_workspace_id,
        onelake_tables_path=body.onelake_tables_path,
        default_schema=body.default_schema,
        direct_lake_mode=body.direct_lake_mode,
    )
    return {"spec": to_jsonable(result.spec), "usedAgent": result.used_agent}


@router.post("/build-definition")
def build_definition(
    spec: dict,
    fmt: str = Query("TMDL", description="TMDL | TMSL"),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Render a model spec to a TMDL/TMSL definition (files + base64 parts).

    Also returns the dropped-relationship ``warnings`` (edges referencing
    unknown tables/columns are pruned before rendering so Fabric accepts the
    dataset) and quick ``metrics`` for the UI to display alongside the files.
    """
    model_spec = SemanticModelSpec.from_dict(spec)
    rendered = container.model_service(tenant).render_definition(model_spec, fmt=fmt)
    definition = rendered.definition
    measure_count = sum(len(t.measures) for t in model_spec.tables)
    return {
        "format": definition.format.value,
        "files": definition.files,
        "definition": definition.definition_payload(),
        "warnings": rendered.warnings,
        "metrics": {
            "tables": len(model_spec.tables),
            "relationships": len(model_spec.relationships),
            "measures": measure_count,
            "files": len(definition.files),
        },
    }


@router.post("/validate")
def validate_model(
    body: ValidateModelRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Run the deterministic consistency checks used by the designer preflight."""
    model_spec = SemanticModelSpec.from_dict(body.spec)
    result = container.model_service(tenant).validate_model(model_spec)
    return {
        "ok": result.ok,
        "errors": [to_jsonable(i) for i in result.errors],
        "warnings": [to_jsonable(i) for i in result.warnings],
        "issues": [to_jsonable(i) for i in result.issues],
    }


@router.post("/suggest")
def suggest_additions(
    body: SuggestModelRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Suggest extra relationships and DAX measures for the supplied spec."""
    service = container.schema_service()
    schemas = service.extract_schemas(body.server, body.database)
    schemas = service.filter_schemas(schemas, body.selected_tables)

    model_spec = SemanticModelSpec.from_dict(body.spec)
    result = container.model_service(tenant).suggest_additions(
        schemas,
        model_spec,
        use_agent=body.use_agent,
        extra_instructions=body.extra_instructions,
    )
    return {
        "suggestions": result.suggestions.to_dict(),
        "status": result.status,
        "agentContributed": result.agent_contributed,
        "error": result.error,
    }


@router.post("/apply-suggestions")
def apply_suggestions(
    body: ApplySuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Merge accepted relationship/measure suggestions into a spec."""
    model_spec = SemanticModelSpec.from_dict(body.spec)
    relationships = [SuggestedRelationship.from_dict(r) for r in body.relationships]
    measures = [SuggestedMeasure.from_dict(m) for m in body.measures]
    updated = container.model_service(tenant).apply_model_suggestions(
        model_spec, relationships=relationships, measures=measures
    )
    return {"spec": to_jsonable(updated)}


@router.post("/publish", status_code=201)
def publish_model(
    body: PublishModelRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Create a semantic model in Fabric from a spec."""
    model_spec = SemanticModelSpec.from_dict(body.spec)
    created = container.model_service(tenant).publish_model(
        body.workspace_id,
        body.display_name,
        model_spec,
        fmt=body.format,
        description=body.description,
    )
    payload = to_jsonable(created)
    op = request.app.state.operations.record(
        "publish_semantic_model", created.status, payload
    )
    return {"operationId": op.id, "status": created.status, "item": payload}


@router.put("/{workspace_id}/{model_id}")
def update_model(
    workspace_id: str,
    model_id: str,
    body: UpdateModelRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Replace a published model's definition (Fabric ``updateDefinition``).

    Renders ``spec`` to TMDL/TMSL and writes it back to Fabric; the Fabric LRO
    is polled to completion by the client so this endpoint returns the
    terminal status synchronously. The freshly written definition is also
    persisted to the tenant's artifact store.
    """
    model_spec = SemanticModelSpec.from_dict(body.spec)
    service = container.model_service(tenant)
    outcome = service.update_model(
        workspace_id,
        model_id,
        model_spec,
        fmt=body.format,
        item_name=body.item_name,
    )
    verification: dict | None = None
    if body.verify:
        try:
            verification = service.verify_update(
                workspace_id, model_id, before=model_spec, fmt=body.format
            )
        except Exception as exc:  # pragma: no cover - verification best-effort
            verification = {"error": str(exc)}
    op = request.app.state.operations.record(
        "update_semantic_model", outcome["status"], outcome
    )
    response = {
        "operationId": op.id,
        "status": outcome["status"],
        "fileCount": outcome["fileCount"],
        "format": outcome["format"],
    }
    if verification is not None:
        response["verification"] = verification
    return response


# -- suggestion endpoints ----------------------------------------------------


@router.post("/suggestions/propose-usability")
def propose_usability_suggestions(
    body: ProposeModelSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Generate deterministic usability write-back suggestions for a model spec."""
    model_spec = SemanticModelSpec.from_dict(body.spec)
    suggestions = container.model_service(tenant).propose_usability_fixes(model_spec)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@router.post("/suggestions/propose-copilot")
def propose_copilot_suggestions(
    body: ProposeModelSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Generate Copilot-readiness write-back suggestions for a model spec."""
    model_spec = SemanticModelSpec.from_dict(body.spec)
    suggestions = container.model_service(tenant).propose_copilot_prep(model_spec)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@router.post("/suggestions/propose-bpa")
def propose_bpa_suggestions(
    body: ProposeModelSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Generate Best Practice Analyzer write-back suggestions for a model spec."""
    model_spec = SemanticModelSpec.from_dict(body.spec)
    suggestions = container.model_service(tenant).propose_bpa_fixes(model_spec)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@router.post("/suggestions/propose-ai")
def propose_ai_suggestions(
    body: ProposeModelSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Generate AI-enhanced description / synonym suggestions for a model spec.

    Uses the Foundry agent to author safe, additive business descriptions and
    Q&A synonyms. Degrades gracefully: ``status`` is ``"ok"``, ``"error"`` or
    ``"unavailable"`` and ``suggestions`` is empty when the agent did not run.
    """
    model_spec = SemanticModelSpec.from_dict(body.spec)
    suggestions, status, error = container.model_service(tenant).propose_ai_fixes(
        model_spec
    )
    return {
        "suggestions": [s.to_dict() for s in suggestions],
        "status": status,
        "error": error,
    }


@router.post("/suggestions/apply")
def apply_audit_suggestions(
    body: ApplyAuditSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Apply accepted audit suggestions to a spec; returns the updated spec.

    Suggestions whose ``status`` is not ``"accepted"`` are passed through
    untouched. Successfully applied suggestions are flipped to ``"applied"``,
    while suggestions targeting an object that no longer exists become
    ``"failed"`` with an explanatory message.
    """
    model_spec = SemanticModelSpec.from_dict(body.spec)
    suggestions = [SuggestionSpec.from_dict(s) for s in body.suggestions]
    updated_spec, updated_suggestions = container.model_service(
        tenant
    ).apply_audit_suggestions(model_spec, suggestions)
    return {
        "spec": to_jsonable(updated_spec),
        "suggestions": [s.to_dict() for s in updated_suggestions],
    }


@router.post("/suggestions/persist")
def persist_suggestions(
    body: PersistSuggestionsRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Persist a suggestion bundle alongside the stored model artifact."""
    ref = ArtifactRef(
        kind="semanticModels",
        workspace_id=body.workspace_id,
        item_id=body.item_id,
        workspace_name=body.workspace_name,
        item_name=body.item_name,
    )
    suggestions = [SuggestionSpec.from_dict(s) for s in body.suggestions]
    key = container.artifact_service(tenant).save_suggestions(ref, suggestions)
    return {"key": key, "count": len(suggestions)}


@router.get("/suggestions/load")
def load_suggestions(
    workspace_id: str = Query(...),
    item_id: str = Query(...),
    workspace_name: str = Query(""),
    item_name: str = Query(""),
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Return the persisted suggestion bundle for a stored model."""
    ref = ArtifactRef(
        kind="semanticModels",
        workspace_id=workspace_id,
        item_id=item_id,
        workspace_name=workspace_name,
        item_name=item_name,
    )
    suggestions = container.artifact_service(tenant).load_suggestions(ref)
    return {"suggestions": [s.to_dict() for s in suggestions]}


@router.patch("/suggestions/status")
def patch_suggestion_status(
    body: SuggestionStatusRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Update one suggestion's lifecycle status (accept / skip / ...)."""
    ref = ArtifactRef(
        kind="semanticModels",
        workspace_id=body.workspace_id,
        item_id=body.item_id,
        workspace_name=body.workspace_name,
        item_name=body.item_name,
    )
    updated = container.artifact_service(tenant).update_suggestion_status(
        ref, body.suggestion_id, body.status
    )
    return {"suggestion": updated.to_dict()}


@router.post("/dax/generate")
def generate_dax(
    body: GenerateDaxMeasureRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Generate a candidate DAX measure for a natural-language intent."""
    model_spec = SemanticModelSpec.from_dict(body.spec)
    generated = container.model_service(tenant).generate_dax_measure(
        model_spec, body.intent, sample_rows=body.sample_rows or None
    )
    return generated.to_dict()


@router.post("/ask")
def ask_model(
    body: AskModelRequest,
    container: ServiceContainer = Depends(get_container),
    tenant: TenantContext = Depends(get_tenant),
) -> dict:
    """Answer a natural-language question against a live semantic model.

    Translates the question to DAX (grounded in the model's TMDL spec) and
    executes it via the official Power BI Modeling MCP server's
    ``dax_query_operations`` tool. Returns the generated DAX, the result
    table, and the full MCP tool transcript.
    """
    from app.intelligence import nl_to_dax
    from app.intelligence.nl_to_dax import NlToDaxError
    from app.intelligence.pbi_modeling_mcp import (
        PowerBiModelingMcpError,
        build_client,
    )

    model_service = container.model_service(tenant)

    # 1. Make sure we have a parsed spec for the model. Prefer the locally
    #    cached definition (persisted by an earlier ``/import`` call) so we
    #    don't hit Fabric on every question. When nothing is cached, ask the
    #    caller to import the model explicitly — that way DAX is built
    #    against a definition the user has reviewed. Callers can opt into
    #    on-the-fly import via ``auto_import`` or force a refresh via
    #    ``refresh``.
    spec: SemanticModelSpec | None = None
    imported_now = False
    if not body.refresh:
        spec = model_service.get_cached_spec(
            body.workspace_id,
            body.model_id,
            workspace_name=body.workspace_name,
            item_name=body.model_name,
        )
    if spec is None:
        if not (body.auto_import or body.refresh):
            raise ValidationError(
                "The semantic model definition has not been imported yet. "
                f"POST /api/v1/semantic-models/{body.workspace_id}/{body.model_id}"
                "/import first, or re-send this request with "
                "\"auto_import\": true to fetch it on the fly."
            )
        imported = model_service.import_model(
            body.workspace_id,
            body.model_id,
            fmt="TMDL",
            workspace_name=body.workspace_name,
            item_name=body.model_name,
        )
        spec = imported["spec"]
        imported_now = True

    # 2. Translate the question to DAX deterministically (offline, grounded).
    #    ``NlToDaxError`` means the question doesn't reference anything in the
    #    model — surface that to the caller as a 422 with a helpful hint via
    #    the shared service-error handler.
    try:
        translation = nl_to_dax(body.question, spec, default_topn=body.top_n)
    except NlToDaxError as exc:
        raise ValidationError(
            f"{exc} Available tables: "
            + ", ".join(t.name for t in spec.tables[:20])
            + ("…" if len(spec.tables) > 20 else "")
        ) from exc

    # 3. Execute via the official Power BI Modeling MCP server.
    try:
        with build_client() as client:
            client.connect_to_fabric_model(
                workspace=body.workspace_name,
                semantic_model=body.model_name,
            )
            execution = client.execute_dax(translation.dax)
    except PowerBiModelingMcpError as exc:
        return {
            "question": body.question,
            "translation": {
                "dax": translation.dax,
                "explanation": translation.explanation,
                "intent": translation.intent,
                "confidence": translation.confidence,
                "referenced_tables": translation.referenced_tables,
                "referenced_columns": translation.referenced_columns,
                "referenced_measures": translation.referenced_measures,
                "warnings": translation.warnings,
            },
            "executed": False,
            "error": str(exc),
            "calls": [],
            "imported_now": imported_now,
        }

    return {
        "question": body.question,
        "translation": {
            "dax": translation.dax,
            "explanation": translation.explanation,
            "intent": translation.intent,
            "confidence": translation.confidence,
            "referenced_tables": translation.referenced_tables,
            "referenced_columns": translation.referenced_columns,
            "referenced_measures": translation.referenced_measures,
            "warnings": translation.warnings,
        },
        "executed": True,
        "columns": execution.columns,
        "rows": execution.rows,
        "calls": [c.to_dict() for c in execution.calls],
        "imported_now": imported_now,
    }
