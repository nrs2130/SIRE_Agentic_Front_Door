---
description: Python conventions for the Nightingale voice-agentic platform
applyTo: "**/*.py"
---

# Python conventions

- **Python 3.11+**, matching `SIRE_demo`. Full type hints on public functions.
- **Async everywhere for I/O**: `async`/`await`, `asyncio`. Voice, tool calls, and orchestrator
  branches are all async so parallelism and latency budgets work.
- **Config via typed dataclass** in `config.py`, loaded from environment (extend the existing
  `SIRE_demo` pattern). No hardcoded endpoints, keys, model names, or deployment names.
- **Auth**: `DefaultAzureCredential` / `AzureCliCredential` preferred over API keys; keep the
  `--use-token-credential` option working.
- **Errors & timeouts**: wrap external calls with `asyncio.wait_for` and explicit timeouts;
  never let a slow tool block a spoken acknowledgment. Convert exceptions into typed results the
  orchestrator can branch on (don't leak stack traces into the voice channel).
- **Latency budgets**: any awaited tool/agent call in the orchestrator must have a budget and a
  fallback event. Record the actual elapsed time as a span attribute.
- **Logging/tracing**: structured logs + OpenTelemetry spans carrying `correlation_id`,
  `urgency`, node name, latency. No `print` in library code.
- **Style/tooling**: `black` + `ruff`; `pytest` for tests (happy path + one timeout/failure
  path per component). Keep functions small and single-responsibility.
- **Secrets**: `.env` is git-ignored; keep `.env.example` current. Never commit secrets.
- **Don't break SIRE**: extend the Voice Live + AI Search RRF code; don't rewrite working paths.
