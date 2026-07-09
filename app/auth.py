"""Authentication helpers shared across the application.

Tokens are acquired with :class:`azure.identity.DefaultAzureCredential`, which
transparently selects the right identity for the environment:

* In Azure (Container Apps, App Service, AKS), the service's **user-assigned or
  system-assigned Managed Identity** is used — no secrets, no ``az login``.
* On a developer machine, it falls back to the Azure CLI (``az login``),
  Visual Studio, or environment credentials. If ``DefaultAzureCredential``
  itself fails to issue a token, the provider makes a second attempt with an
  explicit :class:`azure.identity.AzureCliCredential` before erroring.

If ``DefaultAzureCredential`` issues a token for an identity that Fabric then
rejects (for example an ``EnvironmentCredential`` service principal that is not
enabled for the Fabric APIs), callers can request a token explicitly from the
Azure CLI identity via ``get_token(..., use_fallback=True)``. The Fabric client
uses this to transparently retry a 401/403 with the signed-in CLI user.

The identity that should be preferred can be pinned with the
``AZURE_CLIENT_ID`` environment variable (the user-assigned Managed Identity's
client id in Azure). Two different audiences are used:

* The **Fabric** REST API (``https://api.fabric.microsoft.com``) to enumerate
  workspaces and items.
* The **SQL / database** audience (``https://database.windows.net``) to open
  connections against a Fabric SQL analytics endpoint or Warehouse.

Tokens are cached in-process and refreshed automatically a little before they
expire so callers never hand an expired token to a downstream call.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential, DefaultAzureCredential

# Audience used for the Fabric REST API. ``.default`` applies every statically
# assigned permission for the signed-in identity.
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

# Audience used when connecting to a Fabric SQL endpoint via ODBC.
SQL_SCOPE = "https://database.windows.net/.default"

# Audience used by the Power BI Modeling MCP server when it connects to a
# semantic model in a Fabric workspace via the XMLA endpoint. The MCP server
# accepts a pre-acquired token through the ``PBI_MODELING_MCP_ACCESS_TOKEN``
# environment variable so we never have to surface its interactive sign-in.
POWERBI_XMLA_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Refresh a token this many seconds before it actually expires.
_EXPIRY_SKEW_SECONDS = 300

# Seconds to wait for the Azure CLI (and other subprocess-based developer
# credentials) to respond. ``azure-identity`` defaults to 10 seconds, but a cold
# ``az account get-access-token`` invocation routinely takes 5-8 seconds and can
# exceed 10 seconds when several credentials in the ``DefaultAzureCredential``
# chain are probed concurrently at startup. When that happens the SDK logs a
# misleading "Failed to invoke the Azure CLI" warning even though the developer
# is signed in. A larger, configurable timeout removes that noise. Overridable
# via ``AZURE_CLI_PROCESS_TIMEOUT`` for constrained environments.
try:
    _CLI_PROCESS_TIMEOUT = int(os.environ.get("AZURE_CLI_PROCESS_TIMEOUT", "30"))
except ValueError:
    _CLI_PROCESS_TIMEOUT = 30

def cli_process_timeout() -> int:
    """Return the subprocess timeout (seconds) for CLI-based credentials.

    Shared by the synchronous :class:`TokenProvider` and the asynchronous
    ``DefaultAzureCredential`` instances used by the intelligence agents so a
    single ``AZURE_CLI_PROCESS_TIMEOUT`` override applies everywhere.
    """
    return _CLI_PROCESS_TIMEOUT


class AuthError(RuntimeError):
    """Raised when an access token cannot be acquired."""


@dataclass
class _CachedToken:
    token: str
    expires_on: int


class TokenProvider:
    """Thread-safe, caching provider of Azure AD access tokens.

    A :class:`DefaultAzureCredential` is used for every scope, with an explicit
    :class:`AzureCliCredential` fallback when it cannot issue a token. Each
    scope keeps its own cached token so the Fabric and SQL audiences are
    independent.
    """

    def __init__(self) -> None:
        # ``DefaultAzureCredential`` resolves a Managed Identity in Azure and
        # the Azure CLI locally. ``AZURE_CLIENT_ID`` pins a specific
        # user-assigned Managed Identity when several are available.
        managed_identity_client_id = os.environ.get("AZURE_CLIENT_ID") or None
        self._credential = DefaultAzureCredential(
            managed_identity_client_id=managed_identity_client_id,
            exclude_interactive_browser_credential=True,
            process_timeout=_CLI_PROCESS_TIMEOUT,
        )
        # Explicit fallback used only when the primary credential fails to
        # produce a token (e.g. a Managed Identity probe times out or returns
        # an unauthenticated token chain locally). Resolved lazily so machines
        # without the Azure CLI installed are not penalised at startup.
        self._fallback_credential = AzureCliCredential(
            process_timeout=_CLI_PROCESS_TIMEOUT,
        )
        self._cache: dict[str, _CachedToken] = {}
        self._lock = threading.Lock()

    def get_token(self, scope: str, *, use_fallback: bool = False) -> str:
        """Return a valid bearer token for ``scope``, refreshing if needed.

        When ``use_fallback`` is true the explicit
        :class:`~azure.identity.AzureCliCredential` is used directly instead of
        ``DefaultAzureCredential``. This is how callers recover when the
        primary credential issues a *valid* token for an identity that a
        downstream service (e.g. Fabric) rejects with 401/403 — for example
        when ``EnvironmentCredential`` picks up a service principal that is not
        enabled for Fabric. The fallback token is cached under a separate key so
        the two identities never clobber one another.
        """
        cache_key = f"{scope}#fallback" if use_fallback else scope
        now = int(time.time())
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_on - _EXPIRY_SKEW_SECONDS > now:
                return cached.token

            if use_fallback:
                try:
                    token = self._fallback_credential.get_token(scope)
                except ClientAuthenticationError as exc:  # pragma: no cover - env
                    raise AuthError(
                        "Failed to acquire an access token from the Azure CLI. "
                        "Run 'az login' and try again.\n"
                        f"AzureCliCredential error: {exc}"
                    ) from exc
            else:
                try:
                    token = self._credential.get_token(scope)
                except ClientAuthenticationError as primary_exc:  # pragma: no cover - env
                    # DefaultAzureCredential could not authenticate (common
                    # locally when only the Azure CLI is signed in). Fall back
                    # to an explicit AzureCliCredential before giving up.
                    try:
                        token = self._fallback_credential.get_token(scope)
                    except ClientAuthenticationError as fallback_exc:
                        raise AuthError(
                            "Failed to acquire an access token. In Azure, "
                            "ensure the service's Managed Identity is enabled "
                            "and has access; locally, run 'az login'.\n"
                            f"DefaultAzureCredential error: {primary_exc}\n"
                            f"AzureCliCredential error: {fallback_exc}"
                        ) from fallback_exc

            self._cache[cache_key] = _CachedToken(token.token, token.expires_on)
            return token.token

    def fabric_token(self, *, use_fallback: bool = False) -> str:
        """Convenience accessor for a Fabric API token."""
        return self.get_token(FABRIC_SCOPE, use_fallback=use_fallback)

    def sql_token(self, *, use_fallback: bool = False) -> str:
        """Convenience accessor for a SQL/database token."""
        return self.get_token(SQL_SCOPE, use_fallback=use_fallback)

    def powerbi_xmla_token(self, *, use_fallback: bool = False) -> str:
        """Convenience accessor for a Power BI XMLA token.

        Used to hand a pre-acquired access token to the
        ``@microsoft/powerbi-modeling-mcp`` server so it can connect to a
        semantic model in a Fabric workspace without an interactive prompt.
        """
        return self.get_token(POWERBI_XMLA_SCOPE, use_fallback=use_fallback)


# Module-level singleton so the whole app shares one credential + cache.
_provider: TokenProvider | None = None
_provider_lock = threading.Lock()


def get_token_provider() -> TokenProvider:
    """Return the shared :class:`TokenProvider` singleton."""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = TokenProvider()
    return _provider
