"""Tests for the comms_page L4 MCP tool (happy path + timeout/error + safety invariants)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools.comms_page import (
    CommsAdapter,
    MockCommsAdapter,
    PageReceipt,
    create_comms_adapter,
    send_page,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_sends_page_happy_path() -> None:
    """Happy path: a page is accepted for delivery with a receipt."""
    receipt = await send_page(
        "Dr. Priya Nadkarni", "Suspected sepsis, bed 12", priority="urgent",
        recipient_id="prov-10231", correlation_id="cid-1", config=_FAST,
    )
    assert isinstance(receipt, PageReceipt)
    assert receipt.error is None
    assert receipt.delivery_state == "delivered"
    assert receipt.page_id.startswith("page-")
    assert receipt.priority == "urgent"
    assert receipt.escalation_tier == 2
    assert receipt.correlation_id == "cid-1"
    assert "Engage" in receipt.source_system


async def test_page_always_requires_human_ack() -> None:
    """Safety: every page requires a human to acknowledge/act — no autonomous action."""
    for priority in ("routine", "urgent", "stat"):
        receipt = await send_page("RRT Team Bravo", "msg", priority=priority, config=_FAST)
        assert receipt.requires_human_ack is True


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    receipt = await send_page("Dr. Sam Okafor", "consult please", config=cfg)
    assert receipt.elapsed_ms >= 55


async def test_invalid_priority_returns_error_not_exception() -> None:
    """Error path: an invalid priority yields a typed failed receipt, not a raise."""
    receipt = await send_page("Dr. Elena Ruiz", "msg", priority="whenever", config=_FAST)
    assert receipt.delivery_state == "failed"
    assert receipt.error is not None and "priority" in receipt.error


async def test_missing_recipient_returns_error() -> None:
    """Error path: an empty recipient is rejected at the boundary."""
    receipt = await send_page("   ", "msg", config=_FAST)
    assert receipt.delivery_state == "failed"
    assert receipt.error is not None and "Recipient" in receipt.error


async def test_timeout_returns_typed_receipt() -> None:
    """Timeout path: a slow adapter surfaces as a failed receipt, never hangs."""
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    receipt = await send_page("Dr. Hana Kim", "urgent", priority="stat", config=slow)
    assert receipt.delivery_state == "failed"
    assert receipt.error is not None and "timed out" in receipt.error
    assert receipt.requires_human_ack is True


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the CommsAdapter Protocol the real adapter will implement."""
    adapter = create_comms_adapter(_FAST)
    assert isinstance(adapter, MockCommsAdapter)
    assert isinstance(adapter, CommsAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool returns JSON-serializable output."""
    from src.tools.comms_page import build_server

    build_server(_FAST)  # smoke: server builds without error
    receipt = await send_page("RRT Team Bravo", "code blue bed 4", priority="stat", config=_FAST)
    payload = json.loads(json.dumps(receipt.to_dict(), default=str))
    assert payload["escalation_tier"] == 3
    assert payload["requires_human_ack"] is True
