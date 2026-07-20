---
mode: agent
description: Build one L4 MCP tool (mock backend, real-adapter interface) — pass the tool name as an argument
tools: ['codebase', 'search', 'fetch', 'editFiles', 'runCommands', 'runTests']
---

# /mcp-tool

Build one **L4 MCP tool**: `${input:toolName:which tool? e.g. oncall_lookup, comms_page, labs_hl7, patient_context, bed_telemetry, blood_bank, equipment_locate}`.
Read `docs/01-architecture.md` §5 and `docs/02-stryker-workload-catalog.md` Part C for the real
Vocera Engage adapter this wraps.

## Task
1. In `src/tools/`, implement an MCP server/tool named `${input:toolName}` exposing a small,
   typed interface (clear input/output schemas). `#fetch` the current MCP SDK docs if unsure of
   the server API; pin the version.
2. Back it with a **mock implementation** that:
   - returns **realistic data** for this domain (see the catalog table), and
   - simulates a **realistic latency** via a configurable delay (so latency behavior is
     demoable), read from `config.py`.
3. Put the mock behind the **same interface** a real Vocera adapter would implement, so
   swapping mock → real is a config change. Add a `# Real adapter: <which Vocera adapter>` note
   citing the mapping from the catalog (e.g. `labs_hl7` → Vocera **HL7 Adapter**;
   `patient_context` → **Patient Context REST**; `bed_telemetry` → **iBed Adapter**;
   `oncall_lookup` → AMiON/QGenda/Spok; `blood_bank` → LIS via HL7/Scripted Adapter).
4. Register the tool in `.vscode/mcp.json` (local dev) and export a factory the L3 agents and the
   Foundry hosted-agent registration can attach to.
5. **Safety:** tools do **I/O only** — no business logic, no clinical decisions. Anything that
   would touch a cleared alarm path (e.g. `monitor_alarm`) must be read/notify-only and
   human-in-the-loop.

## Acceptance criteria
- The tool is callable from Copilot Agent mode and from a `pytest` (happy path + one
  timeout/error path).
- Latency is configurable and observable (returns/records elapsed time).
- Interface is identical for mock and (future) real adapter; the real-adapter mapping is noted.
- No business logic in the tool. MCP SDK version pinned + commented.

Confirm the tool name, its interface, and which agent(s) will call it.
