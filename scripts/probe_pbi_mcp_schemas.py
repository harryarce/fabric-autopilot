"""Print the ``inputSchema`` for the two tools we invoke on the Power BI
Modeling MCP server. Run manually to confirm the argument names our client
uses (``workspaceName`` / ``databaseName``) still match the current schema:

    python scripts/probe_pbi_mcp_schemas.py

The script exits 0 on success. If ``connection_operations.connectToFabric``
now requires different parameter names, you will see them in the output
under the "connection_operations" section.
"""

from __future__ import annotations

import json
import logging
import sys

from app.intelligence.pbi_modeling_mcp import PowerBiModelingMcp

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    with PowerBiModelingMcp() as mcp:
        for name in ("connection_operations", "dax_query_operations"):
            schema = mcp.tool_input_schema(name)
            print(f"\n=== {name} inputSchema ===")
            if schema is None:
                print("(not exposed by server)")
            else:
                print(json.dumps(schema, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
