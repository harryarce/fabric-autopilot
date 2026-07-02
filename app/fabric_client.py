"""Thin client for the Microsoft Fabric REST API.

This module wraps the handful of Fabric endpoints the UI needs:

* List the workspaces the signed-in user can access.
* Enumerate the SQL-capable items inside a workspace (SQL analytics
  endpoints, Warehouses and Lakehouses) together with their connection
  strings and the SQL database name to connect to.

Reference docs:
https://learn.microsoft.com/rest/api/fabric/
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .auth import AuthError, TokenProvider

logger = logging.getLogger("fabric.client")

FABRIC_BASE_URL = "https://api.fabric.microsoft.com/v1"
# Base of the Fabric portal used to build shareable deep links to created items.
FABRIC_PORTAL_BASE_URL = "https://app.fabric.microsoft.com"
# Maps a Fabric item ``type`` to its portal route segment for deep links.
_PORTAL_ROUTE_SEGMENTS = {
    "SemanticModel": "datasets",
    "Report": "reports",
    "Lakehouse": "lakehouses",
    "Warehouse": "warehouses",
}
REQUEST_TIMEOUT_SECONDS = 60
# Cap on how long we poll a long-running create operation before giving up.
LRO_MAX_WAIT_SECONDS = 180
# Fallback cool-off when Fabric throttles us but gives no machine-readable hint.
DEFAULT_THROTTLE_RETRY_SECONDS = 60


def _env_float(name: str, default: float) -> float:
    """Read a non-negative float from the environment, falling back on default."""
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# Proactive client-side pacing for *mutating* (POST/PUT/DELETE) Fabric calls.
# Fabric enforces per-principal write limits and answers a burst of
# create/update operations — e.g. the chained semantic-model + report publish,
# or several semantic-model creates in a row — with HTTP 429. Spacing
# successive writes by a minimum interval keeps us under that limit so we rarely
# trip the reactive 429 block below. Tunable per tenant via the env var.
MIN_WRITE_INTERVAL_SECONDS = _env_float("FABRIC_MIN_WRITE_INTERVAL_SECONDS", 1.0)
# How long list responses (workspaces / items / data sources) are reused before
# re-fetching. These collections change rarely but are polled on every UI
# re-render (each Streamlit interaction is a full script rerun), so a short TTL
# collapses that request storm — the main trigger for throttling.  Tunable per
# tenant via the env var: bump it up on chatty UIs, drop to 0 to disable in
# tests.
LIST_CACHE_TTL_SECONDS = _env_float("FABRIC_LIST_CACHE_TTL_SECONDS", 30.0)
# Connection-level retries for transient TLS / network failures. The Fabric
# long-running-operation poller follows a regional redirect host (e.g.
# ``wabi-*-redirect.analysis.windows.net``) that intermittently drops the TLS
# connection mid-poll, surfacing as ``SSLEOFError`` / "Max retries exceeded".
# These retries are scoped to idempotent methods (GET et al., NOT POST) so a
# create call is never silently re-submitted; a dropped connection on a POST
# still fails fast.
TRANSIENT_RETRY_TOTAL = 4
TRANSIENT_RETRY_BACKOFF_SECONDS = 1.0

# Matches the timestamp Fabric embeds in a ``RequestBlocked`` message body, e.g.
# ``Request is blocked by the upstream service until: 6/14/2026 3:56:57 PM (UTC)``
_BLOCKED_UNTIL_RE = re.compile(r"until:\s*(.+?)\s*\(UTC\)", re.IGNORECASE)


def fabric_item_web_url(
    workspace_id: str | None,
    item_id: str | None,
    item_type: str | None,
) -> str | None:
    """Build a shareable Fabric portal deep link to a created item.

    Returns ``None`` when the workspace or item id is missing (e.g. an
    ``Accepted`` create that never surfaced an id), so callers can treat the
    link as best-effort.
    """
    if not workspace_id or not item_id:
        return None
    segment = _PORTAL_ROUTE_SEGMENTS.get(item_type or "", "items")
    return f"{FABRIC_PORTAL_BASE_URL}/groups/{workspace_id}/{segment}/{item_id}"


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) to seconds."""
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))


def _parse_blocked_until(body: str | None) -> datetime | None:
    """Extract the UTC unblock timestamp from a ``RequestBlocked`` message body."""
    match = _BLOCKED_UNTIL_RE.search(body or "")
    if not match:
        return None
    raw = match.group(1).strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def decode_definition_parts(parts: list[dict[str, Any]]) -> dict[str, str]:
    """Decode a Fabric ``definition.parts`` array into a ``{path: text}`` map.

    This is the inverse of
    :attr:`app.intelligence.definition.SemanticModelDefinition.parts`. Only the
    ``InlineBase64`` payload type is understood; parts with any other payload
    type are skipped (their content is not inline and cannot be materialised
    here). Decoding uses UTF-8 with ``errors="replace"`` so a single malformed
    part never aborts an otherwise usable import.
    """
    files: dict[str, str] = {}
    for part in parts:
        path = part.get("path")
        if not path:
            continue
        if part.get("payloadType") != "InlineBase64":
            continue
        payload = part.get("payload") or ""
        try:
            raw = base64.b64decode(payload)
        except (ValueError, TypeError):
            continue
        files[path] = raw.decode("utf-8", errors="replace")
    return files


