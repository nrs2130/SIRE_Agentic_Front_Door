"""Tests for the patient_context L4 MCP tool (happy path + timeout/error + safety)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools.patient_context import (
    MockPatientContextAdapter,
    PatientContextAdapter,
    PatientContextResult,
    create_patient_context_adapter,
    get_patient_context,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_resolves_known_patient() -> None:
    """Happy path: a known bed resolves to a patient + care team."""
    result = await get_patient_context("bed 12", correlation_id="cid-1", config=_FAST)
    assert isinstance(result, PatientContextResult)
    assert result.error is None
    assert result.resolved is True
    assert result.mrn == "MRN-4820193"
    assert result.name == "Robert Alvarez"
    assert result.code_status == "Full Code"
    assert "penicillin" in result.allergies
    assert any(m.role == "attending" for m in result.care_team)
    assert result.correlation_id == "cid-1"
    assert "Patient Context" in result.source_system


async def test_ref_is_case_insensitive() -> None:
    """The lookup normalizes the reference (case/whitespace)."""
    result = await get_patient_context("  BED 12 ", config=_FAST)
    assert result.resolved is True
    assert result.mrn == "MRN-4820193"


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    result = await get_patient_context("room 4", config=cfg)
    assert result.elapsed_ms >= 55


async def test_unknown_patient_returns_error_not_exception() -> None:
    """Error path: an unknown reference yields a typed unresolved result, not a raise."""
    result = await get_patient_context("bed 999", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "bed 999" in result.error


async def test_missing_ref_returns_error() -> None:
    """Error path: an empty reference is rejected at the boundary."""
    result = await get_patient_context("   ", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "required" in result.error


async def test_timeout_returns_typed_result() -> None:
    """Timeout path: a slow endpoint surfaces as an unresolved result, never hangs."""
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    result = await get_patient_context("bed 12", config=slow)
    assert result.resolved is False
    assert result.error is not None and "timed out" in result.error


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the PatientContextAdapter Protocol the real adapter implements."""
    adapter = create_patient_context_adapter(_FAST)
    assert isinstance(adapter, MockPatientContextAdapter)
    assert isinstance(adapter, PatientContextAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool returns JSON-serializable output."""
    from src.tools.patient_context import build_server

    build_server(_FAST)  # smoke: server builds without error
    result = await get_patient_context("room 4", config=_FAST)
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["mrn"] == "MRN-4821050"
    assert payload["care_team"][0]["role"] == "intensivist"
