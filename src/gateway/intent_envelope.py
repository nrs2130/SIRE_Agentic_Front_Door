"""The Intent Envelope — the normalized L1→L2 contract.

The Voice Gateway (L1) produces exactly one :class:`IntentEnvelope` per nurse
utterance; the orchestrator (L2) keys every routing decision, span, and tool call
off it. This module is intentionally dependency-free (no Voice Live / pyaudio /
Azure imports) so the orchestrator can import the contract without pulling in the
gateway's audio stack.

Contract shape (see docs/01-architecture.md §2)::

    {
        "correlation_id": "uuid",       # ties every span/log/tool call together
        "intent": "sepsis_screen",      # canonical verb
        "urgency": "EMERGENCY",         # EMERGENCY | ROUTINE — decided at L1
        "entities": {"patient_ref": "bed 12", "location": "4 West"},
        "patient_context": null,         # filled later by the Patient Context tool
        "utterance": "patient in bed 12 looks septic",
        "spoken_ack_required": true
    }
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Urgency(str, Enum):
    """Urgency class decided cheaply at the gateway — never via an LLM reasoning pass."""

    EMERGENCY = "EMERGENCY"
    ROUTINE = "ROUTINE"


def new_correlation_id() -> str:
    """Return a fresh correlation id for one utterance."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class IntentEnvelope:
    """Normalized output of the Voice Gateway; the single contract L2 consumes."""

    correlation_id: str
    intent: str
    urgency: Urgency
    entities: dict[str, str] = field(default_factory=dict)
    utterance: str = ""
    spoken_ack_required: bool = False
    patient_context: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        intent: str,
        urgency: Urgency | str,
        entities: dict[str, str] | None = None,
        utterance: str = "",
        *,
        spoken_ack_required: bool | None = None,
        patient_context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> "IntentEnvelope":
        """Build an envelope, generating a ``correlation_id`` when not supplied.

        ``spoken_ack_required`` defaults to ``True`` for emergencies (the fast path
        must acknowledge by voice immediately) and ``False`` otherwise.
        """
        urgency = Urgency(urgency)
        if spoken_ack_required is None:
            spoken_ack_required = urgency is Urgency.EMERGENCY
        return cls(
            correlation_id=correlation_id or new_correlation_id(),
            intent=intent,
            urgency=urgency,
            entities=dict(entities or {}),
            utterance=utterance,
            spoken_ack_required=spoken_ack_required,
            patient_context=patient_context,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize in the documented contract order (urgency as a plain string)."""
        return {
            "correlation_id": self.correlation_id,
            "intent": self.intent,
            "urgency": self.urgency.value,
            "entities": dict(self.entities),
            "patient_context": self.patient_context,
            "utterance": self.utterance,
            "spoken_ack_required": self.spoken_ack_required,
        }
