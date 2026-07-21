"""Acceptance tests for the hardened emergency fast path (docs §3.2 + §4).

Covers the /emergency-fastpath criteria: acknowledge-first (< budget even with a 3 s branch),
escalate-first ordering, budget-breach "still working" + completion, out-of-order rolling
status, per-branch latency capture, and OpenTelemetry span attributes. No Azure.
"""

from __future__ import annotations

import asyncio
import time

from config import LatencyBudgets, ToolsConfig
from src.gateway import IntentEnvelope, TextStubGateway, Urgency
from src.orchestrator import BranchSpec, FastPath

_FAST_TOOLS = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


def _emergency() -> IntentEnvelope:
    return IntentEnvelope.create(
        "sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"}, "bed 12 looks septic"
    )


def _sleep_action(seconds: float, detail: str = "ok"):
    async def _action(_env: IntentEnvelope) -> str:
        await asyncio.sleep(seconds)
        return detail
    return _action


# --- (1) Acknowledge first: ack under budget even with a 3 s branch ----------
async def test_ack_under_budget_with_3s_branch() -> None:
    """A branch delayed to 3 s does NOT delay the acknowledgment (< 300 ms) or the run."""
    gateway = TextStubGateway()
    branches = lambda env: [  # noqa: E731
        BranchSpec("comms", _sleep_action(0.0, "paged"), budget_s=0.8, escalation=True,
                   label="paging RRT", done_phrase="RRT paged"),
        BranchSpec("blood_bank", _sleep_action(3.0), budget_s=0.2, label="checking blood bank"),
    ]
    fp = FastPath(gateway, budgets=LatencyBudgets.from_env(), tools=_FAST_TOOLS, branches=branches)

    t0 = time.perf_counter()
    result = await fp.run(_emergency())
    elapsed = time.perf_counter() - t0

    # Acknowledgment measured under the 300 ms budget…
    assert result.ack_latency_ms < 300, f"ack latency {result.ack_latency_ms}ms"
    assert result.spoken[0].startswith("Starting sepsis screen")
    # …and the whole conversation was NOT blocked by the 3 s branch (bounded by its budget).
    assert elapsed < 1.0, f"fast path blocked on slow branch: {elapsed:.3f}s"
    assert result.summary  # still completes


async def test_ack_is_spoken_before_any_branch_detail() -> None:
    """The ack reaches the gateway before any branch result — nothing blocks it."""
    gateway = TextStubGateway()
    result = await FastPath(gateway, tools=_FAST_TOOLS).run(_emergency())
    assert gateway.spoken[0] == result.ack
    # No branch status line precedes the ack.
    assert not gateway.spoken[0].startswith("Status:")


# --- (2) Escalate first -------------------------------------------------------
async def test_escalation_branch_starts_before_slow_branches() -> None:
    """The escalation branch is scheduled + starts before the slower branches (timestamps)."""
    gateway = TextStubGateway()
    branches = lambda env: [  # noqa: E731
        BranchSpec("labs", _sleep_action(0.15), budget_s=2.0, label="ordering labs"),
        BranchSpec("knowledge", _sleep_action(0.15), budget_s=2.0, label="retrieving protocol"),
        BranchSpec("comms", _sleep_action(0.05, "paged"), budget_s=0.8, escalation=True,
                   label="paging RRT", done_phrase="RRT paged"),
    ]
    result = await FastPath(gateway, tools=_FAST_TOOLS, branches=branches).run(_emergency())

    started = result.branch_started_ms
    assert started["comms"] < started["labs"]
    assert started["comms"] < started["knowledge"]
    # The escalation cue is also streamed before the slower branches' cues.
    comms_cue = next(i for i, s in enumerate(result.spoken) if "paging RRT" in s)
    labs_cue = next(i for i, s in enumerate(result.spoken) if "ordering labs" in s)
    assert comms_cue < labs_cue


