---
mode: agent
description: Add OpenTelemetry tracing + a Foundry Control Plane viewing guide
tools: ['codebase', 'search', 'fetch', 'editFiles', 'runCommands', 'runTests']
---

# /observability

Add end-to-end observability so every run is traceable and the **Foundry Control Plane** story
lands in the demo. Read `docs/01-architecture.md` §6.

## Task
1. In `src/telemetry/`, set up **OpenTelemetry** (tracer + exporter configurable via
   `config.py`). `#fetch` the current Azure Monitor / OpenTelemetry-for-AI docs if unsure; pin
   versions.
2. Instrument **every orchestrator node and every MCP tool call** with a span carrying:
   `correlation_id`, `urgency`, node/tool name, and **measured latency**. Emergency-path spans
   also record the acknowledgment latency.
3. Emit a compact **run summary** at the end of each conversation: the routing decision, which
   branches ran, their latencies, and any budget breaches — so a workshop viewer can *see* the
   parallelism and timing.
4. Add `infra/CONTROL_PLANE.md`: a short guide to viewing the **hosted agents**, their tool
   invocations, and traces in the **Foundry Control Plane / portal** — with the exact tabs to
   open during the demo. `#fetch` the current Foundry Control Plane doc for accurate navigation.
5. Ensure spans from **hosted agents** correlate with local orchestrator spans via
   `correlation_id`, so a single trace spans gateway → orchestrator → agents → tools.

## Acceptance criteria
- A single run produces a trace you can follow gateway → orchestrator → agents → tools, keyed by
  `correlation_id`.
- The run summary prints routing, branch latencies, and any budget breaches.
- `infra/CONTROL_PLANE.md` gives correct, current steps to view agents/tools/traces in Foundry.
- OpenTelemetry / Azure Monitor versions pinned + commented.

Show a sample trace/summary from one routine and one emergency run.
