---
mode: agent
description: Build the interactive Streamlit demo app — badge simulator + live orchestration visualization
tools: ['codebase', 'search', 'fetch', 'githubRepo', 'editFiles', 'runCommands', 'runTests']
---

# /demo-app

Build the **interactive demo app** — the front-end a presenter drives at the workshop. Read
`docs/01-architecture.md` §9 (Presentation layer & deployment topologies). This extends
`SIRE_demo`'s existing `streamlit_app.py`; do **not** start from scratch.

## Context
`SIRE_demo` already ships a Streamlit app with START/STOP session controls, a live transcript,
and a search-results panel. We grow it into a **badge simulator + orchestration cockpit** that
makes the latency-aware, multi-agent behavior *visible*.

## Task
Build `streamlit_app.py` (extend the existing one) with:
1. **Badge simulator (input)** — mic capture (reuse the Voice Live path) **and** a text box to
   type an utterance for mic-less/CI demos, plus a big red **PANIC button** that forces
   `urgency=EMERGENCY`.
2. **Live transcript** — nurse (user) turns and the agent's spoken turns, streamed as they occur
   (including the **acknowledgment-first** message on emergencies).
3. **Orchestration cockpit (the money shot)** — visualize the running workflow in real time:
   - the **routing decision** (FAST PATH vs STANDARD PATH) and the `correlation_id`;
   - one row per **concurrent branch** (agent/tool) with a live status (queued → running → done)
     and its **elapsed time vs latency budget** (green within budget, amber on breach with the
     "still working…" note);
   - a running **agent/tool call log** (which MCP tool, inputs summary, latency).
4. **Sepsis panel (when active)** — the **hour-1 checklist** (5 elements) ticking off as branches
   complete, and the **compliance timer** counting the hour-1 window.
5. **Control Plane link** — a button/link to the Foundry portal view of the **hosted agents** and
   traces for the current `correlation_id` (from `/observability`).
6. **Backend toggle** — reuse/extend the existing Streamlit backend toggle so the app can run the
   orchestrator **locally** (calling Foundry hosted agents + MCP tools remotely) or point at a
   **fully-hosted** orchestrator endpoint (see the two topologies in `docs/01-architecture.md` §9).

## How to render live updates
The orchestrator streams **intermediate events** (see `/orchestrator-workflow`). Subscribe to
that event stream and update the cockpit widgets as events arrive (Streamlit
`st.empty()`/`st.status`/fragments or `st.rerun` on a queue). Never block the UI thread on a slow
branch — the whole point is to *show* work continuing while a slow tool runs.

## Acceptance criteria
- Runs with `streamlit run streamlit_app.py` against **mock MCP tools**, no real hospital systems.
- Typing "patient in bed 12 looks septic" shows: immediate acknowledgment in the transcript, the
  FAST PATH badge, **four branches running concurrently** with live timers, the hour-1 checklist
  ticking, and the compliance timer.
- The PANIC button forces an emergency route.
- A branch pushed past its budget turns amber and shows "still working…", and the app stays
  responsive.
- The Control Plane link opens the Foundry view for the current run.
- Reuses the existing Voice Live + RRF + Streamlit code; no duplicated logic.

Summarize the on-stage click-path for the workshop (what the presenter does, in order).
