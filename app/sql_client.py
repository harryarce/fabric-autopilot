"""Connect to a Fabric SQL endpoint and extract detailed schemas.

Connections use the ODBC Driver 18 for SQL Server with Azure AD access-token
authentication (``SQL_COPT_SS_ACCESS_TOKEN``). The token is acquired for the
``https://database.windows.net`` audience via the shared
:class:`~app.auth.TokenProvider`, so no passwords are ever handled.
"""

from __future__ import annotations

import struct
from contextlib import contextmanager
from dataclasses import dataclass, field

from .auth import AuthError, TokenProvider

try:  # pyodbc is optional at import time so the module can be inspected without it
    import pyodbc
except ImportError:  # pragma: no cover - exercised only without the driver
    pyodbc = None  # type: ignore[assignment]

# Connection attribute id for "access token" as defined by msodbcsql.
_SQL_COPT_SS_ACCESS_TOKEN = 1256

ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
CONNECT_TIMEOUT_SECONDS = 30


class SqlClientError(RuntimeError):
    """Raised for connection or query problems against a SQL endpoint."""


class SqlDriverMissingError(SqlClientError):
    """Raised only when the ODBC driver (or pyodbc) is not installed."""


@dataclass(frozen=True)
class ColumnSchema:
    """Detailed metadata for a single column."""

    name: str
    ordinal: int
    data_type: str
    max_length: int | None
    precision: int | None
    scale: int | None
    is_nullable: bool
    default: str | None
    is_primary_key: bool = False

    @property
    def type_display(self) -> str:
        """A friendly type string such as ``varchar(255)`` or ``decimal(18,2)``."""
        t = self.data_type.lower()
        if self.max_length is not None and t in {
            "varchar",
            "nvarchar",
            "char",
            "nchar",
            "varbinary",
            "binary",
        }:
            length = "max" if self.max_length in (-1, None) else self.max_length
            return f"{self.data_type}({length})"
        if self.precision is not None and t in {"decimal", "numeric"}:
            return f"{self.data_type}({self.precision},{self.scale or 0})"
        return self.data_type


@dataclass(frozen=True)
class ForeignKey:
    """A single foreign-key relationship from one column to another."""

    column: str
    references_schema: str
    references_table: str
    references_column: str
    constraint_name: str

    @property
    def references_full(self) -> str:
        return f"{self.references_schema}.{self.references_table}.{self.references_column}"


@dataclass(frozen=True)
class TableSchema:
    """Schema for a table or view, including all of its columns."""

    schema: str
    name: str
    object_type: str  # "TABLE" or "VIEW"
    columns: list[ColumnSchema] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}"


def _encode_access_token(token: str) -> bytes:
    """Pack an Azure AD token the way msodbcsql expects (UTF-16-LE + length)."""
    token_bytes = token.encode("utf-16-le")
    return struct.pack("<i", len(token_bytes)) + token_bytes


def _is_login_failure(exc: "pyodbc.Error") -> bool:
    """True when an ODBC error is an Azure AD login failure (SQLSTATE 28000)."""
    message = str(exc)
    return "28000" in message or "18456" in message


