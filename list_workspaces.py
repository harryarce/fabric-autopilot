"""List Microsoft Fabric workspaces using the Admin REST API.

Uses Azure CLI credentials (`az login`) to authenticate against the
Fabric service and calls the admin "List Workspaces" endpoint:
https://learn.microsoft.com/en-us/rest/api/fabric/admin/workspaces/list-workspaces

The caller must be a Fabric administrator (or a service principal with the
Tenant.Read.All / Tenant.ReadWrite.All scope).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Iterator

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential

# The Fabric API audience. The token is requested for the ".default" scope so
# that all statically-assigned permissions for the signed-in identity apply.
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_BASE_URL = "https://api.fabric.microsoft.com/v1"
LIST_WORKSPACES_URL = f"{FABRIC_BASE_URL}/admin/workspaces"

REQUEST_TIMEOUT_SECONDS = 60


def get_access_token() -> str:
    """Acquire an access token for the Fabric API using the Azure CLI login."""
    credential = AzureCliCredential()
    try:
        token = credential.get_token(FABRIC_SCOPE)
    except ClientAuthenticationError as exc:
        raise SystemExit(
            "Failed to acquire a token via the Azure CLI. "
            "Make sure you have run 'az login' and have access.\n"
            f"Details: {exc}"
        ) from exc
    return token.token


def iter_workspaces(
    token: str,
    *,
    workspace_type: str | None = None,
    capacity_id: str | None = None,
    name: str | None = None,
    state: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield workspaces, transparently following pagination tokens."""
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    )

    params: dict[str, str] = {}
    if workspace_type:
        params["type"] = workspace_type
    if capacity_id:
        params["capacityId"] = capacity_id
    if name:
        params["name"] = name
    if state:
        params["state"] = state

    continuation_token: str | None = None
    while True:
        request_params = dict(params)
        if continuation_token:
            request_params["continuationToken"] = continuation_token

        response = session.get(
            LIST_WORKSPACES_URL,
            params=request_params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise SystemExit(
                f"Request failed with status {response.status_code}: {response.text}"
            )

        payload = response.json()
        yield from payload.get("workspaces", [])

        continuation_token = payload.get("continuationToken")
        if not continuation_token:
            break


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Microsoft Fabric workspaces (Admin API)."
    )
    parser.add_argument(
        "--type",
        dest="workspace_type",
        choices=["Personal", "Workspace", "AdminWorkspace"],
        help="Filter by workspace type.",
    )
    parser.add_argument("--capacity-id", help="Filter by capacity ID (uuid).")
    parser.add_argument("--name", help="Filter by workspace name.")
    parser.add_argument(
        "--state",
        choices=["Active", "Deleted"],
        help="Filter by workspace state.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = get_access_token()

    workspaces = list(
        iter_workspaces(
            token,
            workspace_type=args.workspace_type,
            capacity_id=args.capacity_id,
            name=args.name,
            state=args.state,
        )
    )

    if not workspaces:
        print("No workspaces found.")
        return 0

    print(f"Found {len(workspaces)} workspace(s):\n")
    for ws in workspaces:
        print(f"- {ws.get('name')} ({ws.get('id')})")
        print(f"    type:       {ws.get('type')}")
        print(f"    state:      {ws.get('state')}")
        if ws.get("capacityId"):
            print(f"    capacityId: {ws.get('capacityId')}")
        if ws.get("domainId"):
            print(f"    domainId:   {ws.get('domainId')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
