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

import asyncio
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

# Realistic latency profile for the mock adapters (NOT a demo dial). These model the
# real downstream round-trips the tools stand in for — a Vocera Engage page, an HL7 lab
# order, an EHR/patient-context read — so the fan-out parallelism and the ack-first
# behavior are demonstrated honestly rather than with an artificial slowdown knob.
_REALISTIC_BASE_MS = 400
_REALISTIC_JITTER_MS = 450


def realistic_tools() -> ToolsConfig:
    """Mock tools with a realistic, fixed latency profile (models real downstream systems)."""
    return ToolsConfig(
        use_real_adapter=False,
        mock_latency_ms=_REALISTIC_BASE_MS,
        mock_jitter_ms=_REALISTIC_JITTER_MS,
        timeout_ms=8000,
    )


# Human-readable workflow names the router dispatches to (for the flow visualizer + UI).
WORKFLOW_LABELS: dict[str, str] = {
    "sepsis": "Sepsis Hour-1 Emergency Bundle",
    "emergency": "Emergency Fast Path",
    "sire": "SIRE — resolve person + Engage page",
    "standard": "Standard workflow",
}

# Node status -> fill colour for the live flow graph (Graphviz).
_STATUS_FILL: dict[str, str] = {
    "queued": "#eceff1",
    "running": "#64b5f6",
    "done": "#66bb6a",
    "breach": "#ffb74d",
    "failed": "#ef5350",
    "idle": "#f5f5f5",
}

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


def standard_branch_meta(budgets: LatencyBudgets) -> list[BranchMeta]:
    """The two concurrent enrich branches of the standard/SIRE path (mirrors StandardEnrich)."""
    return [
        BranchMeta("patient_context", "patient context lookup", budgets.patient_context_ms, "context ready"),
        BranchMeta("oncall", "on-call schedule lookup", budgets.comms_tool_ms, "on-call ready"),
    ]


def workflow_key(intent: str, path: str) -> str:
    """Map (intent, path) to a workflow key for the flow visualizer."""
    if path == "fast":
        return "sepsis" if intent in SEPSIS_INTENTS else "emergency"
    if intent in {"contact_provider", "panic_button"} or intent.startswith("contact"):
        return "sire" if intent == "contact_provider" else "standard"
    return "standard"


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
            self.listening: bool = False
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

    def set_listening(self, value: bool) -> None:
        """Voice front door session state (mic open / closed)."""
        with self._lock:
            self.listening = value

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
                listening=self.listening,
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
    listening: bool
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
    tools = tools or realistic_tools()
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
    await _drive(state, gateway, envelope, tools, budgets, window_s)
    return state


async def _drive(
    state: CockpitState,
    gateway: VoiceGatewayBase,
    envelope: IntentEnvelope,
    tools: ToolsConfig,
    budgets: LatencyBudgets,
    window_s: float,
) -> None:
    """Dispatch a built envelope to the right workflow, streaming live state (shared by
    the text and voice front doors). Never lets the UI thread see a raw crash."""
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
    state.set_branches(standard_branch_meta(budgets))
    orch = Orchestrator(gateway, fast_path=FastPath(gateway, budgets=budgets, tools=tools))
    result = await orch.handle(envelope)
    # The standard graph doesn't surface per-branch latencies to the cockpit; the enrich
    # branches ran concurrently and completed, so finalize them as done for the flow view.
    state.finalize_branches({}, {})
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


# --- Live flow visualizer (Graphviz DOT) -------------------------------------
# Renders the routing map so the audience SEES which downstream workflow the voice
# front door chose for the utterance, with each node lit by live status. Client-side
# rendered by st.graphviz_chart (viz.js) — no system Graphviz needed.
_WORKFLOW_ROUTES: tuple[tuple[str, str], ...] = (
    ("sepsis", "EMERGENCY · sepsis"),
    ("emergency", "EMERGENCY · code / RRT / stroke / fall"),
    ("sire", "ROUTINE · page / contact a provider"),
    ("standard", "ROUTINE · locate / blood / general"),
)


def _fill(status: str) -> str:
    return _STATUS_FILL.get(status, _STATUS_FILL["idle"])


def _node(node_id: str, label: str, status: str, *, bold: bool = False) -> str:
    fill = _fill(status)
    pen = ' penwidth=2 color="#37474f"' if bold else ' color="#b0bec5"'
    font = "#ffffff" if status in ("running", "done", "breach", "failed") else "#546e7a"
    return f'{node_id} [label="{label}" fillcolor="{fill}" fontcolor="{font}"{pen}];'


