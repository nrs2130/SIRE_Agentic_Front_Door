"""Demo cockpit backend — drives the orchestrator and exposes LIVE run state.

L6 presentation layer (docs/01-architecture.md §9). This module is **Streamlit-independent**
so it is unit-testable and contains the demo''s only orchestration-client logic (the Streamlit
app stays a thin renderer). It:

* builds an :class:`IntentEnvelope` from a typed utterance (via the gateway''s cheap
  :func:`classify`) or a **PANIC** override (forces ``urgency=EMERGENCY``),
* runs the existing orchestrator — the **sepsis hour-1 workflow** for a sepsis emergency, the
  hardened **fast path** for any other emergency, or the **standard graph** for routine work —
  and never duplicates their logic, and
* subscribes to the orchestrator''s **spoken event stream** (``gateway.speak``) and folds each
  line into a thread-safe :class:`CockpitState`: transcript turns, per-branch lifecycle
  (queued -> running -> done/breach) with elapsed-vs-budget timers, the hour-1 checklist, and
  the compliance clock. The spoken stream IS the event stream (per the workflow''s intermediate
  outputs); we parse the branch cues the fast path already emits (``[name] label…`` on start,
  ``Still working on name…`` on a budget breach, and each branch''s done-phrase in the rolling
  status) rather than inventing a second channel.

Threading: :func:`start_run` spawns a daemon thread that runs the async driver; the UI polls
:meth:`CockpitState.snapshot` and re-renders. All state mutations are lock-guarded.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime

from config import LatencyBudgets, ToolsConfig
from src.agents.sepsis import SEPSIS_INTENTS
from src.gateway.gateway import VoiceGatewayBase
from src.gateway.intent_envelope import IntentEnvelope, Urgency
from src.gateway.urgency import classify
from src.knowledge.protocol_documents import HOUR1_ELEMENTS
from src.orchestrator.fastpath import FastPath
from src.orchestrator.sepsis_workflow import SepsisHour1Workflow
from src.orchestrator.workflow import Orchestrator

logger = logging.getLogger("nightingale.demo.cockpit")

# Foundry portal — the Control Plane entry point (see infra/CONTROL_PLANE.md).
_FOUNDRY_PORTAL = "https://ai.azure.com"
_HOUR1_WINDOW_S = 3600.0

_BRANCH_START_RE = re.compile(r"^\[(\w+)\]")
_BRANCH_BREACH_RE = re.compile(r"[Ss]till working on (\w+)")


@dataclass(frozen=True)
class BranchMeta:
    """Static metadata for one fan-out branch (name, spoken cues, latency budget)."""

    name: str
    label: str
    budget_ms: float
    done_phrase: str
    escalation: bool = False


def sepsis_branch_meta(budgets: LatencyBudgets) -> list[BranchMeta]:
    """The four concurrent sepsis hour-1 branches (mirrors SepsisHour1Workflow._branches)."""
    return [
        BranchMeta("comms", "paging RRT", budgets.comms_tool_ms, "RRT paged", escalation=True),
        BranchMeta("orders", "preparing lactate + cultures", budgets.labs_tool_ms, "labs prepared"),
        BranchMeta("knowledge", "retrieving hour-1 protocol", budgets.knowledge_ms, "protocol cited"),
        BranchMeta("timer", "starting hour-1 clock", budgets.router_ms, "clock started"),
    ]


def emergency_branch_meta(budgets: LatencyBudgets) -> list[BranchMeta]:
    """The four default fast-path branches (mirrors FastPath._default_branches)."""
    return [
        BranchMeta("comms", "paging RRT", budgets.comms_tool_ms, "RRT paged", escalation=True),
        BranchMeta("labs", "ordering lactate + cultures", budgets.labs_tool_ms, "labs ordered"),
        BranchMeta("knowledge", "retrieving hour-1 protocol", budgets.knowledge_ms, "protocol retrieved"),
        BranchMeta("context", "pulling patient context", budgets.patient_context_ms, "context ready"),
    ]


@dataclass
class BranchView:
    """Live view of one branch: status + elapsed-vs-budget timer for the cockpit."""

    name: str
    label: str
    budget_ms: float
    done_phrase: str
    escalation: bool = False
    status: str = "queued"  # queued | running | done | breach | failed
    start_mono: float | None = None
    end_mono: float | None = None
    latency_ms: float | None = None
    detail: str = ""

    def elapsed_ms(self) -> float:
        """Elapsed time (ms): live while running, frozen once done."""
        if self.start_mono is None:
            return 0.0
        end = self.end_mono if self.end_mono is not None else time.monotonic()
        return (end - self.start_mono) * 1000.0

    def over_budget(self) -> bool:
        """True on a breach or once the live elapsed exceeds the soft budget (amber)."""
        return self.status == "breach" or self.elapsed_ms() > self.budget_ms


@dataclass
class TranscriptTurn:
    """One transcript line — nurse (user) or agent (spoken)."""

    ts: str
    role: str  # "nurse" | "agent"
    text: str
    is_ack: bool = False


@dataclass
class ChecklistItem:
    """One hour-1 element with its live action state (decision support, human-in-the-loop)."""

    order: int
    category: str  # diagnostic | treatment
    text: str
    status: str  # pending | ordered | proposed


@dataclass
class SepsisView:
    """Live sepsis panel: screen, hour-1 checklist, citations, compliance clock."""

    active: bool = False
    suspicion: bool | None = None
    sirs: int = 0
    qsofa: int = 0
    flags: list[str] = field(default_factory=list)
    citations: list[tuple[str, str, str]] = field(default_factory=list)  # (id, title, url)
    initial_lactate: float | None = None
    remeasure: bool = False
    protocol_cited: bool = False
    orders_placed: bool = False
    timer_started_mono: float | None = None
    window_s: float = _HOUR1_WINDOW_S

    def elapsed_s(self) -> float:
        if self.timer_started_mono is None:
            return 0.0
        return time.monotonic() - self.timer_started_mono

    def remaining_s(self) -> float:
        return max(0.0, self.window_s - self.elapsed_s())


class CockpitState:
    """Thread-safe shared state between the driver thread and the Streamlit UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    # -- lifecycle -----------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self.running: bool = False
            self.error: str | None = None
            self.utterance: str = ""
            self.intent: str = ""
            self.urgency: str = ""
            self.path: str = ""  # "fast" | "standard"
            self.correlation_id: str = ""
            self.transcript: list[TranscriptTurn] = []
            self.branches: dict[str, BranchView] = {}
            self.branch_order: list[str] = []
            self.call_log: list[tuple[str, str, str, float | None]] = []
            self.sepsis: SepsisView = SepsisView()
            self.summary: str = ""
            self.ack_latency_ms: float | None = None
            self._breaches: set[str] = set()
            self._ack_seen: bool = False
            self.started_mono: float | None = None

    # -- writes (driver thread) ---------------------------------------------
    def start(self, utterance: str, envelope: IntentEnvelope) -> None:
        with self._lock:
            self.running = True
            self.started_mono = time.monotonic()
            self.utterance = utterance
            self.intent = envelope.intent
            self.urgency = envelope.urgency.value
            self.correlation_id = envelope.correlation_id
            self.transcript.append(
                TranscriptTurn(_now(), "nurse", utterance or "[PANIC BUTTON]")
            )

    def set_path(self, path: str) -> None:
        with self._lock:
            self.path = path

    def set_branches(self, metas: list[BranchMeta]) -> None:
        with self._lock:
            self.branches = {
                m.name: BranchView(m.name, m.label, m.budget_ms, m.done_phrase, m.escalation)
                for m in metas
            }
            self.branch_order = [m.name for m in metas]

    def start_sepsis_panel(self) -> None:
        with self._lock:
            self.sepsis = SepsisView(active=True)

    def finish(self, summary: str, ack_latency_ms: float | None) -> None:
        with self._lock:
            self.summary = summary
            self.ack_latency_ms = ack_latency_ms
            self.running = False

    def fail(self, error: str) -> None:
        with self._lock:
            self.error = error
            self.running = False

    def speak(self, text: str) -> None:
        """Fold one spoken line into transcript + branch/checklist state (the event stream)."""
        with self._lock:
            is_ack = False
            if (
                not self._ack_seen
                and self.urgency == Urgency.EMERGENCY.value
                and text.startswith("Starting ")
            ):
                is_ack = True
                self._ack_seen = True
            self.transcript.append(TranscriptTurn(_now(), "agent", text, is_ack))

            start = _BRANCH_START_RE.match(text)
            if start:
                self._mark_running(start.group(1))
            breach = _BRANCH_BREACH_RE.search(text)
            if breach:
                self._mark_breach(breach.group(1))
            # Done-phrase detection off the rolling status line (live checklist ticking).
            low = text.lower()
            for b in self.branches.values():
                if b.status in ("running", "queued") and b.done_phrase.lower() in low:
                    self._mark_done(b.name)

    def finalize_branches(self, latencies: dict[str, float], details: dict[str, str]) -> None:
        """Authoritative per-branch latency + status from the run result (breach wins)."""
        with self._lock:
            for name in self.branch_order:
                b = self.branches[name]
                if name in latencies:
                    b.latency_ms = round(latencies[name], 2)
                b.detail = details.get(name, b.detail)
                b.status = "breach" if name in self._breaches else "done"
                if b.start_mono is not None and b.end_mono is None:
                    b.end_mono = time.monotonic()
                self._log_call(name, b.detail or b.status, b.latency_ms)

    def finalize_sepsis(
        self,
        *,
        suspicion: bool,
        sirs: int,
        qsofa: int,
        flags: list[str],
        citations: list[tuple[str, str, str]],
        initial_lactate: float | None,
        remeasure: bool,
        window_s: float,
    ) -> None:
        with self._lock:
            s = self.sepsis
            s.active = True
            s.suspicion = suspicion
            s.sirs, s.qsofa, s.flags = sirs, qsofa, list(flags)
            s.citations = list(citations)
            s.initial_lactate = initial_lactate
            s.remeasure = remeasure
            s.window_s = window_s
            s.protocol_cited = self.branches.get("knowledge", _MISSING).status == "done"
            s.orders_placed = self.branches.get("orders", _MISSING).status == "done"

    # -- internal (lock already held) ---------------------------------------
    def _mark_running(self, name: str) -> None:
        b = self.branches.get(name)
        if b and b.status == "queued":
            b.status = "running"
            b.start_mono = time.monotonic()

    def _mark_breach(self, name: str) -> None:
        b = self.branches.get(name)
        if b:
            self._breaches.add(name)
            if b.status != "done":
                b.status = "breach"

    def _mark_done(self, name: str) -> None:
        b = self.branches.get(name)
        if not b:
            return
        if b.start_mono is None:
            b.start_mono = time.monotonic()
        b.end_mono = time.monotonic()
        b.status = "breach" if name in self._breaches else "done"
        if name == "timer" and self.sepsis.active and self.sepsis.timer_started_mono is None:
            self.sepsis.timer_started_mono = time.monotonic()

    def _log_call(self, name: str, detail: str, latency_ms: float | None) -> None:
        self.call_log.append((_now(), name, detail, latency_ms))

    # -- read (UI thread) ----------------------------------------------------
    def snapshot(self) -> "CockpitSnapshot":
        with self._lock:
            return CockpitSnapshot(
                running=self.running,
                error=self.error,
                utterance=self.utterance,
                intent=self.intent,
                urgency=self.urgency,
                path=self.path,
                correlation_id=self.correlation_id,
                transcript=list(self.transcript),
                branches=[replace(self.branches[n]) for n in self.branch_order],
                call_log=list(self.call_log),
                sepsis=replace(self.sepsis),
                summary=self.summary,
                ack_latency_ms=self.ack_latency_ms,
            )


