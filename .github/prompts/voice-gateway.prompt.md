---
mode: agent
description: Build the L1 Voice Gateway — Voice Live session that emits an Intent Envelope + urgency
tools: ['codebase', 'search', 'fetch', 'githubRepo', 'editFiles', 'runCommands', 'runTests']
---

# /voice-gateway

Build **L1, the Voice Gateway**. Read `docs/01-architecture.md` §2–§3 and the
`agent-framework.instructions.md` accuracy rules first. `#fetch`
https://learn.microsoft.com/azure/ai-services/speech-service/voice-live before writing SDK code
and pin the SDK version.

## Context
`SIRE_demo`'s `main.py` already runs a Voice Live session with `gpt-realtime` function calling
and dispatches to search tools. Reuse that loop; do **not** rewrite it.

## Task
1. In `src/gateway/`, wrap the Voice Live session so its **only** outputs are:
   - a normalized **Intent Envelope**:
     `{correlation_id, intent, urgency, entities, patient_context: null, utterance,
     spoken_ack_required}`; and
   - a channel to **speak** text back (for acknowledgments and streamed updates).
2. Extend the realtime model's function-calling schema so a single call returns **intent +
   entities + urgency** together. `urgency ∈ {EMERGENCY, ROUTINE}` must be classified **here**,
   in that same call — no separate LLM round trip.
3. Map a **panic-button** signal (badge or a keyboard/UI stand-in) to a hard
   `urgency = EMERGENCY` override that bypasses classification.
4. Define an `IntentEnvelope` dataclass in a shared module (both gateway and orchestrator import
   it — it's the L1↔L2 contract). Generate a `correlation_id` per utterance.
5. Provide a `speak(text)` coroutine the orchestrator can call to stream spoken updates, and make
   sure **barge-in** still works (the nurse can interrupt).
6. Add a **text-stub mode** (type an utterance instead of speaking) so the whole system is
   testable without a mic in CI.

## Acceptance criteria
- A spoken or typed utterance yields a valid `IntentEnvelope` with a `correlation_id`.
- An emergency phrase ("suspected sepsis", "code blue", "patient fell") sets
  `urgency=EMERGENCY` in the single function call; a routine phrase sets `ROUTINE`.
- The panic-button path forces `EMERGENCY` without model classification.
- `pytest` covers: envelope shape, urgency classification (both classes), panic override,
  text-stub mode. All pass.
- Voice Live SDK version is pinned and commented; no invented API symbols.

Explain the envelope contract and how `/orchestrator-workflow` will consume it.
