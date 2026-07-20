"""L3 agent: **sepsis** — run the screen + prepare the hour-1 bundle (clinical, grounded).

Single responsibility (docs/01-architecture.md §3.2, §6): when a nurse invokes a sepsis
screen / hour-1 bundle, this agent **grounds the plan in Foundry IQ** and orchestrates the
hour-1 elements as a *cited, human-in-the-loop* checklist. It:

* retrieves the hour-1 protocol from the **Foundry IQ** knowledge base (mock-backed locally)
  and **cites its sources** (``knowledge_base_retrieve``),
* reads the patient's context/allergies (``patient_context``),
* pages the Rapid Response Team (``comms_page``) — augmenting Engage's routing, and
* orders the **diagnostic** labs the screen requires (``labs_hl7``: lactate, blood cultures).

CLINICAL SAFETY (copilot-instructions.md, prompt step 4): the agent **prepares / reads back /
confirms**. It **never** issues autonomous **treatment** orders (antibiotics, fluids,
vasopressors) — those steps are returned as *proposed*, each cited, requiring a clinician to
acknowledge and act. It never suppresses, reprioritizes, or overrides an alarm (that remains
Engage/EMDAN's FDA-cleared path). Its screen is decision support for clinician review, not an
autonomous decision. ``autonomous_treatment`` is always ``False``.

Runs **locally against mock MCP tools + a mock Foundry IQ provider** with no Azure. The Foundry
hosted-agent registration (with the Foundry IQ KB attached) is a separate step
(:func:`sepsis_hosted_agent_spec` + src/agents/register.py).

SDKs (hosted path only): agent-framework==1.11.0, azure-ai-projects==2.3.0.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from config import ToolsConfig
from src.gateway.intent_envelope import IntentEnvelope, Urgency
from src.knowledge.sepsis_protocol import (
    KNOWLEDGE_TOOL,
    ProtocolCitation,
    ProtocolResult,
    SepsisKnowledgeProvider,
    fallback_sepsis_protocol,
    retrieve_sepsis_protocol,
)
from src.tools.comms_page import send_page
from src.tools.labs_hl7 import order_labs
from src.tools.patient_context import get_patient_context

from .hosted import HostedAgentSpec, HostedMCPTool
from .registry import register_agent

logger = logging.getLogger("nightingale.agents.sepsis")

CAPABILITY = "sepsis"

# Intents this agent handles — screening + the hour-1 bundle for suspected sepsis.
SEPSIS_INTENTS = frozenset({"sepsis_screen", "sepsis_bundle", "start_sepsis_protocol"})

# Diagnostic labs the hour-1 screen requires (objective measurements, human-initiated).
_SCREEN_LABS = ["lactate", "blood cultures"]
_RRT_RECIPIENT = "Rapid Response Team"


@dataclass(frozen=True)
class Hour1Step:
    """One hour-1 element, tied to its cited source and its action state.

    ``status``: ``ordered`` (diagnostic placed) | ``proposed`` (treatment, awaiting a
    clinician) | ``failed``. ``requires_human_ack`` is True for anything a clinician must
    still authorize (all treatment steps).
    """

    order: int
    category: str  # diagnostic | treatment
    text: str
    status: str
    source_id: str
    requires_human_ack: bool


@dataclass(frozen=True)
class SepsisAgentResult:
    """Typed outcome of a sepsis screen/bundle + a spoken update for streaming."""

    correlation_id: str
    intent: str
    spoken_update: str
    grounded: bool  # was the plan grounded live in Foundry IQ (vs cached fallback)?
    screen: str  # positive | negative | unknown (decision support, not autonomous)
    steps: list[Hour1Step]
    citations: list[ProtocolCitation]
    page_id: str | None = None
    lab_order_id: str | None = None
    requires_human_ack: bool = True  # a clinician must confirm/act on the bundle
    autonomous_treatment: bool = False  # invariant: never auto-orders treatment/overrides alarms
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def _priority_for(urgency: Urgency) -> str:
    return "stat" if urgency is Urgency.EMERGENCY else "urgent"


def _num(entities: dict[str, str], key: str) -> float | None:
    try:
        return float(entities[key])
    except (KeyError, TypeError, ValueError):
        return None


def _compute_screen(envelope: IntentEnvelope) -> tuple[str, list[str]]:
    """A transparent qSOFA-style screen (decision support for a clinician to confirm).

    Returns (``positive`` | ``negative`` | ``unknown``, human-readable flags). Uses only
    values the nurse supplied; with no vitals it is ``unknown`` — the agent still prepares
    the bundle because a human explicitly invoked the protocol.
    """
    e = envelope.entities
    rr = _num(e, "rr")
    sbp = _num(e, "sbp")
    ams_raw = str(e.get("mental_status", e.get("ams", ""))).strip().lower()
    have_ams = bool(ams_raw)
    ams = ams_raw in {"altered", "confused", "ams", "true", "yes", "1"}

    if rr is None and sbp is None and not have_ams:
        return "unknown", []

    points = 0
    flags: list[str] = []
    if rr is not None and rr >= 22:
        points += 1
        flags.append(f"RR {rr:g}≥22")
    if sbp is not None and sbp <= 100:
        points += 1
        flags.append(f"SBP {sbp:g}≤100")
    if ams:
        points += 1
        flags.append("altered mentation")
    return ("positive" if points >= 2 else "negative"), flags


def _build_steps(protocol: ProtocolResult, lab_ok: bool) -> list[Hour1Step]:
    """Map cited protocol steps to action states — diagnostics ordered, treatment proposed."""
    steps: list[Hour1Step] = []
    for ps in protocol.steps:
        if ps.category == "diagnostic":
            status = "ordered" if lab_ok else "failed"
            requires_ack = False  # objective measurement, part of the invoked screen
        else:
            status = "proposed"  # treatment is never auto-ordered — a clinician confirms
            requires_ack = True
        steps.append(
            Hour1Step(ps.order, ps.category, ps.text, status, ps.source_id, requires_ack)
        )
    return steps


class SepsisAgent:
    """Grounds + prepares the sepsis hour-1 bundle; no autonomous clinical action."""

    capability = CAPABILITY
    intents = SEPSIS_INTENTS

    def __init__(
        self,
        config: ToolsConfig | None = None,
        *,
        knowledge_provider: SepsisKnowledgeProvider | None = None,
    ) -> None:
        self._config = config or ToolsConfig.from_env()
        self._knowledge_provider = knowledge_provider

    async def handle(self, envelope: IntentEnvelope) -> SepsisAgentResult:
        """Ground the plan, page RRT, order screen labs, and read back a cited checklist."""
        cid = envelope.correlation_id
        patient_ref = envelope.entities.get("patient_ref", "")
        priority = _priority_for(envelope.urgency)
        notes: list[str] = []
        screen, flags = _compute_screen(envelope)
        if flags:
            notes.append("screen (qSOFA, QSOFA-2016): " + ", ".join(flags))

        page_msg = (
            f"Sepsis protocol — {patient_ref or 'patient'} "
            f"{envelope.entities.get('location', '')}".strip()
            + f". Screen {screen}. Please respond."
        )

        # Fan out (docs §3.2): ground the plan, read context, page RRT, and order screen
        # labs concurrently. Each call is individually timeout-bounded, so the join can't
        # hang the conversation. Escalation (page) + diagnostics fire alongside grounding.
        protocol, context, receipt, lab = await asyncio.gather(
            retrieve_sepsis_protocol(
                "sepsis hour-1 bundle", correlation_id=cid,
                config=self._config, provider=self._knowledge_provider,
            ),
            get_patient_context(patient_ref, correlation_id=cid, config=self._config)
            if patient_ref else _none(),
            send_page(
                _RRT_RECIPIENT, page_msg, priority=priority,
                correlation_id=cid, config=self._config,
            ),
            order_labs(
                patient_ref or "unspecified", _SCREEN_LABS, priority=priority,
                correlation_id=cid, config=self._config,
            ),
        )

        # Ground the plan; degrade to the cached hour-1 summary if the KB was unavailable.
        grounded = protocol.grounded and bool(protocol.steps)
        if not grounded:
            notes.append(
                "Foundry IQ unavailable"
                + (f" ({protocol.error})" if protocol.error else "")
                + " — using cached hour-1 summary; a clinician must verify."
            )
            protocol = fallback_sepsis_protocol(correlation_id=cid)

        lab_ok = bool(lab and lab.order_status in ("ordered", "resulted") and not lab.error)
        if not lab_ok:
            notes.append(f"lab order not placed ({lab.error or 'unknown error'})")
        steps = _build_steps(protocol, lab_ok)

        # Read back allergies from context — clinically relevant to the (proposed) antibiotics.
        if context is not None and context.resolved and context.allergies:
            notes.append("allergies on file: " + ", ".join(context.allergies))

        page_ok = bool(receipt and not receipt.error and receipt.delivery_state != "failed")
        if not page_ok:
            notes.append(f"RRT page not delivered ({receipt.error or 'delivery failed'})")

        spoken = _spoken_update(screen, grounded, page_ok, lab_ok, protocol.citations)
        logger.info(
            "sepsis handled correlation_id=%s screen=%s grounded=%s page_ok=%s lab_ok=%s "
            "steps=%d citations=%d",
            cid, screen, grounded, page_ok, lab_ok, len(steps), len(protocol.citations),
        )
        return SepsisAgentResult(
            correlation_id=cid, intent=envelope.intent, spoken_update=spoken,
            grounded=grounded, screen=screen, steps=steps,
            citations=list(protocol.citations),
            page_id=(receipt.page_id or None) if page_ok else None,
            lab_order_id=(lab.order_id or None) if lab_ok else None,
            requires_human_ack=True, autonomous_treatment=False, notes=notes,
        )


async def _none() -> None:
    """Awaitable that yields None (used when there is no patient_ref to look up)."""
    return None


def _spoken_update(
    screen: str,
    grounded: bool,
    page_ok: bool,
    lab_ok: bool,
    citations: list[ProtocolCitation],
) -> str:
    """Short spoken update: what was done, and that treatment awaits a clinician."""
    lead = {
        "positive": "Sepsis screen positive — starting the hour-1 bundle.",
        "negative": "Screen is low-risk, but I've prepped the hour-1 bundle to be safe.",
        "unknown": "Starting the sepsis hour-1 bundle.",
    }[screen]
    done = []
    if page_ok:
        done.append("Rapid Response paged")
    if lab_ok:
        done.append("lactate and blood cultures ordered")
    done_str = (" " + ", ".join(done) + ".") if done else ""
    src = citations[0].source_id if citations else "the sepsis protocol"
    ground_str = (
        f" Steps are cited from {src}."
        if grounded
        else " I couldn't reach the knowledge base, so I'm using a cached hour-1 summary — please verify."
    )
    return (
        f"{lead}{done_str} Antibiotics and fluids are ready for you to confirm and order."
        f"{ground_str}"
    )


def create_sepsis_agent(
    config: ToolsConfig | None = None,
    *,
    knowledge_provider: SepsisKnowledgeProvider | None = None,
) -> SepsisAgent:
    """Factory the orchestrator / hosted-agent registration attach to."""
    return SepsisAgent(config, knowledge_provider=knowledge_provider)


def sepsis_hosted_agent_spec() -> HostedAgentSpec:
    """Foundry hosted-agent spec for sepsis: action tools **plus** the Foundry IQ KB.

    The KB is attached as the ``knowledge_base_retrieve`` MCP tool via a RemoteTool project
    connection (per https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect).
    Server URLs / connection come from env; they default to placeholders for ``--dry-run``.
    """
    model = os.getenv("FOUNDRY_MODEL_NAME", "gpt-realtime")
    labs_url = os.getenv("MCP_LABS_HL7_URL", "https://<host>/mcp/labs_hl7")
    comms_url = os.getenv("MCP_COMMS_PAGE_URL", "https://<host>/mcp/comms_page")
    ctx_url = os.getenv("MCP_PATIENT_CONTEXT_URL", "https://<host>/mcp/patient_context")
    kb_url = os.getenv(
        "FOUNDRY_IQ_KB_MCP_URL",
        "https://<search>.search.windows.net/knowledgebases/<kb>/mcp?api-version=2026-05-01-preview",
    )
    kb_connection = os.getenv("FOUNDRY_IQ_KB_CONNECTION", "nightingale-sepsis-kb-connection")
    return HostedAgentSpec(
        name="nightingale-sepsis",
        model=model,
        instructions=(
            "You are the Nightingale sepsis agent. When a nurse invokes a sepsis screen or "
            "the hour-1 bundle, ALWAYS use the knowledge_base_retrieve tool to fetch the "
            "current Surviving Sepsis Campaign hour-1 protocol and ground every step in it. "
            "Cite the retrieved sources in every answer (e.g. 【source】); if the knowledge "
            "base does not contain the answer, say you don't know rather than guessing. "
            "You augment Vocera Engage and the clinical team: you MAY page the Rapid Response "
            "Team (comms_page), order the diagnostic labs the screen requires (labs_hl7: "
            "lactate, blood cultures), and read back the patient's context and allergies "
            "(patient_context). You MUST NOT issue autonomous treatment orders (antibiotics, "
            "fluids, vasopressors), MUST NOT suppress, reprioritize, or override any alarm, "
            "and MUST NOT make a final clinical decision. Prepare and read back the cited "
            "hour-1 steps for a clinician to confirm and act on."
        ),
        mcp_tools=(
            HostedMCPTool("nightingale_labs_hl7", labs_url, ("labs_hl7",)),
            HostedMCPTool("nightingale_comms_page", comms_url, ("comms_page",)),
            HostedMCPTool("nightingale_patient_context", ctx_url, ("patient_context",)),
            HostedMCPTool(
                "nightingale_sepsis_kb", kb_url, (KNOWLEDGE_TOOL,),
                project_connection_id=kb_connection,
            ),
        ),
    )


# Register with the orchestrator's router for this agent's intents (import-time).
register_agent(CAPABILITY, SEPSIS_INTENTS, create_sepsis_agent)
