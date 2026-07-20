"""Intent → L3 agent registry.

The orchestrator's router switches on *urgency* to pick a path (fast vs standard);
within a path it fans out to the agents that handle the envelope's *intent*. This
registry is the intent→agent map those fan-out branches consult, so an agent module can
"register with the router for its intents" (foundry-agent.prompt.md step 5) by importing
this and calling :func:`register_agent` at import time.

Dependency-free by design — no Agent Framework / Azure imports — so the orchestrator can
consult it cheaply.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentRegistration:
    """A registered L3 agent capability and the intents it handles."""

    capability: str
    intents: frozenset[str]
    factory: Callable[..., Any]  # returns an agent instance with async handle(envelope)


_REGISTRY: dict[str, AgentRegistration] = {}


def register_agent(
    capability: str, intents: Iterable[str], factory: Callable[..., Any]
) -> AgentRegistration:
    """Register an agent capability for a set of intents (idempotent per capability)."""
    reg = AgentRegistration(capability, frozenset(intents), factory)
    _REGISTRY[capability] = reg
    return reg


def agent_for_intent(intent: str) -> AgentRegistration | None:
    """Return the registration handling ``intent`` (first match), or None."""
    for reg in _REGISTRY.values():
        if intent in reg.intents:
            return reg
    return None


def registrations() -> dict[str, AgentRegistration]:
    """Return a copy of the current capability → registration map."""
    return dict(_REGISTRY)
