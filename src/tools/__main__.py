"""Run a Nightingale MCP tool server: ``python -m src.tools <tool_name>``.

Defaults to ``oncall_lookup``. As more L4 tools are added (comms_page, labs_hl7,
patient_context, …) they register here so ``.vscode/mcp.json`` can launch each by name.
"""

from __future__ import annotations

import sys

_TOOLS = {"oncall_lookup", "comms_page", "labs_hl7", "patient_context", "bed_telemetry", "monitor_alarm"}


def main() -> None:
    tool = sys.argv[1] if len(sys.argv) > 1 else "oncall_lookup"
    if tool not in _TOOLS:
        raise SystemExit(f"Unknown tool {tool!r}. Available: {', '.join(sorted(_TOOLS))}")
    if tool == "oncall_lookup":
        from src.tools.oncall_lookup import main as run_oncall

        run_oncall()
    elif tool == "comms_page":
        from src.tools.comms_page import main as run_comms

        run_comms()
    elif tool == "labs_hl7":
        from src.tools.labs_hl7 import main as run_labs

        run_labs()
    elif tool == "patient_context":
        from src.tools.patient_context import main as run_patient

        run_patient()
    elif tool == "bed_telemetry":
        from src.tools.bed_telemetry import main as run_bed

        run_bed()
    elif tool == "monitor_alarm":
        from src.tools.monitor_alarm import main as run_monitor

        run_monitor()


if __name__ == "__main__":
    main()
