"""Cheap, deterministic intent + urgency classification for the Voice Gateway.

Urgency is decided **at L1** (docs/01-architecture.md §2) so the orchestrator's
router never needs an LLM reasoning pass to discover a code blue. Two consumers:

* :data:`ROUTE_INTENT_TOOL` — the Voice Live function-calling schema. The realtime
  model returns ``intent`` + ``entities`` + ``urgency`` together in a *single* call.
* :func:`classify` — a keyword classifier used by the text-stub gateway (so the
  whole system is testable without a mic) and as a defensive fallback if the model
  ever omits ``urgency``.
"""

from __future__ import annotations

import re
from typing import Any

from .intent_envelope import Urgency

# --- Emergency detection ----------------------------------------------------
# Targeted patterns for time-critical clinical events. Word boundaries avoid
# false positives. Order-independent — any match makes the utterance EMERGENCY.
_EMERGENCY_PATTERNS: tuple[str, ...] = (
    r"\bsepsis\b",
    r"\bseptic\b",
    r"\bcode blue\b",
    r"\bcardiac arrest\b",
    r"\brespiratory arrest\b",
    r"\bnot breathing\b",
    r"\bunresponsive\b",
    r"\bno pulse\b",
    r"\bpulseless\b",
    r"\bfell\b",
    r"\bfall\b",
    r"\bbed[ -]?exit\b",
    r"\bstroke\b",
    r"\bstemi\b",
    r"\bchest pain\b",
    r"\bh[ae]morrhage\b",
    r"\bmassive transfusion\b",
    r"\brapid response\b",
    r"\brrt\b",
    r"\bseizure\b",
    r"\banaphyla(?:xis|ctic)\b",
)
_EMERGENCY_RE = re.compile("|".join(_EMERGENCY_PATTERNS), re.IGNORECASE)


def classify_urgency(utterance: str) -> Urgency:
    """Return :attr:`Urgency.EMERGENCY` if the utterance names a critical event."""
    return Urgency.EMERGENCY if _EMERGENCY_RE.search(utterance) else Urgency.ROUTINE


def classify_intent(utterance: str) -> str:
    """Map an utterance to a canonical intent verb (best-effort, keyword-based)."""
    t = utterance.lower()
    if "sepsis" in t or "septic" in t:
        return "sepsis_screen"
    if (
        "code blue" in t
        or "cardiac arrest" in t
        or "not breathing" in t
        or "unresponsive" in t
        or "no pulse" in t
        or "pulseless" in t
    ):
        return "code_blue"
    if re.search(r"\bfell\b", t) or re.search(r"\bfall\b", t) or re.search(r"\bbed[ -]?exit\b", t):
        return "fall_response"
    if "rapid response" in t or re.search(r"\brrt\b", t):
        return "rapid_response"
    if "stroke" in t:
        return "stroke_alert"
    if "stemi" in t or "chest pain" in t:
        return "stemi_alert"
    if "blood" in t and ("bank" in t or "supply" in t or "units" in t or "crossmatch" in t):
        return "check_blood_supply"
    if "locate" in t or "where is" in t or "find the" in t:
        return "locate_equipment"
    if re.search(r"\b(call|page|contact|connect|transfer|reach)\b", t):
        return "contact_provider"
    return "general_request"


# --- Entity extraction ------------------------------------------------------
_PATIENT_RE = re.compile(r"\b(bed|room)\s+([0-9]+[a-z]?)\b", re.IGNORECASE)
_LOCATION_RE = re.compile(r"\b(\d+\s*(?:west|east|north|south)|\d+\s*[wens])\b", re.IGNORECASE)
_ROLE_RE = re.compile(
    r"\b(cardiologist|hospitalist|intensivist|surgeon|physician|anesthesiologist"
    r"|neurologist|resident|attending|charge nurse|respiratory therapist)\b",
    re.IGNORECASE,
)


def extract_entities(utterance: str) -> dict[str, str]:
    """Pull light-weight slots (patient_ref, location, role) from an utterance."""
    entities: dict[str, str] = {}
    if m := _PATIENT_RE.search(utterance):
        entities["patient_ref"] = f"{m.group(1).lower()} {m.group(2)}"
    if m := _LOCATION_RE.search(utterance):
        entities["location"] = re.sub(r"\s+", " ", m.group(1)).strip()
    if m := _ROLE_RE.search(utterance):
        entities["role"] = m.group(1).lower()
    return entities


def classify(utterance: str) -> tuple[str, Urgency, dict[str, str]]:
    """Return ``(intent, urgency, entities)`` for an utterance in one cheap pass."""
    return classify_intent(utterance), classify_urgency(utterance), extract_entities(utterance)


# --- Voice Live function-calling schema -------------------------------------
# A single tool call returns intent + urgency + entities together (docs §2–§3).
# The model MUST classify urgency here, in this call — no separate round trip.
ROUTE_INTENT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "route_intent",
    "description": (
        "Classify the nurse's spoken request into a single routing decision. Call "
        "this exactly once per utterance, as soon as you understand it, BEFORE any "
        "other tool. Return the canonical intent, the urgency, and any extracted "
        "entities together in this one call. Do not resolve the request yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": (
                    "Canonical intent verb, e.g. sepsis_screen, code_blue, "
                    "fall_response, rapid_response, stroke_alert, stemi_alert, "
                    "contact_provider, locate_equipment, check_blood_supply, "
                    "general_request."
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["EMERGENCY", "ROUTINE"],
                "description": (
                    "EMERGENCY for time-critical clinical events (sepsis, code blue, "
                    "cardiac/respiratory arrest, fall, stroke, STEMI, hemorrhage, "
                    "rapid response). ROUTINE for everything else. Decide this here."
                ),
            },
            "entities": {
                "type": "object",
                "description": "Extracted slots (patient_ref, location, role, person, group).",
                "properties": {
                    "patient_ref": {"type": "string"},
                    "location": {"type": "string"},
                    "role": {"type": "string"},
                    "person": {"type": "string"},
                    "group": {"type": "string"},
                },
                "additionalProperties": {"type": "string"},
            },
            "utterance": {
                "type": "string",
                "description": "The verbatim user utterance.",
            },
        },
        "required": ["intent", "urgency"],
    },
}