class FabricApiError(RuntimeError):
    """Raised when a Fabric REST call returns a non-success status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Fabric API request failed ({status_code}): {message}")
        self.status_code = status_code
        self.message = message


class FabricThrottledError(FabricApiError):
    """Raised when Fabric rate-limits or blocks the caller (HTTP 429).

    Fabric returns ``RequestBlocked`` with an ``isRetriable`` flag and, in the
    message body, the UTC timestamp the block expires. This carries enough
    context for the API layer to emit a ``Retry-After`` response and for the
    client itself to short-circuit further calls until the window passes.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        retry_after_seconds: int,
        blocked_until: datetime | None = None,
    ) -> None:
        super().__init__(status_code, message)
        self.retry_after_seconds = retry_after_seconds
        self.blocked_until = blocked_until


@dataclass(frozen=True)
class Workspace:
    """A Fabric workspace the user can access."""

    id: str
    name: str
    type: str | None = None
    capacity_id: str | None = None
    description: str | None = None

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Workspace":
        return cls(
            id=data.get("id", ""),
            name=data.get("displayName") or data.get("name") or "(unnamed)",
            type=data.get("type"),
            capacity_id=data.get("capacityId"),
            description=data.get("description"),
        )


@dataclass(frozen=True)
class SqlEndpoint:
    """A SQL-queryable Fabric item (analytics endpoint / warehouse / lakehouse)."""

    id: str
    name: str
    item_kind: str  # "Warehouse", "Lakehouse" or "SQLEndpoint"
    server: str  # fully qualified SQL endpoint host
    database: str  # database (item) name to target
    workspace_id: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def connection_string(self) -> str:
        """An ODBC-style connection string for ad-hoc tooling / display."""
        return (
            f"Driver={{ODBC Driver 18 for SQL Server}};"
            f"Server={self.server};Database={self.database};"
            "Encrypt=yes;TrustServerCertificate=no;"
        )


@dataclass(frozen=True)
class Lakehouse:
    """A Fabric Lakehouse item with its OneLake and SQL endpoint coordinates.

    Direct Lake semantic models bind to a lakehouse's Delta tables in OneLake,
    so we carry the OneLake ``Tables``/``Files`` paths alongside the SQL
    analytics endpoint that is used to discover column-level schema (the
    Lakehouse REST surface does not expose columns).
    """

    id: str
    name: str
    workspace_id: str
    description: str | None = None
    default_schema: str | None = None
    onelake_tables_path: str | None = None
    onelake_files_path: str | None = None
    sql_endpoint_server: str | None = None
    sql_endpoint_id: str | None = None
    sql_endpoint_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, data: dict[str, Any], *, workspace_id: str) -> "Lakehouse":
        props = data.get("properties", {}) or {}
        sql_props = props.get("sqlEndpointProperties", {}) or {}
        return cls(
            id=data.get("id", ""),
            name=data.get("displayName") or data.get("name") or "(lakehouse)",
            workspace_id=data.get("workspaceId") or workspace_id,
            description=data.get("description"),
            default_schema=props.get("defaultSchema"),
            onelake_tables_path=props.get("oneLakeTablesPath"),
            onelake_files_path=props.get("oneLakeFilesPath"),
            sql_endpoint_server=sql_props.get("connectionString"),
            sql_endpoint_id=sql_props.get("id"),
            sql_endpoint_status=sql_props.get("provisioningStatus"),
            raw=data,
        )

    @property
    def is_schema_enabled(self) -> bool:
        """True when the lakehouse uses schemas (``defaultSchema`` is present)."""
        return bool(self.default_schema)

    @property
    def sql_connection_string(self) -> str | None:
        """ODBC connection string for the lakehouse SQL analytics endpoint."""
        if not self.sql_endpoint_server:
            return None
        return (
            f"Driver={{ODBC Driver 18 for SQL Server}};"
            f"Server={self.sql_endpoint_server};Database={self.name};"
            "Encrypt=yes;TrustServerCertificate=no;"
        )


@dataclass(frozen=True)
class LakehouseTable:
    """A Delta table inside a lakehouse, as listed by the Tables API."""

    name: str
    table_type: str  # "Managed" or "External"
    location: str | None = None
    table_format: str | None = None  # e.g. "Delta"

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "LakehouseTable":
        return cls(
            name=data.get("name", ""),
            table_type=data.get("type") or "Managed",
            location=data.get("location"),
            table_format=data.get("format"),
        )


@dataclass(frozen=True)
class CreatedItem:
    """The outcome of creating a Fabric item (e.g. a semantic model)."""

    id: str | None
    display_name: str
    workspace_id: str
    type: str | None = None
    # ``Created`` (201 sync), ``Succeeded`` (LRO terminal), or ``Accepted``
    # (202 returned without a pollable Location header).
    status: str = "Created"
    # Shareable Fabric portal deep link to the created item (when resolvable).
    web_url: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in ("Created", "Succeeded")


