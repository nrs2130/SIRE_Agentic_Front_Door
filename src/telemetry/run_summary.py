"""Compact per-conversation **run summary** (docs/01-architecture.md §6, /observability step 3).

At the end of each conversation the orchestrator emits one line-oriented summary so a workshop
viewer can *see* the routing decision, which branches ran, their latencies, and any budget
breaches — making the parallel, latency-aware orchestration visible without opening a portal.
The same numbers are on the OpenTelemetry spans, so this is a human-readable echo of the trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BranchTiming:
    """One branch's measured latency vs its budget, and whether it breached."""

    name: str
    latency_ms: float
    budget_ms: float | None = None
    status: str = "done"  # done | pending | failed

    @property
    def breached(self) -> bool:
        """True if the branch was marked pending/failed or ran over its soft budget."""
        if self.status in ("pending", "failed"):
            return True
        return self.budget_ms is not None and self.latency_ms > self.budget_ms


@dataclass(frozen=True)
class RunSummary:
    """Everything a viewer needs to read one run at a glance."""

    correlation_id: str
    path: str  # fast | standard
    intent: str
    urgency: str
    ack_latency_ms: float | None = None
    ack_budget_ms: int | None = None
    branches: list[BranchTiming] = field(default_factory=list)
    total_ms: float | None = None

    @property
    def breaches(self) -> list[str]:
        return [b.name for b in self.branches if b.breached]

    def format(self) -> str:
        """Render the compact multi-line summary."""
        lines = [
            f"RUN correlation_id={self.correlation_id} urgency={self.urgency} "
            f"path={self.path} intent={self.intent}"
        ]
        if self.ack_latency_ms is not None:
            budget = f"/{self.ack_budget_ms}ms" if self.ack_budget_ms else ""
            ok = (
                "OK" if (self.ack_budget_ms is None or self.ack_latency_ms <= self.ack_budget_ms)
                else "OVER"
            )
            lines.append(f"  ack: {self.ack_latency_ms:.2f}ms{budget} {ok}")
        if self.branches:
            rendered = ", ".join(
                f"{b.name} {b.latency_ms:.0f}ms"
                + (f"/{b.budget_ms:.0f}ms" if b.budget_ms else "")
                + (" ✓" if not b.breached else f" ⚠{b.status}")
                for b in self.branches
            )
            lines.append(f"  branches (concurrent): {rendered}")
        if self.total_ms is not None:
            lines.append(f"  total: {self.total_ms:.1f}ms")
        lines.append(
            "  budget breaches: "
            + (", ".join(self.breaches) if self.breaches else "none")
        )
        return "\n".join(lines)
