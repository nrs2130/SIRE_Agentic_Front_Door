"""L4 MCP tool: ``oncall_lookup`` — resolve a clinical role to the on-call person.

Real adapter: Vocera Engage **on-call scheduling adapters** — AMiON / QGenda / Spok /
Lightning Bolt / Shift Admin (docs/02-stryker-workload-catalog.md Part C, "On-call /
staffing"). Engage resolves *role → current person* from the hospital's scheduling
system; this tool is the thin MCP wrapper over that surface.

Layer rules (docs/01-architecture.md §5): a tool does **I/O only** — it looks up who
is on call and returns it. It makes no clinical decision, does not page or call anyone
(that is ``comms_page`` / ``comms_call``), and contains no business logic. Swapping the
mock for the real adapter is a config change (``TOOLS_USE_REAL_ADAPTER``), not a rewrite.

MCP SDK: ``mcp[cli]==1.28.1`` (pinned in requirements.txt; v1.x stable line).
FastMCP server pattern matches the existing ``mcp_server/`` in this repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from config import ToolsConfig
from src.telemetry import traced_tool

logger = logging.getLogger("nightingale.tools.oncall_lookup")

TOOL_NAME = "oncall_lookup"


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class OnCallProvider:
    """A resolved on-call person for a role."""

    role: str
    name: str
    provider_id: str
    pager: str
    phone: str
    presence: str  # available | busy | off
    shift_start: str  # ISO-8601
    shift_end: str  # ISO-8601


@dataclass(frozen=True)
class OnCallLookupResult:
    """Typed output of an on-call lookup (the tool's I/O contract)."""

    role: str
    location: str | None
    resolved_at: str  # ISO-8601
    source_system: str  # which scheduling adapter answered
    elapsed_ms: float
    primary: OnCallProvider | None
    backup: OnCallProvider | None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class OnCallAdapter(Protocol):
    """Interface a real Vocera on-call adapter must implement.

    The mock and the future real adapter share this signature exactly, so the
    orchestrator/agents are unaffected by which one is wired.
    """

    source_system: str

    async def resolve(self, role: str, location: str | None = None) -> OnCallLookupResult:
        """Resolve ``role`` (optionally scoped to ``location``) to the on-call person."""
        ...


# --- Mock adapter (realistic data + configurable latency) --------------------
# Realistic on-call roster keyed by canonical role. Names/IDs are synthetic.
_ROSTER: dict[str, tuple[OnCallProvider, OnCallProvider]] = {
    "hospitalist": (
        OnCallProvider("hospitalist", "Dr. Priya Nadkarni", "prov-10231", "12045",
                       "+1-555-0142", "available", "2026-07-20T07:00:00Z", "2026-07-20T19:00:00Z"),
        OnCallProvider("hospitalist", "Dr. Marcus Webb", "prov-10244", "12088",
                       "+1-555-0177", "available", "2026-07-20T07:00:00Z", "2026-07-20T19:00:00Z"),
    ),
    "cardiologist": (
        OnCallProvider("cardiologist", "Dr. Elena Ruiz", "prov-20455", "13010",
                       "+1-555-0203", "busy", "2026-07-20T08:00:00Z", "2026-07-21T08:00:00Z"),
        OnCallProvider("cardiologist", "Dr. Sam Okafor", "prov-20461", "13022",
                       "+1-555-0219", "available", "2026-07-20T08:00:00Z", "2026-07-21T08:00:00Z"),
    ),
    "intensivist": (
        OnCallProvider("intensivist", "Dr. Hana Kim", "prov-30512", "14005",
                       "+1-555-0311", "available", "2026-07-20T07:00:00Z", "2026-07-20T19:00:00Z"),
        OnCallProvider("intensivist", "Dr. Leo Fontaine", "prov-30519", "14017",
                       "+1-555-0329", "off", "2026-07-20T19:00:00Z", "2026-07-21T07:00:00Z"),
    ),
    "rrt": (
        OnCallProvider("rrt", "RRT Team Bravo (C. Adeyemi, RN)", "team-rrt-b", "15000",
                       "+1-555-0400", "available", "2026-07-20T07:00:00Z", "2026-07-20T19:00:00Z"),
        OnCallProvider("rrt", "RRT Team Alpha (J. Two, RN)", "team-rrt-a", "15001",
                       "+1-555-0401", "available", "2026-07-20T07:00:00Z", "2026-07-20T19:00:00Z"),
    ),
}
# Common synonyms mapped to the canonical roster key.
_ROLE_ALIASES = {
    "on-call hospitalist": "hospitalist",
    "on call hospitalist": "hospitalist",
    "medicine": "hospitalist",
    "cards": "cardiologist",
    "cardiology": "cardiologist",
    "icu": "intensivist",
    "critical care": "intensivist",
    "rapid response": "rrt",
    "rapid response team": "rrt",
}


def _canonical_role(role: str) -> str:
    key = role.strip().lower()
    return _ROLE_ALIASES.get(key, key)


class MockOnCallAdapter:
    """Simulates a Vocera on-call scheduling adapter with realistic data + latency."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (AMiON/QGenda/Spok/Lightning Bolt)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def resolve(self, role: str, location: str | None = None) -> OnCallLookupResult:
        t0 = time.perf_counter()
        await self._simulate_latency()
        canonical = _canonical_role(role)
        resolved_at = datetime.now(timezone.utc).isoformat()
        entry = _ROSTER.get(canonical)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if entry is None:
            logger.info("oncall_lookup role=%r UNRESOLVED elapsed_ms=%s", role, elapsed_ms)
            return OnCallLookupResult(
                role=role, location=location, resolved_at=resolved_at,
                source_system=self.source_system, elapsed_ms=elapsed_ms,
                primary=None, backup=None,
                error=f"No on-call provider found for role '{role}'.",
            )
        primary, backup = entry
        logger.info(
            "oncall_lookup role=%r -> %s elapsed_ms=%s", role, primary.name, elapsed_ms
        )
        return OnCallLookupResult(
            role=canonical, location=location, resolved_at=resolved_at,
            source_system=self.source_system, elapsed_ms=elapsed_ms,
            primary=primary, backup=backup,
        )


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_oncall_adapter(config: ToolsConfig | None = None) -> OnCallAdapter:
    """Return the on-call adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`OnCallAdapter` (same signature) is returned — no caller
    change. The real adapter lands with the live Engage integration.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: Vocera Engage on-call scheduling adapter (AMiON/QGenda/Spok).
        # TODO: implement VoceraOnCallAdapter against the Engage REST surface and
        # return it here. Until then, fail loudly rather than silently mocking.
        raise NotImplementedError(
            "Real Vocera on-call adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockOnCallAdapter(config)


@traced_tool("oncall_lookup")
async def lookup_oncall(
    role: str,
    location: str | None = None,
    *,
    config: ToolsConfig | None = None,
    adapter: OnCallAdapter | None = None,
) -> OnCallLookupResult:
    """Resolve an on-call role, bounded by the configured timeout.

    A slow adapter surfaces as a typed timeout result (``error`` set) rather than
    hanging the orchestrator — the tool never blocks the conversation.
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_oncall_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.resolve(role, location), timeout=config.timeout_ms / 1000.0
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning("oncall_lookup role=%r TIMEOUT elapsed_ms=%s", role, elapsed_ms)
        return OnCallLookupResult(
            role=role, location=location,
            resolved_at=datetime.now(timezone.utc).isoformat(),
            source_system=getattr(adapter, "source_system", "unknown"),
            elapsed_ms=elapsed_ms, primary=None, backup=None,
            error=f"On-call lookup timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the ``oncall_lookup`` tool.

    Returned so both local dev (stdio via ``python -m src.tools``) and the Foundry
    hosted-agent registration (attach the server endpoint as a hosted MCP tool)
    can mount the same tool.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale On-Call Lookup",
        instructions=(
            "Resolve a clinical role (e.g. 'on-call hospitalist', 'cardiologist', "
            "'intensivist', 'RRT') to the current on-call provider. Read-only: this "
            "tool identifies who is on call; it does not page or call them."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def oncall_lookup(role: str, location: str = "") -> str:
        """Resolve a clinical role to the current on-call provider (read-only).

        Args:
            role: Clinical role or team, e.g. "on-call hospitalist", "cardiologist",
                  "intensivist", "RRT".
            location: Optional unit/location to scope the schedule, e.g. "4 West".

        Returns:
            JSON ``OnCallLookupResult`` with ``primary``/``backup`` providers
            (name, IDs, pager, phone, presence, shift window), the ``source_system``
            that answered, and ``elapsed_ms``. On failure, ``error`` is set.
        """
        result = await lookup_oncall(role, location or None, config=config)
        return json.dumps(result.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the on-call lookup MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
