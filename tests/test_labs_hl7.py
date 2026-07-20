"""Tests for the labs_hl7 L4 MCP tool (happy path + timeout/error + safety invariants)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools.labs_hl7 import (
    LabOrderResult,
    LabsAdapter,
    MockLabsAdapter,
    create_labs_adapter,
    order_labs,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_orders_and_results_happy_path() -> None:
    """Happy path: a sepsis lactate+cultures order returns results with the order."""
    result = await order_labs(
        "bed 12", ["lactate", "blood cultures"], priority="stat",
        correlation_id="cid-1", config=_FAST,
    )
    assert isinstance(result, LabOrderResult)
    assert result.error is None
    assert result.order_id.startswith("ord-")
    assert result.order_status == "resulted"  # lactate is final
    assert result.tests == ["LACTATE", "CULT-BLD"]  # aliases normalized
    assert result.priority == "stat"
    assert result.correlation_id == "cid-1"
    lactate = next(r for r in result.results if r.test_code == "LACTATE")
    assert lactate.value == "3.8" and lactate.unit == "mmol/L"


async def test_tool_does_not_interpret_results() -> None:
    """Safety: the tool returns the LIS abnormal flag verbatim; it doesn't judge it.

    The 'H' flag on lactate comes from the LIS template, not a tool decision \u2014 the tool
    surfaces it unchanged and makes no clinical call about what it means.
    """
    result = await order_labs("bed 12", ["lactate"], config=_FAST)
    lactate = result.results[0]
    assert lactate.abnormal_flag == "H"  # verbatim from LIS, not interpreted
    assert result.source_system.startswith("MOCK")


async def test_pending_only_order_is_ordered_not_resulted() -> None:
    """An order with only pending analytes reports 'ordered', not 'resulted'."""
    result = await order_labs("bed 7", ["blood cultures"], config=_FAST)
    assert result.order_status == "ordered"
    assert result.results[0].status == "pending"


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    result = await order_labs("bed 3", ["CBC"], config=cfg)
    assert result.elapsed_ms >= 55


async def test_invalid_priority_returns_error_not_exception() -> None:
    """Error path: an invalid priority yields a typed failed result, not a raise."""
    result = await order_labs("bed 12", ["lactate"], priority="whenever", config=_FAST)
    assert result.order_status == "failed"
    assert result.error is not None and "priority" in result.error


async def test_missing_tests_returns_error() -> None:
    """Error path: an empty test list is rejected at the boundary."""
    result = await order_labs("bed 12", [], config=_FAST)
    assert result.order_status == "failed"
    assert result.error is not None and "test" in result.error.lower()


async def test_timeout_returns_typed_result() -> None:
    """Timeout path: a slow LIS surfaces as a failed result, never hangs."""
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    result = await order_labs("bed 12", ["lactate"], priority="stat", config=slow)
    assert result.order_status == "failed"
    assert result.error is not None and "timed out" in result.error


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the LabsAdapter Protocol the real adapter will implement."""
    adapter = create_labs_adapter(_FAST)
    assert isinstance(adapter, MockLabsAdapter)
    assert isinstance(adapter, LabsAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool returns JSON-serializable output."""
    from src.tools.labs_hl7 import build_server

    build_server(_FAST)  # smoke: server builds without error
    result = await order_labs("bed 12", ["troponin"], priority="stat", config=_FAST)
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["tests"] == ["TROPONIN"]
    assert payload["results"][0]["abnormal_flag"] == "H"
