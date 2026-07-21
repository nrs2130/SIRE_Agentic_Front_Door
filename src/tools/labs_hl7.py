"""L4 MCP tool: ``labs_hl7`` — order labs and read results via the HL7 backbone.

Real adapter: Vocera **HL7 Adapter** (docs/01-architecture.md §5; docs/02-stryker-
workload-catalog.md Part C — "Talks to any HL7-capable system; brings lab results, ADT,
radiology"). This tool is the thin MCP wrapper over the LIS/EHR HL7 interface: it places
an order (HL7 ORM) and reads results (HL7 ORU). The lab system and clinicians own the
actual analysis; this tool only moves the order/result messages.

SAFETY (docs/01-architecture.md, copilot-instructions.md):
- Tools do **I/O only** — this places an order the orchestrator/agent already decided on
  and returns whatever the LIS reports. It makes no clinical decision and interprets
  nothing (no "is this septic?").
- No autonomous medical orders: the agent prepares/places an order a human authorized and
  reads results back for a human to act on — human-in-the-loop.

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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from config import ToolsConfig
from src.telemetry import traced_tool

logger = logging.getLogger("nightingale.tools.labs_hl7")

TOOL_NAME = "labs_hl7"


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class LabResult:
    """A single resulted analyte (HL7 OBX-style), verbatim from the LIS."""

    test_code: str  # e.g. "LACTATE", "CULT-BLD"
    name: str
    value: str
    unit: str
    reference_range: str
    abnormal_flag: str  # "" | N | H | L | HH | LL | A  (HL7 OBX-8; not interpreted here)
    status: str  # pending | preliminary | final


@dataclass(frozen=True)
class LabOrderResult:
    """Typed output of a labs order/read (the tool's I/O contract).

    Records the HL7 hand-off. ``order_status`` reflects the LIS message state, not a
    clinical judgment. ``results`` echoes what the LIS returned — flags included, never
    interpreted by this tool.
    """

    order_id: str
    patient_ref: str
    tests: list[str]
    priority: str  # routine | stat
    order_status: str  # ordered | resulted | failed
    ordered_at: str  # ISO-8601
    source_system: str
    elapsed_ms: float
    results: list[LabResult] = field(default_factory=list)
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class LabsAdapter(Protocol):
    """Interface a real Vocera HL7 labs adapter must implement.

    Mock and future real adapter share this signature exactly, so callers are unaffected
    by which one is wired.
    """

    source_system: str

    async def order_labs(
        self,
        patient_ref: str,
        tests: list[str],
        *,
        priority: str = "routine",
        correlation_id: str | None = None,
    ) -> LabOrderResult:
        """Place an HL7 lab order for ``patient_ref`` and return any available results."""
        ...


# --- Mock adapter (realistic data + configurable latency) --------------------
# Realistic result templates keyed by canonical test code. Values are synthetic and
# returned verbatim (flags included) — the tool does not decide what they mean.
_RESULT_TEMPLATES: dict[str, LabResult] = {
    "LACTATE": LabResult("LACTATE", "Lactate, plasma", "3.8", "mmol/L", "0.5-2.2", "H", "final"),
    "CBC": LabResult("CBC", "WBC", "14.2", "10^3/uL", "4.0-11.0", "H", "final"),
    "CULT-BLD": LabResult("CULT-BLD", "Blood culture", "Pending", "", "No growth", "", "pending"),
    "BMP": LabResult("BMP", "Creatinine", "1.1", "mg/dL", "0.6-1.3", "N", "final"),
    "TROPONIN": LabResult("TROPONIN", "Troponin I, hs", "52", "ng/L", "0-22", "H", "final"),
    "HGB": LabResult("HGB", "Hemoglobin", "7.4", "g/dL", "12.0-16.0", "L", "final"),
}
_TEST_ALIASES = {
    "lactate": "LACTATE",
    "cultures": "CULT-BLD",
    "blood culture": "CULT-BLD",
    "blood cultures": "CULT-BLD",
    "cbc": "CBC",
    "bmp": "BMP",
    "chem": "BMP",
    "troponin": "TROPONIN",
    "trop": "TROPONIN",
    "hemoglobin": "HGB",
    "hgb": "HGB",
}
_PRIORITIES = ("routine", "stat")


def _canonical_test(test: str) -> str:
    key = test.strip().lower()
    return _TEST_ALIASES.get(key, key.upper())


class MockLabsAdapter:
    """Simulates the Vocera HL7 Adapter placing an order + returning results."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (Vocera HL7 Adapter -> LIS/EHR)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def order_labs(
        self,
        patient_ref: str,
        tests: list[str],
        *,
        priority: str = "routine",
        correlation_id: str | None = None,
    ) -> LabOrderResult:
        t0 = time.perf_counter()
        ordered_at = datetime.now(timezone.utc).isoformat()
        priority_norm = priority.strip().lower()

        # Validate at the boundary only (I/O concern, not business logic).
        if priority_norm not in _PRIORITIES:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return LabOrderResult(
                order_id="", patient_ref=patient_ref, tests=list(tests),
                priority=priority, order_status="failed", ordered_at=ordered_at,
                source_system=self.source_system, elapsed_ms=elapsed_ms,
                correlation_id=correlation_id,
                error=f"Invalid priority '{priority}'. Use one of {', '.join(_PRIORITIES)}.",
            )
        if not patient_ref.strip():
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return LabOrderResult(
                order_id="", patient_ref=patient_ref, tests=list(tests),
                priority=priority_norm, order_status="failed", ordered_at=ordered_at,
                source_system=self.source_system, elapsed_ms=elapsed_ms,
                correlation_id=correlation_id, error="patient_ref is required.",
            )
        if not tests:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return LabOrderResult(
                order_id="", patient_ref=patient_ref, tests=list(tests),
                priority=priority_norm, order_status="failed", ordered_at=ordered_at,
                source_system=self.source_system, elapsed_ms=elapsed_ms,
                correlation_id=correlation_id, error="At least one test is required.",
            )

        await self._simulate_latency()
        results: list[LabResult] = []
        for test in tests:
            code = _canonical_test(test)
            template = _RESULT_TEMPLATES.get(code)
            if template is not None:
                results.append(template)
            else:
                # Unknown test still "orders" (HL7 accepts it); result stays pending.
                results.append(
                    LabResult(code, test, "Pending", "", "", "", "pending")
                )
        order_id = f"ord-{uuid.uuid4().hex[:12]}"
        # resulted if anything came back final/preliminary; else ordered (all pending).
        resulted = any(r.status in ("final", "preliminary") for r in results)
        order_status = "resulted" if resulted else "ordered"
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info(
            "labs_hl7 patient=%r tests=%s priority=%s status=%s order_id=%s correlation_id=%s elapsed_ms=%s",
            patient_ref, [_canonical_test(t) for t in tests], priority_norm,
            order_status, order_id, correlation_id, elapsed_ms,
        )
        return LabOrderResult(
            order_id=order_id, patient_ref=patient_ref,
            tests=[_canonical_test(t) for t in tests], priority=priority_norm,
            order_status=order_status, ordered_at=ordered_at,
            source_system=self.source_system, elapsed_ms=elapsed_ms,
            results=results, correlation_id=correlation_id,
        )


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_labs_adapter(config: ToolsConfig | None = None) -> LabsAdapter:
    """Return the labs adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`LabsAdapter` (same signature) is returned — no caller change.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: Vocera HL7 Adapter (ORM order out / ORU result in) to the LIS/EHR.
        # TODO: implement VoceraLabsHL7Adapter against the HL7 interface and return it
        # here. Until then, fail loudly rather than silently mocking.
        raise NotImplementedError(
            "Real Vocera HL7 labs adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockLabsAdapter(config)


@traced_tool("labs_hl7")
async def order_labs(
    patient_ref: str,
    tests: list[str],
    *,
    priority: str = "routine",
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    adapter: LabsAdapter | None = None,
) -> LabOrderResult:
    """Place a lab order via HL7, bounded by the configured timeout.

    A slow LIS surfaces as a typed timeout result (``order_status='failed'``, ``error``
    set) rather than hanging the orchestrator — never blocks the conversation.
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_labs_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.order_labs(
                patient_ref, tests, priority=priority, correlation_id=correlation_id
            ),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "labs_hl7 patient=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            patient_ref, correlation_id, elapsed_ms,
        )
        return LabOrderResult(
            order_id="", patient_ref=patient_ref, tests=list(tests),
            priority=priority, order_status="failed",
            ordered_at=datetime.now(timezone.utc).isoformat(),
            source_system=getattr(adapter, "source_system", "unknown"),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Lab order timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the ``labs_hl7`` tool.

    Returned so both local dev (stdio via ``python -m src.tools labs_hl7``) and the
    Foundry hosted-agent registration (attach the server endpoint as a hosted MCP tool)
    can mount the same tool.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale Labs (HL7)",
        instructions=(
            "Place a lab order and read results over the HL7 interface (e.g. lactate, "
            "blood cultures, CBC, troponin). This moves HL7 order/result messages only — "
            "it does not interpret results or decide what to order. A clinician authorizes "
            "the order and acts on the results."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def labs_hl7(
        patient_ref: str,
        tests: list[str],
        priority: str = "routine",
        correlation_id: str = "",
    ) -> str:
        """Place a lab order and return available results via HL7 (I/O only).

        Args:
            patient_ref: Patient reference (e.g. "bed 12", an MRN, or encounter id).
            tests: Tests to order, e.g. ["lactate", "blood cultures", "CBC"].
            priority: "routine" or "stat" (default "routine").
            correlation_id: Optional envelope correlation id for tracing.

        Returns:
            JSON ``LabOrderResult``: order_id, order_status, and a ``results`` array of
            resulted analytes (value, unit, reference_range, abnormal_flag verbatim from
            the LIS — not interpreted), plus source_system and elapsed_ms. On failure,
            ``error`` is set and ``order_status`` is "failed".
        """
        result = await order_labs(
            patient_ref, list(tests), priority=priority,
            correlation_id=correlation_id or None, config=config,
        )
        return json.dumps(result.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the labs HL7 MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
