"""L4 MCP tool: ``bed_telemetry`` — read Stryker smart-bed telemetry.

Real adapter: Stryker **iBed Adapter** (ProCuity → Engage) (docs/01-architecture.md §5;
docs/02-stryker-workload-catalog.md Part C — "Bed adapters: Stryker iBed (ProCuity →
Engage)"). This tool is the thin MCP wrapper over the bed's telemetry feed: bed-exit
state, position, patient weight, and siderail status. The bed and Engage own the data;
this tool only reads it.

SAFETY (docs/01-architecture.md, copilot-instructions.md):
- Tools do **I/O only** — this is a **read**: it returns the bed's current telemetry.
  It makes no clinical decision, raises no alarm, and changes no bed setting.
- Bed-exit is a safety signal that flows through Engage's cleared alarm path. This tool
  is **read/notify-only**: it surfaces the state for a human to act on; it never arms,
  suppresses, or overrides the bed-exit alarm — human-in-the-loop.

MCP SDK: ``mcp[cli]==1.28.1`` (pinned in requirements.txt; v1.x stable line).
FastMCP server pattern matches the existing ``mcp_server/`` in this repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from config import ToolsConfig
from src.telemetry import traced_tool

logger = logging.getLogger("nightingale.tools.bed_telemetry")

TOOL_NAME = "bed_telemetry"


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class BedTelemetryResult:
    """Typed output of a bed-telemetry read (the tool's read-only I/O contract).

    All fields are reported verbatim from the bed. ``bed_exit_alarm`` reflects the bed's
    own alarm state (owned by Engage's cleared path) — this tool never sets it.
    """

    bed_ref: str
    resolved: bool
    bed_id: str | None
    location: str | None
    bed_exit_alarm: str | None  # armed | triggered | off  (verbatim from bed)
    patient_present: bool | None
    position: str | None  # e.g. "head 30°", "flat", "chair"
    head_of_bed_deg: int | None
    weight_kg: float | None
    siderails: dict[str, str] | None = None  # {upper_left: up/down, ...}  verbatim
    brake_set: bool | None = None
    last_updated: str = ""
    source_system: str = ""
    retrieved_at: str = ""
    elapsed_ms: float = 0.0
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class BedTelemetryAdapter(Protocol):
    """Interface a real Stryker iBed telemetry adapter must implement.

    Mock and future real adapter share this signature exactly, so callers are unaffected
    by which one is wired.
    """

    source_system: str

    async def read(
        self, bed_ref: str, *, correlation_id: str | None = None
    ) -> BedTelemetryResult:
        """Read current telemetry for ``bed_ref``."""
        ...


# --- Mock adapter (realistic data + configurable latency) --------------------
# Realistic bed telemetry keyed by a normalized bed reference. Synthetic data.
_BEDS: dict[str, BedTelemetryResult] = {
    "bed 12": BedTelemetryResult(
        bed_ref="bed 12", resolved=True, bed_id="PROCUITY-4W-12", location="4 West, bed 12",
        bed_exit_alarm="armed", patient_present=True, position="head 30°",
        head_of_bed_deg=30, weight_kg=82.4,
        siderails={"upper_left": "up", "upper_right": "up", "lower_left": "down", "lower_right": "down"},
        brake_set=True, last_updated="2026-07-20T14:59:40Z",
    ),
    "bed 7": BedTelemetryResult(
        bed_ref="bed 7", resolved=True, bed_id="PROCUITY-4W-07", location="4 West, bed 7",
        bed_exit_alarm="triggered", patient_present=False, position="flat",
        head_of_bed_deg=0, weight_kg=0.0,
        siderails={"upper_left": "up", "upper_right": "down", "lower_left": "down", "lower_right": "down"},
        brake_set=True, last_updated="2026-07-20T15:00:02Z",
    ),
    "room 4": BedTelemetryResult(
        bed_ref="room 4", resolved=True, bed_id="PROCUITY-ICU-04", location="ICU, room 4",
        bed_exit_alarm="armed", patient_present=True, position="chair",
        head_of_bed_deg=60, weight_kg=95.1,
        siderails={"upper_left": "up", "upper_right": "up", "lower_left": "up", "lower_right": "up"},
        brake_set=True, last_updated="2026-07-20T14:58:11Z",
    ),
}


def _normalize_ref(bed_ref: str) -> str:
    return " ".join(bed_ref.strip().lower().split())


class MockBedTelemetryAdapter:
    """Simulates the Stryker iBed telemetry feed, with realistic latency."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (Stryker iBed Adapter, ProCuity -> Engage)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def read(
        self, bed_ref: str, *, correlation_id: str | None = None
    ) -> BedTelemetryResult:
        t0 = time.perf_counter()
        retrieved_at = datetime.now(timezone.utc).isoformat()

        if not bed_ref.strip():
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return BedTelemetryResult(
                bed_ref=bed_ref, resolved=False, bed_id=None, location=None,
                bed_exit_alarm=None, patient_present=None, position=None,
                head_of_bed_deg=None, weight_kg=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error="bed_ref is required.",
            )

        await self._simulate_latency()
        record = _BEDS.get(_normalize_ref(bed_ref))
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if record is None:
            logger.info(
                "bed_telemetry ref=%r UNRESOLVED correlation_id=%s elapsed_ms=%s",
                bed_ref, correlation_id, elapsed_ms,
            )
            return BedTelemetryResult(
                bed_ref=bed_ref, resolved=False, bed_id=None, location=None,
                bed_exit_alarm=None, patient_present=None, position=None,
                head_of_bed_deg=None, weight_kg=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error=f"No bed found for reference '{bed_ref}'.",
            )
        logger.info(
            "bed_telemetry ref=%r -> %s exit=%s correlation_id=%s elapsed_ms=%s",
            bed_ref, record.bed_id, record.bed_exit_alarm, correlation_id, elapsed_ms,
        )
        return replace(
            record,
            bed_ref=bed_ref,
            source_system=self.source_system,
            retrieved_at=retrieved_at,
            elapsed_ms=elapsed_ms,
            correlation_id=correlation_id,
        )


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_bed_telemetry_adapter(
    config: ToolsConfig | None = None,
) -> BedTelemetryAdapter:
    """Return the bed-telemetry adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`BedTelemetryAdapter` (same signature) is returned — no caller
    change.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: Stryker iBed Adapter (ProCuity -> Engage) telemetry feed.
        # TODO: implement StrykeriBedAdapter against the iBed telemetry surface and return
        # it here. Until then, fail loudly rather than silently mocking.
        raise NotImplementedError(
            "Real Stryker iBed adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockBedTelemetryAdapter(config)


@traced_tool("bed_telemetry")
async def read_bed_telemetry(
    bed_ref: str,
    *,
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    adapter: BedTelemetryAdapter | None = None,
) -> BedTelemetryResult:
    """Read bed telemetry, bounded by the configured timeout.

    A slow bed feed surfaces as a typed timeout result (``resolved=False``, ``error`` set)
    rather than hanging the orchestrator — never blocks the conversation.
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_bed_telemetry_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.read(bed_ref, correlation_id=correlation_id),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "bed_telemetry ref=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            bed_ref, correlation_id, elapsed_ms,
        )
        return BedTelemetryResult(
            bed_ref=bed_ref, resolved=False, bed_id=None, location=None,
            bed_exit_alarm=None, patient_present=None, position=None,
            head_of_bed_deg=None, weight_kg=None,
            source_system=getattr(adapter, "source_system", "unknown"),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Bed telemetry read timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the ``bed_telemetry`` tool.

    Returned so both local dev (stdio via ``python -m src.tools bed_telemetry``) and the
    Foundry hosted-agent registration (attach the server endpoint as a hosted MCP tool)
    can mount the same tool.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale Bed Telemetry",
        instructions=(
            "Read a Stryker smart bed's current telemetry by reference (bed/room): "
            "bed-exit alarm state, patient present, position, head-of-bed angle, weight, "
            "and siderail status. Read-only: this reports the bed's state; it never arms, "
            "suppresses, or overrides the bed-exit alarm, and changes no bed setting."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def bed_telemetry(bed_ref: str, correlation_id: str = "") -> str:
        """Read a smart bed's current telemetry (read-only).

        Args:
            bed_ref: Bed reference — bed/room (e.g. "bed 12", "room 4").
            correlation_id: Optional envelope correlation id for tracing.

        Returns:
            JSON ``BedTelemetryResult``: bed_id, location, bed_exit_alarm, patient_present,
            position, head_of_bed_deg, weight_kg, siderails, brake_set, last_updated (all
            verbatim from the bed), plus source_system and elapsed_ms. If not found/timed
            out, ``resolved`` is false and ``error`` is set.
        """
        result = await read_bed_telemetry(
            bed_ref, correlation_id=correlation_id or None, config=config
        )
        return json.dumps(result.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the bed telemetry MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