@dataclass
class CockpitSnapshot:
    """Immutable point-in-time copy of :class:`CockpitState` for rendering."""

    running: bool
    error: str | None
    utterance: str
    intent: str
    urgency: str
    path: str
    correlation_id: str
    transcript: list[TranscriptTurn]
    branches: list[BranchView]
    call_log: list[tuple[str, str, str, float | None]]
    sepsis: SepsisView
    summary: str
    ack_latency_ms: float | None


_MISSING = BranchView("", "", 0.0, "")


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class _CockpitGateway(VoiceGatewayBase):
    """Gateway whose only job is to forward spoken lines into a :class:`CockpitState`."""

    def __init__(self, state: CockpitState) -> None:
        super().__init__(None)
        self._state = state

    async def speak(self, text: str) -> None:
        self._state.speak(text)


def control_plane_url(project_endpoint: str | None = None) -> str:
    """Foundry portal (Control Plane) link — filter Tracing by the run''s correlation_id."""
    return _FOUNDRY_PORTAL


async def run_cockpit(
    state: CockpitState,
    utterance: str,
    *,
    panic: bool = False,
    tools: ToolsConfig | None = None,
    budgets: LatencyBudgets | None = None,
    window_s: float = _HOUR1_WINDOW_S,
) -> CockpitState:
    """Run one orchestration for ``utterance`` and stream live state into ``state``.

    ``panic`` forces ``urgency=EMERGENCY`` (keeping the typed intent when present). Routes a
    sepsis emergency to :class:`SepsisHour1Workflow`, any other emergency to the hardened
    :class:`FastPath`, and routine work to the standard graph — reusing each as-is.
    """
    tools = tools or ToolsConfig.from_env()
    budgets = budgets or LatencyBudgets.from_env()
    state.reset()
    gateway = _CockpitGateway(state)

    intent, urgency, entities = classify(utterance) if utterance else ("panic_button", Urgency.ROUTINE, {})
    if panic:
        urgency = Urgency.EMERGENCY
        if not utterance:
            intent = "panic_button"
    envelope = IntentEnvelope.create(intent, urgency, entities, utterance or "[PANIC BUTTON]")
    state.start(utterance, envelope)

    try:
        is_emergency = envelope.urgency is Urgency.EMERGENCY
        is_sepsis = envelope.intent in SEPSIS_INTENTS
        if is_emergency and is_sepsis:
            await _run_sepsis(state, gateway, envelope, tools, budgets, window_s)
        elif is_emergency:
            await _run_emergency(state, gateway, envelope, tools, budgets)
        else:
            await _run_standard(state, gateway, envelope, tools, budgets)
    except Exception as exc:  # never let the UI thread see a raw crash
        logger.exception("cockpit run failed correlation_id=%s", envelope.correlation_id)
        state.fail(str(exc))
    return state


