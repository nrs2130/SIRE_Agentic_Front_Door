"""Hardened **emergency fast path** (docs/01-architecture.md §3.2 + §4).

The demo's money shot: on an ``EMERGENCY`` envelope this engine

1. **acknowledges first** — speaks a spoken acknowledgment *before any tool call runs*, and
   measures the acknowledgment latency (must stay under the ``spoken_ack_ms`` budget),
2. **escalates first** — schedules the escalation/notification branch (``comms_page``) before
   the slower branches (orders, retrieval, context), so the team is reached while enrichment
   runs,
3. applies a **per-branch latency budget** from :class:`config.LatencyBudgets` with
   ``asyncio.wait_for``; on breach it speaks a "still working on X" cue and **continues**
   (the branch is marked ``pending`` rather than blocking the conversation),
4. dispatches slow branches **speculatively** (as concurrent tasks) and only joins at a
   **fan-in barrier** for the final summary — everything streams as it completes,
5. folds **out-of-order** branch completions into a **rolling spoken status** (custom
   aggregator), and
6. records **per-branch latency** as OpenTelemetry span attributes (feeds /observability).

Pure ``asyncio`` (not the workflow graph) so the ordering + latency guarantees are explicit
and directly testable. Branches call the existing **mock-backed L4 tools** — no Azure. Default
branches model the sepsis/emergency fan-out (comms → labs → knowledge → context); tests inject
custom branches to exercise budgets/ordering.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import LatencyBudgets, ToolsConfig
from src.gateway.intent_envelope import IntentEnvelope
from src.knowledge.sepsis_protocol import retrieve_sepsis_protocol
from src.telemetry import ATTR_PREFIX, get_tracer
from src.tools.comms_page import send_page
from src.tools.labs_hl7 import order_labs
from src.tools.patient_context import get_patient_context

if TYPE_CHECKING:
    from opentelemetry import trace

    from src.gateway.gateway import VoiceGatewayBase

logger = logging.getLogger("nightingale.orchestrator.fastpath")


@dataclass(frozen=True)
class BranchSpec:
    """One fast-path branch: an async action bounded by a soft latency budget.

    ``action`` performs the tool call and returns a short human detail string; it may raise on
    a hard failure. ``escalation`` branches are scheduled *first* on the fan-out.
    """

    name: str
    action: Callable[[IntentEnvelope], Awaitable[str]]
    budget_s: float
    escalation: bool = False
    label: str = ""  # spoken progress cue when the branch starts
    done_phrase: str = ""  # short past-tense phrase for the rolling status when done


@dataclass(frozen=True)
class BranchOutcome:
    """A single branch's result, with the latency measurements the cockpit/observability need."""

    name: str
    status: str  # done | pending (budget breach) | failed (exception)
    detail: str
    latency_ms: float
    started_ms: float  # when the branch began, offset from t0 (proves escalation-first)
    budget_ms: float


@dataclass
class FastPathResult:
    """Outcome of one emergency fast-path run (+ measured latencies for reporting)."""

    correlation_id: str
    ack: str
    ack_latency_ms: float
    spoken: list[str] = field(default_factory=list)  # every spoken line, in emission order
    outcomes: list[BranchOutcome] = field(default_factory=list)
    summary: str = ""

    @property
    def branch_latencies_ms(self) -> dict[str, float]:
        return {o.name: o.latency_ms for o in self.outcomes}

    @property
    def branch_started_ms(self) -> dict[str, float]:
        return {o.name: o.started_ms for o in self.outcomes}


def _summarize(outcomes: dict[str, BranchOutcome], order: list[str]) -> str:
    """Fold branch outcomes into 'done: …; pending: …; failed: …' (deterministic order)."""
    buckets: dict[str, list[str]] = {"done": [], "pending": [], "failed": []}
    for name in order:
        o = outcomes.get(name)
        if o is not None:
            buckets.setdefault(o.status, []).append(name)
    parts = [f"{status}: {', '.join(names)}" for status, names in buckets.items() if names]
    return "; ".join(parts) + "." if parts else "nothing to report."


