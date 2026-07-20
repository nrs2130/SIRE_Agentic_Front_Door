---
mode: agent
description: Build the L2 orchestrator — Agent Framework workflow with router, fast/standard paths, streaming
tools: ['codebase', 'search', 'fetch', 'githubRepo', 'editFiles', 'runCommands', 'runTests']
---

# /orchestrator-workflow

Build **L2, the orchestrator**, as a Microsoft Agent Framework **workflow graph**. Read
`docs/01-architecture.md` §3 and `agent-framework.instructions.md` first. **`#fetch`**
https://learn.microsoft.com/agent-framework/workflows/orchestrations/concurrent and the
workflows overview before coding; pin the Agent Framework package version.

## Task
1. In `src/orchestrator/`, build a workflow that consumes an `IntentEnvelope` and produces
   streamed events + a final spoken summary.
2. **Router executor** — deterministic switch on `urgency` (and `intent`):
   - `EMERGENCY → fast path`
   - `ROUTINE → standard path`
   It must be a plain executor (no LLM call) so routing adds ~0 ms.
3. **Standard path** — enrich (Patient Context + on-call lookup, concurrently) → resolve entity
   via the SIRE tool → act (comms) → speak a read-back confirmation.
4. **Fast path** — a stub for now (the full behavior is hardened in `/emergency-fastpath`), but
   already: emit a spoken **acknowledgment event first**, then fan-out to branches concurrently.
5. **Streaming** — subscribe to workflow events and forward **intermediate outputs** to the
   gateway's `speak()` as they arrive. Use a **custom aggregator** for the final spoken summary
   ("done: …; pending: …") when branches complete out of order.
6. Wire **two placeholder agents** (simple echo/mock agents) so you can prove routing +
   concurrency + streaming end-to-end before real agents exist.
7. Thread `correlation_id` through every node.

## Acceptance criteria
- `pytest` proves: an `EMERGENCY` envelope takes the fast path and emits an **acknowledgment
  event before** a deliberately-delayed branch resolves; a `ROUTINE` envelope takes the standard
  path.
- Concurrent branches run in parallel (assert wall-clock < sum of branch delays).
- Intermediate events are streamed (not just a final result).
- Agent Framework version pinned + commented; documented patterns used, no invented symbols
  (leave `# TODO: verify` if any symbol is uncertain).

Explain the graph shape and how `/mcp-tool` and `/foundry-agent` plug agents into it.
