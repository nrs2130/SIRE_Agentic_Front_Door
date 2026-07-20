"""Tests for L2, the orchestrator workflow (routing, concurrency, streaming)."""

from __future__ import annotations

import time

from src.gateway import IntentEnvelope, TextStubGateway, Urgency
from src.orchestrator import Orchestrator
from src.orchestrator.executors import summarize_branches
from src.orchestrator.messages import BranchResult


def _emergency() -> IntentEnvelope:
    return IntentEnvelope.create(
        "sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"}, "bed 12 looks septic"
    )


def _routine() -> IntentEnvelope:
    return IntentEnvelope.create(
        "contact_provider", Urgency.ROUTINE, {"role": "cardiologist"}, "call the cardiologist"
    )


async def test_emergency_takes_fast_path_ack_before_branches() -> None:
    """EMERGENCY routes to the fast path and speaks the ack before a delayed branch resolves."""
    gateway = TextStubGateway()
    # Branches deliberately slow (0.2s) so the ack must precede their resolution.
    orch = Orchestrator(gateway, branch_delay=0.2, branch_budget=2.0)
    result = await orch.handle(_emergency())

    assert result.path == "fast"
    # The spoken acknowledgment is the very first thing said…
    assert result.spoken, "expected streamed intermediate speech"
    assert result.spoken[0].startswith("Starting sepsis screen")
    # …and it is spoken before the final summary (which requires all branches done).
    assert result.summary is not None
    assert "done:" in result.summary
    # Gateway received the ack before the summary.
    assert gateway.spoken[0].startswith("Starting sepsis screen")
    assert gateway.spoken[-1] == result.summary


async def test_routine_takes_standard_path() -> None:
    """ROUTINE routes to the standard path and speaks a read-back confirmation."""
    gateway = TextStubGateway()
    orch = Orchestrator(gateway, branch_delay=0.02)
    result = await orch.handle(_routine())

    assert result.path == "standard"
    assert result.summary is not None
    assert result.summary.startswith("Confirmed: contact provider")
    assert "cardiologist" in result.summary


async def test_branches_run_concurrently() -> None:
    """Fan-out branches run in parallel: wall-clock < sum of the two branch delays."""
    gateway = TextStubGateway()
    delay = 0.25
    orch = Orchestrator(gateway, branch_delay=delay, branch_budget=2.0)

    start = time.perf_counter()
    await orch.handle(_emergency())
    elapsed = time.perf_counter() - start

    # Two 0.25s branches run concurrently (~0.25s), not sequentially (~0.5s).
    assert elapsed < 2 * delay, f"branches not concurrent: {elapsed:.3f}s"


async def test_intermediate_events_streamed() -> None:
    """Intermediate progress is streamed, not just the final result."""
    gateway = TextStubGateway()
    orch = Orchestrator(gateway, branch_delay=0.02)
    result = await orch.handle(_emergency())

    # More than one spoken cue means we streamed intermediates before the summary.
    assert len(result.spoken) >= 2
    # The final summary is spoken last, after the intermediates.
    assert gateway.spoken[-1] == result.summary


async def test_correlation_id_threaded() -> None:
    """The envelope's correlation_id is preserved through the run."""
    gateway = TextStubGateway()
    env = _emergency()
    orch = Orchestrator(gateway, branch_delay=0.02)
    result = await orch.handle(env)
    assert result.correlation_id == env.correlation_id


async def test_budget_breach_marks_pending() -> None:
    """A branch that exceeds its latency budget is reported 'pending', not blocking."""
    gateway = TextStubGateway()
    # delay (0.2) > budget (0.05) => both fast branches go 'pending'.
    orch = Orchestrator(gateway, branch_delay=0.2, branch_budget=0.05)
    result = await orch.handle(_emergency())
    assert result.summary is not None
    assert "pending:" in result.summary


def test_summarize_branches_done_and_pending() -> None:
    """The custom aggregator folds mixed statuses into a 'done: …; pending: …' summary."""
    env = _emergency()
    results = [
        BranchResult(env, "comms", "done", "paged"),
        BranchResult(env, "context", "pending", "slow"),
    ]
    summary = summarize_branches(results)
    assert summary == "done: comms; pending: context."
