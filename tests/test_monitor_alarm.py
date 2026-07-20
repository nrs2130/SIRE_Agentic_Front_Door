"""Tests for the monitor_alarm L4 MCP tool (read-only + timeout/error + safety invariants)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools import monitor_alarm as monitor_alarm_mod
from src.tools.monitor_alarm import (
    MockMonitorAdapter,
    MonitorAdapter,
    MonitorAlarmResult,
    create_monitor_adapter,
    read_monitor,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_reads_monitor_with_active_alarms() -> None:
    """Happy path: a monitor read returns vitals + the monitor's own alarm state."""
    result = await read_monitor("bed 12", correlation_id="cid-1", config=_FAST)
    assert isinstance(result, MonitorAlarmResult)
    assert result.error is None
    assert result.resolved is True
    assert result.monitor_id == "CARESCAPE-4W-12"
    assert "SpO2 LOW" in result.active_alarms
    assert any(v.name == "HR" and v.alarm_state == "high" for v in result.vitals)
    assert result.correlation_id == "cid-1"


async def test_active_alarm_requires_human_ack() -> None:
    """Safety: any active alarm sets requires_human_ack — a human must act, not the agent."""
    critical = await read_monitor("room 4", config=_FAST)
    assert critical.highest_severity == "critical"
    assert critical.requires_human_ack is True


async def test_no_alarm_does_not_require_ack() -> None:
    """A monitor with no active alarm reports requires_human_ack false."""
    result = await read_monitor("bed 7", config=_FAST)
    assert result.active_alarms == []
    assert result.requires_human_ack is False


def test_tool_is_read_only_no_write_path() -> None:
    """Safety: the adapter exposes ONLY read — no acknowledge/silence/suppress method exists."""
    adapter = create_monitor_adapter(_FAST)
    for forbidden in ("acknowledge", "silence", "suppress", "ack", "clear", "set_alarm", "write"):
        assert not hasattr(adapter, forbidden), f"monitor adapter must not expose {forbidden!r}"
    # The module exposes no write helper either.
    assert not hasattr(monitor_alarm_mod, "acknowledge_alarm")
    assert not hasattr(monitor_alarm_mod, "silence_alarm")


async def test_alarm_state_reported_verbatim() -> None:
    """Safety: per-vital alarm_state is surfaced verbatim; the tool doesn't judge it."""
    result = await read_monitor("bed 12", config=_FAST)
    spo2 = next(v for v in result.vitals if v.name == "SpO2")
    assert spo2.alarm_state == "low"  # from the monitor, not a tool decision


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    result = await read_monitor("bed 12", config=cfg)
    assert result.elapsed_ms >= 55


async def test_unknown_monitor_returns_error_not_exception() -> None:
    """Error path: an unknown reference yields a typed unresolved result, not a raise."""
    result = await read_monitor("bed 999", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "bed 999" in result.error


async def test_missing_ref_returns_error() -> None:
    """Error path: an empty reference is rejected at the boundary."""
    result = await read_monitor("   ", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "required" in result.error


async def test_timeout_returns_typed_result() -> None:
    """Timeout path: a slow feed surfaces as an unresolved result, never hangs."""
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    result = await read_monitor("bed 12", config=slow)
    assert result.resolved is False
    assert result.error is not None and "timed out" in result.error


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the MonitorAdapter Protocol the real adapter implements."""
    adapter = create_monitor_adapter(_FAST)
    assert isinstance(adapter, MockMonitorAdapter)
    assert isinstance(adapter, MonitorAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool returns JSON-serializable output."""
    from src.tools.monitor_alarm import build_server

    build_server(_FAST)  # smoke: server builds without error
    result = await read_monitor("room 4", config=_FAST)
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["monitor_id"] == "CARESCAPE-ICU-04"
    assert payload["requires_human_ack"] is True
