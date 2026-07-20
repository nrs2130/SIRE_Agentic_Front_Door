---
description: Microsoft Agent Framework + Foundry + Voice Live patterns and doc links
applyTo: "src/orchestrator/**,src/agents/**,src/gateway/**,src/knowledge/**,infra/**"
---

# Agent Framework / Foundry / Voice Live — patterns & accuracy rules

## Accuracy first (these SDKs change)
Before writing SDK code: (1) **`#fetch` the current Microsoft Learn page**, (2) **pull a working
example** from the official samples repo, and (3) **pin the exact package version** in
`requirements.txt`, noting it in a comment. If a symbol is uncertain, implement the documented
**pattern** and leave `# TODO: verify against <url>` — never invent a class or method name.

## Reference implementations (adapt working code, don't guess)
Use `#githubRepo microsoft-foundry/foundry-samples` — the official "embedded samples in Azure AI
Foundry docs" repo (Python, C#, Bicep; MIT-licensed). Workflow:
- Search it for the closest sample to your task (hosted agents, MCP tools, knowledge/Foundry IQ,
  Voice Live, deployment).
- **Primary:** `samples/python/` (our repo is Python). **.NET patterns:** `samples/csharp/`.
  **Deployment templates:** `infrastructure/` (Bicep) and `.infra/`.
- **Adapt, don't copy:** fit the sample to our layer boundaries (gateway / orchestrator / agents
  / tools / knowledge), keep it single-responsibility, and **still pin versions**. Note which
  sample you adapted in a code comment. The repo moves fast — re-fetch rather than assuming paths.

Authoritative docs (fetch these):
- Agent Framework overview & workflows: https://learn.microsoft.com/agent-framework/
- Concurrent orchestration (fan-out/fan-in): https://learn.microsoft.com/agent-framework/workflows/orchestrations/concurrent
- Sequential / handoff orchestration: https://learn.microsoft.com/agent-framework/workflows/orchestrations/
- Hosted MCP tools: https://learn.microsoft.com/agent-framework/agents/tools/hosted-mcp-tools
- Foundry Agent Service & Control Plane: https://learn.microsoft.com/azure/foundry/
- Foundry IQ knowledge bases: https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect
- Voice Live API: https://learn.microsoft.com/azure/ai-services/speech-service/voice-live

## Orchestration patterns to use
- Model orchestration as a **workflow graph** of executors + edges.
- **Router**: a deterministic executor that switches on `urgency` and `intent`. Not an LLM call.
- **Concurrent / fan-out**: run independent agents/tools in parallel; join at a **fan-in
  barrier** only where a result is truly needed. Prefer the framework's concurrent builder;
  drop to explicit fan-out / fan-in-barrier edges when you need custom aggregation.
- **Streaming**: subscribe to workflow events and forward **intermediate outputs** to the
  gateway as they arrive, so the nurse hears live progress. Use a **custom aggregator** when the
  spoken update needs a domain-specific merge ("done: …; pending: …").
- **Sequential / handoff**: use for staged flows (enrich → resolve → act) or delegation.

## Emergency fast path (non-negotiable)
- Emit the spoken acknowledgment **before** any slow branch resolves.
- Fire escalation/notification **first** on the fan-out.
- Each branch has a **latency budget**; on breach, emit a "still working on X" event and keep
  going. Never block the conversation on a slow tool.

## Agents (L3)
- One capability per agent, single responsibility, independently testable against **mock MCP
  tools**.
- Provide a **Foundry Agent Service** registration path so each agent is a **hosted agent**
  visible in the **Control Plane**. Attach MCP tool endpoints as **hosted MCP tools**.
- Clinical agents ground answers in **Foundry IQ** and **cite sources**.

## Safety / regulatory
- The agent **augments** Vocera Engage's routing/alarm path; it never autonomously suppresses,
  reprioritizes, or overrides clinical alarms (EMDAN is FDA 510(k)-cleared).
- No autonomous medical orders — prepare, read back, and require human confirmation.
- Keep mock and real adapter behind the **same MCP interface** so mock → real is config, not a
  rewrite.
