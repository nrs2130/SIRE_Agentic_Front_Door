# 03 — Copilot build runbook (the "/" command sequence)

Follow this top to bottom. Each step is a slash command you run in **GitHub Copilot Chat**
(VS Code, **Agent mode**, a strong model selected, the **Foundry Build** chat mode active).
The prompt files carry the detailed instructions and acceptance criteria — this runbook is the
*order* and the *why*.

## Before you start
- VS Code + Copilot; Python 3.11+; `az login` done; access to a Microsoft Foundry project with
  a `gpt-realtime` (Voice Live) deployment and an Azure AI Search service.
- Copy this kit's `.github/` and `docs/` into your new repo (or into a fork of `SIRE_demo`).
- Open Copilot Chat → gear icon → confirm your **Instructions** and **Prompt Files** are listed.
- Switch Copilot Chat to **Agent mode**; select the **Foundry Build** chat mode; pick a capable
  model for planning.

> **Three habits that make or break this build**
> 1. **`#fetch` the live Microsoft Learn docs** before any SDK step (Agent Framework, Foundry,
>    Voice Live move fast). Every prompt file reminds Copilot to do this — let it.
> 2. **Pull a working example** from `#githubRepo microsoft-foundry/foundry-samples` (start in
>    `samples/python/`; `samples/csharp/` for .NET patterns; `infrastructure/` for Bicep deploy
>    templates) and **adapt** it to our layers — don't copy blindly. Docs = the contract, samples
>    = the reference implementation.
> 3. **Run and test after every step.** Each component is defined-done only when it runs against
>    mock tools and has a passing `pytest`.

---

## Step 0 — (optional) refresh the repo-wide instructions
We ship a curated `.github/copilot-instructions.md`. Only if you've heavily changed the repo:
```
/init
```
…then diff Copilot's version against ours and keep the stronger rules. Normally **skip this** —
our file is better tuned for this architecture.

## Step 1 — Scaffold the repo and MCP wiring
```
/scaffold-repo
```
Creates the `src/{gateway,orchestrator,agents,tools,knowledge,telemetry}` layout, `config.py`
(extending the `SIRE_demo` pattern), `requirements.txt` (with **pinned** versions),
`.env.example`, `pytest` scaffolding, and **`.vscode/mcp.json`** registering the existing
`mcp_server/` so Copilot Agent can call SIRE search while it builds.

**Check:** `pip install -r requirements.txt` succeeds; `pytest` collects; MCP server shows up
in Copilot's tool list.

## Step 2 — Voice Gateway (L1)
```
/voice-gateway
```
Extends `SIRE_demo`'s Voice Live loop into a gateway that emits the **Intent Envelope** and
classifies **urgency (EMERGENCY|ROUTINE) at this layer**. Panic-button intent → hard EMERGENCY.

**Check:** speaking (or a text stub) yields a well-formed envelope with a `correlation_id`; an
"emergency" phrase sets `urgency=EMERGENCY` without an extra LLM round trip.

## Step 3 — Orchestrator skeleton (L2)
```
/orchestrator-workflow
```
Builds the Microsoft Agent Framework **workflow graph**: a deterministic **router** →
**fast path** vs **standard path**, with streaming of **intermediate events** back to the
gateway. Wire two placeholder agents first (echo agents) to prove routing + streaming.

**Check:** an EMERGENCY envelope takes the fast path and streams an acknowledgment event
*before* the (stubbed) slow branch resolves.

## Step 4 — MCP tools (L4) — build the mocks first
Run once per tool (pass the tool name as an argument):
```
/mcp-tool oncall_lookup
/mcp-tool comms_page
/mcp-tool labs_hl7
/mcp-tool patient_context
/mcp-tool bed_telemetry
/mcp-tool blood_bank
```
Each creates an MCP tool with a **mock backend** that returns realistic data after a realistic
delay, behind the **same interface** a real Vocera adapter would implement. (SIRE's
`sire_resolve_entity` already exists — just register it.)

**Check:** each tool is callable from Copilot Agent and from a `pytest`; delays are configurable
so you can demo latency behavior.

## Step 5 — Specialized agents (L3)
Run per capability you're demoing (start with two):
```
/foundry-agent comms
/foundry-agent sepsis
```
Creates single-responsibility agents that call the L4 tools, plus a **Foundry Agent Service**
registration script so each appears as a **hosted agent** in the **Control Plane**.