class SqlEndpointClient:
    """Opens token-authenticated connections to a Fabric SQL endpoint."""

    def __init__(self, token_provider: TokenProvider) -> None:
        self._tokens = token_provider

    @contextmanager
    def connect(self, server: str, database: str):
        """Yield an open ``pyodbc`` connection, closing it on exit."""
        if pyodbc is None:
            raise SqlDriverMissingError(
                "pyodbc is not installed. Install it and the 'ODBC Driver 18 for "
                "SQL Server' to connect to Fabric SQL endpoints."
            )

        if ODBC_DRIVER not in pyodbc.drivers():
            raise SqlDriverMissingError(
                f"The '{ODBC_DRIVER}' is not installed. Available drivers: "
                f"{', '.join(pyodbc.drivers()) or 'none'}."
            )

        connection_string = (
            f"Driver={{{ODBC_DRIVER}}};"
            f"Server={server};Database={database};"
            "Encrypt=yes;TrustServerCertificate=no;"
            f"Connection Timeout={CONNECT_TIMEOUT_SECONDS};"
        )
        conn = self._open(connection_string, server, database)
        try:
            yield conn
        finally:
            conn.close()

    def _open(self, connection_string: str, server: str, database: str):
        """Open a connection, retrying with the Azure CLI identity on auth failure.

        ``DefaultAzureCredential`` may issue a valid token for an identity the
        warehouse rejects (login failure 18456) — for example an
        ``EnvironmentCredential`` service principal picked up from
        ``AZURE_CLIENT_*`` env vars that has no access (or is in another
        tenant). In that case we retry once with the signed-in Azure CLI user,
        the same Entra identity the original interactive app used.
        """
        try:
            return pyodbc.connect(
                connection_string,
                attrs_before={
                    _SQL_COPT_SS_ACCESS_TOKEN: _encode_access_token(
                        self._tokens.sql_token()
                    )
                },
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except pyodbc.Error as exc:  # pragma: no cover - environment specific
            if not _is_login_failure(exc):
                raise SqlClientError(
                    f"Could not connect to {server}/{database}: {exc}"
                ) from exc
            try:
                fallback_token = self._tokens.sql_token(use_fallback=True)
            except AuthError:
                raise SqlClientError(
                    f"Could not connect to {server}/{database}: {exc}"
                ) from exc
            try:
                return pyodbc.connect(
                    connection_string,
                    attrs_before={
                        _SQL_COPT_SS_ACCESS_TOKEN: _encode_access_token(
                            fallback_token
                        )
                    },
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except pyodbc.Error as retry_exc:  # pragma: no cover - env specific
                raise SqlClientError(
                    f"Could not connect to {server}/{database}: {retry_exc}"
                ) from retry_exc

    def test_connection(self, server: str, database: str) -> str:
        """Open a connection and return the server's @@VERSION string."""
        with self.connect(server, database) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT @@VERSION;")
            row = cursor.fetchone()
            return row[0] if row else "(no version returned)"

    def get_schemas(self, server: str, database: str) -> list[TableSchema]:
        """Return detailed schemas for every table and view in the database."""
        with self.connect(server, database) as conn:
            cursor = conn.cursor()
            objects = self._fetch_objects(cursor)
            columns_by_object = self._fetch_columns(cursor)
            primary_keys = self._fetch_primary_keys(cursor)
            foreign_keys = self._fetch_foreign_keys(cursor)

        tables: list[TableSchema] = []
        for (schema, name), object_type in objects.items():
            raw_columns = columns_by_object.get((schema, name), [])
            pk_cols = primary_keys.get((schema, name), set())
            columns = [
                ColumnSchema(
                    name=col["name"],
                    ordinal=col["ordinal"],
                    data_type=col["data_type"],
                    max_length=col["max_length"],
                    precision=col["precision"],
                    scale=col["scale"],
                    is_nullable=col["is_nullable"],
                    default=col["default"],
                    is_primary_key=col["name"] in pk_cols,
                )
                for col in raw_columns
            ]
            tables.append(
                TableSchema(
                    schema=schema,
                    name=name,
                    object_type=object_type,
                    columns=columns,
                    foreign_keys=foreign_keys.get((schema, name), []),
                )
            )

        tables.sort(key=lambda t: (t.object_type, t.full_name.casefold()))
        return tables

    # -- internal query helpers -------------------------------------------

    @staticmethod
    def _fetch_objects(cursor) -> dict[tuple[str, str], str]:
        cursor.execute(
            """
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW')
            ORDER BY TABLE_SCHEMA, TABLE_NAME;
            """
        )
        result: dict[tuple[str, str], str] = {}
        for schema, name, table_type in cursor.fetchall():
            kind = "VIEW" if table_type == "VIEW" else "TABLE"
            result[(schema, name)] = kind
        return result

    @staticmethod
    def _fetch_columns(cursor) -> dict[tuple[str, str], list[dict]]:
        cursor.execute(
            """
            SELECT
                TABLE_SCHEMA,
                TABLE_NAME,
                COLUMN_NAME,
                ORDINAL_POSITION,
                DATA_TYPE,
                CHARACTER_MAXIMUM_LENGTH,
                NUMERIC_PRECISION,
                NUMERIC_SCALE,
                IS_NULLABLE,
                COLUMN_DEFAULT
            FROM INFORMATION_SCHEMA.COLUMNS
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
            """
        )
        result: dict[tuple[str, str], list[dict]] = {}
        for row in cursor.fetchall():
            key = (row[0], row[1])
            result.setdefault(key, []).append(
                {
                    "name": row[2],
                    "ordinal": row[3],
                    "data_type": row[4],
                    "max_length": row[5],
                    "precision": row[6],
                    "scale": row[7],
                    "is_nullable": row[8] == "YES",
                    "default": row[9],
                }
            )
        return result

    @staticmethod
    def _fetch_primary_keys(cursor) -> dict[tuple[str, str], set[str]]:
        cursor.execute(
            """
            SELECT
                kcu.TABLE_SCHEMA,
                kcu.TABLE_NAME,
                kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS kcu
                ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY';
            """
        )
        result: dict[tuple[str, str], set[str]] = {}
        for schema, name, column in cursor.fetchall():
            result.setdefault((schema, name), set()).add(column)
        return result

    @staticmethod
    def _fetch_foreign_keys(cursor) -> dict[tuple[str, str], list[ForeignKey]]:
        cursor.execute(
            """
            SELECT
                fk_schema.name  AS fk_schema,
                fk_table.name   AS fk_table,
                fk_col.name     AS fk_column,
                ref_schema.name AS ref_schema,
                ref_table.name  AS ref_table,
                ref_col.name    AS ref_column,
                fk.name         AS constraint_name
            FROM sys.foreign_keys AS fk
            JOIN sys.foreign_key_columns AS fkc
                ON fk.object_id = fkc.constraint_object_id
            JOIN sys.tables  AS fk_table   ON fkc.parent_object_id = fk_table.object_id
            JOIN sys.schemas AS fk_schema  ON fk_table.schema_id = fk_schema.schema_id
            JOIN sys.columns AS fk_col
                ON fkc.parent_object_id = fk_col.object_id
                AND fkc.parent_column_id = fk_col.column_id
            JOIN sys.tables  AS ref_table  ON fkc.referenced_object_id = ref_table.object_id
            JOIN sys.schemas AS ref_schema ON ref_table.schema_id = ref_schema.schema_id
            JOIN sys.columns AS ref_col
                ON fkc.referenced_object_id = ref_col.object_id
                AND fkc.referenced_column_id = ref_col.column_id
            ORDER BY fk_schema, fk_table;
            """
        )
        result: dict[tuple[str, str], list[ForeignKey]] = {}
        for row in cursor.fetchall():
            key = (row[0], row[1])
            result.setdefault(key, []).append(
                ForeignKey(
                    column=row[2],
                    references_schema=row[3],
                    references_table=row[4],
                    references_column=row[5],
                    constraint_name=row[6],
                )
            )
        return result


def available_odbc_drivers() -> list[str]:
    """Return installed ODBC drivers, or an empty list when pyodbc is missing."""
    if pyodbc is None:
        return []
    return list(pyodbc.drivers())
