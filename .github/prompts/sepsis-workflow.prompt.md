---
mode: agent
description: Assemble the flagship sepsis showcase — 4 concurrent agents, live voice updates, hour-1 timer
tools: ['codebase', 'search', 'fetch', 'editFiles', 'runCommands', 'runTests']
---

# /sepsis-workflow

Assemble the **sepsis hour-1 showcase** — the flagship emergency demo. Depends on the gateway,
orchestrator + fast path, the `sepsis`/`comms` agents, the `labs_hl7`/`patient_context`/
`bed_telemetry`/`monitor` tools, and the Foundry IQ `sepsis-protocols` KB. Read
`docs/02-stryker-workload-catalog.md` Part D and `docs/01-architecture.md` §3.2.

## Task
Wire the end-to-end flow for utterance "**patient in bed 12 looks septic**":
1. **Screening agent** — read vitals (RR, SBP, temp, HR) from `bed_telemetry`/`monitor` mocks +
   mentation from the nurse's voice; compute **SIRS** and **qSOFA**; flag "suspicion of sepsis".
2. On a positive screen, take the **fast path** and immediately speak: "Starting the sepsis
   hour-1 protocol and paging the response team." Then **fan-out to four concurrent agents**:
   - **orders** → place lactate + blood cultures (via `labs_hl7`),
   - **comms/escalation** → page provider/RRT by role (via `comms_page`),
   - **knowledge** → fetch the hour-1 bundle from Foundry IQ (with citations),
   - **timer/compliance** → start the **hour-1 window** clock.
3. **Read back the 5-element checklist** (measure lactate; cultures before antibiotics;
   broad-spectrum antibiotics; 30 mL/kg crystalloid if hypotensive or lactate ≥4; vasopressors
   for MAP ≥65) and stream **live spoken progress** as each branch completes.
4. **Compliance tracking** — the timer agent tracks each step against the hour-1 window and
   prompts **re-measure lactate if initial > 2 mmol/L**.
5. **Human-in-the-loop** — orders are *prepared and read back for confirmation*, not autonomously
   executed; protocol text is decision support, not a medical order.

## Acceptance criteria
- End-to-end test from the utterance: immediate acknowledgment (under budget), **four branches
  run concurrently** (assert wall-clock < sum of delays), spoken progress streamed, checklist
  read back with **citations**, and the compliance timer active.
- The lactate re-measure prompt fires when the mock returns an initial lactate > 2.
- No autonomous clinical action; all orders require confirmation.
- Reuses existing agents/tools — no duplicated logic.

Summarize the demo script (what the nurse says / hears) for the workshop.
