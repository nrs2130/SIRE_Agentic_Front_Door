"""The **sepsis hour-1 showcase** — the flagship emergency demo (docs §3.2, Part D).

Wires the end-to-end flow for "patient in bed 12 looks septic" by **reusing** existing pieces:

* **screening** reads vitals from the ``monitor_alarm`` mock + mentation from the envelope and
  computes **SIRS** and **qSOFA** to flag suspicion of sepsis,
* on a positive screen it runs the hardened :class:`~src.orchestrator.fastpath.FastPath`
  (acknowledge-first, escalate-first, budgeted, speculative, OTel) with **four concurrent
  branches** — orders (``labs_hl7``), comms/escalation (``comms_page``), knowledge (Foundry IQ
  ``sepsis-protocols``), and timer/compliance,
* it **reads back the cited 5-element hour-1 checklist** (from the KB corpus),
* a :class:`ComplianceTimer` tracks the **hour-1 window** and prompts a **lactate re-measure**
  when the initial value is > 2 mmol/L, and
* everything is **human-in-the-loop**: treatments are prepared for confirmation, protocol text
  is decision support — no autonomous clinical action, no alarm override.

No duplicated logic: the L4 tools, the Foundry IQ retrieval, and the hour-1 corpus are reused;
this module only adds the screening scorer, the compliance timer, and the orchestration wiring.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import LatencyBudgets, ToolsConfig
from src.gateway.intent_envelope import IntentEnvelope
from src.knowledge.protocol_documents import HOUR1_ELEMENTS
from src.knowledge.sepsis_protocol import ProtocolCitation, retrieve_sepsis_protocol
from src.telemetry import ATTR_PREFIX, get_tracer
from src.tools.comms_page import send_page
from src.tools.labs_hl7 import order_labs
from src.tools.monitor_alarm import read_monitor

from .fastpath import BranchSpec, FastPath

if TYPE_CHECKING:
    from opentelemetry import trace

    from src.gateway.gateway import VoiceGatewayBase

logger = logging.getLogger("nightingale.orchestrator.sepsis")

_ACK = "Starting the sepsis hour-1 protocol and paging the response team."
_HOUR1_WINDOW_S = 3600  # the Surviving Sepsis Campaign hour-1 compliance window


# --- Screening (reads monitor vitals; computes SIRS + qSOFA) ------------------
@dataclass(frozen=True)
class SepsisScreen:
    """Result of the bedside sepsis screen — decision support for a clinician, not a decision."""

    suspicion: bool
    sirs_score: int
    qsofa_score: int
    sirs_positive: bool
    qsofa_positive: bool
    flags: list[str]
    vitals: dict[str, float]
    source: str  # where the vitals came from (monitor)


def _vital(value: str) -> float | None:
    """Parse a monitor vital value ('24', '38.6', '88/54' → systolic 88) to a float."""
    try:
        return float(value.split("/")[0].strip())
    except (ValueError, AttributeError):
        return None


def score_screen(
    *,
    rr: float | None,
    sbp: float | None,
    temp: float | None,
    hr: float | None,
    wbc: float | None = None,
    altered_mentation: bool = False,
) -> SepsisScreen:
    """Compute SIRS + qSOFA from vitals (docs Part D). ≥2 of either flags suspicion.

    Pure + transparent so the screen is explainable at the bedside. Missing vitals simply
    don't contribute — the score reflects what was measured.
    """
    sirs = 0
    qsofa = 0
    flags: list[str] = []
    if temp is not None and (temp > 38.3 or temp < 36):
        sirs += 1
        flags.append(f"temp {temp:g}")
    if hr is not None and hr > 90:
        sirs += 1
        flags.append(f"HR {hr:g}")
    if rr is not None and rr > 20:
        sirs += 1
        flags.append(f"RR {rr:g}>20")
    if wbc is not None and (wbc > 12 or wbc < 4):
        sirs += 1
        flags.append(f"WBC {wbc:g}")
    if rr is not None and rr >= 22:
        qsofa += 1
        flags.append(f"RR {rr:g}>=22")
    if sbp is not None and sbp <= 100:
        qsofa += 1
        flags.append(f"SBP {sbp:g}<=100")
    if altered_mentation:
        qsofa += 1
        flags.append("altered mentation")

    sirs_pos = sirs >= 2
    qsofa_pos = qsofa >= 2
    vitals = {
        k: v for k, v in
        {"rr": rr, "sbp": sbp, "temp": temp, "hr": hr, "wbc": wbc}.items() if v is not None
    }
    return SepsisScreen(
        suspicion=sirs_pos or qsofa_pos, sirs_score=sirs, qsofa_score=qsofa,
        sirs_positive=sirs_pos, qsofa_positive=qsofa_pos,
        flags=sorted(set(flags)), vitals=vitals, source="monitor",
    )


# --- Compliance timer (hour-1 window + lactate re-measure prompt) -------------
@dataclass
class ComplianceTimer:
    """Tracks the hour-1 window and the lactate re-measure prompt (docs Part D step f)."""

    window_s: float = _HOUR1_WINDOW_S
    started_at: float | None = None
    lactate_remeasure_due: bool = False
    initial_lactate: float | None = None

    def start(self) -> None:
        self.started_at = time.monotonic()

    @property
    def active(self) -> bool:
        return self.started_at is not None and self.elapsed_s < self.window_s

    @property
    def elapsed_s(self) -> float:
        return (time.monotonic() - self.started_at) if self.started_at is not None else 0.0

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.window_s - self.elapsed_s)

    def note_lactate(self, value: float | None) -> bool:
        """Record the initial lactate; flag a re-measure if it is > 2 mmol/L. Returns the flag."""
        self.initial_lactate = value
        if value is not None and value > 2.0:
            self.lactate_remeasure_due = True
        return self.lactate_remeasure_due


# --- Workflow result ----------------------------------------------------------
@dataclass
class SepsisHour1Result:
    """Outcome of the sepsis hour-1 showcase (+ measured latencies for the cockpit)."""

    correlation_id: str
    suspicion: bool
    screen: SepsisScreen
    spoken: list[str] = field(default_factory=list)
    screen_ms: float = 0.0
    ack_latency_ms: float | None = None
    branch_latencies_ms: dict[str, float] = field(default_factory=dict)
    checklist: list[str] = field(default_factory=list)
    citations: list[ProtocolCitation] = field(default_factory=list)
    initial_lactate: float | None = None
    lactate_remeasure_prompted: bool = False
    timer_active: bool = False
    autonomous_clinical_action: bool = False  # invariant: always False
    summary: str = ""


class SepsisHour1Workflow:
    """Screen → (positive) → fast-path 4-branch hour-1 bundle → cited read-back → compliance."""

    def __init__(
        self,
        gateway: "VoiceGatewayBase",
        *,
        tools: ToolsConfig | None = None,
        budgets: LatencyBudgets | None = None,
        tracer: "trace.Tracer | None" = None,
        window_s: float = _HOUR1_WINDOW_S,
    ) -> None:
        self._gateway = gateway
        self._tools = tools or ToolsConfig.from_env()
        self._budgets = budgets or LatencyBudgets.from_env()
        self._tracer = tracer or get_tracer("nightingale.orchestrator.sepsis")
        self._window_s = window_s

    async def run(self, envelope: IntentEnvelope) -> SepsisHour1Result:
        """Run the full sepsis hour-1 showcase for ``envelope``."""
        cid = envelope.correlation_id
        spoken: list[str] = []
        with self._tracer.start_as_current_span("sepsis_hour1") as span:
            span.set_attribute(f"{ATTR_PREFIX}.correlation_id", cid)
            span.set_attribute(f"{ATTR_PREFIX}.intent", envelope.intent)

            # (1) SCREEN — read vitals from the monitor, compute SIRS + qSOFA.
            t0 = time.perf_counter()
            screen = await self._screen(envelope, spoken)
            screen_ms = round((time.perf_counter() - t0) * 1000, 2)
            span.set_attribute(f"{ATTR_PREFIX}.sepsis.suspicion", screen.suspicion)
            span.set_attribute(f"{ATTR_PREFIX}.sepsis.sirs", screen.sirs_score)
            span.set_attribute(f"{ATTR_PREFIX}.sepsis.qsofa", screen.qsofa_score)

            if not screen.suspicion:
                # Negative screen: document and stop — no bundle, no autonomous action.
                await self._speak(spoken,
                    "Objective screen is low-risk (SIRS and qSOFA negative). Documenting; "
                    "no hour-1 bundle launched. Please reassess and confirm.")
                summary = "Sepsis screen negative — documented, no bundle launched."
                await self._speak(spoken, summary)
                return SepsisHour1Result(
                    correlation_id=cid, suspicion=False, screen=screen, spoken=spoken,
                    screen_ms=screen_ms, summary=summary,
                )

            # (2) POSITIVE → fast path. Four concurrent branches; comms escalates first.
            holder: dict[str, object] = {}
            timer = ComplianceTimer(window_s=self._window_s)
            fast = FastPath(
                self._gateway, budgets=self._budgets, tools=self._tools,
                tracer=self._tracer, ack_text=_ACK,
                branches=lambda env: self._branches(env, holder, timer),
            )
            fp = await fast.run(envelope)
            spoken.extend(fp.spoken)

            # (3) READ BACK the cited 5-element hour-1 checklist.
            checklist, citations = await self._read_back_checklist(holder, spoken)

            # (4) COMPLIANCE — lactate re-measure prompt if initial > 2 mmol/L.
            lactate = self._lactate_value(holder)
            remeasure = timer.note_lactate(lactate)
            if remeasure:
                await self._speak(spoken,
                    f"Initial lactate is {lactate:g} mmol/L, above 2 — I'll prompt a re-measure "
                    "within the hour-1 window.")

            # (5) HUMAN-IN-THE-LOOP read-back of the compliance state.
            summary = (
                "Sepsis hour-1 bundle prepared — RRT paged, lactate and cultures ordered, "
                "protocol cited, hour-1 clock running. Antibiotics, fluids, and vasopressors "
                "are ready for you to confirm and order; I won't start them autonomously."
            )
            await self._speak(spoken, summary)
            span.set_attribute(f"{ATTR_PREFIX}.sepsis.lactate_remeasure", remeasure)
            span.set_attribute(f"{ATTR_PREFIX}.sepsis.timer_active", timer.active)
            logger.info(
                "sepsis hour-1 done correlation_id=%s suspicion=%s ack_ms=%s remeasure=%s",
                cid, screen.suspicion, fp.ack_latency_ms, remeasure,
            )
            return SepsisHour1Result(
                correlation_id=cid, suspicion=True, screen=screen, spoken=spoken,
                screen_ms=screen_ms, ack_latency_ms=fp.ack_latency_ms,
                branch_latencies_ms=fp.branch_latencies_ms, checklist=checklist,
                citations=citations, initial_lactate=lactate,
                lactate_remeasure_prompted=remeasure, timer_active=timer.active,
                autonomous_clinical_action=False, summary=summary,
            )

    # -- screening -----------------------------------------------------------
    async def _screen(self, envelope: IntentEnvelope, spoken: list[str]) -> SepsisScreen:
        cid = envelope.correlation_id
        patient_ref = envelope.entities.get("patient_ref", "")
        await self._speak(spoken, f"Reading {patient_ref or 'the patient'}'s vitals…")
        monitor = await read_monitor(patient_ref, correlation_id=cid, config=self._tools)

        vitals = {v.name.upper(): v.value for v in monitor.vitals} if monitor.resolved else {}
        rr = _vital(vitals.get("RR", ""))
        hr = _vital(vitals.get("HR", ""))
        temp = _vital(vitals.get("TEMP", ""))
        sbp = _vital(vitals.get("NIBP", vitals.get("ABP", vitals.get("BP", ""))))
        altered = str(
            envelope.entities.get("mental_status", envelope.entities.get("ams", ""))
        ).strip().lower() in {"altered", "confused", "ams", "true", "yes"}

        screen = score_screen(rr=rr, sbp=sbp, temp=temp, hr=hr, altered_mentation=altered)
        if screen.suspicion:
            await self._speak(spoken,
                f"Suspicion of sepsis — SIRS {screen.sirs_score}, qSOFA {screen.qsofa_score} "
                f"({', '.join(screen.flags)}).")
        return screen

    # -- fast-path branches (reuse the L4 tools + Foundry IQ) -----------------
    def _branches(
        self, envelope: IntentEnvelope, holder: dict, timer: ComplianceTimer
    ) -> list[BranchSpec]:
        cid = envelope.correlation_id
        patient_ref = envelope.entities.get("patient_ref", "unspecified")
        b = self._budgets

        async def orders(_env: IntentEnvelope) -> str:
            r = await order_labs(
                patient_ref, ["lactate", "blood cultures"], priority="stat",
                correlation_id=cid, config=self._tools,
            )
            holder["labs"] = r
            return f"labs {r.order_status}" if not r.error else f"labs error: {r.error}"

        async def comms(_env: IntentEnvelope) -> str:
            r = await send_page(
                "Rapid Response Team", f"Sepsis hour-1 — {patient_ref}, please respond",
                priority="stat", correlation_id=cid, config=self._tools,
            )
            holder["page"] = r
            return f"RRT {r.delivery_state}" if not r.error else f"page error: {r.error}"

        async def knowledge(_env: IntentEnvelope) -> str:
            r = await retrieve_sepsis_protocol(
                "sepsis hour-1 bundle", correlation_id=cid, config=self._tools
            )
            holder["protocol"] = r
            return "hour-1 protocol retrieved (cited)" if r.grounded else "protocol cached"

        async def compliance(_env: IntentEnvelope) -> str:
            timer.start()
            holder["timer"] = timer
            return "hour-1 window started"

        return [
            BranchSpec("comms", comms, b.comms_tool_ms / 1000, escalation=True,
                       label="paging RRT", done_phrase="RRT paged"),
            BranchSpec("orders", orders, b.labs_tool_ms / 1000,
                       label="preparing lactate + cultures", done_phrase="labs prepared"),
            BranchSpec("knowledge", knowledge, b.knowledge_ms / 1000,
                       label="retrieving hour-1 protocol", done_phrase="protocol cited"),
            BranchSpec("timer", compliance, b.router_ms / 1000,
                       label="starting hour-1 clock", done_phrase="clock started"),
        ]

    # -- checklist read-back (cited) -----------------------------------------
    async def _read_back_checklist(
        self, holder: dict, spoken: list[str]
    ) -> tuple[list[str], list[ProtocolCitation]]:
        from src.knowledge.sepsis_protocol import ProtocolResult

        protocol = holder.get("protocol")
        citations = list(protocol.citations) if isinstance(protocol, ProtocolResult) else []
        # The 5 elements come from the KB corpus (HOUR1_ELEMENTS); treatments need confirmation.
        checklist: list[str] = []
        for order, category, text in HOUR1_ELEMENTS:
            tag = "prepared" if category == "diagnostic" else "for your confirmation"
            checklist.append(f"{order}. {text} [{tag}]")
        src = ", ".join(c.source_id for c in citations) if citations else "the sepsis protocol"
        await self._speak(spoken, "Hour-1 checklist — " + " ".join(checklist) + f" Sources: {src}.")
        return checklist, citations

    def _lactate_value(self, holder: dict) -> float | None:
        """Pull the initial lactate value from the labs order result, if present."""
        from src.tools.labs_hl7 import LabOrderResult

        labs = holder.get("labs")
        if not isinstance(labs, LabOrderResult):
            return None
        for res in labs.results:
            if res.test_code.upper() == "LACTATE":
                try:
                    return float(res.value)
                except (ValueError, TypeError):
                    return None
        return None

    async def _speak(self, spoken: list[str], text: str) -> None:
        spoken.append(text)
        await self._gateway.speak(text)
