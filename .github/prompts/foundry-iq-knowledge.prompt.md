---
mode: agent
description: Build a Foundry IQ knowledge base over Azure AI Search for clinical protocols — pass the KB name
tools: ['codebase', 'search', 'fetch', 'editFiles', 'runCommands', 'runTests']
---

# /foundry-iq-knowledge

Stand up a **Foundry IQ knowledge base** for grounded, citation-backed protocol retrieval:
`${input:kbName:knowledge base name, e.g. sepsis-protocols}`. Read `docs/01-architecture.md` §6
and `docs/02-stryker-workload-catalog.md` Part D. **`#fetch`**
https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect before coding.

## Task
1. In `src/knowledge/`, add an ingestion script that indexes protocol documents into an Azure
   AI Search index (reuse the existing AI Search service from `config.py`). Seed it with the
   **sepsis hour-1 bundle** content from `docs/02-stryker-workload-catalog.md` Part D (5
   elements + SIRS/qSOFA screening), each chunk carrying a **source citation** field. Structure
   it so more protocols (code blue, RRT, fall) can be added later.
2. Create the **Foundry IQ knowledge base** over that index and connect it to the clinical
   agent(s) so their answers are **grounded with citations**.
3. Add a retrieval smoke test: "What is the sepsis hour-1 bundle?" must return the 5 elements
   **with citations**, and "What qualifies as suspicion of sepsis?" must return SIRS/qSOFA.
4. **Clinical-accuracy guardrail:** mark the seeded content as the 2018 SSC hour-1 update and add
   a `# TODO: confirm against SSC 2021` note. The agent must not present protocol text as a
   medical order — it's decision support, human-in-the-loop.

## Acceptance criteria
- Ingestion is idempotent and re-runnable; documents carry citation metadata.
- The knowledge base is connected to the sepsis agent; answers include citations.
- Retrieval smoke test passes for both queries.
- Any Azure Search / Foundry IQ SDK versions pinned + commented; no invented API symbols.

Explain how the sepsis agent uses this KB and where to add the next protocol.
