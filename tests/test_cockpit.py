"""Cockpit backend (L6 demo) — orchestration-client behavior against mock tools, no Azure.

Covers the acceptance path (sepsis emergency → fast path, four concurrent branches, hour-1
checklist, compliance timer), the PANIC override, a budget breach turning a branch amber, and
the routine standard path. Streamlit is never imported — the driver is UI-independent.
"""

from __future__ import annotations

from config import LatencyBudgets, ToolsConfig
from src.demo.cockpit import (
    CockpitState,
    build_checklist,
    control_plane_url,
    run_cockpit,
    sepsis_branch_meta,
)
from src.gateway.intent_envelope import Urgency

_FAST_TOOLS = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=20, mock_jitter_ms=0, timeout_ms=3000
)


async def test_sepsis_emergency_runs_fast_path_with_four_branches() -> None:
    """'patient in bed 12 looks septic' → fast path, 4 concurrent branches, cited hour-1 bundle."""
    state = CockpitState()
    await run_cockpit(state, "patient in bed 12 looks septic", tools=_FAST_TOOLS)
    snap = state.snapshot()

    assert snap.path == "fast"
    assert snap.urgency == Urgency.EMERGENCY.value
    assert snap.intent == "sepsis_screen"
    assert snap.correlation_id
    # Four concurrent branches, each measured and finalized.
    assert [b.name for b in snap.branches] == ["comms", "orders", "knowledge", "timer"]
    assert all(b.status in ("done", "breach") for b in snap.branches)
    assert all(b.latency_ms is not None for b in snap.branches)
    # Escalation branch (comms) is flagged first.
    assert snap.branches[0].name == "comms" and snap.branches[0].escalation
    # Acknowledgment measured (fast path speaks before any tool runs).
    assert snap.ack_latency_ms is not None
    assert any(t.is_ack for t in snap.transcript)
    # Sepsis suspicion + citation-backed, human-in-the-loop.
    assert snap.sepsis.suspicion is True
    assert snap.sepsis.citations
    assert snap.summary


async def test_hour1_checklist_ticks_and_treatments_stay_proposed() -> None:
    """Diagnostics flip to ordered when the orders branch lands; treatments stay clinician-gated."""
    state = CockpitState()
    await run_cockpit(state, "patient in bed 12 looks septic", tools=_FAST_TOOLS)
    checklist = build_checklist(state.snapshot())

    assert len(checklist) == 5
    diagnostics = [i for i in checklist if i.category == "diagnostic"]
    treatments = [i for i in checklist if i.category == "treatment"]
    assert diagnostics and all(i.status == "ordered" for i in diagnostics)
    # Treatments are NEVER auto-ordered — always proposed for a clinician to confirm.
    assert treatments and all(i.status == "proposed" for i in treatments)


async def test_compliance_timer_starts_and_lactate_remeasure_prompted() -> None:
    """The hour-1 clock starts (timer branch) and a high initial lactate prompts a re-measure."""
    state = CockpitState()
    await run_cockpit(state, "patient in bed 12 looks septic", tools=_FAST_TOOLS)
    s = state.snapshot().sepsis

    assert s.timer_started_mono is not None
    assert s.elapsed_s() >= 0.0
    assert 0.0 <= s.remaining_s() <= s.window_s
    # bed 12's mock returns lactate > 2 → a re-measure is due.
    assert s.remeasure is True


async def test_panic_forces_emergency() -> None:
    """The PANIC button forces an EMERGENCY route regardless of the typed text."""
    state = CockpitState()
    await run_cockpit(state, "", panic=True, tools=_FAST_TOOLS)
    snap = state.snapshot()
    assert snap.urgency == Urgency.EMERGENCY.value
    assert snap.path == "fast"


async def test_branch_breach_turns_amber() -> None:
    """A branch pushed past its budget breaches (amber) but the run still completes."""
    # Tight labs budget + slow mock → the orders/labs branch must breach.
    budgets = LatencyBudgets(
        spoken_ack_ms=300, router_ms=10, comms_tool_ms=5000,
        labs_tool_ms=5, knowledge_ms=5000, patient_context_ms=5000,
    )
    slow = ToolsConfig(
        use_real_adapter=False, mock_latency_ms=250, mock_jitter_ms=0, timeout_ms=3000
    )
    state = CockpitState()
    await run_cockpit(state, "patient in bed 12 looks septic", tools=slow, budgets=budgets)
    snap = state.snapshot()

    orders = next(b for b in snap.branches if b.name == "orders")
    assert orders.status == "breach"
    assert orders.over_budget()
    # The run still finishes (never blocks on the slow branch).
    assert snap.running is False
    assert snap.summary


async def test_routine_utterance_takes_standard_path() -> None:
    """A non-emergency utterance routes to the standard path (no ack, no sepsis panel)."""
    state = CockpitState()
    await run_cockpit(state, "find nurse Barbara in cardiology", tools=_FAST_TOOLS)
    snap = state.snapshot()
    assert snap.path == "standard"
    assert snap.urgency == Urgency.ROUTINE.value
    assert snap.ack_latency_ms is None
    assert snap.sepsis.active is False


def test_sepsis_branch_meta_matches_budgets() -> None:
    """The cockpit's branch metadata mirrors the workflow's budgets (names + soft timeouts)."""
    budgets = LatencyBudgets.from_env()
    metas = sepsis_branch_meta(budgets)
    assert [m.name for m in metas] == ["comms", "orders", "knowledge", "timer"]
    assert metas[0].escalation is True
    assert metas[1].budget_ms == budgets.labs_tool_ms


def test_control_plane_url_is_foundry_portal() -> None:
    assert control_plane_url().startswith("https://ai.azure.com")
