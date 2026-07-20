"""Typed messages that flow between orchestrator executors.

The Agent Framework routes each message to the executor whose ``@handler`` input
type matches it. That type-based routing is what makes the router deterministic:
it emits either a :class:`FastPathRequest` or a :class:`StandardPathRequest`, and
only the matching path's entry executor is invoked — no LLM, ~0 ms.

Every message carries the :class:`IntentEnvelope` (which holds ``correlation_id``)
so the id threads through every node.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.gateway.intent_envelope import IntentEnvelope


@dataclass
class FastPathRequest:
    """Router → emergency fast path."""

    envelope: IntentEnvelope


@dataclass
class StandardPathRequest:
    """Router → routine standard path."""

    envelope: IntentEnvelope


@dataclass
class BranchTask:
    """Broadcast from a dispatcher to its concurrent fan-out branches."""

    envelope: IntentEnvelope


@dataclass
class BranchResult:
    """A single branch's outcome, collected at a fan-in barrier."""

    envelope: IntentEnvelope
    branch: str
    status: str  # "done" | "pending" (pending = latency budget exceeded)
    detail: str

    @property
    def correlation_id(self) -> str:
        return self.envelope.correlation_id


@dataclass
class StandardProgress:
    """Accumulating state passed along the sequential standard path."""

    envelope: IntentEnvelope
    notes: list[str] = field(default_factory=list)

    @property
    def correlation_id(self) -> str:
        return self.envelope.correlation_id
