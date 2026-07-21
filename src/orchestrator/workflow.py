"""Assemble the L2 orchestration workflow graph and wrap it for the gateway.

Graph shape (start = router)::

                         ┌─ FastPathDispatch ─ fan-out ─┐
    IntentEnvelope → Router ┤   (ack first)   [comms][context] ─ fan-in → FastSummary
                         │
                         └─ StandardEnrich ─ fan-out ─[patient][oncall]─ fan-in →
                              StandardResolve → StandardAct → StandardSummary

The router routes by message TYPE (FastPathRequest vs StandardPathRequest), so
exactly one path runs. Fan-out branches execute concurrently within a superstep;
the fan-in barrier joins them. ``output_from`` marks the two summaries as terminal
output; every other ``yield_output`` becomes an *intermediate* event streamed to
the gateway's ``speak()``.

agent-framework==1.11.0 (pinned in requirements.txt); WorkflowBuilder API verified
against https://learn.microsoft.com/agent-framework/workflows/.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_framework import Workflow, WorkflowBuilder

from src.gateway.intent_envelope import IntentEnvelope, Urgency
from src.telemetry import ATTR_PREFIX, BranchTiming, RunSummary, get_tracer

from .executors import (
    FastPathDispatch,
    FastSummary,
    RouterExecutor,
    StandardAct,
    StandardEnrich,
    StandardResolve,
    StandardSummary,
    make_mock_agent,
)
from .fastpath import FastPath

if TYPE_CHECKING:
    from src.gateway.gateway import VoiceGatewayBase

logger = logging.getLogger("nightingale.orchestrator")

# Default branch delays / latency budgets (seconds). Emergency branches are
# quick to keep the demo snappy; budgets mirror docs/01-architecture.md §4.
_DEFAULT_BRANCH_DELAY = 0.05
_DEFAULT_BUDGET = 2.0


def build_workflow(
        *,
        branch_delay: float = _DEFAULT_BRANCH_DELAY,
        branch_budget: float = _DEFAULT_BUDGET,
) -> Workflow:
    """Build the router + fast/standard-path workflow graph."""
    router = RouterExecutor(id="router")

    # Fast path: two placeholder agents fan out after the spoken acknowledgment.
    fast_dispatch = FastPathDispatch(id="fast_dispatch")
    comms_agent = make_mock_agent(
        id="fast_comms", name="comms", action="paging RRT",
        delay=branch_delay, budget=branch_budget,
    )
    context_agent = make_mock_agent(
        id="fast_context", name="context", action="pulling patient context",
        delay=branch_delay, budget=branch_budget,
    )
    fast_summary = FastSummary(id="fast_summary")

    # Standard path: enrich (patient context + on-call) → resolve → act → read-back.
    std_enrich = StandardEnrich(id="std_enrich")
    patient_agent = make_mock_agent(
        id="std_patient", name="patient_context", action="patient context lookup",
        delay=branch_delay, budget=branch_budget,
    )
    oncall_agent = make_mock_agent(
        id="std_oncall", name="oncall", action="on-call schedule lookup",
        delay=branch_delay, budget=branch_budget,
    )
    std_resolve = StandardResolve(id="std_resolve")
    std_act = StandardAct(id="std_act")
    std_summary = StandardSummary(id="std_summary")

    builder = WorkflowBuilder(
        start_executor=router,
        # The two summaries produce the final spoken output; every other
        # yield_output is streamed as an intermediate progress cue.
        output_from=[fast_summary, std_summary],
        intermediate_output_from="all_other",
    )
    # Router fans to both entry executors; message TYPE selects the live path.
    builder.add_edge(router, fast_dispatch)
    builder.add_edge(router, std_enrich)
    # Fast path fan-out / fan-in.
    builder.add_fan_out_edges(fast_dispatch, [comms_agent, context_agent])
    builder.add_fan_in_edges([comms_agent, context_agent], fast_summary)
    # Standard path fan-out / fan-in, then sequential resolve → act → summary.
    builder.add_fan_out_edges(std_enrich, [patient_agent, oncall_agent])
    builder.add_fan_in_edges([patient_agent, oncall_agent], std_resolve)
    builder.add_edge(std_resolve, std_act)
    builder.add_edge(std_act, std_summary)
    return builder.build()


@dataclass
class OrchestrationResult:
    """Outcome of one orchestration run."""

    correlation_id: str
    path: str  # "fast" | "standard"
    spoken: list[str] = field(default_factory=list)  # intermediate cues, in order
    summary: str | None = None  # final spoken summary
    ack_latency_ms: float | None = None  # emergency acknowledgment latency (fast path)
    branch_latencies_ms: dict[str, float] = field(default_factory=dict)  # per-branch (fast path)
    run_summary: "RunSummary | None" = None  # compact observability summary (docs §6)

    @property
    def run_summary_text(self) -> str:
        """The formatted run summary (routing, branch latencies, breaches), or ''."""
        return self.run_summary.format() if self.run_summary else ""


class Orchestrator:
    """Drives the workflow for one :class:`IntentEnvelope` and streams speech.

    EMERGENCY envelopes take the hardened :class:`FastPath` (acknowledge-first, escalate-first,
    budgeted, speculative — docs §3.2). ROUTINE envelopes run the standard-path workflow graph.
    Either way, every intermediate spoken update is forwarded to the gateway's ``speak()`` as
    it arrives, then the final summary is spoken.

    Each run is wrapped in a ``conversation`` OpenTelemetry span keyed by ``correlation_id`` so a
    single trace spans gateway → orchestrator → agents → tools, and a compact run summary is
    emitted at the end (docs/01-architecture.md §6, /observability).
    """

    def __init__(
        self,
        gateway: "VoiceGatewayBase",
        *,
        branch_delay: float = _DEFAULT_BRANCH_DELAY,
        branch_budget: float = _DEFAULT_BUDGET,
        fast_path: "FastPath | None" = None,
    ) -> None:
        self._gateway = gateway
        self._workflow = build_workflow(
            branch_delay=branch_delay, branch_budget=branch_budget
        )
        self._fast_path = fast_path or FastPath(gateway)
        self._tracer = get_tracer("nightingale.orchestrator")

    async def handle(self, envelope: IntentEnvelope) -> OrchestrationResult:
        """Run the emergency fast path or the standard graph, streaming spoken updates."""
        cid = envelope.correlation_id
        with self._tracer.start_as_current_span("conversation") as span:
            span.set_attribute(f"{ATTR_PREFIX}.correlation_id", cid)
            span.set_attribute(f"{ATTR_PREFIX}.urgency", envelope.urgency.value)
            span.set_attribute(f"{ATTR_PREFIX}.intent", envelope.intent)
            if envelope.urgency is Urgency.EMERGENCY:
                result = await self._handle_fast(envelope)
            else:
                result = await self._handle_standard(envelope)
            # (3) Emit the compact run summary — routing, branch latencies, breaches.
            if result.run_summary is not None:
                span.set_attribute(f"{ATTR_PREFIX}.budget_breaches",
                                   ",".join(result.run_summary.breaches) or "none")
                logger.info("run summary:\n%s", result.run_summary.format())
            return result

    async def _handle_fast(self, envelope: IntentEnvelope) -> OrchestrationResult:
        """Emergency: delegate to the hardened FastPath (it streams via the gateway itself)."""
        cid = envelope.correlation_id
        logger.info(
            "orchestration start correlation_id=%s urgency=EMERGENCY path=fast", cid
        )
        fp = await self._fast_path.run(envelope)
        logger.info("orchestration done correlation_id=%s path=fast", cid)
        run_summary = RunSummary(
            correlation_id=cid, path="fast", intent=envelope.intent,
            urgency=envelope.urgency.value, ack_latency_ms=fp.ack_latency_ms,
            ack_budget_ms=self._fast_path.spoken_ack_budget_ms,
            branches=[
                BranchTiming(o.name, o.latency_ms, o.budget_ms, o.status)
                for o in fp.outcomes
            ],
        )
        return OrchestrationResult(
            correlation_id=cid, path="fast", spoken=fp.spoken, summary=fp.summary,
            ack_latency_ms=fp.ack_latency_ms, branch_latencies_ms=fp.branch_latencies_ms,
            run_summary=run_summary,
        )

    async def _handle_standard(self, envelope: IntentEnvelope) -> OrchestrationResult:
        """Routine: run the standard-path workflow graph with streaming."""
        cid = envelope.correlation_id
        logger.info(
            "orchestration start correlation_id=%s urgency=%s path=standard",
            cid, envelope.urgency.value,
        )
        result = OrchestrationResult(correlation_id=cid, path="standard")
        t0 = time.perf_counter()
        async for event in self._workflow.run(envelope, stream=True):
            etype = getattr(event, "type", None)
            if etype == "intermediate":
                text = str(event.data)
                result.spoken.append(text)
                await self._gateway.speak(text)
            elif etype == "output":
                result.summary = str(event.data)
                await self._gateway.speak(result.summary)
        total_ms = round((time.perf_counter() - t0) * 1000, 2)
        result.run_summary = RunSummary(
            correlation_id=cid, path="standard", intent=envelope.intent,
            urgency=envelope.urgency.value, total_ms=total_ms,
        )
        logger.info("orchestration done correlation_id=%s path=standard", cid)
        return result
