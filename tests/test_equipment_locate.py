"""Tests for the equipment_locate L4 MCP tool (read-only + timeout/error + safety)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools import equipment_locate as equipment_locate_mod
from src.tools.equipment_locate import (
    EquipmentLocateAdapter,
    EquipmentLocateResult,
    MockEquipmentLocateAdapter,
    create_equipment_locate_adapter,
    locate_equipment,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_locates_known_equipment() -> None:
    """Happy path: a known type returns candidate units + a nearest available unit."""
    result = await locate_equipment("infusion pump", correlation_id="cid-1", config=_FAST)
    assert isinstance(result, EquipmentLocateResult)
    assert result.error is None
    assert result.resolved is True
    assert result.equipment_type == "infusion_pump"  # alias normalized
    assert result.nearest_available is not None
    assert result.nearest_available.status == "available"
    assert len(result.units) >= 2
    assert result.correlation_id == "cid-1"
    assert "Smart Equipment Management" in result.source_system


async def test_near_location_prefers_matching_unit() -> None:
    """near_location biases the nearest-available selection toward that unit."""
    result = await locate_equipment("infusion pump", near_location="5 East", config=_FAST)
    assert result.nearest_available is not None
    assert "5 east" in result.nearest_available.location.lower()


async def test_type_alias_normalized() -> None:
    """Common synonyms normalize (e.g. 'vent' -> ventilator)."""
    result = await locate_equipment("vent", config=_FAST)
    assert result.equipment_type == "ventilator"
    assert result.resolved is True


def test_tool_is_read_only_no_reserve_path() -> None:
    """Safety: the adapter exposes ONLY locate — no reserve/dispatch/move method."""
    adapter = create_equipment_locate_adapter(_FAST)
    for forbidden in ("reserve", "dispatch", "move", "assign", "request", "allocate", "write"):
        assert not hasattr(adapter, forbidden), f"equipment adapter must not expose {forbidden!r}"
    assert not hasattr(equipment_locate_mod, "reserve_equipment")
    assert not hasattr(equipment_locate_mod, "dispatch_equipment")


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    result = await locate_equipment("wheelchair", config=cfg)
    assert result.elapsed_ms >= 55


async def test_unknown_type_returns_error_not_exception() -> None:
    """Error path: an unknown type yields a typed unresolved result, not a raise."""
    result = await locate_equipment("time machine", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "time machine" in result.error


async def test_missing_type_returns_error() -> None:
    """Error path: an empty type is rejected at the boundary."""
    result = await locate_equipment("   ", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "required" in result.error


async def test_timeout_returns_typed_result() -> None:
    """Timeout path: a slow RTLS surfaces as an unresolved result, never hangs."""
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    result = await locate_equipment("infusion pump", config=slow)
    assert result.resolved is False
    assert result.error is not None and "timed out" in result.error


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the EquipmentLocateAdapter Protocol the real adapter implements."""
    adapter = create_equipment_locate_adapter(_FAST)
    assert isinstance(adapter, MockEquipmentLocateAdapter)
    assert isinstance(adapter, EquipmentLocateAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool returns JSON-serializable output."""
    from src.tools.equipment_locate import build_server

    build_server(_FAST)  # smoke: server builds without error
    result = await locate_equipment("bladder scanner", config=_FAST)
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["equipment_type"] == "bladder_scanner"
    assert payload["nearest_available"]["status"] == "available"
