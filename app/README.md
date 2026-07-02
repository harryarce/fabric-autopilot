# Fabric SQL Explorer

A modular **Streamlit** UI that guides you through Microsoft Fabric SQL discovery:

1. **Browse** every Fabric workspace your identity can access.
2. **Select** a workspace.
3. **List** its SQL analytics endpoints, Warehouses and Lakehouses.
4. **Retrieve** the connection string.
5. **Open** a token-authenticated SQL connection.
6. **Extract** detailed table & view schemas (columns, types, nullability, keys).

Authentication uses your **Azure CLI** login (`az login`) — no passwords are ever
stored. The Fabric REST API token and the SQL (`database.windows.net`) token are
acquired and cached automatically.

## Project layout

| File | Responsibility |
| --- | --- |
| `auth.py` | Shared, thread-safe Azure AD token provider (cached per scope). |
| `fabric_client.py` | Fabric REST API: list workspaces & SQL endpoints. |
| `sql_client.py` | ODBC connection + INFORMATION_SCHEMA schema extraction. |
| `streamlit_app.py` | The Streamlit UI wiring everything together. |

## Prerequisites

- Python 3.10+
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
  (required for steps 5–6)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r app/requirements.txt
az login
```

## Run

```powershell
streamlit run app/streamlit_app.py
```

The app opens in your browser. Use the sidebar to track progress, refresh data,
or start over.

## Notes

- Workspaces come from `GET /v1/workspaces` (the workspaces the signed-in user
  can access) — no Fabric admin role required.
- SQL endpoints are gathered from Warehouses, Lakehouse SQL analytics endpoints,
  and standalone SQL analytics endpoint items, then de-duplicated by server +
  database.
- Schema extraction reads `INFORMATION_SCHEMA.TABLES`, `COLUMNS`, and primary-key
  constraints — read-only, no data is modified.
