"""End-to-end tests for the sepsis hour-1 showcase (docs Part D + §3.2).

Drives the flow from the utterance "patient in bed 12 looks septic" through the text-stub
gateway, exercising: screening (SIRS/qSOFA from the monitor), fast-path acknowledgment under
budget, four concurrent branches, streamed progress, cited checklist read-back, the lactate
re-measure prompt, and the human-in-the-loop invariant. No Azure.
"""

from __future__ import annotations

import time

from config import LatencyBudgets, ToolsConfig
from src.gateway import TextStubGateway, Urgency
from src.orchestrator import SepsisHour1Workflow, score_screen

# Distinct, non-trivial mock latency so concurrency is measurable but tests stay quick.
_TOOLS = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=120, mock_jitter_ms=0, timeout_ms=3000
)
_BUDGETS = LatencyBudgets.from_env()


async def _run_septic_bed12():
    """Run the showcase from the canonical utterance via the gateway; return (gateway, result)."""
    gateway = TextStubGateway()
    envelope = await gateway.submit("patient in bed 12 looks septic")
    # The gateway classifies this as an emergency sepsis screen (L1 decision).
    assert envelope.urgency is Urgency.EMERGENCY
    assert envelope.intent == "sepsis_screen"
    assert envelope.entities.get("patient_ref") == "bed 12"
    wf = SepsisHour1Workflow(gateway, tools=_TOOLS, budgets=_BUDGETS)
    result = await wf.run(envelope)
    return gateway, result


# --- End-to-end acceptance ----------------------------------------------------
async def test_end_to_end_positive_screen_runs_hour1_bundle() -> None:
    """Positive screen → ack under budget, four concurrent branches, cited checklist, timer."""
    t0 = time.perf_counter()
    gateway, result = await _run_septic_bed12()
    elapsed = time.perf_counter() - t0

    # Screen is positive (bed 12: temp 38.6, HR 118, RR 24, SBP 88 → SIRS 3, qSOFA 2).
    assert result.suspicion is True
    assert result.screen.sirs_positive and result.screen.qsofa_positive

    # Immediate acknowledgment under the spoken-ack budget (fast-path entry latency).
    assert result.ack_latency_ms is not None
    assert result.ack_latency_ms < _BUDGETS.spoken_ack_ms  # < 300 ms
    assert any("hour-1 protocol and paging the response team" in s for s in result.spoken)

    # Four branches ran, and CONCURRENTLY: wall-clock < screen + sum of the branch delays.
    assert set(result.branch_latencies_ms) == {"comms", "orders", "knowledge", "timer"}
    # comms/orders/knowledge ~120ms each (timer instant). Sequential would be ~screen+360ms.
    assert elapsed < (0.12 + 0.36), f"branches not concurrent: {elapsed:.3f}s"

    # Cited 5-element checklist read back, treatments flagged for confirmation.
    assert len(result.checklist) == 5
    assert result.citations, "checklist must carry citations"
    assert any(c.source_id == "SSC-HR1-2018" for c in result.citations)
    assert any("for your confirmation" in step for step in result.checklist)  # treatments

    # Compliance timer active.
    assert result.timer_active is True

    # Final summary spoken last.
    assert gateway.spoken[-1] == result.summary


async def test_streams_live_progress() -> None:
    """Spoken progress is streamed as branches complete (rolling status), not just a summary."""
    gateway, result = await _run_septic_bed12()
    # Reading vitals + suspicion + ack + rolling status lines + checklist + summary.
    assert len(result.spoken) >= 6
    assert any(s.startswith("Reading ") for s in result.spoken)
    assert any("Suspicion of sepsis" in s for s in result.spoken)
    assert any(s.startswith("Status:") for s in result.spoken)  # rolling aggregator


async def test_lactate_remeasure_prompt_fires_when_initial_gt_2() -> None:
    """The mock lactate is 3.8 (>2) → the re-measure prompt fires and is spoken."""
    gateway, result = await _run_septic_bed12()
    assert result.initial_lactate is not None and result.initial_lactate > 2
    assert result.lactate_remeasure_prompted is True
    assert any("re-measure" in s.lower() for s in result.spoken)


async def test_no_autonomous_clinical_action() -> None:
    """Human-in-the-loop: treatments prepared for confirmation, nothing auto-executed."""
    _gateway, result = await _run_septic_bed12()
    assert result.autonomous_clinical_action is False
    # Treatment elements (antibiotics/fluids/vasopressors) are flagged for confirmation.
    treatment_steps = [s for s in result.checklist if "for your confirmation" in s]
    assert len(treatment_steps) == 3
    assert any("confirm and order" in s for s in result.spoken)


async def test_ack_precedes_branch_results() -> None:
    """The acknowledgment reaches the gateway before any branch status/detail line."""
    gateway, _result = await _run_septic_bed12()
    ack_idx = next(i for i, s in enumerate(gateway.spoken)
                   if "paging the response team" in s)
    status_idx = next(i for i, s in enumerate(gateway.spoken) if s.startswith("Status:"))
    assert ack_idx < status_idx


# --- Screening scorer (unit) --------------------------------------------------
def test_score_screen_positive_bed12_vitals() -> None:
    """bed 12 vitals: SIRS (temp/HR/RR) and qSOFA (RR/SBP) both positive."""
    screen = score_screen(rr=24, sbp=88, temp=38.6, hr=118)
    assert screen.sirs_score == 3 and screen.sirs_positive
    assert screen.qsofa_score == 2 and screen.qsofa_positive
    assert screen.suspicion is True


def test_score_screen_negative_normal_vitals() -> None:
    """Normal vitals: neither SIRS nor qSOFA positive → no suspicion."""
    screen = score_screen(rr=16, sbp=122, temp=37.0, hr=78)
    assert not screen.sirs_positive and not screen.qsofa_positive
    assert screen.suspicion is False


async def test_negative_screen_launches_no_bundle() -> None:
    """A negative screen documents and stops — no fast path, no autonomous action."""
    gateway = TextStubGateway()
    # bed 7 is stable (HR 78, RR 16, SBP 122, no fever) → screen negative.
    envelope = await gateway.submit("check bed 7 for possible sepsis")
    # Force the patient_ref to bed 7 (utterance may classify differently); build directly.
    from src.gateway import IntentEnvelope

    env7 = IntentEnvelope.create(
        "sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 7"}, "check bed 7 for sepsis"
    )
    result = await SepsisHour1Workflow(gateway, tools=_TOOLS, budgets=_BUDGETS).run(env7)
    assert result.suspicion is False
    assert result.ack_latency_ms is None  # fast path never ran
    assert not result.checklist
    assert "negative" in result.summary.lower()
