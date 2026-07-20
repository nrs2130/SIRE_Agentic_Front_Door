---
description: Focused chat mode for building the Nightingale voice-agentic platform on Microsoft Foundry
tools: ['codebase', 'search', 'fetch', 'githubRepo', 'editFiles', 'runCommands', 'runTests', 'usages']
---

# Foundry Build mode

Use this mode for the whole Nightingale build. It keeps Copilot oriented to the architecture,
the Microsoft stack, and the latency/safety rules.

**Always in scope**
- Target stack: **Azure Voice Live** (front door) → **Microsoft Agent Framework** workflow
  (orchestration) → **Foundry Agent Service** hosted agents (Control Plane) → **MCP tools**
  (Vocera Engage adapters, mocked) → **Foundry IQ** (protocol grounding).
- Read `.github/copilot-instructions.md`, `docs/01-architecture.md`, and the relevant
  `.instructions.md` before acting.

**Operating rules**
1. `#fetch` current Microsoft Learn docs before any SDK code; pin exact versions; don't invent
   API symbols.
2. Respect layer boundaries (gateway / orchestrator / agents / tools / knowledge).
3. Emergency intents: acknowledge first, escalate first, enrich in parallel, budget every branch.
4. Clinical actions are human-in-the-loop; the agent augments Vocera Engage, never overrides its
   FDA-cleared alarm path.
5. Everything runs against **mock MCP tools**; every component ships with a `pytest`.
6. Prefer small diffs; run tests and show results after each change.

**Preferred flow:** plan → (fetch docs) → implement one component → test → summarize what
changed and what's next. When a `.github/prompts/*.prompt.md` matches the task, follow its
acceptance criteria exactly.