# --- (3) Budget breach: "still working" + still completes ---------------------
async def test_budget_breach_emits_still_working_and_completes() -> None:
    """A branch over budget emits 'still working', is marked pending, and the run completes."""
    gateway = TextStubGateway()
    branches = lambda env: [  # noqa: E731
        BranchSpec("comms", _sleep_action(0.0, "paged"), budget_s=0.8, escalation=True,
                   done_phrase="RRT paged"),
        BranchSpec("blood_bank", _sleep_action(1.0), budget_s=0.1, label="checking blood bank"),
    ]
    result = await FastPath(gateway, tools=_FAST_TOOLS, branches=branches).run(_emergency())

    assert any("Still working on blood_bank" in s for s in result.spoken)
    outcomes = {o.name: o for o in result.outcomes}
    assert outcomes["blood_bank"].status == "pending"
    assert outcomes["comms"].status == "done"
    # The workflow still completes with a final summary that reflects the pending branch.
    assert "pending: blood_bank" in result.summary
    assert "done: comms" in result.summary


# --- (5) Rolling aggregator from out-of-order completions ---------------------
async def test_rolling_status_from_out_of_order_completions() -> None:
    """Branches finishing out of order produce a rolling spoken status; fast one ✓ first."""
    gateway = TextStubGateway()
    branches = lambda env: [  # noqa: E731
        # Listed slow-first, but 'fast' completes first → appears done earlier in the stream.
        BranchSpec("slow", _sleep_action(0.20), budget_s=2.0, done_phrase="slow done"),
        BranchSpec("fast", _sleep_action(0.02), budget_s=2.0, done_phrase="fast done"),
    ]
    result = await FastPath(gateway, tools=_FAST_TOOLS, branches=branches).run(_emergency())

    status_lines = [s for s in result.spoken if s.startswith("Status:")]
    assert len(status_lines) >= 2  # one per branch completion
    # First status line shows 'fast' done but 'slow' still awaited.
    assert "fast done ✓" in status_lines[0]
    assert "awaiting slow" in status_lines[0]
    # Final status line shows both done.
    assert "slow done ✓" in status_lines[-1] and "fast done ✓" in status_lines[-1]


# --- (6) Per-branch latency captured -----------------------------------------
async def test_per_branch_latencies_recorded() -> None:
    """Every branch reports a measured latency + start offset for the cockpit/observability."""
    gateway = TextStubGateway()
    result = await FastPath(gateway, tools=_FAST_TOOLS).run(_emergency())

    for name in ("comms", "labs", "knowledge", "context"):
        assert name in result.branch_latencies_ms
        assert result.branch_latencies_ms[name] >= 0
        assert name in result.branch_started_ms
    # comms is the escalation branch → starts first.
    assert result.branch_started_ms["comms"] <= min(
        result.branch_started_ms[n] for n in ("labs", "knowledge", "context")
    )


# --- (6) Latencies recorded as OpenTelemetry span attributes ------------------
async def test_latencies_recorded_as_otel_span_attributes() -> None:
    """Per-branch latency + ack latency are set as OpenTelemetry span attributes."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.fastpath")

    gateway = TextStubGateway()
    fp = FastPath(gateway, tools=_FAST_TOOLS, tracer=tracer)
    await fp.run(_emergency())

    spans = {s.name: s for s in exporter.get_finished_spans()}
    # Parent fast-path span carries the acknowledgment latency + urgency.
    parent = spans["fastpath"]
    assert "nightingale.ack_latency_ms" in parent.attributes
    assert parent.attributes["nightingale.urgency"] == "EMERGENCY"
    # Each branch span carries a numeric latency + status.
    branch_spans = [s for n, s in spans.items() if n.startswith("fastpath.branch.")]
    assert len(branch_spans) == 4
    for s in branch_spans:
        assert isinstance(s.attributes["nightingale.branch.latency_ms"], (int, float))
        assert s.attributes["nightingale.branch.status"] in {"done", "pending", "failed"}


# --- Default branches run against the mock tools (no Azure) -------------------
async def test_default_branches_all_done_with_fast_tools() -> None:
    """The default sepsis fan-out (comms/labs/knowledge/context) completes on mock tools."""
    gateway = TextStubGateway()
    result = await FastPath(gateway, tools=_FAST_TOOLS).run(_emergency())

    outcomes = {o.name: o for o in result.outcomes}
    assert set(outcomes) == {"comms", "labs", "knowledge", "context"}
    assert all(o.status == "done" for o in result.outcomes)
    assert "done: comms, labs, knowledge, context" in result.summary
