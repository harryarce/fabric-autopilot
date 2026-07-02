"""Schema discovery & export service.

Wraps the Fabric REST client (to enumerate SQL endpoints / lakehouses) and the
ODBC SQL client (to extract detailed table/column/key metadata), plus the
deterministic exporters. This is the logic that previously lived inline in the
Streamlit *Schema Explorer* page.
"""

from __future__ import annotations

from app.exporters import ExportResult, export
from app.fabric_client import FabricClient, Lakehouse, SqlEndpoint
from app.sql_client import SqlDriverMissingError, SqlEndpointClient, TableSchema

from .errors import DependencyUnavailableError, NotFoundError, UpstreamError


class SchemaService:
    """Discover data sources and extract / export their schemas."""

    def __init__(
        self, fabric_client: FabricClient, sql_client: SqlEndpointClient
    ) -> None:
        self._fabric = fabric_client
        self._sql = sql_client

    # -- discovery --------------------------------------------------------

    def list_sql_endpoints(self, workspace_id: str) -> list[SqlEndpoint]:
        """List SQL analytics endpoints / warehouses in a workspace."""
        try:
            return self._fabric.list_sql_endpoints(workspace_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(
                f"Failed to list SQL endpoints: {exc}"
            ) from exc

    def list_lakehouses(self, workspace_id: str) -> list[Lakehouse]:
        """List lakehouses in a workspace."""
        try:
            return self._fabric.list_lakehouses(workspace_id)
        except Exception as exc:  # pragma: no cover - upstream
            raise UpstreamError(f"Failed to list lakehouses: {exc}") from exc

    # -- extraction -------------------------------------------------------

    def extract_schemas(self, server: str, database: str) -> list[TableSchema]:
        """Extract full table/column/key metadata from a SQL endpoint.

        Raises:
            DependencyUnavailableError: when the ODBC driver is not installed.
            UpstreamError: when the connection or query fails.
        """
        if not server or not database:
            raise NotFoundError("A server and database are required.")
        try:
            return self._sql.get_schemas(server, database)
        except SqlDriverMissingError as exc:
            raise DependencyUnavailableError(
                "The Microsoft ODBC Driver 18 for SQL Server is required to "
                "extract schemas. It is baked into the API container image; "
                "install it locally to run outside Azure.",
                details={"reason": str(exc)},
            ) from exc
        except Exception as exc:  # pragma: no cover - env/connection
            raise UpstreamError(f"Schema extraction failed: {exc}") from exc

    @staticmethod
    def filter_schemas(
        schemas: list[TableSchema], selected_tables: list[str] | None
    ) -> list[TableSchema]:
        """Restrict ``schemas`` to the ``schema.name`` identifiers selected.

        A ``None`` selection returns ``schemas`` unchanged (use every extracted
        object). Names are matched case-insensitively against ``"<schema>.<name>"``.
        This is the selection logic that previously lived inline in the API's
        design/suggest routes; centralising it keeps every surface consistent.
        """
        if selected_tables is None:
            return schemas
        wanted = {name.casefold() for name in selected_tables}
        return [
            s for s in schemas if f"{s.schema}.{s.name}".casefold() in wanted
        ]

    # -- export -----------------------------------------------------------

    def export_schemas(
        self, fmt: str, endpoint: SqlEndpoint, schemas: list[TableSchema]
    ) -> ExportResult:
        """Serialize ``schemas`` to ``markdown`` | ``json`` | ``sql``."""
        normalized = (fmt or "markdown").strip().lower()
        if normalized not in {"markdown", "json", "sql"}:
            raise NotFoundError(
                f"Unknown export format '{fmt}'. Use markdown, json, or sql."
            )
        return export(normalized, endpoint, schemas)
