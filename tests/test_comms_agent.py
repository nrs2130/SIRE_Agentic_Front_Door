"""Tests for the L3 comms agent (happy path + tool-failure/timeout + registration)."""

from __future__ import annotations

from config import ToolsConfig
from src.agents import agent_for_intent
from src.agents.comms import (
    COMMS_INTENTS,
    CommsAgent,
    CommsAgentResult,
    comms_hosted_agent_spec,
    create_comms_agent,
)
from src.agents.hosted import register_hosted_agent
from src.gateway.intent_envelope import IntentEnvelope, Urgency

# Fast, deterministic config: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


def _envelope(intent, urgency, entities) -> IntentEnvelope:
    return IntentEnvelope.create(intent, urgency, entities, f"utterance for {intent}")


async def test_resolves_role_and_pages() -> None:
    """Happy path: a role is resolved via oncall_lookup, then paged via comms_page."""
    agent = CommsAgent(_FAST)
    env = _envelope("contact_provider", Urgency.ROUTINE, {"role": "cardiologist"})
    result = await agent.handle(env)

    assert isinstance(result, CommsAgentResult)
    assert result.resolved is True
    assert result.error is None
    assert result.recipient and result.recipient.startswith("Dr. ")
    assert result.recipient_id is not None  # came from oncall_lookup
    assert result.page_id and result.page_id.startswith("page-")
    assert result.delivery_state == "delivered"
    assert result.requires_human_ack is True  # safety: human acts
    assert "Paged" in result.spoken_update
    assert result.correlation_id == env.correlation_id


async def test_emergency_pages_stat_and_defaults_team() -> None:
    """An emergency team intent with no explicit role pages the default team, STAT."""
    agent = CommsAgent(_FAST)
    env = _envelope("rapid_response", Urgency.EMERGENCY, {"patient_ref": "bed 12"})
    result = await agent.handle(env)
    assert result.resolved is True
    assert result.escalation_tier == 3  # stat -> tier 3
    assert "stat" in result.spoken_update


async def test_pages_explicit_person_without_lookup() -> None:
    """A named person is paged directly (no on-call resolution needed)."""
    agent = CommsAgent(_FAST)
    env = _envelope("contact_provider", Urgency.ROUTINE, {"person": "Dr. Jane Roe"})
    result = await agent.handle(env)
    assert result.resolved is True
    assert result.recipient == "Dr. Jane Roe"
    assert result.recipient_id is None


async def test_no_recipient_returns_typed_result() -> None:
    """With neither person nor resolvable role, the agent returns a typed no-recipient result."""
    agent = CommsAgent(_FAST)
    env = _envelope("contact_provider", Urgency.ROUTINE, {})
    result = await agent.handle(env)
    assert result.resolved is False
    assert result.error is not None
    assert "don't have a person or role" in result.spoken_update


async def test_page_timeout_degrades_gracefully() -> None:
    """Tool-failure/timeout path: a slow comms_page yields a failed result, never hangs."""
    # Page mock delay (200ms) exceeds the 20ms timeout; oncall lookup is fast enough
    # (its own budget) but the page times out.
    slow_page = ToolsConfig(
        use_real_adapter=False, mock_latency_ms=200, mock_jitter_ms=0, timeout_ms=20
    )
    agent = CommsAgent(slow_page)
    env = _envelope("contact_provider", Urgency.EMERGENCY, {"person": "Dr. Jane Roe"})
    result = await agent.handle(env)
    assert result.resolved is False
    assert result.error is not None and "timed out" in result.error
    assert "couldn't reach" in result.spoken_update
    assert result.requires_human_ack is True


def test_registered_with_router_for_its_intents() -> None:
    """The agent registers itself for its intents so the router can dispatch to it."""
    for intent in COMMS_INTENTS:
        reg = agent_for_intent(intent)
        assert reg is not None and reg.capability == "comms"
        assert reg.factory is create_comms_agent


def test_hosted_registration_dry_run_prints_plan(capsys) -> None:
    """The registration --dry-run produces a correct plan and touches no Azure."""
    spec = comms_hosted_agent_spec()
    plan = register_hosted_agent(spec, project_endpoint=None, dry_run=True)

    assert plan["action"] == "create_hosted_agent"
    assert plan["agent"]["name"] == "nightingale-comms"
    assert plan["auth"] == "DefaultAzureCredential"
    tool_labels = {t["server_label"] for t in plan["agent"]["tools"]}
    assert tool_labels == {"nightingale_oncall_lookup", "nightingale_comms_page"}
    allowed = {tool for t in plan["agent"]["tools"] for tool in t["allowed_tools"]}
    assert allowed == {"oncall_lookup", "comms_page"}
    # It printed the JSON plan.
    assert "create_hosted_agent" in capsys.readouterr().out
