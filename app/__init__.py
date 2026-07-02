"""Fabric SQL Explorer application package.

A modular Streamlit UI to browse Microsoft Fabric workspaces, discover SQL
analytics endpoints / warehouses, retrieve their connection strings, connect,
and inspect detailed table & view schemas.
"""

__all__ = ["auth", "fabric_client", "sql_client"]
