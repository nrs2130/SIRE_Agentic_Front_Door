"""L4 MCP tool: ``monitor_alarm`` — READ vitals and alarm state from a patient monitor.

Real adapter: Vocera **Monitor adapters** — GE Carescape (broadcast protocol),
Nihon Kohden, Spacelabs (REST/SSE), Sotera (docs/01-architecture.md §5; docs/02-stryker-
workload-catalog.md Part C — "Monitor adapters: Read vitals / alarms"). This tool is the
thin MCP wrapper over the monitor's read feed.

⚠️ SAFETY — THIS TOOL IS THE ONE THE PROMPT SINGLES OUT (docs/01-architecture.md,
copilot-instructions.md):
- **READ / NOTIFY ONLY.** It returns the monitor's current vitals and its OWN alarm
  state. It has NO write path: it cannot silence, acknowledge, arm, suppress, reprioritize,
  or change any alarm or threshold. There is deliberately no such method on the interface.
- The clinical alarm path (GE/Nihon Kohden/Spacelabs/Sotera → Engage/EMDAN) is
  **FDA 510(k)-cleared**. This agent **augments** it by surfacing state for a human; it
  **never** touches the cleared path.
- **Human-in-the-loop, always.** ``requires_human_ack`` is True on every active-alarm read;
  the agent reads vitals back and a clinician acts — no autonomous clinical action.

MCP SDK: ``mcp[cli]==1.28.1`` (pinned in requirements.txt; v1.x stable line).
FastMCP server pattern matches the existing ``mcp_server/`` in this repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from config import ToolsConfig
from src.telemetry import traced_tool

logger = logging.getLogger("nightingale.tools.monitor_alarm")

TOOL_NAME = "monitor_alarm"


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class VitalSign:
    """A single vital reading (verbatim from the monitor)."""

    name: str  # e.g. "HR", "SpO2", "NIBP", "RR", "Temp"
    value: str
    unit: str
    alarm_state: str  # normal | high | low | critical  (the monitor's own state)


@dataclass(frozen=True)
class MonitorAlarmResult:
    """Typed output of a monitor READ (the tool's read-only I/O contract).

    Every field is reported verbatim from the monitor. ``active_alarms`` and per-vital
    ``alarm_state`` reflect the monitor's OWN cleared-path alarm state — this tool never
    sets, clears, or changes them.
    """

    monitor_ref: str
    resolved: bool
    monitor_id: str | None
    location: str | None
    vitals: list[VitalSign] = field(default_factory=list)
    active_alarms: list[str] = field(default_factory=list)  # verbatim alarm labels
    highest_severity: str | None = None  # none | low | high | critical (reported)
    requires_human_ack: bool = False  # True whenever an alarm is active — human acts
    last_updated: str = ""
    source_system: str = ""
    retrieved_at: str = ""
    elapsed_ms: float = 0.0
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class MonitorAdapter(Protocol):
    """Interface a real Vocera monitor adapter must implement.

    Note the interface exposes ONLY ``read``. There is intentionally no acknowledge/
    silence/suppress method — the cleared alarm path is not writable from here.
    """

    source_system: str

    async def read(
        self, monitor_ref: str, *, correlation_id: str | None = None
    ) -> MonitorAlarmResult:
        """Read current vitals + alarm state for ``monitor_ref`` (read-only)."""
        ...


# --- Mock adapter (realistic data + configurable latency) --------------------
# Realistic monitor snapshots keyed by a normalized reference. Synthetic data.
_MONITORS: dict[str, MonitorAlarmResult] = {
    "bed 12": MonitorAlarmResult(
        monitor_ref="bed 12", resolved=True, monitor_id="CARESCAPE-4W-12",
        location="4 West, bed 12",
        vitals=[
            VitalSign("HR", "118", "bpm", "high"),
            VitalSign("SpO2", "91", "%", "low"),
            VitalSign("NIBP", "88/54", "mmHg", "low"),
            VitalSign("RR", "24", "br/min", "high"),
            VitalSign("Temp", "38.6", "°C", "high"),
        ],
        active_alarms=["SpO2 LOW", "NIBP LOW", "HR HIGH"],
        highest_severity="high", requires_human_ack=True,
        last_updated="2026-07-20T15:00:05Z",
    ),
    "bed 7": MonitorAlarmResult(
        monitor_ref="bed 7", resolved=True, monitor_id="CARESCAPE-4W-07",
        location="4 West, bed 7",
        vitals=[
            VitalSign("HR", "78", "bpm", "normal"),
            VitalSign("SpO2", "97", "%", "normal"),
            VitalSign("NIBP", "122/76", "mmHg", "normal"),
            VitalSign("RR", "16", "br/min", "normal"),
        ],
        active_alarms=[],
        highest_severity="none", requires_human_ack=False,
        last_updated="2026-07-20T15:00:01Z",
    ),
    "room 4": MonitorAlarmResult(
        monitor_ref="room 4", resolved=True, monitor_id="CARESCAPE-ICU-04",
        location="ICU, room 4",
        vitals=[
            VitalSign("HR", "148", "bpm", "critical"),
            VitalSign("SpO2", "84", "%", "critical"),
            VitalSign("ABP", "70/40", "mmHg", "critical"),
            VitalSign("RR", "32", "br/min", "high"),
        ],
        active_alarms=["SpO2 CRITICAL", "ABP CRITICAL", "HR CRITICAL"],
        highest_severity="critical", requires_human_ack=True,
        last_updated="2026-07-20T15:00:08Z",
    ),
}


def _normalize_ref(monitor_ref: str) -> str:
    return " ".join(monitor_ref.strip().lower().split())


class MockMonitorAdapter:
    """Simulates a patient-monitor read feed (GE Carescape / etc.), read-only."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (Monitor adapter: GE Carescape / Nihon Kohden / Spacelabs / Sotera)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def read(
        self, monitor_ref: str, *, correlation_id: str | None = None
    ) -> MonitorAlarmResult:
        t0 = time.perf_counter()
        retrieved_at = datetime.now(timezone.utc).isoformat()

        if not monitor_ref.strip():
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return MonitorAlarmResult(
                monitor_ref=monitor_ref, resolved=False, monitor_id=None, location=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error="monitor_ref is required.",
            )

        await self._simulate_latency()
        record = _MONITORS.get(_normalize_ref(monitor_ref))
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if record is None:
            logger.info(
                "monitor_alarm ref=%r UNRESOLVED correlation_id=%s elapsed_ms=%s",
                monitor_ref, correlation_id, elapsed_ms,
            )
            return MonitorAlarmResult(
                monitor_ref=monitor_ref, resolved=False, monitor_id=None, location=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error=f"No monitor found for reference '{monitor_ref}'.",
            )
        logger.info(
            "monitor_alarm ref=%r -> %s severity=%s alarms=%d correlation_id=%s elapsed_ms=%s",
            monitor_ref, record.monitor_id, record.highest_severity,
            len(record.active_alarms), correlation_id, elapsed_ms,
        )
        return replace(
            record,
            monitor_ref=monitor_ref,
            source_system=self.source_system,
            retrieved_at=retrieved_at,
            elapsed_ms=elapsed_ms,
            correlation_id=correlation_id,
        )


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_monitor_adapter(config: ToolsConfig | None = None) -> MonitorAdapter:
    """Return the monitor adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`MonitorAdapter` (same read-only signature) is returned — no
    caller change, and still no write path.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: Vocera Monitor adapter (GE Carescape / Nihon Kohden / Spacelabs /
        # Sotera) — READ feed only. TODO: implement VoceraMonitorAdapter and return it.
        # It must remain read-only; do NOT add an acknowledge/silence path.
        raise NotImplementedError(
            "Real Vocera monitor adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockMonitorAdapter(config)


@traced_tool("monitor_alarm")
async def read_monitor(
    monitor_ref: str,
    *,
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    adapter: MonitorAdapter | None = None,
) -> MonitorAlarmResult:
    """Read monitor vitals + alarm state (read-only), bounded by the configured timeout.

    A slow monitor feed surfaces as a typed timeout result (``resolved=False``, ``error``
    set) rather than hanging the orchestrator — never blocks the conversation.
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_monitor_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.read(monitor_ref, correlation_id=correlation_id),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "monitor_alarm ref=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            monitor_ref, correlation_id, elapsed_ms,
        )
        return MonitorAlarmResult(
            monitor_ref=monitor_ref, resolved=False, monitor_id=None, location=None,
            source_system=getattr(adapter, "source_system", "unknown"),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Monitor read timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the READ-ONLY ``monitor_alarm`` tool.

    Only a read tool is registered. No acknowledge/silence tool exists — the cleared
    alarm path is not writable from Nightingale.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale Monitor (read-only)",
        instructions=(
            "READ a patient monitor's current vitals and alarm state by reference "
            "(bed/room). This is READ/NOTIFY ONLY: it reports vitals and the monitor's own "
            "alarm state so a clinician can act. It CANNOT silence, acknowledge, suppress, "
            "or change any alarm — that FDA-cleared path is never touched. Always human-in-"
            "the-loop."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def monitor_alarm(monitor_ref: str, correlation_id: str = "") -> str:
        """Read a patient monitor's vitals + alarm state (READ-ONLY, human-in-the-loop).

        This tool never silences, acknowledges, or changes an alarm — it only reports.

        Args:
            monitor_ref: Monitor reference — bed/room (e.g. "bed 12", "room 4").
            correlation_id: Optional envelope correlation id for tracing.

        Returns:
            JSON ``MonitorAlarmResult``: monitor_id, location, vitals (name/value/unit/
            alarm_state, verbatim), active_alarms, highest_severity, requires_human_ack
            (true whenever an alarm is active), plus source_system and elapsed_ms. If not
            found/timed out, ``resolved`` is false and ``error`` is set.
        """
        result = await read_monitor(
            monitor_ref, correlation_id=correlation_id or None, config=config
        )
        return json.dumps(result.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the monitor (read-only) MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