class FastPath:
    """Acknowledge-first, escalate-first, budgeted, speculative emergency orchestration."""

    def __init__(
        self,
        gateway: "VoiceGatewayBase",
        *,
        budgets: LatencyBudgets | None = None,
        tools: ToolsConfig | None = None,
        branches: Callable[[IntentEnvelope], list[BranchSpec]] | None = None,
        tracer: "trace.Tracer | None" = None,
        ack_text: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._budgets = budgets or LatencyBudgets.from_env()
        self._tools = tools or ToolsConfig.from_env()
        self._branch_factory = branches or self._default_branches
        self._tracer = tracer or get_tracer("nightingale.orchestrator.fastpath")
        self._ack_text = ack_text

    # -- public API ----------------------------------------------------------
    async def run(self, envelope: IntentEnvelope) -> FastPathResult:
        """Run the emergency fast path for ``envelope`` and stream spoken updates."""
        cid = envelope.correlation_id
        t0 = time.perf_counter()
        spoken: list[str] = []

        with self._tracer.start_as_current_span("fastpath") as span:
            span.set_attribute(f"{ATTR_PREFIX}.correlation_id", cid)
            span.set_attribute(f"{ATTR_PREFIX}.urgency", envelope.urgency.value)
            span.set_attribute(f"{ATTR_PREFIX}.intent", envelope.intent)

            # (1) ACKNOWLEDGE FIRST — before any branch/tool is even created.
            ack = self._ack(envelope)
            await self._speak(spoken, ack)
            ack_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            span.set_attribute(f"{ATTR_PREFIX}.ack_latency_ms", ack_latency_ms)
            if ack_latency_ms > self._budgets.spoken_ack_ms:  # should never happen
                logger.warning(
                    "ack latency %.1fms EXCEEDED budget %dms correlation_id=%s",
                    ack_latency_ms, self._budgets.spoken_ack_ms, cid,
                )

            # (2)+(4) ESCALATE FIRST, then speculatively dispatch the rest as tasks.
            specs = sorted(self._branch_factory(envelope), key=lambda b: not b.escalation)
            tasks: list[asyncio.Task[BranchOutcome]] = []
            for spec in specs:
                if spec.label:
                    await self._speak(spoken, f"[{spec.name}] {spec.label}…")
                tasks.append(
                    asyncio.create_task(
                        self._run_branch(spec, envelope, t0, spoken), name=spec.name
                    )
                )
                if spec.escalation:
                    # Yield so the escalation branch starts (records started_ms + hits its
                    # tool await) BEFORE the slower branches are created — guarantees ordering.
                    await asyncio.sleep(0)

            # (5) ROLLING AGGREGATOR — stream status as branches finish, out of order.
            outcomes: dict[str, BranchOutcome] = {}
            order = [s.name for s in specs]
            for finished in asyncio.as_completed(tasks):
                outcome = await finished
                outcomes[outcome.name] = outcome
                await self._speak(spoken, self._rolling_status(outcomes, specs))

            # Fan-in barrier crossed (all branches accounted for) → final spoken summary.
            summary = "Emergency response — " + _summarize(outcomes, order)
            await self._speak(spoken, summary)
            span.set_attribute(f"{ATTR_PREFIX}.branch_count", len(order))
            logger.info(
                "fast-path done correlation_id=%s ack_ms=%.1f branches=%s",
                cid, ack_latency_ms, {n: round(o.latency_ms, 1) for n, o in outcomes.items()},
            )
            return FastPathResult(
                correlation_id=cid, ack=ack, ack_latency_ms=ack_latency_ms,
                spoken=spoken, outcomes=[outcomes[n] for n in order if n in outcomes],
                summary=summary,
            )

    # -- internals -----------------------------------------------------------
    async def _run_branch(
        self,
        spec: BranchSpec,
        envelope: IntentEnvelope,
        t0: float,
        spoken: list[str],
    ) -> BranchOutcome:
        """Run one branch under its soft budget; breach → 'still working' + pending (no block)."""
        cid = envelope.correlation_id
        started_ms = round((time.perf_counter() - t0) * 1000, 2)
        b0 = time.perf_counter()
        with self._tracer.start_as_current_span(f"fastpath.branch.{spec.name}") as span:
            span.set_attribute(f"{ATTR_PREFIX}.correlation_id", cid)
            span.set_attribute(f"{ATTR_PREFIX}.branch", spec.name)
            span.set_attribute(f"{ATTR_PREFIX}.branch.escalation", spec.escalation)
            span.set_attribute(f"{ATTR_PREFIX}.branch.budget_ms", spec.budget_s * 1000)
            span.set_attribute(f"{ATTR_PREFIX}.branch.started_ms", started_ms)
            try:
                detail = await asyncio.wait_for(spec.action(envelope), timeout=spec.budget_s)
                status, detail = "done", detail
            except asyncio.TimeoutError:
                # (3) Budget breach: speak a "still working" cue and continue — never block.
                status = "pending"
                detail = f"exceeded {int(spec.budget_s * 1000)}ms budget"
                await self._speak(spoken, f"Still working on {spec.name}…")
                logger.info("branch PENDING name=%s correlation_id=%s", spec.name, cid)
            except Exception as exc:  # degrade, don't crash the conversation
                status = "failed"
                detail = str(exc)
                logger.warning("branch FAILED name=%s error=%s correlation_id=%s", spec.name, exc, cid)
            latency_ms = round((time.perf_counter() - b0) * 1000, 2)
            span.set_attribute(f"{ATTR_PREFIX}.branch.latency_ms", latency_ms)
            span.set_attribute(f"{ATTR_PREFIX}.branch.status", status)
            return BranchOutcome(
                spec.name, status, detail, latency_ms, started_ms, spec.budget_s * 1000
            )

    async def _speak(self, spoken: list[str], text: str) -> None:
        spoken.append(text)
        await self._gateway.speak(text)

    def _ack(self, envelope: IntentEnvelope) -> str:
        if self._ack_text:
            return self._ack_text
        return (
            f"Starting {envelope.intent.replace('_', ' ')} now — "
            "acknowledging and paging the team."
        )

    def _rolling_status(
        self, outcomes: dict[str, BranchOutcome], specs: list[BranchSpec]
    ) -> str:
        """Rolling spoken status from out-of-order completions (custom aggregator)."""
        parts: list[str] = []
        for spec in specs:
            o = outcomes.get(spec.name)
            if o is None:
                parts.append(f"awaiting {spec.name}…")
            elif o.status == "done":
                parts.append(f"{spec.done_phrase or spec.name} ✓")
            elif o.status == "pending":
                parts.append(f"{spec.name} still working…")
            else:
                parts.append(f"{spec.name} failed ✗")
        return "Status: " + ", ".join(parts)

    # -- default branches (sepsis/emergency fan-out over mock-backed tools) ---
    def _default_branches(self, envelope: IntentEnvelope) -> list[BranchSpec]:
        cid = envelope.correlation_id
        patient_ref = envelope.entities.get("patient_ref", "unspecified")
        intent = envelope.intent.replace("_", " ")

        async def page_rrt(_env: IntentEnvelope) -> str:
            r = await send_page(
                "Rapid Response Team", f"{intent} — please respond",
                priority="stat", correlation_id=cid, config=self._tools,
            )
            return f"RRT {r.delivery_state}" if not r.error else f"page error: {r.error}"

        async def order_stat_labs(_env: IntentEnvelope) -> str:
            r = await order_labs(
                patient_ref, ["lactate", "blood cultures"], priority="stat",
                correlation_id=cid, config=self._tools,
            )
            return f"labs {r.order_status}" if not r.error else f"labs error: {r.error}"

        async def get_protocol(_env: IntentEnvelope) -> str:
            r = await retrieve_sepsis_protocol(
                "sepsis hour-1 bundle", correlation_id=cid, config=self._tools
            )
            return "hour-1 protocol retrieved" if r.grounded else "protocol unavailable"

        async def pull_context(_env: IntentEnvelope) -> str:
            r = await get_patient_context(patient_ref, correlation_id=cid, config=self._tools)
            return "patient context ready" if r.resolved else "context unavailable"

        b = self._budgets
        return [
            BranchSpec("comms", page_rrt, b.comms_tool_ms / 1000, escalation=True,
                       label="paging RRT", done_phrase="RRT paged"),
            BranchSpec("labs", order_stat_labs, b.labs_tool_ms / 1000,
                       label="ordering lactate + cultures", done_phrase="labs ordered"),
            BranchSpec("knowledge", get_protocol, b.knowledge_ms / 1000,
                       label="retrieving hour-1 protocol", done_phrase="protocol retrieved"),
            BranchSpec("context", pull_context, b.patient_context_ms / 1000,
                       label="pulling patient context", done_phrase="context ready"),
        ]