@dataclass(frozen=True)
class FabricItem:
    """A listed Fabric item (semantic model or report) inside a workspace.

    Shared shape for the ``list-semantic-models`` and ``list-reports``
    responses, which return the same envelope (``id``, ``displayName``,
    ``description``, ``type``, ``workspaceId``).
    """

    id: str
    display_name: str
    workspace_id: str
    type: str
    description: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(
        cls, data: dict[str, Any], *, workspace_id: str, item_type: str
    ) -> "FabricItem":
        return cls(
            id=data.get("id", ""),
            display_name=data.get("displayName") or data.get("name") or "(unnamed)",
            workspace_id=data.get("workspaceId") or workspace_id,
            type=data.get("type") or item_type,
            description=data.get("description"),
            raw=data,
        )


@dataclass(frozen=True)
class ItemDefinition:
    """A fetched item public definition: format plus decoded part files.

    ``files`` is the decoded ``{relative_path: text}`` map (the inverse of the
    base64 ``parts`` that were posted to create the item), ready to be parsed,
    persisted to the artifact store, or re-rendered.
    """

    format: str | None
    files: dict[str, str]
    raw_parts: list[dict[str, Any]] = field(default_factory=list, repr=False)


class FabricClient:
    """Stateless-ish wrapper around the Fabric REST API.

    A :class:`requests.Session` is reused for connection pooling. The bearer
    token is injected per request from the shared :class:`TokenProvider`, so
    token refresh is transparent to callers.
    """

    def __init__(self, token_provider: TokenProvider) -> None:
        self._tokens = token_provider
        self._session = requests.Session()
        # Retry transient connection / TLS failures (e.g. ``SSLEOFError`` from a
        # Fabric LRO redirect host dropping the connection mid-poll). ``status=0``
        # leaves HTTP status handling (401/403 fallback, 429 throttling) to the
        # explicit logic below; only connection/read errors are retried, and only
        # for idempotent methods so a POST create is never re-sent.
        retry = Retry(
            total=TRANSIENT_RETRY_TOTAL,
            connect=TRANSIENT_RETRY_TOTAL,
            read=TRANSIENT_RETRY_TOTAL,
            status=0,
            backoff_factor=TRANSIENT_RETRY_BACKOFF_SECONDS,
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        # When the primary identity's token is rejected by Fabric (401/403) we
        # transparently retry with the Azure CLI credential and remember that
        # choice for the remainder of this client's life.
        self._use_fallback = False
        # When Fabric blocks us (HTTP 429 / RequestBlocked) we remember when the
        # block expires and fail fast locally until then, instead of hammering
        # the throttled endpoint and prolonging the block.
        self._blocked_until: datetime | None = None
        # Monotonic timestamp of the last *mutating* request, used to space out
        # writes (see ``_pace_write``) so a burst of creates/updates stays under
        # Fabric's per-principal write limit and rarely triggers a 429.
        self._last_write_monotonic: float | None = None
        # Short-lived cache of list responses, keyed by a logical name, holding
        # ``(expiry_monotonic, value)``. Guards against the per-render request
        # storm from the Streamlit UI.
        self._list_cache: dict[str, tuple[float, Any]] = {}

    # -- low level helpers -------------------------------------------------

    def _cache_get(self, key: str) -> Any | None:
        """Return a non-expired cached value for ``key`` or ``None``."""
        entry = self._list_cache.get(key)
        if entry is None:
            return None
        expiry, value = entry
        if time.monotonic() >= expiry:
            self._list_cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        self._list_cache[key] = (time.monotonic() + LIST_CACHE_TTL_SECONDS, value)

    def _cache_invalidate(self, *keys: str) -> None:
        for key in keys:
            self._list_cache.pop(key, None)

    def _raise_if_blocked(self) -> None:
        """Fail fast while a previously observed Fabric block is still active."""
        if self._blocked_until is None:
            return
        now = datetime.now(timezone.utc)
        if now >= self._blocked_until:
            self._blocked_until = None
            return
        retry_after = max(1, int((self._blocked_until - now).total_seconds()))
        raise FabricThrottledError(
            429,
            f"Request is blocked by Fabric until {self._blocked_until.isoformat()}.",
            retry_after_seconds=retry_after,
            blocked_until=self._blocked_until,
        )

    def _pace_write(self) -> None:
        """Space out consecutive mutating calls to stay under Fabric write limits.

        Fabric answers bursty create/update traffic (the chained model + report
        publish, or several semantic-model creates in a loop) with HTTP 429.
        Enforcing a minimum gap between writes keeps us under the per-principal
        limit so the reactive 429 block above is rarely hit. This is a pure
        local sleep — it never issues a request — and is a no-op when the
        interval is configured to zero.
        """
        interval = MIN_WRITE_INTERVAL_SECONDS
        if interval <= 0:
            return
        last = self._last_write_monotonic
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                logger.debug("Pacing Fabric write: sleeping %.2fs", wait)
                time.sleep(wait)
        self._last_write_monotonic = time.monotonic()

    def _throttled_error(self, response: requests.Response) -> FabricThrottledError:
        """Record the block window from a 429 response and build the error."""
        blocked_until = _parse_blocked_until(response.text)
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        now = datetime.now(timezone.utc)
        if retry_after is None and blocked_until is not None:
            retry_after = max(1, int((blocked_until - now).total_seconds()))
        if retry_after is None:
            retry_after = DEFAULT_THROTTLE_RETRY_SECONDS
        if blocked_until is None:
            blocked_until = now + timedelta(seconds=retry_after)
        self._blocked_until = blocked_until
        return FabricThrottledError(
            response.status_code,
            response.text,
            retry_after_seconds=retry_after,
            blocked_until=blocked_until,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                f"Bearer {self._tokens.fabric_token(use_fallback=self._use_fallback)}"
            ),
            "Accept": "application/json",
        }

    def _fallback_headers(self) -> dict[str, str] | None:
        """Switch to the Azure CLI identity and return retry headers.

        Returns ``None`` (so the caller keeps the original 401/403 response)
        when the fallback is unavailable — either it has already been tried or
        the Azure CLI cannot issue a token (e.g. ``az login`` has not been run).
        The ``_use_fallback`` flag is only flipped once a fallback token is
        successfully acquired, so subsequent requests reuse the cached token.
        """
        if self._use_fallback:
            return None
        try:
            token = self._tokens.fabric_token(use_fallback=True)
        except AuthError:
            return None
        self._use_fallback = True
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self._raise_if_blocked()
        response = self._session.get(
            url,
            headers=self._headers(),
            params=params or {},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in (401, 403):
            retry_headers = self._fallback_headers()
            if retry_headers is not None:
                response = self._session.get(
                    url,
                    headers=retry_headers,
                    params=params or {},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
        if response.status_code == 429:
            raise self._throttled_error(response)
        if response.status_code != 200:
            raise FabricApiError(response.status_code, response.text)
        return response.json()

    def _post(self, url: str, body: dict[str, Any]) -> requests.Response:
        self._raise_if_blocked()
        self._pace_write()
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        response = self._session.post(
            url,
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in (401, 403):
            retry_headers = self._fallback_headers()
            if retry_headers is not None:
                retry_headers["Content-Type"] = "application/json"
                response = self._session.post(
                    url,
                    headers=retry_headers,
                    json=body,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
        if response.status_code == 429:
            raise self._throttled_error(response)
        if response.status_code not in (200, 201, 202):
            raise FabricApiError(response.status_code, response.text)
        return response

    def _wait_for_operation(
        self, response: requests.Response
    ) -> tuple[str, dict[str, Any] | None]:
        """Poll a long-running operation (HTTP 202) until it terminates.

        Implements the Fabric LRO pattern documented at
        https://learn.microsoft.com/rest/api/fabric/articles/long-running-operation:

        1. Use the ``Location`` header (or ``x-ms-operation-id``) to poll the
           Get Operation State endpoint with the ``Retry-After`` cadence.
        2. The only terminal statuses are ``Succeeded`` and ``Failed``.
        3. On ``Succeeded``, fetch the result from
           ``/v1/operations/{id}/result`` to get the created item details.

        Returns ``(status, result_body)`` where ``result_body`` is the item
        payload when one is available.
        """
        operation_url = response.headers.get("Location")
        operation_id = response.headers.get("x-ms-operation-id")
        if not operation_url and operation_id:
            operation_url = f"{FABRIC_BASE_URL}/operations/{operation_id}"
        if not operation_url:
            # No way to poll — treat the 202 as in-flight and return early.
            return "Accepted", None

        deadline = time.monotonic() + LRO_MAX_WAIT_SECONDS
        retry_after = int(response.headers.get("Retry-After", "5") or "5")
        while True:
            if time.monotonic() > deadline:
                raise FabricApiError(
                    408, "Timed out waiting for the Fabric operation to complete."
                )
            time.sleep(max(1, min(retry_after, 15)))
            poll = self._session.get(
                operation_url,
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if poll.status_code not in (200, 202):
                raise FabricApiError(poll.status_code, poll.text)
            payload = poll.json() if poll.content else {}
            status = payload.get("status", "")
            if status == "Succeeded":
                # Fetch the result body when the operation produced one.
                result_body: dict[str, Any] | None = None
                if operation_id is None:
                    # Derive the id from the polling URL's last segment.
                    operation_id = operation_url.rstrip("/").rsplit("/", 1)[-1]
                result_url = f"{FABRIC_BASE_URL}/operations/{operation_id}/result"
                result_resp = self._session.get(
                    result_url,
                    headers=self._headers(),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                if result_resp.status_code == 200 and result_resp.content:
                    result_body = result_resp.json()
                return status, result_body
            if status == "Failed":
                error = payload.get("error") or {}
                raise FabricApiError(
                    500, error.get("message") or "Operation failed."
                )
            # Non-terminal: ``NotStarted`` or ``Running`` — keep polling.
            retry_after = int(
                poll.headers.get("Retry-After", str(retry_after)) or retry_after
            )

    def _paged(
        self, url: str, *, value_key: str, params: dict[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Iterate a paginated Fabric collection following continuation tokens."""
        base_params = dict(params or {})
        continuation: str | None = None
        while True:
            request_params = dict(base_params)
            if continuation:
                request_params["continuationToken"] = continuation
            payload = self._get(url, request_params)
            yield from payload.get(value_key, [])
            continuation = payload.get("continuationToken")
            if not continuation:
                break

    # -- workspaces --------------------------------------------------------

    def list_workspaces(self) -> list[Workspace]:
        """Return all workspaces the signed-in user can access, sorted by name."""
        cached = self._cache_get("workspaces")
        if cached is not None:
            return cached
        url = f"{FABRIC_BASE_URL}/workspaces"
        workspaces = [
            Workspace.from_payload(item)
            for item in self._paged(url, value_key="value")
        ]
        workspaces.sort(key=lambda w: w.name.casefold())
        self._cache_set("workspaces", workspaces)
        return workspaces

    # -- semantic models ---------------------------------------------------

    def create_semantic_model(
        self,
        workspace_id: str,
        display_name: str,
        definition: dict[str, Any],
        *,
        description: str | None = None,
    ) -> CreatedItem:
        """Create a semantic model in ``workspace_id`` from a public definition.

        ``definition`` must be a Fabric ``SemanticModelDefinition`` object, i.e.
        ``{"format": "TMDL"|"TMSL", "parts": [...]}`` such as the one produced by
        :meth:`app.intelligence.definition.SemanticModelDefinition.definition_payload`.

        Handles both the synchronous (201) and long-running (202) responses.
        """
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/semanticModels"
        body: dict[str, Any] = {
            "displayName": display_name,
            "definition": definition,
        }
        if description:
            body["description"] = description[:256]

        response = self._post(url, body)
        self._cache_invalidate(f"items:semanticModels:{workspace_id}")
        if response.status_code in (200, 201):
            data = response.json() if response.content else {}
            item_id = data.get("id")
            item_type = data.get("type", "SemanticModel")
            return CreatedItem(
                id=item_id,
                display_name=data.get("displayName", display_name),
                workspace_id=workspace_id,
                type=item_type,
                status="Created",
                web_url=fabric_item_web_url(workspace_id, item_id, item_type),
            )

        # 202 Accepted — poll the long-running operation to completion.
        status, result = self._wait_for_operation(response)
        result = result or {}
        item_id = result.get("id")
        item_workspace = result.get("workspaceId", workspace_id)
        item_type = result.get("type", "SemanticModel")
        return CreatedItem(
            id=item_id,
            display_name=result.get("displayName", display_name),
            workspace_id=item_workspace,
            type=item_type,
            status=status,
            web_url=fabric_item_web_url(item_workspace, item_id, item_type),
        )

    def list_semantic_models(self, workspace_id: str) -> list[FabricItem]:
        """List semantic models in a workspace, sorted by display name."""
        return self._list_items(workspace_id, "semanticModels", "SemanticModel")

    def get_semantic_model_definition(
        self, workspace_id: str, semantic_model_id: str, *, fmt: str = "TMDL"
    ) -> ItemDefinition:
        """Fetch a semantic model's public definition (TMDL or TMSL).

        Returns an :class:`ItemDefinition` with the part files already decoded.
        Handles both the synchronous (200) and long-running (202) responses.
        """
        return self._get_item_definition(
            workspace_id, "semanticModels", semantic_model_id, fmt=fmt
        )

    def update_semantic_model_definition(
        self,
        workspace_id: str,
        semantic_model_id: str,
        definition: dict[str, Any],
    ) -> str:
        """Replace a semantic model's definition (write-back). Returns status."""
        return self._update_item_definition(
            workspace_id, "semanticModels", semantic_model_id, definition
        )

    # -- reports -----------------------------------------------------------

    def list_reports(self, workspace_id: str) -> list[FabricItem]:
        """List reports in a workspace, sorted by display name."""
        return self._list_items(workspace_id, "reports", "Report")

    def get_report_definition(
        self, workspace_id: str, report_id: str, *, fmt: str | None = None
    ) -> ItemDefinition:
        """Fetch a report's public definition (PBIR parts), decoded."""
        return self._get_item_definition(
            workspace_id, "reports", report_id, fmt=fmt
        )

    def create_report(
        self,
        workspace_id: str,
        display_name: str,
        definition: dict[str, Any],
        *,
        description: str | None = None,
    ) -> CreatedItem:
        """Create a report in ``workspace_id`` from a public PBIR definition.

        ``definition`` must be a Fabric ``ReportDefinition`` object
        (``{"parts": [...]}``). Handles the synchronous (201) and long-running
        (202) responses.
        """
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/reports"
        body: dict[str, Any] = {
            "displayName": display_name,
            "definition": definition,
        }
        if description:
            body["description"] = description[:256]

        response = self._post(url, body)
        self._cache_invalidate(f"items:reports:{workspace_id}")
        if response.status_code in (200, 201):
            data = response.json() if response.content else {}
            item_id = data.get("id")
            item_type = data.get("type", "Report")
            return CreatedItem(
                id=item_id,
                display_name=data.get("displayName", display_name),
                workspace_id=workspace_id,
                type=item_type,
                status="Created",
                web_url=fabric_item_web_url(workspace_id, item_id, item_type),
            )

        status, result = self._wait_for_operation(response)
        result = result or {}
        item_id = result.get("id")
        item_workspace = result.get("workspaceId", workspace_id)
        item_type = result.get("type", "Report")
        return CreatedItem(
            id=item_id,
            display_name=result.get("displayName", display_name),
            workspace_id=item_workspace,
            type=item_type,
            status=status,
            web_url=fabric_item_web_url(item_workspace, item_id, item_type),
        )

    def update_report_definition(
        self,
        workspace_id: str,
        report_id: str,
        definition: dict[str, Any],
    ) -> str:
        """Replace a report's definition (write-back). Returns status."""
        return self._update_item_definition(
            workspace_id, "reports", report_id, definition
        )

    # -- item-definition helpers (shared across item types) ---------------

    def _list_items(
        self, workspace_id: str, collection: str, item_type: str
    ) -> list[FabricItem]:
        """List items of ``item_type`` from ``/workspaces/{id}/{collection}``."""
        cache_key = f"items:{collection}:{workspace_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/{collection}"
        items = [
            FabricItem.from_payload(
                payload, workspace_id=workspace_id, item_type=item_type
            )
            for payload in self._paged(url, value_key="value")
        ]
        items.sort(key=lambda it: it.display_name.casefold())
        self._cache_set(cache_key, items)
        return items

    def _get_item_definition(
        self,
        workspace_id: str,
        collection: str,
        item_id: str,
        *,
        fmt: str | None,
    ) -> ItemDefinition:
        """POST ``.../{id}/getDefinition`` and decode the returned parts.

        The Get*Definition endpoints are long-running: 200 returns the
        definition inline, 202 returns an operation to poll whose result body
        carries the definition.
        """
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/"
            f"{collection}/{item_id}/getDefinition"
        )
        if fmt:
            url = f"{url}?format={fmt}"
        response = self._post(url, {})
        if response.status_code == 200:
            body = response.json() if response.content else {}
        else:
            _status, result = self._wait_for_operation(response)
            body = result or {}
        definition = body.get("definition") or {}
        parts = definition.get("parts") or []
        return ItemDefinition(
            format=definition.get("format"),
            files=decode_definition_parts(parts),
            raw_parts=parts,
        )

    def _update_item_definition(
        self,
        workspace_id: str,
        collection: str,
        item_id: str,
        definition: dict[str, Any],
    ) -> str:
        """POST ``.../{id}/updateDefinition`` (write-back). Returns the status."""
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/"
            f"{collection}/{item_id}/updateDefinition"
        )
        response = self._post(url, {"definition": definition})
        if response.status_code in (200, 201):
            return "Updated"
        status, _result = self._wait_for_operation(response)
        return status

    # -- lakehouses --------------------------------------------------------

    def _lakehouses_payload(self, workspace_id: str) -> list[dict[str, Any]]:
        """Return (and cache) the raw ``List Lakehouses`` page payloads.

        ``list_lakehouses`` and the SQL-endpoint aggregator both need this same
        page. Caching it here means a single Fabric GET feeds both — a frequent
        Streamlit re-render pattern that previously issued the request twice.
        """
        cache_key = f"lakehouses_raw:{workspace_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/lakehouses"
        items = list(self._paged(url, value_key="value"))
        self._cache_set(cache_key, items)
        return items

    def list_lakehouses(self, workspace_id: str) -> list[Lakehouse]:
        """Return the lakehouses in ``workspace_id``, sorted by name.

        https://learn.microsoft.com/rest/api/fabric/lakehouse/items/list-lakehouses
        """
        cache_key = f"lakehouses:{workspace_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        lakehouses = [
            Lakehouse.from_payload(item, workspace_id=workspace_id)
            for item in self._lakehouses_payload(workspace_id)
        ]
        lakehouses.sort(key=lambda lh: lh.name.casefold())
        self._cache_set(cache_key, lakehouses)
        return lakehouses

    def get_lakehouse_definition(
        self, workspace_id: str, lakehouse_id: str, *, fmt: str | None = None
    ) -> ItemDefinition:
        """Fetch a lakehouse public definition (metadata, shortcuts, ALM, ...).

        https://learn.microsoft.com/rest/api/fabric/lakehouse/items/get-lakehouse-definition
        """
        return self._get_item_definition(
            workspace_id, "lakehouses", lakehouse_id, fmt=fmt
        )

    def list_lakehouse_tables(
        self, workspace_id: str, lakehouse_id: str
    ) -> list[LakehouseTable]:
        """List the Delta tables in a lakehouse.

        https://learn.microsoft.com/rest/api/fabric/lakehouse/tables/list-tables

        The Tables API is preview; if it is unavailable in the tenant the
        failure is swallowed and an empty list is returned so callers can fall
        back to SQL-endpoint discovery. Throttling (429) is *not* swallowed —
        masking it as "no tables" would silently misrepresent the data and the
        block window matters to upstream callers.
        """
        cache_key = f"lakehouse_tables:{workspace_id}:{lakehouse_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/"
            f"lakehouses/{lakehouse_id}/tables"
        )
        try:
            rows = list(self._paged(url, value_key="data"))
        except FabricThrottledError:
            raise
        except FabricApiError:
            return []
        tables = [LakehouseTable.from_payload(row) for row in rows]
        tables.sort(key=lambda t: t.name.casefold())
        self._cache_set(cache_key, tables)
        return tables

    # -- SQL endpoints -----------------------------------------------------

    def list_sql_endpoints(self, workspace_id: str) -> list[SqlEndpoint]:
        """Return every SQL-queryable item in a workspace with its endpoint.

        Combines Warehouses, Lakehouse SQL analytics endpoints and standalone
        SQL analytics endpoint items. Items without a resolvable connection
        string (for example a Lakehouse whose endpoint is still provisioning)
        are skipped. The aggregate is cached so a Streamlit rerun that picks a
        source twice does not re-issue all three underlying list calls.
        """
        cache_key = f"sql_endpoints:{workspace_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        endpoints: list[SqlEndpoint] = []
        endpoints.extend(self._list_warehouses(workspace_id))
        endpoints.extend(self._list_lakehouse_endpoints(workspace_id))
        endpoints.extend(self._list_sql_analytics_endpoints(workspace_id))

        # De-duplicate by (server, database) — a lakehouse and its analytics
        # endpoint can otherwise surface the same target twice.
        seen: set[tuple[str, str]] = set()
        unique: list[SqlEndpoint] = []
        for ep in endpoints:
            key = (ep.server.casefold(), ep.database.casefold())
            if key in seen:
                continue
            seen.add(key)
            unique.append(ep)

        unique.sort(key=lambda e: e.name.casefold())
        self._cache_set(cache_key, unique)
        return unique

    def _warehouses_payload(self, workspace_id: str) -> list[dict[str, Any]]:
        """Cached ``List Warehouses`` page payloads."""
        cache_key = f"warehouses_raw:{workspace_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/warehouses"
        items = list(self._paged(url, value_key="value"))
        self._cache_set(cache_key, items)
        return items

    def _list_warehouses(self, workspace_id: str) -> Iterator[SqlEndpoint]:
        for item in self._warehouses_payload(workspace_id):
            props = item.get("properties", {}) or {}
            server = props.get("connectionString")
            if not server:
                continue
            name = item.get("displayName") or "(warehouse)"
            yield SqlEndpoint(
                id=item.get("id", ""),
                name=name,
                item_kind="Warehouse",
                server=server,
                database=name,
                workspace_id=workspace_id,
                raw=item,
            )

    def _list_lakehouse_endpoints(self, workspace_id: str) -> Iterator[SqlEndpoint]:
        for item in self._lakehouses_payload(workspace_id):
            props = item.get("properties", {}) or {}
            sql_props = props.get("sqlEndpointProperties", {}) or {}
            server = sql_props.get("connectionString")
            if not server:
                continue
            name = item.get("displayName") or "(lakehouse)"
            yield SqlEndpoint(
                id=sql_props.get("id") or item.get("id", ""),
                name=name,
                item_kind="Lakehouse",
                server=server,
                database=name,
                workspace_id=workspace_id,
                raw=item,
            )

    def _sql_analytics_endpoints_payload(
        self, workspace_id: str
    ) -> list[dict[str, Any]] | None:
        """Cached ``List SQL Endpoints`` page payloads.

        Returns ``None`` when the preview API is unavailable in this tenant so
        the SQL-endpoint aggregator can treat it as "no extra endpoints"
        without losing the distinction from "empty list". 429s propagate.
        """
        cache_key = f"sql_endpoints_raw:{workspace_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/sqlEndpoints"
        try:
            items = list(self._paged(url, value_key="value"))
        except FabricThrottledError:
            raise
        except FabricApiError:
            return None
        self._cache_set(cache_key, items)
        return items

    def _list_sql_analytics_endpoints(
        self, workspace_id: str
    ) -> Iterator[SqlEndpoint]:
        """List standalone SQL analytics endpoint items, if the API exposes them.

        This endpoint is preview and may be unavailable in some tenants; a
        non-throttle failure is treated as "no extra endpoints" rather than
        fatal so warehouses/lakehouses still work. 429 is re-raised so the
        client block window is honored upstream instead of being hidden.
        """
        items = self._sql_analytics_endpoints_payload(workspace_id)
        if not items:
            return
        for item in items:
            props = item.get("properties", {}) or {}
            server = props.get("connectionString")
            if not server:
                continue
            name = item.get("displayName") or "(sql endpoint)"
            yield SqlEndpoint(
                id=item.get("id", ""),
                name=name,
                item_kind="SQLEndpoint",
                server=server,
                database=name,
                workspace_id=workspace_id,
                raw=item,
            )


    # ------------------------------------------------------------------
    # Phase 3: Lifecycle (Git integration + Deployment Pipelines)
    # ------------------------------------------------------------------

    def git_connect(
        self,
        workspace_id: str,
        *,
        provider: str,
        organization: str,
        project: str,
        repository: str,
        branch: str,
        directory: str = "/",
    ) -> dict[str, Any]:
        """Connect a workspace to a Git repository (Azure DevOps or GitHub)."""
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/git/connect"
        body = {
            "gitProviderDetails": {
                "organizationName": organization,
                "projectName": project,
                "repositoryName": repository,
                "branchName": branch,
                "directoryName": directory,
                "gitProviderType": provider,
            }
        }
        resp = self._post(url, body)
        return resp.json() if resp.content else {}

    def git_status(self, workspace_id: str) -> dict[str, Any]:
        """Return current Git connection status for the workspace."""
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/git/status"
        return self._get(url)

    def git_commit_to_repo(
        self,
        workspace_id: str,
        *,
        comment: str,
        items: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Commit workspace changes (optionally a subset of items) to Git."""
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/git/commitToGit"
        body: dict[str, Any] = {
            "mode": "Selective" if items else "All",
            "comment": comment,
        }
        if items:
            body["items"] = items
        resp = self._post(url, body)
        if resp.status_code == 202:
            return self._wait_for_operation(resp)
        return "Succeeded", (resp.json() if resp.content else None)

    def git_update_from_repo(
        self, workspace_id: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Pull from Git into the workspace (LRO)."""
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/git/updateFromGit"
        resp = self._post(url, {})
        if resp.status_code == 202:
            return self._wait_for_operation(resp)
        return "Succeeded", (resp.json() if resp.content else None)

    def list_deployment_pipelines(self) -> list[dict[str, Any]]:
        """List deployment pipelines visible to the platform identity."""
        url = f"{FABRIC_BASE_URL}/deploymentPipelines"
        return list(self._paged(url, value_key="value"))

    def deploy_to_stage(
        self,
        pipeline_id: str,
        *,
        source_stage_id: str,
        target_stage_id: str,
        note: str | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Deploy from one pipeline stage to the next (LRO)."""
        url = f"{FABRIC_BASE_URL}/deploymentPipelines/{pipeline_id}/deploy"
        body: dict[str, Any] = {
            "sourceStageId": source_stage_id,
            "targetStageId": target_stage_id,
        }
        if note:
            body["note"] = note
        if items:
            body["items"] = items
        resp = self._post(url, body)
        if resp.status_code == 202:
            return self._wait_for_operation(resp)
        return "Succeeded", (resp.json() if resp.content else None)

    # ------------------------------------------------------------------
    # Phase 3: Governance (permissions, sensitivity labels, tags)
    # ------------------------------------------------------------------

    def list_item_role_assignments(
        self, workspace_id: str, item_id: str
    ) -> list[dict[str, Any]]:
        """List role assignments for a single item."""
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/items/{item_id}"
            "/roleAssignments"
        )
        return list(self._paged(url, value_key="value"))

    def set_item_permissions(
        self,
        workspace_id: str,
        item_id: str,
        *,
        principal_id: str,
        principal_type: str,
        role: str,
    ) -> dict[str, Any]:
        """Grant a role on one item to a principal."""
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/items/{item_id}"
            "/roleAssignments"
        )
        body = {
            "principal": {"id": principal_id, "type": principal_type},
            "role": role,
        }
        resp = self._post(url, body)
        return resp.json() if resp.content else {}

    def apply_sensitivity_label(
        self,
        workspace_id: str,
        item_id: str,
        *,
        label_id: str,
        assignment_method: str = "Standard",
    ) -> dict[str, Any]:
        """Apply a Microsoft Information Protection sensitivity label to an item."""
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/items/{item_id}"
            "/sensitivityLabel"
        )
        body = {"labelId": label_id, "assignmentMethod": assignment_method}
        resp = self._post(url, body)
        return resp.json() if resp.content else {}

    def remove_sensitivity_label(
        self, workspace_id: str, item_id: str
    ) -> None:
        """Remove the sensitivity label from an item."""
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/items/{item_id}"
            "/sensitivityLabel"
        )
        headers = self._headers()
        self._raise_if_blocked()
        self._pace_write()
        resp = self._session.delete(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code not in (200, 202, 204):
            raise FabricApiError(resp.status_code, resp.text)

    def apply_tags(
        self, workspace_id: str, item_id: str, *, tag_ids: list[str]
    ) -> dict[str, Any]:
        """Apply one or more tags to an item."""
        url = (
            f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/items/{item_id}/tags"
        )
        body = {"tags": [{"id": tid} for tid in tag_ids]}
        resp = self._post(url, body)
        return resp.json() if resp.content else {}

    def list_workspace_role_assignments(
        self, workspace_id: str
    ) -> list[dict[str, Any]]:
        """List role assignments at the workspace scope."""
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/roleAssignments"
        return list(self._paged(url, value_key="value"))

    def add_workspace_role_assignment(
        self,
        workspace_id: str,
        *,
        principal_id: str,
        principal_type: str,
        role: str,
    ) -> dict[str, Any]:
        """Assign a workspace role (e.g. Admin/Member/Contributor/Viewer)."""
        url = f"{FABRIC_BASE_URL}/workspaces/{workspace_id}/roleAssignments"
        body = {
            "principal": {"id": principal_id, "type": principal_type},
            "role": role,
        }
        resp = self._post(url, body)
        return resp.json() if resp.content else {}
