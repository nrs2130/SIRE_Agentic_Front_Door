---
mode: agent
description: Harden the L2 emergency fast path — acknowledge-first, latency budgets, speculative dispatch
tools: ['codebase', 'search', 'fetch', 'editFiles', 'runCommands', 'runTests']
---

# /emergency-fastpath

Harden the orchestrator's **emergency fast path** so it meets the latency requirement. Read
`docs/01-architecture.md` §3.2 and §4. This is the demo's money shot — get the behavior exactly
right.

## Task
1. **Acknowledge first.** On an `EMERGENCY` envelope, emit a spoken acknowledgment event
   **before any slow tool call resolves**. Assert this in a test.
2. **Escalate first.** On the fan-out, fire the escalation/notification tool (`comms_page`)
   **before** slower branches (orders, retrieval). The team is being reached while enrichment
   runs.
3. **Latency budgets.** Give every branch a soft timeout from `config.py` (see the budget table
   in `docs/01-architecture.md` §4). Wrap awaits in `asyncio.wait_for`. On breach, emit a
   "still working on X" spoken event and **continue** — never block the conversation.
4. **Speculative / async dispatch.** Kick off slow branches immediately; only join at a
   **fan-in barrier** where a result is truly needed for the next spoken step. Everything else
   streams as it completes.
5. **Custom aggregator.** Produce a rolling spoken status ("paged RRT ✓, lactate order placed ✓,
   awaiting blood bank…") from out-of-order branch completions.
6. **Record latencies** as OpenTelemetry span attributes for every branch (feeds `/observability`).

## Acceptance criteria
- Test: with a branch artificially delayed to 3 s, the **acknowledgment latency stays under
  budget** (e.g. < 300 ms) — assert the measured value.
- Test: escalation branch starts before the slow branches (assert ordering/timestamps).
- Test: a branch that exceeds its budget emits a "still working" event and the workflow still
  completes.
- No `await` on a slow tool blocks the acknowledgment or the whole conversation.

Report the measured acknowledgment and per-branch latencies from the test run.
