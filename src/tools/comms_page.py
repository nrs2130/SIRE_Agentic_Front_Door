"""L4 MCP tool: ``comms_page`` — send a page/notification to a provider or team.

Real adapter: Vocera **Engage escalation + Smartbadge call/page** (docs/01-architecture.md
§5; docs/02-stryker-workload-catalog.md Part C — Engage escalation routing over the
Scripted / SIP / Nurse-call adapters). This tool is the thin MCP wrapper that *hands a
notification to Engage*; Engage owns the actual routing, presence-aware escalation, and
delivery to the Smartbadge.

SAFETY (docs/01-architecture.md, copilot-instructions.md):
- Tools do **I/O only** — this hands off a page; it makes no clinical decision and does
  not choose *whether* to escalate.
- The agent **augments** Engage's routing; it never suppresses, reprioritizes, or overrides
  a cleared clinical alarm. Engage's alarm middleware (EMDAN) is FDA 510(k)-cleared.
- Anything clinical is **human-in-the-loop**: this tool *prepares/sends* a page for a human
  to receive and act on; it issues no autonomous medical orders.

MCP SDK: ``mcp[cli]==1.28.1`` (pinned in requirements.txt; v1.x stable line).
FastMCP server pattern matches the existing ``mcp_server/`` in this repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from config import ToolsConfig
from src.telemetry import traced_tool

logger = logging.getLogger("nightingale.tools.comms_page")

TOOL_NAME = "comms_page"

# Allowed priorities. STAT maps to Engage's highest routine escalation tier; it does NOT
# touch the cleared alarm path — that remains Engage/EMDAN's responsibility.
_PRIORITIES = ("routine", "urgent", "stat")


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class PageReceipt:
    """Typed acknowledgment that Engage accepted a page for delivery (the I/O contract).

    This records that the notification was *handed off* to Engage — not that a human has
    acted on it. ``delivery_state`` reflects Engage's routing state, not clinical outcome.
    """

    page_id: str
    recipient: str
    recipient_id: str | None
    priority: str
    message: str
    delivery_state: str  # queued | delivered | failed
    escalation_tier: int  # Engage escalation tier this entered at
    requires_human_ack: bool  # always True — a human must acknowledge/act
    accepted_at: str  # ISO-8601
    source_system: str
    elapsed_ms: float
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class CommsAdapter(Protocol):
    """Interface a real Vocera Engage escalation/paging adapter must implement.

    Mock and future real adapter share this signature exactly, so callers are unaffected
    by which one is wired.
    """

    source_system: str

    async def page(
        self,
        recipient: str,
        message: str,
        *,
        priority: str = "routine",
        recipient_id: str | None = None,
        correlation_id: str | None = None,
    ) -> PageReceipt:
        """Hand a notification to Engage for routing to ``recipient``."""
        ...


# --- Mock adapter (realistic behavior + configurable latency) ----------------
class MockCommsAdapter:
    """Simulates Engage accepting a page for escalation/delivery, with realistic latency."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (Vocera Engage escalation + Smartbadge)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def page(
        self,
        recipient: str,
        message: str,
        *,
        priority: str = "routine",
        recipient_id: str | None = None,
        correlation_id: str | None = None,
    ) -> PageReceipt:
        t0 = time.perf_counter()
        priority_norm = priority.strip().lower()
        accepted_at = datetime.now(timezone.utc).isoformat()

        # Validate at the boundary only (I/O concern, not business logic).
        if priority_norm not in _PRIORITIES:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return PageReceipt(
                page_id="", recipient=recipient, recipient_id=recipient_id,
                priority=priority, message=message, delivery_state="failed",
                escalation_tier=0, requires_human_ack=True, accepted_at=accepted_at,
                source_system=self.source_system, elapsed_ms=elapsed_ms,
                correlation_id=correlation_id,
                error=f"Invalid priority '{priority}'. Use one of {', '.join(_PRIORITIES)}.",
            )
        if not recipient.strip():
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return PageReceipt(
                page_id="", recipient=recipient, recipient_id=recipient_id,
                priority=priority_norm, message=message, delivery_state="failed",
                escalation_tier=0, requires_human_ack=True, accepted_at=accepted_at,
                source_system=self.source_system, elapsed_ms=elapsed_ms,
                correlation_id=correlation_id, error="Recipient is required.",
            )

        await self._simulate_latency()
        # Higher priority enters escalation at a higher tier (Engage semantics).
        tier = {"routine": 1, "urgent": 2, "stat": 3}[priority_norm]
        page_id = f"page-{uuid.uuid4().hex[:12]}"
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info(
            "comms_page recipient=%r priority=%s tier=%d page_id=%s correlation_id=%s elapsed_ms=%s",
            recipient, priority_norm, tier, page_id, correlation_id, elapsed_ms,
        )
        return PageReceipt(
            page_id=page_id, recipient=recipient, recipient_id=recipient_id,
            priority=priority_norm, message=message, delivery_state="delivered",
            escalation_tier=tier, requires_human_ack=True, accepted_at=accepted_at,
            source_system=self.source_system, elapsed_ms=elapsed_ms,
            correlation_id=correlation_id,
        )


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_comms_adapter(config: ToolsConfig | None = None) -> CommsAdapter:
    """Return the comms adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`CommsAdapter` (same signature) is returned — no caller change.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: Vocera Engage escalation + Smartbadge call/page (Scripted/SIP).
        # TODO: implement VoceraCommsAdapter against the Engage escalation surface and
        # return it here. Until then, fail loudly rather than silently mocking.
        raise NotImplementedError(
            "Real Vocera Engage paging adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockCommsAdapter(config)


@traced_tool("comms_page")
async def send_page(
    recipient: str,
    message: str,
    *,
    priority: str = "routine",
    recipient_id: str | None = None,
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    adapter: CommsAdapter | None = None,
) -> PageReceipt:
    """Hand a page to Engage, bounded by the configured timeout.

    A slow adapter surfaces as a typed timeout receipt (``delivery_state='failed'``,
    ``error`` set) rather than hanging the orchestrator — never blocks the conversation.
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_comms_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.page(
                recipient, message, priority=priority,
                recipient_id=recipient_id, correlation_id=correlation_id,
            ),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "comms_page recipient=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            recipient, correlation_id, elapsed_ms,
        )
        return PageReceipt(
            page_id="", recipient=recipient, recipient_id=recipient_id,
            priority=priority, message=message, delivery_state="failed",
            escalation_tier=0, requires_human_ack=True,
            accepted_at=datetime.now(timezone.utc).isoformat(),
            source_system=getattr(adapter, "source_system", "unknown"),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Page hand-off timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the ``comms_page`` tool.

    Returned so both local dev (stdio via ``python -m src.tools comms_page``) and the
    Foundry hosted-agent registration (attach the server endpoint as a hosted MCP tool)
    can mount the same tool.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale Comms Page",
        instructions=(
            "Hand a page/notification to Vocera Engage for routing to a provider or team "
            "(e.g. the on-call hospitalist, RRT). This AUGMENTS Engage's routing — it never "
            "suppresses or overrides a cleared clinical alarm, and a human must acknowledge "
            "and act on every page. Resolve the recipient with oncall_lookup first."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def comms_page(
        recipient: str,
        message: str,
        priority: str = "routine",
        recipient_id: str = "",
        correlation_id: str = "",
    ) -> str:
        """Send a page/notification to a provider or team via Vocera Engage (I/O only).

        Args:
            recipient: Who to page — a resolved person or team name/role
                       (e.g. "Dr. Priya Nadkarni", "RRT Team Bravo").
            message: The page text (what the human should see/act on).
            priority: "routine", "urgent", or "stat" (default "routine").
            recipient_id: Optional provider/team id from oncall_lookup.
            correlation_id: Optional envelope correlation id for tracing.

        Returns:
            JSON ``PageReceipt``: page_id, delivery_state, escalation_tier,
            requires_human_ack (always true), source_system, elapsed_ms. On failure,
            ``error`` is set and ``delivery_state`` is "failed".
        """
        receipt = await send_page(
            recipient, message, priority=priority,
            recipient_id=recipient_id or None,
            correlation_id=correlation_id or None, config=config,
        )
        return json.dumps(receipt.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the comms page MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
