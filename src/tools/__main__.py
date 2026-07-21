"""Run a Nightingale MCP tool server: ``python -m src.tools <tool_name> [--http]``.

Defaults to ``oncall_lookup`` over **stdio** (local dev / ``.vscode/mcp.json``). Pass
``--http`` to serve the same tool over **streamable-HTTP** at ``0.0.0.0:$PORT/mcp`` — the
transport Foundry hosted agents attach to as a hosted MCP tool endpoint (used by the
Container Apps deployment in ``infra/``). Reuses each tool's existing ``build_server()`` so
there is a single source of truth for the tool definition.

As more L4 tools are added they register here so both ``.vscode/mcp.json`` (stdio) and the
deployment (HTTP) can launch each by name.
"""

from __future__ import annotations

import logging
import os
import sys

_TOOLS = {
    "oncall_lookup", "comms_page", "labs_hl7", "patient_context",
    "bed_telemetry", "monitor_alarm", "blood_bank", "equipment_locate",
}


def _build_server(tool: str):
    """Return the FastMCP server for ``tool`` (imported lazily, per tool)."""
    if tool == "oncall_lookup":
        from src.tools.oncall_lookup import build_server
    elif tool == "comms_page":
        from src.tools.comms_page import build_server
    elif tool == "labs_hl7":
        from src.tools.labs_hl7 import build_server
    elif tool == "patient_context":
        from src.tools.patient_context import build_server
    elif tool == "bed_telemetry":
        from src.tools.bed_telemetry import build_server
    elif tool == "monitor_alarm":
        from src.tools.monitor_alarm import build_server
    elif tool == "blood_bank":
        from src.tools.blood_bank import build_server
    elif tool == "equipment_locate":
        from src.tools.equipment_locate import build_server
    else:  # pragma: no cover - guarded by main()
        raise SystemExit(f"Unknown tool {tool!r}.")
    return build_server()


def main() -> None:
    argv = sys.argv[1:]
    http = "--http" in argv
    positional = [a for a in argv if not a.startswith("-")]
    tool = positional[0] if positional else "oncall_lookup"
    if tool not in _TOOLS:
        raise SystemExit(f"Unknown tool {tool!r}. Available: {', '.join(sorted(_TOOLS))}")

    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    server = _build_server(tool)

    if not http:
        server.run()  # stdio (local dev / .vscode/mcp.json)
        return

    # Serve over streamable-HTTP for a hosted MCP endpoint (Foundry attaches to /mcp).
    # ``.settings`` is mutable on the pinned mcp==1.28.1 FastMCP; bind all interfaces + $PORT.
    server.settings.host = "0.0.0.0"  # noqa: S104 - container listens on all interfaces
    server.settings.port = int(os.getenv("PORT", "8080"))
    # (1) Behind Container Apps / the Foundry gateway, the forwarded Host header
    # (``<app>...azurecontainerapps.io:443``) trips FastMCP's DNS-rebinding host check and
    # returns HTTP 421 "Misdirected Request". Disable it — ingress already terminates TLS and
    # restricts hosts. (2) ``stateless_http`` makes each tool call independent so no
    # ``mcp-session-id`` must stick to one replica (safe even though we pin to 1 replica).
    server.settings.transport_security.enable_dns_rebinding_protection = False
    server.settings.stateless_http = True
    logging.getLogger("nightingale.tools").info(
        "serving %s over streamable-http on %s:%s/mcp (stateless, host-check off)",
        tool, server.settings.host, server.settings.port,
    )
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
