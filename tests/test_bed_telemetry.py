"""Tests for the bed_telemetry L4 MCP tool (happy path + timeout/error + safety)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools.bed_telemetry import (
    BedTelemetryAdapter,
    BedTelemetryResult,
    MockBedTelemetryAdapter,
    create_bed_telemetry_adapter,
    read_bed_telemetry,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_reads_known_bed() -> None:
    """Happy path: a known bed resolves to full telemetry."""
    result = await read_bed_telemetry("bed 12", correlation_id="cid-1", config=_FAST)
    assert isinstance(result, BedTelemetryResult)
    assert result.error is None
    assert result.resolved is True
    assert result.bed_id == "PROCUITY-4W-12"
    assert result.bed_exit_alarm == "armed"
    assert result.patient_present is True
    assert result.head_of_bed_deg == 30
    assert result.siderails["upper_left"] == "up"
    assert result.correlation_id == "cid-1"
    assert "iBed" in result.source_system


async def test_bed_exit_triggered_is_reported_verbatim() -> None:
    """Safety: a triggered bed-exit is surfaced verbatim; the tool doesn't act on it."""
    result = await read_bed_telemetry("bed 7", config=_FAST)
    assert result.bed_exit_alarm == "triggered"  # reported, not acted upon
    assert result.patient_present is False


async def test_ref_is_case_insensitive() -> None:
    """The lookup normalizes the reference (case/whitespace)."""
    result = await read_bed_telemetry("  ROOM 4 ", config=_FAST)
    assert result.resolved is True
    assert result.bed_id == "PROCUITY-ICU-04"


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    result = await read_bed_telemetry("bed 12", config=cfg)
    assert result.elapsed_ms >= 55


async def test_unknown_bed_returns_error_not_exception() -> None:
    """Error path: an unknown reference yields a typed unresolved result, not a raise."""
    result = await read_bed_telemetry("bed 999", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "bed 999" in result.error


async def test_missing_ref_returns_error() -> None:
    """Error path: an empty reference is rejected at the boundary."""
    result = await read_bed_telemetry("   ", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "required" in result.error


async def test_timeout_returns_typed_result() -> None:
    """Timeout path: a slow feed surfaces as an unresolved result, never hangs."""
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    result = await read_bed_telemetry("bed 12", config=slow)
    assert result.resolved is False
    assert result.error is not None and "timed out" in result.error


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the BedTelemetryAdapter Protocol the real adapter implements."""
    adapter = create_bed_telemetry_adapter(_FAST)
    assert isinstance(adapter, MockBedTelemetryAdapter)
    assert isinstance(adapter, BedTelemetryAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool returns JSON-serializable output."""
    from src.tools.bed_telemetry import build_server

    build_server(_FAST)  # smoke: server builds without error
    result = await read_bed_telemetry("room 4", config=_FAST)
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["bed_id"] == "PROCUITY-ICU-04"
    assert payload["position"] == "chair"
