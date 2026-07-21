"""Tests for L2, the orchestrator workflow (routing, concurrency, streaming)."""

from __future__ import annotations

from config import ToolsConfig
from src.gateway import IntentEnvelope, TextStubGateway, Urgency
from src.orchestrator import FastPath, Orchestrator
from src.orchestrator.executors import summarize_branches
from src.orchestrator.messages import BranchResult

# Low-latency mock tools so the fast path completes quickly + deterministically in tests.
_FAST_TOOLS = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


def _emergency() -> IntentEnvelope:
    return IntentEnvelope.create(
        "sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"}, "bed 12 looks septic"
    )


def _routine() -> IntentEnvelope:
    return IntentEnvelope.create(
        "contact_provider", Urgency.ROUTINE, {"role": "cardiologist"}, "call the cardiologist"
    )


def _fast_orchestrator(gateway: TextStubGateway) -> Orchestrator:
    """Orchestrator whose emergency FastPath uses low-latency mock tools."""
    return Orchestrator(gateway, fast_path=FastPath(gateway, tools=_FAST_TOOLS))


async def test_emergency_takes_fast_path_ack_before_branches() -> None:
    """EMERGENCY routes to the hardened fast path, acknowledging before branches resolve."""
    gateway = TextStubGateway()
    result = await _fast_orchestrator(gateway).handle(_emergency())

    assert result.path == "fast"
    # The spoken acknowledgment is the very first thing said…
    assert result.spoken, "expected streamed intermediate speech"
    assert result.spoken[0].startswith("Starting sepsis screen")
    assert gateway.spoken[0].startswith("Starting sepsis screen")
    # …measured under the acknowledgment budget…
    assert result.ack_latency_ms is not None and result.ack_latency_ms < 300
    # …and the final summary is spoken last, after all branches joined.
    assert result.summary is not None and "done:" in result.summary
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


async def test_intermediate_events_streamed() -> None:
    """Intermediate progress is streamed, not just the final result."""
    gateway = TextStubGateway()
    result = await _fast_orchestrator(gateway).handle(_emergency())

    # More than one spoken cue means we streamed intermediates before the summary.
    assert len(result.spoken) >= 2
    # The final summary is spoken last, after the intermediates.
    assert gateway.spoken[-1] == result.summary


async def test_correlation_id_threaded() -> None:
    """The envelope's correlation_id is preserved through the run."""
    gateway = TextStubGateway()
    env = _emergency()
    result = await _fast_orchestrator(gateway).handle(env)
    assert result.correlation_id == env.correlation_id


def test_summarize_branches_done_and_pending() -> None:
    """The custom aggregator folds mixed statuses into a 'done: …; pending: …' summary."""
    env = _emergency()
    results = [
        BranchResult(env, "comms", "done", "paged"),
        BranchResult(env, "context", "pending", "slow"),
    ]
    summary = summarize_branches(results)
    assert summary == "done: comms; pending: context."

