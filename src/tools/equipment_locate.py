"""L4 MCP tool: ``equipment_locate`` — find the last-seen location of medical equipment.

Real adapter: Stryker **Smart Equipment Management** "last seen" RTLS + **ProCare**
(docs/01-architecture.md §5 — equipment_locate maps to "Smart Equipment Management 'last
 seen' + ProCare"; docs/02-stryker-workload-catalog.md §7 "locate/request equipment").
This tool is the thin MCP wrapper over that read surface: where a device type was last
seen and its status. RTLS/ProCare own the tracking data; this tool only reads it.

SAFETY (docs/01-architecture.md, copilot-instructions.md):
- Tools do **I/O only** — this is a **read**: it returns candidate devices and their last
  known location/status. It makes no clinical decision and changes nothing (it does not
  reserve, dispatch, or move equipment).
- A human retrieves or requests the equipment — human-in-the-loop. This tool answers
  "where's a working pump?"; it never acts on the answer.

MCP SDK: ``mcp[cli]==1.28.1`` (pinned in requirements.txt; v1.x stable line).
FastMCP server pattern matches the existing ``mcp_server/`` in this repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from config import ToolsConfig
from src.telemetry import traced_tool

logger = logging.getLogger("nightingale.tools.equipment_locate")

TOOL_NAME = "equipment_locate"

# Canonical equipment types. Read-only; no reservation.
_TYPE_ALIASES = {
    "pump": "infusion_pump",
    "iv pump": "infusion_pump",
    "infusion pump": "infusion_pump",
    "vent": "ventilator",
    "ventilator": "ventilator",
    "wheelchair": "wheelchair",
    "defibrillator": "defibrillator",
    "defib": "defibrillator",
    "crash cart": "crash_cart",
    "code cart": "crash_cart",
    "bladder scanner": "bladder_scanner",
    "scanner": "bladder_scanner",
    "ekg": "ecg_machine",
    "ecg": "ecg_machine",
    "telemetry": "telemetry_pack",
    "tele pack": "telemetry_pack",
}


def _canonical_type(equipment_type: str) -> str:
    key = " ".join(equipment_type.strip().lower().split())
    return _TYPE_ALIASES.get(key, key.replace(" ", "_"))


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class EquipmentUnit:
    """One tracked device and its last-seen location/status (verbatim from RTLS)."""

    asset_id: str
    equipment_type: str
    location: str  # e.g. "4 West, clean utility"
    status: str  # available | in_use | cleaning | maintenance  (verbatim)
    battery_pct: int | None  # None if not battery-powered
    last_seen: str  # ISO-8601, from the RTLS 'last seen' feed


@dataclass(frozen=True)
class EquipmentLocateResult:
    """Typed output of an equipment-locate read (the tool's read-only I/O contract)."""

    equipment_type: str
    resolved: bool
    nearest_available: EquipmentUnit | None
    units: list[EquipmentUnit] = field(default_factory=list)
    source_system: str = ""
    retrieved_at: str = ""
    elapsed_ms: float = 0.0
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class EquipmentLocateAdapter(Protocol):
    """Interface a real Stryker Smart Equipment Management / ProCare adapter must implement.

    Note the interface exposes ONLY ``locate`` — no reserve/dispatch/move method. Location
    is read; a human retrieves the equipment.
    """

    source_system: str

    async def locate(
        self,
        equipment_type: str,
        *,
        near_location: str | None = None,
        correlation_id: str | None = None,
    ) -> EquipmentLocateResult:
        """Read candidate units of ``equipment_type`` and their last-seen locations."""
        ...


# --- Mock adapter (realistic data + configurable latency) --------------------
# Realistic RTLS inventory keyed by canonical equipment type. Synthetic data.
_INVENTORY: dict[str, list[EquipmentUnit]] = {
    "infusion_pump": [
        EquipmentUnit("IP-2201", "infusion_pump", "4 West, clean utility", "available", 94, "2026-07-20T14:58:00Z"),
        EquipmentUnit("IP-2207", "infusion_pump", "4 West, bed 9", "in_use", 61, "2026-07-20T15:00:03Z"),
        EquipmentUnit("IP-2215", "infusion_pump", "5 East, clean utility", "available", 88, "2026-07-20T14:55:12Z"),
        EquipmentUnit("IP-2230", "infusion_pump", "biomed", "maintenance", None, "2026-07-20T09:12:00Z"),
    ],
    "ventilator": [
        EquipmentUnit("VENT-1102", "ventilator", "ICU storage", "available", 100, "2026-07-20T14:50:00Z"),
        EquipmentUnit("VENT-1108", "ventilator", "ICU, room 6", "in_use", 100, "2026-07-20T15:00:01Z"),
    ],
    "wheelchair": [
        EquipmentUnit("WC-330", "wheelchair", "main lobby", "available", None, "2026-07-20T14:40:00Z"),
        EquipmentUnit("WC-341", "wheelchair", "4 West, hallway", "available", None, "2026-07-20T14:59:30Z"),
    ],
    "defibrillator": [
        EquipmentUnit("DEFIB-77", "defibrillator", "4 West, crash cart", "available", 97, "2026-07-20T14:57:00Z"),
    ],
    "crash_cart": [
        EquipmentUnit("CART-12", "crash_cart", "4 West, alcove B", "available", None, "2026-07-20T14:59:00Z"),
    ],
    "bladder_scanner": [
        EquipmentUnit("BS-5", "bladder_scanner", "5 East, clean utility", "cleaning", 72, "2026-07-20T14:45:00Z"),
        EquipmentUnit("BS-8", "bladder_scanner", "4 West, clean utility", "available", 80, "2026-07-20T14:58:40Z"),
    ],
}


class MockEquipmentLocateAdapter:
    """Simulates the Stryker Smart Equipment Management RTLS feed, with realistic latency."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (Stryker Smart Equipment Management RTLS + ProCare)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def locate(
        self,
        equipment_type: str,
        *,
        near_location: str | None = None,
        correlation_id: str | None = None,
    ) -> EquipmentLocateResult:
        t0 = time.perf_counter()
        retrieved_at = datetime.now(timezone.utc).isoformat()

        if not equipment_type.strip():
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return EquipmentLocateResult(
                equipment_type=equipment_type, resolved=False, nearest_available=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error="equipment_type is required.",
            )

        await self._simulate_latency()
        canonical = _canonical_type(equipment_type)
        units = _INVENTORY.get(canonical)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if not units:
            logger.info(
                "equipment_locate type=%r UNRESOLVED correlation_id=%s elapsed_ms=%s",
                equipment_type, correlation_id, elapsed_ms,
            )
            return EquipmentLocateResult(
                equipment_type=canonical, resolved=False, nearest_available=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error=f"No tracked equipment found for type '{equipment_type}'.",
            )

        # "nearest available" is a simple read heuristic: first available unit whose
        # location matches near_location if given, else the first available unit. This is
        # selection over read data, not a clinical decision.
        available = [u for u in units if u.status == "available"]
        nearest = None
        if available:
            if near_location:
                loc = near_location.strip().lower()
                nearest = next(
                    (u for u in available if loc in u.location.lower()), available[0]
                )
            else:
                nearest = available[0]
        logger.info(
            "equipment_locate type=%r -> %s units=%d correlation_id=%s elapsed_ms=%s",
            equipment_type, nearest.asset_id if nearest else None,
            len(units), correlation_id, elapsed_ms,
        )
        return EquipmentLocateResult(
            equipment_type=canonical, resolved=True, nearest_available=nearest,
            units=units, source_system=self.source_system, retrieved_at=retrieved_at,
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
        )


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_equipment_locate_adapter(
    config: ToolsConfig | None = None,
) -> EquipmentLocateAdapter:
    """Return the equipment-locate adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`EquipmentLocateAdapter` (same read-only signature) is returned —
    no caller change, and still no reserve/dispatch path.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: Stryker Smart Equipment Management 'last seen' RTLS + ProCare —
        # READ location only. TODO: implement StrykerEquipmentLocateAdapter and return it.
        # Keep it read-only; no reserve/dispatch/move path.
        raise NotImplementedError(
            "Real Stryker equipment-locate adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockEquipmentLocateAdapter(config)


@traced_tool("equipment_locate")
async def locate_equipment(
    equipment_type: str,
    *,
    near_location: str | None = None,
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    adapter: EquipmentLocateAdapter | None = None,
) -> EquipmentLocateResult:
    """Locate equipment, bounded by the configured timeout.

    A slow RTLS surfaces as a typed timeout result (``resolved=False``, ``error`` set)
    rather than hanging the orchestrator — never blocks the conversation.
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_equipment_locate_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.locate(
                equipment_type, near_location=near_location, correlation_id=correlation_id
            ),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "equipment_locate type=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            equipment_type, correlation_id, elapsed_ms,
        )
        return EquipmentLocateResult(
            equipment_type=equipment_type, resolved=False, nearest_available=None,
            source_system=getattr(adapter, "source_system", "unknown"),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Equipment locate timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the ``equipment_locate`` tool.

    Returned so both local dev (stdio via ``python -m src.tools equipment_locate``) and the
    Foundry hosted-agent registration (attach the server endpoint as a hosted MCP tool)
    can mount the same tool.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale Equipment Locate",
        instructions=(
            "Find where a type of medical equipment (infusion pump, ventilator, wheelchair, "
            "defibrillator, crash cart, bladder scanner) was last seen and its status, via "
            "the RTLS. Read-only: it reports candidate units and locations; it does not "
            "reserve, dispatch, or move equipment. A human retrieves it."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def equipment_locate(
        equipment_type: str, near_location: str = "", correlation_id: str = ""
    ) -> str:
        """Locate medical equipment by type via the RTLS (read-only).

        Args:
            equipment_type: What to find, e.g. "infusion pump", "ventilator", "wheelchair".
            near_location: Optional unit/location to prefer, e.g. "4 West".
            correlation_id: Optional envelope correlation id for tracing.

        Returns:
            JSON ``EquipmentLocateResult``: nearest_available (asset_id/location/status/
            battery/last_seen) and a ``units`` array of all candidates (verbatim from RTLS),
            plus source_system and elapsed_ms. If none found/timed out, ``resolved`` is
            false and ``error`` is set.
        """
        result = await locate_equipment(
            equipment_type, near_location=near_location or None,
            correlation_id=correlation_id or None, config=config,
        )
        return json.dumps(result.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the equipment locate MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
