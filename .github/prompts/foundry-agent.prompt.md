---
mode: agent
description: Build one L3 specialized agent and its Foundry hosted-agent registration — pass the capability name
tools: ['codebase', 'search', 'fetch', 'editFiles', 'runCommands', 'runTests']
---

# /foundry-agent

Build one **L3 specialized agent**: `${input:capability:which capability? e.g. comms, sepsis, beds, supplies, equipment}`.
Read `docs/01-architecture.md` §3, §6 and `agent-framework.instructions.md` first. **`#fetch`**
https://learn.microsoft.com/azure/foundry/ and the hosted-MCP-tools doc before coding; pin the
Foundry SDK version.

## Task
1. In `src/agents/`, implement a single-responsibility agent for `${input:capability}` that:
   - accepts the relevant slice of the `IntentEnvelope`,
   - calls its **L4 MCP tools** (mock-backed) to do its work, and
   - returns a typed result plus a short **spoken-update** string for streaming.
2. Keep it focused: `comms` pages/calls by role; `sepsis` runs screening + hour-1 steps;
   `beds` reads ProCuity telemetry; `supplies` checks blood/stock; `equipment` locates devices.
   No cross-capability logic.
3. **Foundry hosted-agent registration**: add a script under `infra/` (or `src/agents/<cap>/
   register.py`) that creates this agent in **Foundry Agent Service** so it appears as a
   **hosted agent** in the **Control Plane**, and attaches its MCP tools as **hosted MCP tools**.
   Support a `--dry-run` that prints what would be created without calling Azure. Use
   `DefaultAzureCredential`.
4. If the capability is clinical (e.g. `sepsis`), connect it to the **Foundry IQ** knowledge base
   (built by `/foundry-iq-knowledge`) and make it **cite sources**. Clinical actions are
   **human-in-the-loop** — the agent prepares/reads back/confirms; it never issues autonomous
   orders or overrides alarms.
5. Register the agent with the orchestrator's router for its intents.

## Acceptance criteria
- The agent runs **locally against mock MCP tools** with no Azure dependency.
- `pytest` covers a happy path and one tool-failure/timeout path; both pass.
- The registration script `--dry-run` prints a correct plan; real run creates a hosted agent
  visible in the Foundry Control Plane.
- Clinical agents cite Foundry IQ sources and take no autonomous clinical action.
- Foundry SDK version pinned + commented; no invented API symbols.

Confirm the agent's responsibility, the tools it calls, and the intents it handles.