async def _run_sepsis(
    state: CockpitState,
    gateway: VoiceGatewayBase,
    envelope: IntentEnvelope,
    tools: ToolsConfig,
    budgets: LatencyBudgets,
    window_s: float,
) -> None:
    state.set_path("fast")
    state.set_branches(sepsis_branch_meta(budgets))
    state.start_sepsis_panel()
    wf = SepsisHour1Workflow(gateway, tools=tools, budgets=budgets, window_s=window_s)
    result = await wf.run(envelope)
    state.finalize_branches(
        result.branch_latencies_ms,
        {n: "" for n in result.branch_latencies_ms},
    )
    state.finalize_sepsis(
        suspicion=result.suspicion,
        sirs=result.screen.sirs_score,
        qsofa=result.screen.qsofa_score,
        flags=result.screen.flags,
        citations=[(c.source_id, c.title, c.url) for c in result.citations],
        initial_lactate=result.initial_lactate,
        remeasure=result.lactate_remeasure_prompted,
        window_s=window_s,
    )
    state.finish(result.summary, result.ack_latency_ms)


async def _run_emergency(
    state: CockpitState,
    gateway: VoiceGatewayBase,
    envelope: IntentEnvelope,
    tools: ToolsConfig,
    budgets: LatencyBudgets,
) -> None:
    state.set_path("fast")
    state.set_branches(emergency_branch_meta(budgets))
    orch = Orchestrator(gateway, fast_path=FastPath(gateway, budgets=budgets, tools=tools))
    result = await orch.handle(envelope)
    state.finalize_branches(result.branch_latencies_ms, {})
    state.finish(result.summary or "", result.ack_latency_ms)


