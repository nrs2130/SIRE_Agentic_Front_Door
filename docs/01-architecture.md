# 01 — Architecture & latency-aware orchestration

This document is the design Copilot builds toward. It maps every requirement — voice front
door, Microsoft Agent Framework orchestration, Foundry hosted agents + Control Plane, MCP
tools, Foundry IQ, parallel processing, and emergency low-latency routing — onto concrete
components.

## 1. Layered architecture

| Layer | Component | Responsibility | Microsoft primitive |
|---|---|---|---|
| L0 | **Vocera Smartbadge** (device) | Wake word, panic button, hands-free audio | Real Stryker/Vocera hardware; in the demo, a web/CLI mic stands in |
| L1 | **Voice Gateway** | Audio I/O, barge-in, STT→intent, TTS; emit **Intent Envelope** + urgency | **Azure Voice Live API** (`gpt-realtime`) |
| L2 | **Orchestrator** | Route by urgency; run agents concurrently; stream progress | **Microsoft Agent Framework** *Workflow* (graph) |
| L3 | **Specialized agents** | One capability each (comms, sepsis, beds, supplies, …) | **Foundry Agent Service** hosted agents |
| L4 | **Tools** | I/O to hospital systems | **MCP servers** wrapping Vocera Engage adapters |
| L4b | **Knowledge** | Protocol retrieval, citation-backed | **Foundry IQ** knowledge base over Azure AI Search |
| L5 | **Control plane / observability** | See/govern agents, tools, runs; traces | **Foundry Control Plane** + OpenTelemetry |

### The key design decision
Position Nightingale as an **LLM orchestration layer *above* Vocera Engage**. Engage is the
real, FDA-cleared middleware that already does filtering/routing/escalation across 150+
integrations. Your agents **call Engage's documented interfaces** (or mock them for the demo)
and **augment** them with reasoning, multi-step workflows, and conversational feedback. They
never replace the cleared alarm path. This is both the most *credible* story for Stryker and
the safest one.

## 2. The Intent Envelope (the contract between L1 and L2)

The Voice Gateway's single output is a normalized object. Everything downstream keys off it.

```jsonc
{
  "correlation_id": "uuid",          // ties every span/log/tool call together
  "intent": "sepsis_screen",         // canonical verb (see intent registry)
  "urgency": "EMERGENCY",            // EMERGENCY | ROUTINE — decided HERE, cheaply
  "entities": {                       // extracted slots
    "patient_ref": "bed 12",
    "location": "4 West"
  },
  "patient_context": null,            // filled later by Patient Context tool
  "utterance": "patient in bed 12 looks septic",
  "spoken_ack_required": true
}
```

**Why urgency is decided at L1:** the realtime model already classifies intent via function
calling. Add urgency as a field on that same call (a cheap classification, no extra round
trip). The router must never need a full LLM reasoning pass to discover "this is a code blue."
The panic button on the badge is a hard override → `urgency = EMERGENCY`.

## 3. Orchestration with Microsoft Agent Framework

The Agent Framework models orchestration as a **workflow graph** of executors connected by
edges, with first-class support for:
- **Concurrent orchestration** — multiple agents/executors process in parallel and results are
  aggregated (`BuildConcurrent` in .NET / concurrent builder in Python; or manual **fan-out /
  fan-in barrier** edges for custom behavior).
- **Sequential** and **handoff / group-chat** orchestration for staged or delegated work.
- **Streaming events** — `AgentResponseUpdateEvent`, `WorkflowOutputEvent`, and **intermediate
  outputs** you can surface as live progress while the workflow is still running.

> Verify the exact current API names by `#fetch`-ing
> https://learn.microsoft.com/agent-framework/workflows/ before coding. Pin the package version.

### 3.1 The router (fast path vs standard path)

```
IntentEnvelope
     │
  [ROUTER executor]
     ├── urgency == EMERGENCY ──▶ FAST PATH  (pre-warmed, minimal hops)
     └── urgency == ROUTINE   ──▶ STANDARD PATH (full enrichment)
```

The router is a plain executor (deterministic switch on `urgency` + `intent`), **not** an LLM
call, so routing adds ~0 ms.

### 3.2 Fast path (emergency) — "acknowledge first, enrich in parallel"

The single most important pattern for the demo. For an emergency:

