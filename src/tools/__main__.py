"""Run a Nightingale MCP tool server: ``python -m src.tools <tool_name>``.

Defaults to ``oncall_lookup``. As more L4 tools are added (comms_page, labs_hl7,
patient_context, …) they register here so ``.vscode/mcp.json`` can launch each by name.
"""

from __future__ import annotations

import sys

_TOOLS = {"oncall_lookup"}


def main() -> None:
    tool = sys.argv[1] if len(sys.argv) > 1 else "oncall_lookup"
    if tool not in _TOOLS:
        raise SystemExit(f"Unknown tool {tool!r}. Available: {', '.join(sorted(_TOOLS))}")
    if tool == "oncall_lookup":
        from src.tools.oncall_lookup import main as run_oncall

        run_oncall()


if __name__ == "__main__":
    main()