async def _run_standard(
    state: CockpitState,
    gateway: VoiceGatewayBase,
    envelope: IntentEnvelope,
    tools: ToolsConfig,
    budgets: LatencyBudgets,
) -> None:
    state.set_path("standard")
    orch = Orchestrator(gateway, fast_path=FastPath(gateway, budgets=budgets, tools=tools))
    result = await orch.handle(envelope)
    state.finish(result.summary or "", None)


def build_checklist(snapshot: CockpitSnapshot) -> list[ChecklistItem]:
    """Derive the hour-1 checklist state from the live branch statuses (ticks as work lands).

    Diagnostics (measure lactate, obtain cultures) flip to ``ordered`` when the ``orders``
    branch completes; treatments stay ``proposed`` — a clinician confirms and acts (never
    autonomous). Independent of Streamlit so it is unit-testable.
    """
    orders_done = any(b.name == "orders" and b.status == "done" for b in snapshot.branches)
    items: list[ChecklistItem] = []
    for order, category, text in HOUR1_ELEMENTS:
        if category == "diagnostic":
            status = "ordered" if orders_done else "pending"
        else:
            status = "proposed"
        items.append(ChecklistItem(order, category, text, status))
    return items


def start_run(
    state: CockpitState,
    utterance: str,
    *,
    panic: bool = False,
    tools: ToolsConfig | None = None,
    budgets: LatencyBudgets | None = None,
) -> threading.Thread:
    """Spawn a daemon thread that runs :func:`run_cockpit` (for the Streamlit app)."""
    import asyncio  # noqa: PLC0415 - only needed off the UI thread

    def _target() -> None:
        try:
            asyncio.run(run_cockpit(state, utterance, panic=panic, tools=tools, budgets=budgets))
        except Exception as exc:  # pragma: no cover - defensive
            state.fail(str(exc))

    thread = threading.Thread(target=_target, name="cockpit-run", daemon=True)
    thread.start()
    return thread
