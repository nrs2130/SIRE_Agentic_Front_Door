"""Tests for the blood_bank L4 MCP tool (read-only + timeout/error + safety invariants)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools import blood_bank as blood_bank_mod
from src.tools.blood_bank import (
    BloodBankAdapter,
    BloodBankResult,
    MockBloodBankAdapter,
    check_blood_bank,
    create_blood_bank_adapter,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_checks_crossmatched_patient() -> None:
    """Happy path: a crossmatched patient reports ready units + inventory."""
    result = await check_blood_bank("bed 12", correlation_id="cid-1", config=_FAST)
    assert isinstance(result, BloodBankResult)
    assert result.error is None
    assert result.resolved is True
    assert result.crossmatch_status == "complete"
    assert result.crossmatch_units_ready == 2
    assert result.patient_blood_type == "O+"
    assert result.massive_transfusion_protocol_available is True
    assert any(u.product == "prbc" for u in result.units)
    assert result.correlation_id == "cid-1"
    assert "Blood Bank" in result.source_system


async def test_product_filter() -> None:
    """A product filter narrows the returned units to that product."""
    result = await check_blood_bank("bed 12", product="platelets", config=_FAST)
    assert result.units, "expected platelet availability"
    assert all(u.product == "platelets" for u in result.units)


async def test_product_alias_normalized() -> None:
    """Common product synonyms normalize (e.g. 'red cells' -> prbc)."""
    result = await check_blood_bank("bed 12", product="red cells", config=_FAST)
    assert result.units and all(u.product == "prbc" for u in result.units)


async def test_unknown_patient_reports_general_inventory() -> None:
    """An unknown patient still returns general (uncrossmatched) inventory, xmatch none."""
    result = await check_blood_bank("bed 999", config=_FAST)
    assert result.resolved is True
    assert result.crossmatch_status == "none"
    assert result.crossmatch_units_ready == 0
    assert result.units  # general O-/A+ etc still reported


def test_tool_is_read_only_no_order_path() -> None:
    """Safety: the adapter exposes ONLY check — no order/reserve/release/transfuse method."""
    adapter = create_blood_bank_adapter(_FAST)
    for forbidden in ("order", "reserve", "release", "transfuse", "allocate", "issue", "write"):
        assert not hasattr(adapter, forbidden), f"blood-bank adapter must not expose {forbidden!r}"
    assert not hasattr(blood_bank_mod, "order_blood")
    assert not hasattr(blood_bank_mod, "reserve_units")


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    result = await check_blood_bank("bed 12", config=cfg)
    assert result.elapsed_ms >= 55


async def test_missing_ref_returns_error() -> None:
    """Error path: an empty reference is rejected at the boundary."""
    result = await check_blood_bank("   ", config=_FAST)
    assert result.resolved is False
    assert result.error is not None and "required" in result.error


async def test_timeout_returns_typed_result() -> None:
    """Timeout path: a slow LIS surfaces as an unresolved result, never hangs."""
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    result = await check_blood_bank("bed 12", config=slow)
    assert result.resolved is False
    assert result.error is not None and "timed out" in result.error


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the BloodBankAdapter Protocol the real adapter implements."""
    adapter = create_blood_bank_adapter(_FAST)
    assert isinstance(adapter, MockBloodBankAdapter)
    assert isinstance(adapter, BloodBankAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool returns JSON-serializable output."""
    from src.tools.blood_bank import build_server

    build_server(_FAST)  # smoke: server builds without error
    result = await check_blood_bank("room 4", config=_FAST)
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["crossmatch_status"] == "in_progress"
    assert payload["massive_transfusion_protocol_available"] is True