```
FAST PATH (e.g., sepsis / code blue / fall):
  t0  ── emit spoken_ack IMMEDIATELY ("Starting sepsis protocol, paging the team")
  t0  ── fan-out, all concurrent:
          ├─ CommsAgent.page(role="RRT")          # fire the escalation FIRST
          ├─ OrdersAgent.place(lactate, cultures) # slow (LIS round-trip) — don't wait
          ├─ KnowledgeAgent.get(sepsis_hour1)     # Foundry IQ RAG
          └─ TimerAgent.start(hour1_window)       # compliance clock
  stream ── as each branch returns, speak an update ("lactate ordered", "RRT paged")
  fan-in ── barrier ONLY on the pieces the next spoken summary needs
```

Rules the code must follow (also in `copilot-instructions.md`):
- The spoken acknowledgment is emitted **before** any slow tool call resolves.
- Escalation/notification fires **first** on the fan-out, not last.
- Each branch has a **latency budget**; if exceeded, emit a "still working on X" voice cue and
  continue — never freeze the conversation.

### 3.3 Standard path (routine) — full enrichment, order not critical

```
STANDARD PATH (e.g., "call the on-call cardiologist"):
  ── enrich: PatientContext + on-call schedule lookup (concurrent)
  ── SIREAgent.resolve(role/name → person)   # existing RRF entity resolution
  ── CommsAgent.call(person)
  ── speak confirmation with read-back
```

