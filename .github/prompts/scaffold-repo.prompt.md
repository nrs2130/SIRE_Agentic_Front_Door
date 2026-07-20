---
mode: agent
description: Scaffold the Nightingale repo layout, config, deps, tests, and MCP wiring
tools: ['codebase', 'search', 'fetch', 'githubRepo', 'editFiles', 'runCommands', 'runTests']
---

# /scaffold-repo

Scaffold the foundation for the Nightingale voice-agentic platform. Read
`.github/copilot-instructions.md` and `docs/01-architecture.md` first.

## Context
This repo extends `#githubRepo nrs2130/SIRE_demo` (Azure Voice Live + Azure AI Search RRF, with
an existing `mcp_server/`). We're adding orchestration, more agents, and Foundry deployment.

## Task
1. Create the source layout:
   ```
   src/gateway/ src/orchestrator/ src/agents/ src/tools/ src/knowledge/ src/telemetry/
   infra/ tests/
   ```
   Add `__init__.py` where needed and a one-line module docstring stating each layer's job.
2. Create `config.py` as a **typed dataclass** loaded from environment, following the
   `SIRE_demo` pattern. Include (at least): Voice Live endpoint/model/voice, AI Search
   endpoint/indexes, Foundry project endpoint, auth mode (token vs key), and per-node latency
   budgets. No hardcoded secrets or endpoints.
3. Create `requirements.txt` with **pinned versions**. Before adding any Microsoft SDK, `#fetch`
   its current Microsoft Learn/PyPI page and pin the version you verified; add a comment with the
   version. Include: the Voice Live SDK, the Microsoft Agent Framework package, the Foundry
   project/agents SDK, an MCP SDK, OpenTelemetry, and `pytest`/`pytest-asyncio`.
4. Create `.env.example` documenting every variable in `config.py`. Ensure `.env` is git-ignored.
5. Create `.vscode/mcp.json` registering the existing `mcp_server/` (SIRE search) so Copilot
   Agent mode can call it during the build. Leave a placeholder block for additional local MCP
   tool servers we'll add in `/mcp-tool`.
6. Add `tests/conftest.py` and one trivial passing test so `pytest` is wired up.
7. Add a short `CONTRIBUTING`/dev section to the README explaining `pip install -r
   requirements.txt`, `pytest`, and how to run against mocks.

## Acceptance criteria
- `pip install -r requirements.txt` succeeds; every Microsoft SDK version is pinned and
  commented with the doc/PyPI page you verified.
- `pytest` collects and the trivial test passes.
- `config.py` reads everything from env; no secrets or endpoints hardcoded.
- `.vscode/mcp.json` lists the SIRE MCP server; the SIRE tool appears in Copilot's tool picker.
- Do **not** modify `SIRE_demo`'s working Voice Live / RRF code — only extend/scaffold around it.

Explain what you created and what `/voice-gateway` will build next.
