# Nightingale — Voice-Agentic "Front Door" for Stryker Smart Care

> **What this is.** A GitHub Copilot *build kit*: a set of custom instructions, prompt
> files (slash commands), and runbooks that drive GitHub Copilot to build a voice-first,
> latency-aware, **multi-agent** platform on top of your existing
> [`SIRE_demo`](https://github.com/nrs2130/SIRE_demo) Voice Live repo, orchestrated with the
> **Microsoft Agent Framework** and deployed into a **Microsoft Foundry** project.
>
> **Codename "Nightingale"** is a placeholder — rename freely. SIRE becomes *one* agent
> behind the front door; this kit adds the orchestration, the other agentic workflows, and
> the Foundry deployment.

---

## 1. The idea in one paragraph

A nurse talks to a **Vocera Smartbadge** (the "smart badge" — Stryker acquired Vocera in
2022). Voice is the **front door**. Behind it, **Azure Voice Live API** turns speech into
intent; a **Microsoft Agent Framework** workflow **orchestrates** a set of specialized,
Foundry-**hosted** agents (call a doctor, prep a room, check blood supply, run the sepsis
hour-1 bundle, read a smart-bed's real-time data, locate equipment…). Each agent reaches
real hospital systems through **MCP tool calls** that wrap **Vocera Engage** adapters, and
grounds its answers in **Foundry IQ** knowledge bases. The orchestrator is **latency-aware**:
"emergency" intents (code blue, sepsis, fall) take a pre-warmed **fast path** with an
immediate spoken acknowledgment, while slower enrichment runs **in parallel** and streams
live voice feedback ("paging the on-call intensivist now… lactate order placed… blood bank
confirms 2 units…").

**One orchestration, many workflows. SIRE is just the first.**

---

## 2. What you already have vs. what this kit builds

| Layer | Today in `SIRE_demo` | What Nightingale adds |
|---|---|---|
| Voice front door | Voice Live session, `gpt-realtime` function calling | Keep — becomes the **Voice Gateway**, emits a normalized *Intent Envelope* + urgency |
| Retrieval | AI Search multi-strategy RRF (entity resolution) | Keep as the **SIRE agent** (person/group resolution), exposed via your existing `mcp_server/` |
| Tools | 1 MCP server (SIRE search) | **Many MCP tools** wrapping Vocera adapters (HL7 labs, Patient Context REST, iBed, nurse-call, on-call scheduling) |
| Orchestration | none (single function loop) | **Agent Framework workflow**: router + fan-out/fan-in + emergency fast path |
| Agents | none (one model) | **Foundry hosted agents** you can see/govern in the **Control Plane** |
| Knowledge | none | **Foundry IQ** knowledge base (sepsis & code protocols, citation-backed) |
| Deploy | local Streamlit/CLI | **Microsoft Foundry project** with hosted agents + observability |

---

## 3. The build kit (files in this repo)

```
.github/
  copilot-instructions.md          # ALWAYS-ON context Copilot reads on every request
  instructions/
    python.instructions.md         # applies to **/*.py
    agent-framework.instructions.md# applies to orchestrator/agent code
  prompts/                         # each is a "/slash-command" you run in Copilot Chat
    scaffold-repo.prompt.md        # /scaffold-repo
    voice-gateway.prompt.md        # /voice-gateway
    orchestrator-workflow.prompt.md# /orchestrator-workflow
    mcp-tool.prompt.md             # /mcp-tool
    foundry-agent.prompt.md        # /foundry-agent
    foundry-iq-knowledge.prompt.md # /foundry-iq-knowledge
    emergency-fastpath.prompt.md   # /emergency-fastpath
    sepsis-workflow.prompt.md      # /sepsis-workflow
    observability.prompt.md        # /observability
    demo-app.prompt.md             # /demo-app  (interactive Streamlit cockpit)
  agents/
    agentic-architect.agent.md     # a custom "planner/architect" agent persona
  chatmodes/
    foundry-build.chatmode.md      # a focused chat mode for the whole build
docs/
  01-architecture.md               # the layered architecture + latency design
  02-stryker-workload-catalog.md   # the 16 workloads (the "what else can we add" answer)
  03-copilot-runbook.md            # the exact order to run the "/" commands
```

**How the pieces relate**
- `copilot-instructions.md` is loaded automatically on every Copilot request — it's the
  guardrails and glossary so Copilot always knows the stack, the layers, and the rules.
- `*.instructions.md` add path-specific rules (Python style, Agent Framework patterns).
- `*.prompt.md` are **reusable tasks you invoke with `/`** — one per build phase.
- `*.agent.md` / `*.chatmode.md` give Copilot a focused role for the whole effort.
- `docs/` is the human-readable design + the step-by-step runbook.

---

## 4. Quick start (10 minutes to first slash command)

> Prereqs: VS Code (latest), GitHub Copilot enabled, Python 3.11+, Azure CLI (`az login`),
> access to a **Microsoft Foundry** project with a `gpt-realtime` (Voice Live) deployment
> and an Azure AI Search service.

1. **Create the new repo** and copy the whole `.github/` and `docs/` folders from this kit
   into it. (Or start from a fork of `SIRE_demo` and drop these in.)
2. **Open the repo in VS Code.** Confirm Copilot picks up the customizations:
   Chat view → gear icon → *Instructions* / *Prompt Files* should list the files above.
3. **Wire up MCP** so Copilot's Agent mode can call your SIRE tools while it builds. Create
   `.vscode/mcp.json` (the `/scaffold-repo` prompt does this for you) pointing at the
   existing `mcp_server/`.
4. **Open Copilot Chat, switch to Agent mode**, pick a strong model, and select the
   **`Foundry Build`** chat mode (from `chatmodes/`).
5. **Run the runbook.** Follow `docs/03-copilot-runbook.md` — it's just a sequence of slash
   commands starting with `/scaffold-repo`.

---

## 5. How to drive GitHub Copilot (the "/" commands)

GitHub Copilot's customization model has four moving parts you'll use here. See
`docs/03-copilot-runbook.md` for the exact sequence; this is the cheat sheet.

**A. Built-in slash commands** (type `/` in Copilot Chat):
- `/init` — generate a first `.github/copilot-instructions.md` from your codebase. *(We ship
  a better one — use `/init` only if you want Copilot to refresh it after big changes.)*
- `/explain`, `/fix`, `/tests`, `/doc` — explain code, propose fixes, generate tests, write docs.
- `/create-prompt`, `/create-instruction`, `/create-agent` — scaffold **new** customization
  files with AI help (use these to add more workflows later).

**B. Your prompt files as slash commands.** Every file in `.github/prompts/*.prompt.md`
shows up in chat as `/<filename>`. Example: `/sepsis-workflow` runs the whole sepsis build
task with the context and acceptance criteria baked in. You can pass an argument, e.g.
`/mcp-tool blood-bank`.

**C. Context variables** (type `#` or `@` in chat) — feed Copilot the right context:
- `#codebase` (or `@workspace`) — let Copilot search the whole repo.
- `#file:search_client.py` — pin a specific file into context.
- `#fetch https://learn.microsoft.com/...` — **pull live docs** so Copilot uses the *current*
  Agent Framework / Foundry / Voice Live APIs instead of guessing. **Use this a lot** — these
  SDKs move fast.
- `#githubRepo nrs2130/SIRE_demo` — reference the original repo for patterns.

**D. Agent mode + MCP.** Switch Copilot Chat to **Agent mode** for multi-file, multi-step
edits. Register MCP servers in `.vscode/mcp.json` so Copilot can actually *call* your SIRE
search tools (and the new mock adapters) while building and testing.

> **Golden rule for accuracy:** the Microsoft Agent Framework, Foundry Agent Service, and
> Voice Live SDKs are evolving. Before writing SDK code, every prompt file tells Copilot to
> (1) `#fetch` the authoritative Microsoft Learn page, (2) pull a **working example** from the
> official samples repo `#githubRepo microsoft-foundry/foundry-samples` (start in
> `samples/python/`; `samples/csharp/` for .NET, `infrastructure/` for Bicep deploy templates)
> and **adapt** it, and (3) **pin exact package versions**. Docs are the contract; the samples
> repo is the reference implementation. Treat the code Copilot writes as a draft to run, not gospel.

---

## 6. The architecture at a glance

```
 ┌──────────────┐  voice   ┌───────────────────────────────────────────────┐
 │ Vocera        │◀────────▶│  L1  VOICE GATEWAY  (Azure Voice Live)         │
 │ Smartbadge    │  (badge  │  gpt-realtime → IntentEnvelope{intent,entities,│
 │  + panic btn  │  or web) │  urgency=EMERGENCY|ROUTINE, patient_ctx}       │
 └──────────────┘          └───────────────┬───────────────────────────────┘
                                            │  (urgency decided cheaply, here)
                       ┌────────────────────▼─────────────────────┐
                       │  L2  ORCHESTRATOR (Microsoft Agent        │
                       │       Framework Workflow — a graph)       │
                       │   ROUTER → { fast-path | standard-path }  │
                       │   fan-out ∥ … ∥ fan-in barrier            │
                       │   streams intermediate events → voice     │
                       └───┬───────────┬───────────┬───────────────┘
             ┌─────────────▼──┐ ┌──────▼───────┐ ┌─▼──────────────┐
             │ L3 Comms/Escal.│ │ L3 Sepsis    │ │ L3 SIRE (entity│  … hosted in
             │    agent       │ │    agent     │ │   resolution)  │  Foundry Agent
             └───────┬────────┘ └──────┬───────┘ └─────┬──────────┘  Service
                     │ MCP tool calls  │                │            (Control Plane)
        ┌────────────▼─────────────────▼────────────────▼─────────────┐
        │ L4 TOOLS = MCP servers wrapping Vocera Engage adapters        │
        │  on-call sched · HL7 labs · Patient Context REST · iBed bed   │
        │  telemetry · nurse-call SIP · monitor adapters · blood bank   │
        └───────────────────────────────┬──────────────────────────────┘
                        ┌────────────────▼─────────────────┐
                        │ L4b  FOUNDRY IQ knowledge base    │
                        │  (Azure AI Search): sepsis / code │
                        │  protocols, citation-backed RAG   │
                        └───────────────────────────────────┘
```

Full detail — including the **latency-aware fast path**, the fan-out/fan-in patterns, and
the mapping to Agent Framework primitives — is in **`docs/01-architecture.md`**.

---

## 7. The other agentic workflows (your "what else can we add?")

Short answer: the "smart badge" is **Vocera**, and Stryker already ships the whole stack —
the badge, the **Engage** workflow/alarm middleware (150+ documented integrations),
connected **ProCuity** beds, **LIFENET/LIFEPAK**, **Triton** blood-loss, and a public
**adapter catalog that reads like a menu of agent tools**. So beyond SIRE you can credibly
demo: **code blue activation**, **rapid response**, **sepsis hour-1 bundle**, **fall /
bed-exit response**, **monitor-alarm triage**, **postpartum-hemorrhage / massive transfusion**,
**STEMI/stroke pre-alert**, **locate/request equipment**, **on-call/consult resolution**,
**critical lab-value callback**, and **shift-handoff lookup**.

The full catalog — each mapped to the real Vocera/Stryker integration it would use, tagged
🔴 emergency vs 🟢 routine, with sources — is in **`docs/02-stryker-workload-catalog.md`**.

> **Integrity note for the workshop:** position the agent as an **LLM orchestration layer
> _above_ Vocera Engage**, *augmenting* the routing/alarm path — not replacing it. Engage's
> alarm notification (EMDAN) is **FDA 510(k)-cleared**, so anything touching clinical alarms
> is **human-in-the-loop**. Field-level device schemas, a Stryker-native RTLS, and an open
> third-party FHIR API are **not publicly documented** — mark them "to confirm with Stryker."
> Drawing that line makes you *more* credible, not less.

---

## 8. Suggested demo spine (for the workshop)

1. **Routine loop first** — "Call the on-call hospitalist." Proves voice → intent → tool →
   spoken confirmation (reuses SIRE entity resolution).
2. **Emergency, multi-agent** — "I have a patient with suspected sepsis in bed 12." Shows the
   **fast path**, **parallel** order-placement + paging + a compliance **timer**, and **live
   voice feedback** while slow steps run — the money shot for latency-aware orchestration.
3. **Control plane tab** — flip to Foundry to show the **hosted agents**, their tool calls,
   and traces. This is what makes it feel like a *platform*, not a script.

---

## 9. Is it interactive? Yes — the Streamlit cockpit

After it's built and deployed, you drive everything through an **interactive Streamlit app**
(built by `/demo-app`, extending `SIRE_demo`'s existing `streamlit_app.py`). It's the presenter's
cockpit and it's what makes the orchestration *visible*:
- a **badge simulator** — mic **and** a text box (for mic-less/CI demos) **and** a red **PANIC**
  button that forces an emergency route;
- the **live transcript**, including the acknowledgment-first message on emergencies;
- an **orchestration cockpit** showing the routing decision, each **concurrent branch** with a
  live **elapsed-vs-budget** timer (green within budget, amber + "still working…" on breach), and
  the agent/tool call log;
- the **sepsis hour-1 checklist** ticking off and the **compliance timer**; and
- a **Control Plane link** to the Foundry view of the hosted agents + traces for the run.

**Two topologies** (toggle in the app — see `docs/01-architecture.md` §9):
**(A) local orchestrator** calling Foundry hosted agents/tools remotely — best for the workshop,
maximum visibility; **(B) fully hosted** orchestrator with the app as a thin client — more
production-like. Both run against **mock MCP tools**, so no real hospital systems are needed to
demo. Start with (A).

## 10. Where to go next

- Read `docs/01-architecture.md` (design) → `docs/02-stryker-workload-catalog.md` (scope) →
  `docs/03-copilot-runbook.md` (do it).
- Then open Copilot Chat and run `/scaffold-repo`. Finish the build with `/demo-app`.

---

## 11. Development (contributing)

The `/scaffold-repo` step lays down the source layout (`src/gateway`, `src/orchestrator`,
`src/agents`, `src/tools`, `src/knowledge`, `src/telemetry`), a typed `config.py`, pinned
`requirements.txt`, and the `pytest` harness. To work on the platform locally:

```bash
# 1. Create and activate a virtual environment (Python 3.11+)
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate

# 2. Install pinned dependencies
pip install -r requirements.txt

# 3. Configure environment (never commit .env — it's git-ignored)
copy .env.example .env      # Windows  (cp on macOS/Linux)
#   Fill in Voice Live + AI Search values. Foundry/telemetry vars are optional
#   locally — the orchestrator runs against mock MCP tools without them.

# 4. Run the tests
pytest
```

**Running against mocks.** Every component's definition of done is that it runs locally
against **mock MCP tools** with no real hospital systems. The mock adapters live in
`src/tools/` and share the same MCP interface as the (future) real Vocera Engage adapters, so
swapping mock → real is a config change, not a rewrite. The existing SIRE search tool is
registered in `.vscode/mcp.json` so Copilot Agent mode can call it while you build.

**Conventions** (see `.github/instructions/`): Python 3.11+, full type hints, `async`/`await`
for all I/O, config from environment via the typed dataclasses in `config.py`, and
OpenTelemetry spans on every orchestrator node and tool call. Format with `black` + `ruff`;
each component ships a `pytest` test (happy path + one timeout/failure path).
