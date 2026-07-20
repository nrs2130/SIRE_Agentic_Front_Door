"""L3 agent: **comms** — page/call a provider or team by role.

Single responsibility (docs/01-architecture.md §3): resolve *role → current person* and
hand a page to Vocera Engage. It owns no clinical logic — it notifies. It calls two L4 MCP
tools:

* ``oncall_lookup`` — resolve a clinical role to the on-call provider, and
* ``comms_page`` — hand the page to Engage escalation + Smartbadge.

Safety: the agent **augments** Engage's routing; every page ``requires_human_ack`` and it
issues no autonomous clinical order or alarm override (copilot-instructions.md). It runs
**locally against the mock MCP tools** with no Azure dependency; the Foundry hosted-agent
registration is a separate step (see :func:`comms_hosted_agent_spec` + src/agents/register.py).

Agent Framework / Foundry SDKs are only needed for the *hosted* deployment path
(agent-framework==1.11.0, azure-ai-projects==2.3.0), not for this local capability.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from config import ToolsConfig
from src.gateway.intent_envelope import IntentEnvelope, Urgency
from src.tools.comms_page import send_page
from src.tools.oncall_lookup import lookup_oncall

from .hosted import HostedAgentSpec, HostedMCPTool
from .registry import register_agent

logger = logging.getLogger("nightingale.agents.comms")

CAPABILITY = "comms"

# Intents this agent handles — anything whose action is "notify a person/team".
COMMS_INTENTS = frozenset(
    {"contact_provider", "code_blue", "rapid_response", "stemi_alert", "stroke_alert"}
)

# When only an intent (no explicit role/person) is given, the team to page.
_INTENT_DEFAULT_ROLE = {
    "rapid_response": "rrt",
    "code_blue": "rrt",
    "stemi_alert": "cardiologist",
    "stroke_alert": "neurologist",
}


@dataclass(frozen=True)
class CommsAgentResult:
    """Typed outcome of a comms action + a spoken update for streaming."""

    correlation_id: str
    intent: str
    resolved: bool
    spoken_update: str
    recipient: str | None = None
    recipient_id: str | None = None
    page_id: str | None = None
    delivery_state: str | None = None
    escalation_tier: int | None = None
    requires_human_ack: bool = True
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def _priority_for(urgency: Urgency) -> str:
    return "stat" if urgency is Urgency.EMERGENCY else "routine"


def _build_message(envelope: IntentEnvelope, recipient: str) -> str:
    intent = envelope.intent.replace("_", " ")
    parts = [f"{intent} — please respond"]
    if patient := envelope.entities.get("patient_ref"):
        parts.append(f"patient {patient}")
    if loc := envelope.entities.get("location"):
        parts.append(f"location {loc}")
    return ", ".join(parts) + f". (paging {recipient})"


class CommsAgent:
    """Resolves a role to a person and pages them via Engage (mock-backed locally)."""

    capability = CAPABILITY
    intents = COMMS_INTENTS

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()

    async def handle(self, envelope: IntentEnvelope) -> CommsAgentResult:
        """Resolve the recipient and page them; return a typed result + spoken update."""
        cid = envelope.correlation_id
        priority = _priority_for(envelope.urgency)
        notes: list[str] = []
        resolved_role: str | None = None
        recipient: str | None = None
        recipient_id: str | None = None

        person = envelope.entities.get("person")
        role = envelope.entities.get("role") or _INTENT_DEFAULT_ROLE.get(envelope.intent)

        if person:
            recipient = person
        elif role:
            resolved_role = role
            oncall = await lookup_oncall(
                role, envelope.entities.get("location"), config=self._config
            )
            if oncall.error or oncall.primary is None:
                # Degrade gracefully: page the role label directly, note the miss.
                notes.append(f"on-call lookup failed ({oncall.error}); paging role directly")
                recipient = role
            else:
                recipient = oncall.primary.name
                recipient_id = oncall.primary.provider_id
                notes.append(f"resolved {role} -> {oncall.primary.name}")
        else:
            logger.info("comms no-recipient correlation_id=%s intent=%s", cid, envelope.intent)
            return CommsAgentResult(
                correlation_id=cid, intent=envelope.intent, resolved=False,
                spoken_update="I don't have a person or role to contact for that request.",
                error="No recipient (person or role) in the intent envelope.",
            )

        message = _build_message(envelope, recipient)
        receipt = await send_page(
            recipient, message, priority=priority,
            recipient_id=recipient_id, correlation_id=cid, config=self._config,
        )
        if receipt.error or receipt.delivery_state == "failed":
            spoken = f"I couldn't reach {recipient}—{receipt.error or 'delivery failed'}."
            logger.warning("comms page FAILED correlation_id=%s recipient=%s", cid, recipient)
            return CommsAgentResult(
                correlation_id=cid, intent=envelope.intent, resolved=False,
                spoken_update=spoken, recipient=recipient, recipient_id=recipient_id,
                delivery_state=receipt.delivery_state, notes=notes, error=receipt.error,
            )

        who = recipient + (f", the on-call {resolved_role}" if resolved_role and recipient_id else "")
        spoken = f"Paged {who} — {priority}. Awaiting acknowledgment."
        logger.info(
            "comms page OK correlation_id=%s recipient=%s tier=%s page_id=%s",
            cid, recipient, receipt.escalation_tier, receipt.page_id,
        )
        return CommsAgentResult(
            correlation_id=cid, intent=envelope.intent, resolved=True,
            spoken_update=spoken, recipient=recipient, recipient_id=recipient_id,
            page_id=receipt.page_id, delivery_state=receipt.delivery_state,
            escalation_tier=receipt.escalation_tier,
            requires_human_ack=receipt.requires_human_ack, notes=notes,
        )


def create_comms_agent(config: ToolsConfig | None = None) -> CommsAgent:
    """Factory the orchestrator / hosted-agent registration attach to."""
    return CommsAgent(config)


def comms_hosted_agent_spec() -> HostedAgentSpec:
    """The Foundry hosted-agent spec for comms (MCP tools attached, no clinical grounding).

    Server URLs come from env so the same spec works across environments; they default to
    placeholders for ``--dry-run``.
    """
    oncall_url = os.getenv("MCP_ONCALL_LOOKUP_URL", "https://<host>/mcp/oncall_lookup")
    comms_url = os.getenv("MCP_COMMS_PAGE_URL", "https://<host>/mcp/comms_page")
    model = os.getenv("FOUNDRY_MODEL_NAME", "gpt-realtime")
    return HostedAgentSpec(
        name="nightingale-comms",
        model=model,
        instructions=(
            "You are the Nightingale comms agent. Resolve a clinical role to the current "
            "on-call provider with oncall_lookup, then hand a page to Engage with "
            "comms_page. You notify people; you make no clinical decision, issue no order, "
            "and never override an alarm. Every page requires a human to acknowledge and "
            "act. Read the recipient and delivery state back to the nurse."
        ),
        mcp_tools=(
            HostedMCPTool("nightingale_oncall_lookup", oncall_url, ("oncall_lookup",)),
            HostedMCPTool("nightingale_comms_page", comms_url, ("comms_page",)),
        ),
    )


# Register with the orchestrator's router for this agent's intents (import-time).
register_agent(CAPABILITY, COMMS_INTENTS, create_comms_agent)