**Check:** agents run locally against mocks; the registration script (dry-run) shows what will
be created in Foundry.

## Step 6 — Foundry IQ knowledge (L4b)
```
/foundry-iq-knowledge sepsis-protocols
```
Ingests the sepsis / code protocol docs into an Azure AI Search index and creates a **Foundry
IQ knowledge base** connected to the knowledge/sepsis agent for **citation-backed** retrieval.

**Check:** the sepsis agent answers "what's the hour-1 bundle?" with cited sources.

## Step 7 — Emergency fast path + latency budgets
```
/emergency-fastpath
```
Hardens L2: **acknowledge-first**, escalation fires **first** on fan-out, per-node **latency
budgets** with spoken "still working on X" fallbacks, and **speculative dispatch** (kick off
slow branches, keep going, fan-in only where needed).

**Check:** the end-to-end test asserts acknowledgment latency is under budget while a slow
branch is deliberately delayed.

## Step 8 — The sepsis showcase workflow
```
/sepsis-workflow
```
Assembles the flagship demo: screening (SIRS/qSOFA from `bed_telemetry`/`monitor` + voice) →
**four concurrent agents** (screening, orders, comms/escalation, timer/compliance) → live voice
updates → hour-1 compliance timer.

**Check:** "patient in bed 12 looks septic" produces immediate acknowledgment, parallel
order+page+timer, and streamed spoken progress; the compliance timer tracks the hour-1 window.

## Step 9 — Observability + Control Plane
```
/observability
```
Adds OpenTelemetry spans on every node and tool (tagged `correlation_id`, `urgency`, latency),
and a short `infra/` guide to view agents/tools/traces in the **Foundry Control Plane**.

**Check:** a run produces a trace you can follow end-to-end; the Foundry portal lists the
hosted agents and their tool calls.

## Step 10 — Deploy to the Foundry project
Ask Copilot in Agent mode (no dedicated prompt file — it's environment-specific):
```
Using #fetch of the current Microsoft Foundry Agent Service docs and #githubRepo
microsoft-foundry/foundry-samples (adapt the infrastructure/ Bicep templates), generate the
deployment scripts in infra/ to publish our hosted agents and attach the MCP tool endpoints and
the Foundry IQ knowledge base. Use DefaultAzureCredential. Pin versions. Explain each az/bicep step.
```
**Check:** agents appear in the Foundry portal / Control Plane; a smoke test drives one routine
and one emergency flow end-to-end.

## Step 11 — The interactive demo app (Streamlit cockpit)
```
/demo-app
```
Extends `SIRE_demo`'s `streamlit_app.py` into the presenter's cockpit: badge simulator (mic +
text + PANIC button), live transcript, the **orchestration cockpit** (routing decision +
concurrent branches with live elapsed-vs-budget timers + tool-call log), the sepsis hour-1
checklist + compliance timer, a **Control Plane link**, and a backend toggle for the two
deployment topologies (`docs/01-architecture.md` §9).

**Check:** `streamlit run streamlit_app.py` against mocks; typing "patient in bed 12 looks
septic" shows the acknowledgment-first message, four concurrent branches with live timers, the
checklist ticking, and the compliance clock — all while a deliberately-slow branch keeps
streaming progress.

---

## Adding a new workflow later (repeatable pattern)
Pick any workflow from `docs/02-stryker-workload-catalog.md` and run:
```
/mcp-tool <adapter>        # e.g. /mcp-tool equipment_locate
/foundry-agent <capability># e.g. /foundry-agent equipment
```
Then reference it from the router. Because tools and agents are single-responsibility behind
stable interfaces, new workflows are additive — **one orchestration, many workflows**.

## Handy ad-hoc commands during the build
- `/explain #file:src/orchestrator/router.py` — understand generated code.
- `/fix` — on a failing test or stack trace.
- `/tests #file:src/agents/sepsis.py` — generate/extend tests.
- `/create-prompt` — scaffold a new prompt file when you invent a new repeatable task.
- `#fetch <learn.microsoft.com URL>` — always, before SDK code.
- `@workspace` / `#codebase` — ask cross-file questions ("where is urgency set?").
