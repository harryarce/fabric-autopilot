"""Live end-to-end probe of the Ask-Your-Model flow.

Attempts to connect to a real Fabric workspace/semantic model via the
Power BI Modeling MCP server, then run a trivial DAX query. Reads target
names from CLI args or env vars so it never bakes tenant identifiers into
the repo:

    python scripts/probe_ask_model.py <workspace-name> <semantic-model-name>
    # or set FABRIC_WORKSPACE_NAME / FABRIC_SEMANTIC_MODEL_NAME env vars.

Prints the raw MCP responses (including any error diagnostics) so a
non-zero exit is actionable without extra logging setup.
"""

from __future__ import annotations

import logging
import os
import sys

from app.intelligence.pbi_modeling_mcp import (
    PowerBiModelingMcpError,
    build_client,
)

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
)


def main(argv: list[str]) -> int:
    workspace = (
        argv[1] if len(argv) > 1 else os.environ.get("FABRIC_WORKSPACE_NAME", "")
    ).strip()
    model = (
        argv[2]
        if len(argv) > 2
        else os.environ.get("FABRIC_SEMANTIC_MODEL_NAME", "")
    ).strip()
    if not workspace or not model:
        print(
            "Usage: probe_ask_model.py <workspace> <semantic-model>",
            file=sys.stderr,
        )
        return 2

    print(f"Connecting to workspace={workspace!r} model={model!r}...")
    # ``build_client`` auto-picks the right auth path per host:
    #   * On a workstation (``az`` on PATH), no token is injected — the MCP
    #     server opens the correct-user's browser sign-in. Complete the
    #     popup when it appears; otherwise the call will time out.
    #   * On a headless host (no ``az``, or POWERBI_MODELING_MCP_HEADLESS=
    #     true), it injects a Power BI XMLA token from the platform's
    #     managed identity / Azure CLI.
    # ``PROBE_HEADLESS`` (true/false) forces a specific mode.
    override = os.environ.get("PROBE_HEADLESS", "").strip().lower()
    headless: bool | None
    if override in {"1", "true", "yes", "on"}:
        headless = True
    elif override in {"0", "false", "no", "off"}:
        headless = False
    else:
        headless = None
    use_fallback = os.environ.get("PROBE_USE_CLI_FALLBACK") == "1"
    with build_client(
        use_fallback_token=use_fallback, headless=headless
    ) as mcp:
        try:
            call = mcp.connect_to_fabric_model(workspace, model)
        except PowerBiModelingMcpError as exc:
            print(f"CONNECT FAILED:\n{exc}", file=sys.stderr)
            return 1
        print("CONNECT OK. text=")
        print(call.text)
        if call.structured is not None:
            print(f"structured={call.structured}")

        try:
            result = mcp.execute_dax("EVALUATE ROW(\"one\", 1)")
        except PowerBiModelingMcpError as exc:
            print(f"EXECUTE FAILED:\n{exc}", file=sys.stderr)
            return 1
        print(f"EXECUTE OK. columns={result.columns} rows={result.rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
