"""Tests for the oncall_lookup L4 MCP tool (happy path + timeout/error paths)."""

from __future__ import annotations

import json

from config import ToolsConfig
from src.tools.oncall_lookup import (
    MockOnCallAdapter,
    OnCallAdapter,
    OnCallLookupResult,
    create_oncall_adapter,
    lookup_oncall,
)

# Fast, deterministic config for tests: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


async def test_resolves_known_role() -> None:
    """Happy path: a known role resolves to a primary (and backup) provider."""
    result = await lookup_oncall("on-call hospitalist", "4 West", config=_FAST)
    assert isinstance(result, OnCallLookupResult)
    assert result.error is None
    assert result.role == "hospitalist"  # alias normalized
    assert result.location == "4 West"
    assert result.primary is not None and result.primary.name.startswith("Dr. ")
    assert result.backup is not None
    assert "AMiON" in result.source_system


async def test_latency_is_observable() -> None:
    """The tool records elapsed time, and the mock honors the configured delay."""
    cfg = ToolsConfig(use_real_adapter=False, mock_latency_ms=60, mock_jitter_ms=0, timeout_ms=3000)
    result = await lookup_oncall("cardiologist", config=cfg)
    assert result.elapsed_ms >= 55  # ~60ms configured delay is reflected


async def test_unknown_role_returns_error_not_exception() -> None:
    """Error path: an unknown role yields a typed error result, not a raise."""
    result = await lookup_oncall("astrologer", config=_FAST)
    assert result.primary is None
    assert result.error is not None
    assert "astrologer" in result.error


async def test_timeout_returns_typed_result() -> None:
    """Timeout path: a slow adapter surfaces as a timeout result, never hangs."""
    # Mock delay (200ms) far exceeds the 20ms timeout.
    slow = ToolsConfig(use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20)
    result = await lookup_oncall("intensivist", config=slow)
    assert result.primary is None
    assert result.error is not None and "timed out" in result.error
    assert result.elapsed_ms >= 20


def test_mock_satisfies_adapter_interface() -> None:
    """The mock conforms to the OnCallAdapter Protocol the real adapter will implement."""
    adapter = create_oncall_adapter(_FAST)
    assert isinstance(adapter, MockOnCallAdapter)
    assert isinstance(adapter, OnCallAdapter)  # runtime_checkable Protocol
    assert hasattr(adapter, "source_system")


async def test_tool_returns_json() -> None:
    """The MCP tool callable returns JSON-serializable output."""
    from src.tools.oncall_lookup import build_server

    # Build the server (registers the tool) and invoke the underlying logic.
    build_server(_FAST)  # smoke: server builds without error
    result = await lookup_oncall("RRT", config=_FAST)
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    assert payload["role"] == "rrt"
    assert payload["primary"]["pager"] == "15000"