### 3.4 Parallelism you get for free
Independent sub-tasks *within* a workflow (e.g., in sepsis: order labs ∥ page provider ∥ start
timer ∥ fetch protocol) are modeled as concurrent branches. The framework aggregates them;
you attach a custom aggregator when you need a domain-specific merge (e.g., "summarize what's
done and what's pending" for the spoken update).

## 4. Latency budget model

Give every node a soft timeout and a spoken fallback. Suggested starting budgets (tune during
the workshop dry-run):

| Node | Budget | On breach |
|---|---|---|
| Spoken acknowledgment (emergency) | 300 ms | N/A — must always meet this |
| Router | 10 ms | N/A |
| Comms/escalation tool | 800 ms | "I'm still reaching the team…" |
| Labs/orders (HL7) tool | 2 s | "Placing the order now…" |
| Foundry IQ retrieval | 1 s | proceed with cached top protocol |
| Patient Context REST | 700 ms | proceed without context, note it |

Implement budgets with `asyncio.wait_for` / `asyncio.wait(..., timeout=...)` around branch
awaits, and surface breaches as intermediate events (which become voice cues). Record actual
latencies as OpenTelemetry span attributes.

## 5. Tools = MCP wrappers over Vocera Engage adapters

Each tool is a thin MCP tool. For the demo, back it with a **mock** that returns realistic
data after a realistic delay (so the latency behavior is visible). Map each to a real Vocera
integration surface so the story is grounded (full mapping in `02-stryker-workload-catalog.md`).

| MCP tool | Wraps (real Vocera/Stryker surface) | Demo mock returns |
|---|---|---|
| `sire_resolve_entity` | **existing SIRE `mcp_server/`** (AI Search RRF) | person/group + confidence |
| `oncall_lookup` | On-call scheduling adapters (AMiON/QGenda/Spok/Lightning Bolt) | role → current person |
| `comms_page` / `comms_call` | Engage escalation + Smartbadge call/page | ack + escalation state |
| `labs_hl7` | **Vocera HL7 Adapter** (labs/results) | lactate/culture order + result |
| `patient_context` | **Vocera Patient Context REST** service | patient + care team |
| `bed_telemetry` | **Stryker iBed Adapter** (ProCuity) | bed-exit/position/weight/siderail |
| `monitor_alarm` | Monitor adapters (GE Carescape, Nihon Kohden, Spacelabs, Sotera) | vitals + alarm |
| `blood_bank` | LIS/Blood Bank via HL7 or Scripted Adapter | units available/crossmatch |
| `equipment_locate` | **Smart Equipment Management** "last seen" + ProCare | device location/status |

Keep the mock and the (future) real adapter behind the **same MCP tool interface**, so
swapping mock → real is a config change, not a rewrite.

## 6. Foundry deployment & Control Plane

- **Hosted agents:** each L3 agent is created in **Foundry Agent Service** so it appears in the
  Foundry portal and the **Control Plane** (unified visibility/governance over agents, models,
  tools). Verify current creation flow at
  https://learn.microsoft.com/azure/foundry/ before coding.
- **Hosted MCP tools:** attach your MCP server endpoint(s) to the hosted agents ("bring your
  own MCP server"). See https://learn.microsoft.com/agent-framework/agents/tools/hosted-mcp-tools.
- **Foundry IQ:** create a knowledge base over the Azure AI Search index holding sepsis/code
  protocols, and connect it to the relevant agents for grounded, citation-backed answers. See
  https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect.
- **Control plane value in the demo:** flip to the Foundry portal to show the hosted agents,
  their tool invocations, and traces — this is what proves it's a *platform*.

## 7. Repository shape Copilot should create

```
src/
  gateway/            # L1 Voice Live session + IntentEnvelope + urgency classifier
  orchestrator/       # L2 Agent Framework workflow, router, fast/standard paths, budgets
  agents/             # L3 one module per agent (comms, sepsis, beds, supplies, sire, ...)
  tools/              # L4 MCP servers (mock + real interface) per adapter
  knowledge/          # L4b Foundry IQ setup + protocol ingestion
  telemetry/          # L5 OpenTelemetry setup, correlation, latency spans
  config.py           # typed config (extends SIRE_demo pattern)
infra/                # Foundry project + deployment (bicep/az or SDK scripts)
tests/                # pytest: per-layer + an end-to-end sepsis latency test
docs/                 # this kit
```

## 8. Requirement → component traceability

| Your requirement | Where it lives |
|---|---|
| Voice Live as the foundation | L1 Voice Gateway (extends `SIRE_demo`) |
| Orchestrated with Microsoft Agent Framework / VS Code | L2 Orchestrator; built via Copilot in VS Code |
| Deployed into a Microsoft Foundry project | `infra/` + Foundry Agent Service |
| Hosted agents visible in the control plane | L3 agents in Foundry; L5 Control Plane |
| MCP tool calls | L4 tools (incl. existing SIRE MCP server) |
| Foundry IQ | L4b knowledge base for protocols |
| Parallel processing | L2 fan-out/concurrent branches |
| Emergency → very fast response | L1 urgency + L2 fast path + acknowledge-first |
| Kick off slow work, keep giving live feedback | L2 speculative dispatch + streamed intermediate events → voice |
| Other agentic workflows beyond SIRE | L3 agents from `02-stryker-workload-catalog.md` |
| Interactive after it's built | L6 Presentation layer — Streamlit demo app (§9) |

## 9. Presentation layer & deployment topologies (yes — it's interactive)

The system is driven through an **interactive Streamlit app** (extending `SIRE_demo`'s existing
`streamlit_app.py`) — the presenter's cockpit for the workshop. It's a **client of the Voice
Gateway + Orchestrator**, not a new layer of logic. Built by `/demo-app`. It renders:
- a **badge simulator** (mic + text input + a **PANIC** button that forces `EMERGENCY`),
- the **live transcript** (including the acknowledgment-first message),
- an **orchestration cockpit** that visualizes the routing decision, each **concurrent branch**
  with a live **elapsed-vs-budget** timer (green/amber), and the agent/tool call log,
- the **sepsis hour-1 checklist + compliance timer**, and
- a **Control Plane link** to the Foundry view of the hosted agents/traces for the run.

This is what makes the latency-aware, parallel orchestration *visible* on stage — you watch the
fast-path acknowledgment fire while slow branches keep streaming progress.

### Two deployment topologies (pick per environment)
```
(A) LOCAL ORCHESTRATOR — best for the workshop / most visibility
    Streamlit app ──▶ Voice Gateway (local) ──▶ Orchestrator (local, Agent Framework)
                                                    │ remote calls
                                                    ├─▶ Foundry HOSTED agents (Control Plane)
                                                    └─▶ MCP tools (hosted or local mocks)
    + Foundry IQ knowledge base (remote)
    Pros: you see everything locally; easiest to demo & debug. The hosted agents + traces still
    show up in the Foundry Control Plane.

(B) FULLY HOSTED — closer to production
    Streamlit app (thin client) ──▶ Orchestrator endpoint (hosted/containerized in Foundry)
                                        └─▶ hosted agents + hosted MCP tools + Foundry IQ
    Pros: the app is a thin client; orchestration runs server-side. More prod-like; a bit more
    infra to stand up.
```
The app's **backend toggle** (already present in `SIRE_demo`, extended by `/demo-app`) switches
between (A) and (B). Start with (A) for the workshop; graduate to (B) if you want the
production-style story. Either way the **mock MCP tools** let the whole thing run with no real
hospital systems connected.
