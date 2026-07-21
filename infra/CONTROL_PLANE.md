# Foundry Control Plane — viewing agents, tools, and traces (demo guide)

This is the "it's a *platform*" moment: flip to the Foundry portal and show the **hosted agents**,
their **MCP tool invocations**, and the **end-to-end traces** for a run — all keyed by
`correlation_id`. Steps below are verified against the current Microsoft Learn docs
(fetched 2026-07-20):
- Enable/associate Application Insights + view traces:
  https://learn.microsoft.com/azure/ai-foundry/concepts/trace
- Azure Monitor OpenTelemetry (Python) setup:
  https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable?tabs=python

## How our telemetry reaches the Control Plane

Nightingale emits OpenTelemetry spans for **every orchestrator node** and **every MCP tool call**
(`src/telemetry/`). Each span carries `nightingale.correlation_id`, `nightingale.urgency`, the
node/tool name, and `nightingale.latency_ms`; emergency runs also carry
`nightingale.ack_latency_ms`. Spans nest under one `conversation` span, so a single run is **one
trace**: gateway → orchestrator → agents → tools.

Traces flow to the **same Application Insights resource** that the Foundry project is linked to,
so hosted-agent spans and our local orchestrator spans land together and correlate by
`correlation_id`.

### One-time setup
1. **Link Application Insights to the Foundry project.** In the Foundry portal
   (https://ai.azure.com) → your project → **Tracing** (left nav). If no Application Insights is
   associated, click **Create new** (or connect an existing one). Requires at least Contributor on
   the Foundry resource.
2. **Copy the connection string.** Project → **Tracing → Manage data source → Connection string**.
3. **Point Nightingale at it.** Set `APPLICATIONINSIGHTS_CONNECTION_STRING` in `.env` (see
   `.env.example`). On startup we call `configure_telemetry()`
   (`src/telemetry/tracing.py`), which invokes the **Azure Monitor OpenTelemetry Distro**
   (`azure-monitor-opentelemetry==1.8.9`) — no code change needed.
   - For a **local/offline demo**, set `OTEL_CONSOLE_EXPORT=true` instead to print spans to the
     console (no Azure needed).

## Demo runbook — exact tabs to open

Run one **routine** and one **emergency** utterance first (so there's data), then, in the Foundry
portal (https://ai.azure.com → your project):

1. **Agents** (left nav) — show the hosted agents (`nightingale-sepsis`, `nightingale-comms`)
   registered in **Foundry Agent Service**. Open one to show its **model**, **instructions**, and
   its attached **MCP tools** (e.g. `nightingale-sepsis` → `knowledge_base_retrieve` + the action
   tools). This is the "governed, visible agents" story.
2. **Tracing** (left nav) — the trace list. Each row = one run: **Trace ID**, **start time**,
   **duration**, **status**, **operations** (span count). Sort by most recent.
3. **Open the emergency trace** — the timeline shows the nested spans:
   `conversation` → `fastpath` → `fastpath.branch.comms` / `.labs` / `.knowledge` / `.timer`
   → each branch's `tool.*` span. Point out:
   - `nightingale.ack_latency_ms` on the `fastpath` span (well under the 300 ms budget),
   - the branch spans **overlapping in time** (parallelism you can *see*),
   - `tool.comms_page` starting first (escalate-first),
   - `nightingale.latency_ms` and `nightingale.branch.status` on each branch.
4. **Filter by `correlation_id`** — in the trace's attributes (or a Logs/KQL query, below) to prove
   a single id threads gateway → orchestrator → agents → tools.
5. **Open the routine trace** — contrast: `conversation` → `orchestrator.node.router` →
   `std_enrich` → `std_resolve` → `std_act` → `std_summary`, sequential, no `ack_latency_ms`.

### Optional: KQL in Application Insights
In the Application Insights resource → **Logs**, correlate everything for one run:
```kql
dependencies
| where customDimensions["nightingale.correlation_id"] == "<paste-correlation-id>"
| project timestamp, name, duration, customDimensions
| order by timestamp asc
```

## What the run summary shows (console echo of the trace)
Every conversation also logs a compact **run summary** (`src/telemetry/run_summary.py`) — routing
decision, which branches ran, their latencies vs budgets, and any breaches — so the parallelism and
timing are legible even without opening the portal. Show it in the terminal alongside the trace.
