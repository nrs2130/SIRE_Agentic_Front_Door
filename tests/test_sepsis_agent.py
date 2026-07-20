"""Tests for the L3 sepsis agent (grounded happy path + KB-failure/timeout + safety).

Runs entirely against mock MCP tools + a mock Foundry IQ provider — no Azure.
"""

from __future__ import annotations

from config import ToolsConfig
from src.agents import agent_for_intent
from src.agents.hosted import register_hosted_agent
from src.agents.sepsis import (
    SEPSIS_INTENTS,
    Hour1Step,
    SepsisAgent,
    SepsisAgentResult,
    create_sepsis_agent,
    sepsis_hosted_agent_spec,
)
from src.gateway.intent_envelope import IntentEnvelope, Urgency
from src.knowledge.sepsis_protocol import ProtocolResult, SepsisKnowledgeProvider

# Fast, deterministic config: minimal latency, generous timeout.
_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


def _envelope(intent, urgency, entities) -> IntentEnvelope:
    return IntentEnvelope.create(intent, urgency, entities, f"utterance for {intent}")


class _RaisingKnowledgeProvider:
    """A Foundry IQ provider stand-in that fails, to exercise the degraded path."""

    source_system = "MOCK (Foundry IQ — raising)"

    async def retrieve(self, query: str, *, correlation_id=None) -> ProtocolResult:
        raise RuntimeError("knowledge base unreachable")


async def test_grounds_pages_and_orders_screen_labs() -> None:
    """Happy path: plan grounded in Foundry IQ with citations; RRT paged; diagnostics ordered."""
    agent = SepsisAgent(_FAST)
    env = _envelope("sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"})
    result = await agent.handle(env)

    assert isinstance(result, SepsisAgentResult)
    assert result.grounded is True
    assert result.citations and result.citations[0].source_id == "SSC-HR1-2021"
    assert result.page_id is not None  # RRT paged (augments Engage)
    assert result.lab_order_id is not None  # lactate + cultures ordered
    # Diagnostics ordered; treatment steps proposed, awaiting a clinician.
    diagnostics = [s for s in result.steps if s.category == "diagnostic"]
    treatments = [s for s in result.steps if s.category == "treatment"]
    assert diagnostics and all(s.status == "ordered" for s in diagnostics)
    assert treatments and all(s.status == "proposed" for s in treatments)
    assert all(s.source_id for s in result.steps)  # every step is cited
    assert "confirm" in result.spoken_update.lower()
    assert result.correlation_id == env.correlation_id


async def test_never_takes_autonomous_clinical_action() -> None:
    """Safety invariant: treatment is never auto-ordered; a human must ack; no alarm override."""
    agent = SepsisAgent(_FAST)
    env = _envelope("start_sepsis_protocol", Urgency.EMERGENCY, {"patient_ref": "bed 12"})
    result = await agent.handle(env)

    assert result.autonomous_treatment is False
    assert result.requires_human_ack is True
    treatments = [s for s in result.steps if s.category == "treatment"]
    assert treatments and all(
        s.status == "proposed" and s.requires_human_ack for s in treatments
    )


async def test_screen_positive_from_vitals() -> None:
    """qSOFA-style screen flags positive from supplied vitals (decision support)."""
    agent = SepsisAgent(_FAST)
    env = _envelope(
        "sepsis_screen", Urgency.EMERGENCY,
        {"patient_ref": "bed 12", "rr": "24", "sbp": "95"},
    )
    result = await agent.handle(env)
    assert result.screen == "positive"
    assert any("qSOFA" in n for n in result.notes)


async def test_reads_back_allergies_from_context() -> None:
    """Patient context is read back (penicillin allergy is relevant to proposed antibiotics)."""
    agent = SepsisAgent(_FAST)
    env = _envelope("sepsis_screen", Urgency.ROUTINE, {"patient_ref": "bed 12"})
    result = await agent.handle(env)
    assert any("penicillin" in n.lower() for n in result.notes)


async def test_knowledge_failure_degrades_to_cached_no_autonomous_action() -> None:
    """KB-failure path: retrieval fails → cached hour-1 summary, flagged unverified, still safe."""
    agent = SepsisAgent(_FAST, knowledge_provider=_RaisingKnowledgeProvider())
    env = _envelope("sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"})
    result = await agent.handle(env)

    assert result.grounded is False  # not grounded live
    assert result.steps  # but still has the cached bundle — no hang, no fabrication
    assert result.citations  # cached summary still cites its source
    assert any("cached" in n.lower() for n in result.notes)
    assert "verify" in result.spoken_update.lower()
    # Safety holds even when degraded.
    assert result.autonomous_treatment is False
    assert result.requires_human_ack is True


async def test_knowledge_timeout_degrades_gracefully() -> None:
    """Timeout path: a slow KB provider times out, agent degrades rather than hanging."""

    class _SlowProvider:
        source_system = "MOCK (Foundry IQ — slow)"

        async def retrieve(self, query: str, *, correlation_id=None) -> ProtocolResult:
            import asyncio

            await asyncio.sleep(0.5)  # exceeds the 20ms timeout below
            raise AssertionError("should have timed out before returning")

    slow_cfg = ToolsConfig(
        use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=20
    )
    agent = SepsisAgent(slow_cfg, knowledge_provider=_SlowProvider())
    env = _envelope("sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"})
    result = await agent.handle(env)
    assert result.grounded is False
    assert result.steps  # cached fallback still present
    assert result.autonomous_treatment is False


def test_registered_with_router_for_its_intents() -> None:
    """The agent registers itself for its intents so the router can dispatch to it."""
    for intent in SEPSIS_INTENTS:
        reg = agent_for_intent(intent)
        assert reg is not None and reg.capability == "sepsis"
        assert reg.factory is create_sepsis_agent


def test_hosted_registration_dry_run_prints_plan(capsys) -> None:
    """--dry-run plan includes the Foundry IQ KB tool + the three action tools, no Azure."""
    spec = sepsis_hosted_agent_spec()
    plan = register_hosted_agent(spec, project_endpoint=None, dry_run=True)

    assert plan["action"] == "create_hosted_agent"
    assert plan["agent"]["name"] == "nightingale-sepsis"
    tools = {t["server_label"]: t for t in plan["agent"]["tools"]}
    assert set(tools) == {
        "nightingale_labs_hl7",
        "nightingale_comms_page",
        "nightingale_patient_context",
        "nightingale_sepsis_kb",
    }
    # The KB tool exposes knowledge_base_retrieve and carries a project connection.
    kb = tools["nightingale_sepsis_kb"]
    assert kb["allowed_tools"] == ["knowledge_base_retrieve"]
    assert kb["project_connection_id"] == "nightingale-sepsis-kb-connection"
    # Action tools have no project connection.
    assert "project_connection_id" not in tools["nightingale_labs_hl7"]
    # Instructions enforce grounding + human-in-the-loop.
    instr = plan["agent"]["instructions"].lower()
    assert "knowledge_base_retrieve" in instr and "cite" in instr
    assert "must not issue autonomous treatment" in instr
    assert "create_hosted_agent" in capsys.readouterr().out


def test_isinstance_provider_protocol() -> None:
    """The mock provider satisfies the SepsisKnowledgeProvider protocol (swap-in contract)."""
    from src.knowledge.sepsis_protocol import MockSepsisKnowledgeProvider

    assert isinstance(MockSepsisKnowledgeProvider(_FAST), SepsisKnowledgeProvider)
