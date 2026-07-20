"""Tests for L1, the Voice Gateway (envelope contract, urgency, panic, text stub)."""

from __future__ import annotations

import uuid

import pytest

from src.gateway import (
    ROUTE_INTENT_TOOL,
    IntentEnvelope,
    TextStubGateway,
    Urgency,
)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def test_envelope_shape() -> None:
    """A typed utterance yields a well-formed envelope with a valid correlation_id."""
    gateway = TextStubGateway()
    envelope = await gateway.submit("call the on-call cardiologist")

    assert isinstance(envelope, IntentEnvelope)
    assert _is_uuid(envelope.correlation_id)
    assert isinstance(envelope.intent, str) and envelope.intent
    assert isinstance(envelope.urgency, Urgency)
    assert isinstance(envelope.entities, dict)
    assert envelope.patient_context is None
    assert isinstance(envelope.spoken_ack_required, bool)
    assert envelope.utterance == "call the on-call cardiologist"

    # to_dict emits the documented contract order and a plain-string urgency.
    data = envelope.to_dict()
    assert list(data) == [
        "correlation_id",
        "intent",
        "urgency",
        "entities",
        "patient_context",
        "utterance",
        "spoken_ack_required",
    ]
    assert data["urgency"] in {"EMERGENCY", "ROUTINE"}


@pytest.mark.parametrize(
    "utterance",
    [
        "patient in bed 12 looks septic",
        "code blue in room 4",
        "the patient fell",
        "I need a rapid response now",
    ],
)
async def test_urgency_emergency(utterance: str) -> None:
    """Emergency phrases classify as EMERGENCY and require a spoken acknowledgment."""
    gateway = TextStubGateway()
    envelope = await gateway.submit(utterance)
    assert envelope.urgency is Urgency.EMERGENCY
    assert envelope.spoken_ack_required is True


@pytest.mark.parametrize(
    "utterance",
    [
        "call the on-call cardiologist",
        "look up the day-shift charge nurse",
        "where is the infusion pump",
    ],
)
async def test_urgency_routine(utterance: str) -> None:
    """Routine phrases classify as ROUTINE and do not force an immediate ack."""
    gateway = TextStubGateway()
    envelope = await gateway.submit(utterance)
    assert envelope.urgency is Urgency.ROUTINE
    assert envelope.spoken_ack_required is False


async def test_entities_extracted() -> None:
    """Light-weight slots (patient_ref) are pulled from the utterance."""
    gateway = TextStubGateway()
    envelope = await gateway.submit("patient in bed 12 looks septic")
    assert envelope.intent == "sepsis_screen"
    assert envelope.entities.get("patient_ref") == "bed 12"


async def test_panic_override() -> None:
    """The panic button forces EMERGENCY without any model classification."""
    gateway = TextStubGateway()
    envelope = await gateway.panic()
    assert envelope.urgency is Urgency.EMERGENCY
    assert envelope.intent == "panic_button"
    assert envelope.spoken_ack_required is True
    assert envelope.utterance == "[PANIC BUTTON]"


async def test_text_stub_mode_streams_and_speaks() -> None:
    """Envelopes reach the orchestrator via the async iterator; speak() is captured."""
    gateway = TextStubGateway()
    submitted = await gateway.submit("call the on-call cardiologist")
    await gateway.speak("Calling the on-call cardiologist now.")
    gateway.close()

    received = [env async for env in gateway.envelopes()]
    assert [e.correlation_id for e in received] == [submitted.correlation_id]
    assert gateway.spoken == ["Calling the on-call cardiologist now."]


async def test_correlation_ids_unique() -> None:
    """Each utterance gets its own correlation_id."""
    gateway = TextStubGateway()
    first = await gateway.submit("call the cardiologist")
    second = await gateway.submit("call the hospitalist")
    assert first.correlation_id != second.correlation_id


def test_route_intent_tool_schema() -> None:
    """The Voice Live function schema returns intent + urgency in a single call."""
    props = ROUTE_INTENT_TOOL["parameters"]["properties"]
    assert ROUTE_INTENT_TOOL["name"] == "route_intent"
    assert props["urgency"]["enum"] == ["EMERGENCY", "ROUTINE"]
    assert set(ROUTE_INTENT_TOOL["parameters"]["required"]) == {"intent", "urgency"}