def flow_dot(snapshot: CockpitSnapshot) -> str:
    """Build a Graphviz DOT graph of the live routing flow for ``snapshot``.

    Shows the voice front door → router → the four candidate workflows, with the chosen
    one and its concurrent branches lit by live status (queued → running → done/breach).
    Answers 'what flow is happening for this query' at a glance.
    """
    started = bool(snapshot.path) or bool(snapshot.transcript)
    active = workflow_key(snapshot.intent, snapshot.path) if snapshot.path else None
    running = snapshot.running

    badge_s = "done" if started else "idle"
    gw_s = "done" if snapshot.intent else "idle"
    router_s = "running" if (running and snapshot.path) else ("done" if snapshot.path else "idle")

    urgency = snapshot.urgency or "urgency"
    lines: list[str] = [
        "digraph nightingale {",
        "  rankdir=TB; bgcolor=\"transparent\"; pad=0.2; nodesep=0.35; ranksep=0.45;",
        '  node [shape=box style="rounded,filled" fontname="Segoe UI" fontsize=11];',
        '  edge [color="#90a4ae" fontname="Segoe UI" fontsize=9];',
        f'  {_node("badge", "🎧 Badge / mic", badge_s)}',
        f'  {_node("gateway", "Voice Gateway\\ngpt-realtime · Intent + urgency", gw_s)}',
        f'  {_node("router", f"Router\\n{urgency}", router_s)}',
        "  badge -> gateway; gateway -> router;",
    ]

    for key, cond in _WORKFLOW_ROUTES:
        is_active = key == active
        if is_active:
            wf_status = "running" if running else ("done" if snapshot.summary else "running")
        else:
            wf_status = "idle"
        lines.append(f'  {_node(f"wf_{key}", WORKFLOW_LABELS[key], wf_status, bold=is_active)}')
        edge_style = "" if is_active else ' style=dashed color="#cfd8dc"'
        lines.append(f'  router -> wf_{key} [label="{cond}"{edge_style}];')

    # Light up the active workflow's concurrent branches + the human-in-the-loop terminal.
    if active and snapshot.branches:
        for b in snapshot.branches:
            nid = f"br_{active}_{b.name}"
            tag = "⚡ " if b.escalation else ""
            lines.append(f'  {_node(nid, f"{tag}{b.name}\\n{b.label}", b.status)}')
            lines.append(f"  wf_{active} -> {nid};")
        term_s = "done" if (snapshot.summary and not running) else "idle"
        lines.append(f'  {_node(f"ack_{active}", "Read-back &\\nclinician confirm", term_s)}')
        for b in snapshot.branches:
            lines.append(f"  br_{active}_{b.name} -> ack_{active};")

    lines.append("}")
    return "\n".join(lines)


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


# --- Voice front door (Azure Voice Live, gpt-realtime) -----------------------
# The demo's thesis: the nurse SPEAKS; the front door classifies intent + urgency and
# routes to the right downstream workflow. This runs the existing VoiceLiveGateway and
# drives the same cockpit per emitted IntentEnvelope — the mic is the primary input; the
# text box is only a mic-less / CI fallback. Heavy deps (azure-ai-voicelive, pyaudio) are
# imported lazily inside the session, so importing this module stays CI-safe.


class _VoiceCockpitGateway(VoiceGatewayBase):
    """Bridges the live Voice Live gateway's spoken channel into a :class:`CockpitState`.

    Subclassing the real gateway would pull in audio deps at import; instead we compose:
    the Voice Live gateway (built lazily in the session) emits envelopes, and its
    ``speak`` is redirected here so orchestrator progress lands in the cockpit.
    """

    def __init__(self, state: CockpitState) -> None:
        super().__init__(None)
        self._state = state

    async def speak(self, text: str) -> None:
        self._state.speak(text)


async def run_voice_cockpit(
    state: CockpitState,
    *,
    tools: ToolsConfig | None = None,
    budgets: LatencyBudgets | None = None,
    window_s: float = _HOUR1_WINDOW_S,
    stop_event: threading.Event | None = None,
) -> None:
    """Open a Voice Live session and drive the cockpit for each spoken utterance.

    Consumes the gateway's :class:`IntentEnvelope` stream; each envelope resets the cockpit
    and runs the routed workflow via :func:`_drive`. Errors (no mic, no creds) surface on
    ``state.error`` rather than crashing the UI thread.
    """
    tools = tools or realistic_tools()
    budgets = budgets or LatencyBudgets.from_env()
    state.reset()
    state.set_listening(True)
    try:
        # Lazy: keep the module importable without Azure Voice Live / pyaudio installed.
        from azure.identity.aio import AzureCliCredential  # noqa: PLC0415
        from config import AppConfig  # noqa: PLC0415
        from src.gateway.gateway import VoiceLiveGateway  # noqa: PLC0415

        cfg = AppConfig.from_env()
        bridge = _VoiceCockpitGateway(state)

        async with AzureCliCredential() as credential:
            gateway = VoiceLiveGateway(cfg, credential)
            # Redirect the live gateway's spoken channel into the cockpit bridge.
            gateway.speak = bridge.speak  # type: ignore[method-assign]

            async def _consume() -> None:
                async for env in gateway.envelopes():
                    state.reset()
                    state.set_listening(True)
                    state.start(env.utterance or "(voice)", env)
                    await _drive(state, gateway, env, tools, budgets, window_s)

            consumer = asyncio.create_task(_consume())
            session = asyncio.create_task(gateway.run())
            while not session.done():
                if stop_event is not None and stop_event.is_set():
                    gateway.close()
                    break
                await asyncio.sleep(0.2)
            session.cancel()
            consumer.cancel()
            await asyncio.gather(session, consumer, return_exceptions=True)
    except Exception as exc:  # no mic / no creds / SDK missing — degrade gracefully
        logger.exception("voice front door failed")
        state.fail(f"Voice front door unavailable: {exc}")
    finally:
        state.set_listening(False)


def start_voice_session(
    state: CockpitState,
    *,
    tools: ToolsConfig | None = None,
    budgets: LatencyBudgets | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Spawn a daemon thread running the Voice Live front door; returns (thread, stop_event)."""
    stop_event = threading.Event()

    def _target() -> None:
        try:
            asyncio.run(
                run_voice_cockpit(state, tools=tools, budgets=budgets, stop_event=stop_event)
            )
        except Exception as exc:  # pragma: no cover - defensive
            state.fail(str(exc))

    thread = threading.Thread(target=_target, name="cockpit-voice", daemon=True)
    thread.start()
    return thread, stop_event
