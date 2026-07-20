# Copilot instructions — Nightingale voice-agentic platform

You are helping build **Nightingale**, a voice-first, latency-aware, **multi-agent** platform
for hospital nurses. It extends the existing `SIRE_demo` repo (Azure Voice Live + Azure AI
Search RRF) into an orchestrated system deployed on **Microsoft Foundry**. Read this file on
every request. Follow it unless the user explicitly overrides it.

## Product context (what we're building)
- A nurse speaks to a **Vocera Smartbadge** (Stryker acquired Vocera in 2022). Voice is the
  "front door." **Azure Voice Live API** (`gpt-realtime`) does speech ⇄ intent.
- A **Microsoft Agent Framework** *workflow* orchestrates specialized agents (call a doctor,
  prep a room, check blood supply, run the sepsis hour-1 bundle, read smart-bed telemetry,
  locate equipment, activate code blue/RRT, …).
- Agents are **hosted in Foundry Agent Service** and visible in the **Foundry Control Plane**.
- Agents reach hospital systems via **MCP tool calls** that wrap **Vocera Engage** adapters.
- Clinical protocols are grounded via **Foundry IQ** knowledge bases over Azure AI Search.
- `SIRE` (entity/person/group resolution via multi-strategy RRF) is **one agent** behind the
  front door, exposed through the existing `mcp_server/`.

## Architectural layers (respect these boundaries)
1. **Voice Gateway** — owns the Voice Live session. Its only job: audio I/O, barge-in, and
   producing a normalized **Intent Envelope** `{intent, entities, urgency, patient_context,
   correlation_id}`. It does **not** contain business logic. It **must** classify
   `urgency ∈ {EMERGENCY, ROUTINE}` cheaply, at this layer, so routing is instant.
2. **Orchestrator** — a Microsoft Agent Framework workflow (a graph). A **router** dispatches
   the envelope to a **fast path** (emergency) or **standard path** (routine). Independent
   sub-tasks run **concurrently** (fan-out) and join at a **fan-in barrier** only where a real
   join is required. It streams **intermediate output events** back to the gateway for live
   spoken feedback.
3. **Agents** — one focused agent per capability. Small, testable, single-responsibility.
   Deployed as Foundry hosted agents.
4. **Tools (MCP)** — thin wrappers over Vocera Engage adapters and other systems. For the demo
   these are **mock simulators** with a realistic latency profile. Never bake business logic
   into a tool; tools do I/O only.
5. **Knowledge (Foundry IQ)** — citation-backed retrieval for protocols. Agents cite sources.

## Latency & safety are first-class requirements
- **Decide urgency at the gateway**, not after an LLM reasoning pass.
- **Emergency fast path:** acknowledge by voice *immediately*, fire the escalation/notify tool
  *first*, then enrich context in parallel. Never block the acknowledgment on a slow tool.
- **Speculative/async dispatch:** kick off slow tools as fan-out branches; keep going; only
  wait at a fan-in barrier when the result is truly needed. Emit "still working on X" voice
  cues when a branch exceeds its latency budget.
- Every orchestrator node has a **latency budget** (a soft timeout) and a **correlation_id**.
- **Human-in-the-loop for anything clinical.** Vocera's alarm middleware (EMDAN) is **FDA
  510(k)-cleared**; the agent **augments** Engage's routing, it never autonomously suppresses,
  reprioritizes, or overrides clinical alarms, and it never gives autonomous medical orders —
  it prepares/reads back/confirms and a human approves.

## Tech + conventions
- **Language:** Python 3.11+ (matches `SIRE_demo`). Type hints everywhere. `async`/`await` for
  all I/O and orchestration. `ruff` + `black`; `pytest` for tests.
- **Config:** environment variables via a typed dataclass in `config.py` (follow the existing
  `SIRE_demo` pattern). Never hardcode endpoints, keys, model names, or deployment names.
- **Auth:** prefer Microsoft Entra ID / `DefaultAzureCredential` (or `AzureCliCredential`)
  over API keys, matching the existing repo's `--use-token-credential` path.
- **Observability:** OpenTelemetry traces/spans on every orchestrator node and tool call,
  tagged with `correlation_id`, `urgency`, node name, and latency. This feeds the Control Plane.
- **Secrets:** `.env` is git-ignored; provide `.env.example`. Never commit secrets.

## SDK accuracy rule (IMPORTANT — these APIs move fast)
Before writing any Microsoft Agent Framework, Foundry Agent Service, Foundry IQ, or Voice Live
code:
1. `#fetch` the current Microsoft Learn page for that API (links in
   `.github/instructions/agent-framework.instructions.md`).
2. **Pull a working code example** from the official samples repo
   `#githubRepo microsoft-foundry/foundry-samples` (start in `samples/python/`; use
   `samples/csharp/` for .NET patterns and `infrastructure/` for Bicep deploy templates). Find
   the closest sample to the task, **adapt it** to our architecture and layer boundaries — do not
   copy blindly, and never widen its scope.
3. **Pin exact package versions** in `requirements.txt`; state the version you used in a
   comment near the code, and note which sample you adapted.
4. If an API name is uncertain, prefer the **pattern** shown in the sample/docs over an invented
   symbol, and leave a `# TODO: verify against <url>` marker rather than guessing.
Do not fabricate SDK class or method names. If you can't verify one, say so. Docs are the
contract; the samples repo is the reference implementation.

## Definition of done for any component
- Runs locally against **mock MCP tools** with no real hospital systems required.
- Has a `pytest` test (happy path + one failure/timeout path).
- Emits OpenTelemetry spans.
- Respects the layer boundaries above (no business logic in gateway or tools).
- Emergency paths have a measured acknowledgment latency in the test output.

## Working style
- Prefer small, reviewable diffs. One component per prompt/turn.
- When a prompt file (`.github/prompts/*.prompt.md`) exists for the task, follow its
  acceptance criteria exactly.
- Keep `SIRE_demo`'s working code working — extend, don't rewrite, the Voice Live + RRF paths.
- Ask before introducing a new external dependency; justify it in one line.
