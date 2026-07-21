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
    flow_dot,
    realistic_tools,
    run_cockpit,
    sepsis_branch_meta,
    workflow_key,
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


# --- Routing map / flow visualizer -------------------------------------------
def test_realistic_tools_is_a_fixed_profile() -> None:
    """The mock profile is fixed + realistic (models real downstream systems), not a dial."""
    t = realistic_tools()
    assert t.use_real_adapter is False
    assert 100 <= t.mock_latency_ms <= 2000  # a plausible downstream round-trip
    assert t.mock_jitter_ms > 0


def test_workflow_key_routes_intents() -> None:
    assert workflow_key("sepsis_screen", "fast") == "sepsis"
    assert workflow_key("code_blue", "fast") == "emergency"
    assert workflow_key("contact_provider", "standard") == "sire"
    assert workflow_key("locate_equipment", "standard") == "standard"


async def test_contact_provider_routes_to_sire_workflow() -> None:
    """'page the on-call cardiologist' → standard path, SIRE resolve+page workflow."""
    state = CockpitState()
    await run_cockpit(state, "page the on-call cardiologist", tools=_FAST_TOOLS)
    snap = state.snapshot()
    assert snap.path == "standard"
    assert snap.intent == "contact_provider"
    assert workflow_key(snap.intent, snap.path) == "sire"
    # The SIRE/standard path surfaces its concurrent enrich branches for the flow view.
    assert [b.name for b in snap.branches] == ["patient_context", "oncall"]


async def test_flow_dot_lights_active_workflow_only() -> None:
    """The routing graph shows all four candidate workflows but lights only the chosen one."""
    state = CockpitState()
    await run_cockpit(state, "patient in bed 12 looks septic", tools=_FAST_TOOLS)
    dot = flow_dot(state.snapshot())

    assert dot.startswith("digraph")
    # Front-door nodes + all four candidate workflows are always drawn (the map).
    for node in ("badge", "gateway", "router", "wf_sepsis", "wf_emergency", "wf_sire", "wf_standard"):
        assert node in dot
    # The chosen workflow's concurrent branches are rendered as lit child nodes.
    for name in ("comms", "orders", "knowledge", "timer"):
        assert f"br_sepsis_{name}" in dot
    # Inactive routes are dashed; the active one is not.
    assert "wf_emergency" in dot and "style=dashed" in dot


def test_flow_dot_handles_empty_prerun_snapshot() -> None:
    """Before any run the map still renders (all candidates idle), so the presenter can show it."""
    dot = flow_dot(CockpitState().snapshot())
    assert dot.startswith("digraph") and dot.rstrip().endswith("}")
    assert all(f"wf_{k}" in dot for k in ("sepsis", "emergency", "sire", "standard"))

