"""Schema extraction and export routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.fabric_client import SqlEndpoint
from fabric_services import ServiceContainer

from ..dependencies import get_container
from ..models import ExportSchemaRequest, ExtractSchemaRequest, to_jsonable

router = APIRouter(prefix="/schemas", tags=["schemas"])


@router.post("/extract")
def extract_schemas(
    body: ExtractSchemaRequest,
    container: ServiceContainer = Depends(get_container),
) -> list[dict]:
    """Extract full table/column/key metadata from a SQL endpoint."""
    schemas = container.schema_service().extract_schemas(body.server, body.database)
    return [to_jsonable(s) for s in schemas]


@router.post("/export")
def export_schemas(
    body: ExportSchemaRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    """Extract then serialize schemas to markdown | json | sql for download."""
    service = container.schema_service()
    schemas = service.extract_schemas(body.server, body.database)
    endpoint = SqlEndpoint(
        id="",
        name=body.endpoint_name or body.database,
        item_kind="SQLEndpoint",
        server=body.server,
        database=body.database,
        workspace_id="",
    )
    result = service.export_schemas(body.format, endpoint, schemas)
    return to_jsonable(result)
