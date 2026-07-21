"""L4 MCP tool: ``blood_bank`` — check blood-product availability / crossmatch status.

Real adapter: LIS / Blood Bank via the Vocera **HL7 Adapter** (or a **Scripted Adapter**
for a facility-specific blood-bank API) (docs/01-architecture.md §5; docs/02-stryker-
workload-catalog.md Part C — the HL7 Adapter "brings lab results" and the Scripted Adapter
is the "escape hatch for any custom agent tool"). This tool is the thin MCP wrapper over
that read surface: how many units of a product are available and the crossmatch status for
a patient. The LIS/blood bank owns the inventory and the crossmatch; this tool only reads it.

SAFETY (docs/01-architecture.md, copilot-instructions.md):
- Tools do **I/O only** — this is a **read**: it reports available units and crossmatch
  status. It makes no clinical decision, does not order, reserve, release, or transfuse
  product, and changes nothing.
- A human authorizes and performs any transfusion — human-in-the-loop. This tool exists to
  answer "do we have blood ready?" for a nurse; it never acts on the answer.

MCP SDK: ``mcp[cli]==1.28.1`` (pinned in requirements.txt; v1.x stable line).
FastMCP server pattern matches the existing ``mcp_server/`` in this repo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from config import ToolsConfig
from src.telemetry import traced_tool

logger = logging.getLogger("nightingale.tools.blood_bank")

TOOL_NAME = "blood_bank"

# Recognized blood products (canonical). Read-only; no ordering.
_PRODUCTS = ("prbc", "ffp", "platelets", "cryo", "whole_blood")
_PRODUCT_ALIASES = {
    "rbc": "prbc",
    "red cells": "prbc",
    "packed cells": "prbc",
    "packed red blood cells": "prbc",
    "blood": "prbc",
    "plasma": "ffp",
    "fresh frozen plasma": "ffp",
    "platelet": "platelets",
    "plts": "platelets",
    "cryoprecipitate": "cryo",
    "whole blood": "whole_blood",
}


def _canonical_product(product: str) -> str:
    key = product.strip().lower()
    return _PRODUCT_ALIASES.get(key, key)


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class ProductAvailability:
    """Availability of one blood product (verbatim from the LIS/blood bank)."""

    product: str
    units_available: int
    blood_type: str  # e.g. "O-", "A+", or "crossmatched" scope
    location: str  # e.g. "main blood bank", "OR fridge"


@dataclass(frozen=True)
class BloodBankResult:
    """Typed output of a blood-bank read (the tool's read-only I/O contract).

    ``crossmatch_status`` and ``units`` are reported verbatim from the LIS — this tool
    never orders, reserves, releases, or transfuses.
    """

    patient_ref: str | None
    resolved: bool
    patient_blood_type: str | None
    crossmatch_status: str | None  # none | in_progress | complete  (verbatim)
    crossmatch_units_ready: int | None
    units: list[ProductAvailability] = field(default_factory=list)
    massive_transfusion_protocol_available: bool | None = None
    source_system: str = ""
    retrieved_at: str = ""
    elapsed_ms: float = 0.0
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class BloodBankAdapter(Protocol):
    """Interface a real LIS / blood-bank adapter must implement.

    Note the interface exposes ONLY ``check`` — there is intentionally no order/reserve/
    release method. Availability is read; a human acts.
    """

    source_system: str

    async def check(
        self,
        patient_ref: str,
        *,
        product: str | None = None,
        correlation_id: str | None = None,
    ) -> BloodBankResult:
        """Read blood-product availability + crossmatch status for ``patient_ref``."""
        ...


# --- Mock adapter (realistic data + configurable latency) --------------------
# Realistic blood-bank snapshots keyed by a normalized patient reference. Synthetic.
def _default_units() -> list[ProductAvailability]:
    return [
        ProductAvailability("prbc", 4, "O-", "main blood bank"),
        ProductAvailability("prbc", 6, "A+", "main blood bank"),
        ProductAvailability("ffp", 8, "AB", "main blood bank"),
        ProductAvailability("platelets", 3, "pooled", "main blood bank"),
    ]


_PATIENTS: dict[str, BloodBankResult] = {
    "bed 12": BloodBankResult(
        patient_ref="bed 12", resolved=True, patient_blood_type="O+",
        crossmatch_status="complete", crossmatch_units_ready=2,
        units=[
            ProductAvailability("prbc", 2, "O+ (crossmatched)", "OR fridge"),
            ProductAvailability("prbc", 6, "O-", "main blood bank"),
            ProductAvailability("ffp", 4, "AB", "main blood bank"),
            ProductAvailability("platelets", 2, "pooled", "main blood bank"),
        ],
        massive_transfusion_protocol_available=True,
    ),
    "room 4": BloodBankResult(
        patient_ref="room 4", resolved=True, patient_blood_type="A-",
        crossmatch_status="in_progress", crossmatch_units_ready=0,
        units=[
            ProductAvailability("prbc", 4, "O-", "main blood bank"),
            ProductAvailability("ffp", 6, "AB", "main blood bank"),
            ProductAvailability("platelets", 1, "pooled", "main blood bank"),
        ],
        massive_transfusion_protocol_available=True,
    ),
}


def _normalize_ref(patient_ref: str) -> str:
    return " ".join(patient_ref.strip().lower().split())


class MockBloodBankAdapter:
    """Simulates a LIS/blood-bank read feed, with realistic latency."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (LIS/Blood Bank via HL7 / Scripted Adapter)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def check(
        self,
        patient_ref: str,
        *,
        product: str | None = None,
        correlation_id: str | None = None,
    ) -> BloodBankResult:
        t0 = time.perf_counter()
        retrieved_at = datetime.now(timezone.utc).isoformat()

        if not patient_ref.strip():
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return BloodBankResult(
                patient_ref=patient_ref, resolved=False, patient_blood_type=None,
                crossmatch_status=None, crossmatch_units_ready=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error="patient_ref is required.",
            )

        await self._simulate_latency()
        record = _PATIENTS.get(_normalize_ref(patient_ref))
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if record is None:
            # Unknown patient: still report general (uncrossmatched) inventory — the LIS
            # can answer "what O- do we have?" without a patient crossmatch.
            logger.info(
                "blood_bank ref=%r NO-CROSSMATCH correlation_id=%s elapsed_ms=%s",
                patient_ref, correlation_id, elapsed_ms,
            )
            record = BloodBankResult(
                patient_ref=patient_ref, resolved=True, patient_blood_type=None,
                crossmatch_status="none", crossmatch_units_ready=0,
                units=_default_units(),
                massive_transfusion_protocol_available=True,
            )

        units = record.units
        if product is not None:
            canonical = _canonical_product(product)
            units = [u for u in record.units if u.product == canonical]

        result = replace(
            record,
            patient_ref=patient_ref, units=units,
            source_system=self.source_system, retrieved_at=retrieved_at,
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
        )
        logger.info(
            "blood_bank ref=%r xmatch=%s ready=%s products=%d correlation_id=%s elapsed_ms=%s",
            patient_ref, result.crossmatch_status, result.crossmatch_units_ready,
            len(result.units), correlation_id, elapsed_ms,
        )
        return result


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_blood_bank_adapter(config: ToolsConfig | None = None) -> BloodBankAdapter:
    """Return the blood-bank adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`BloodBankAdapter` (same read-only signature) is returned — no
    caller change, and still no order/reserve path.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: LIS / Blood Bank via HL7 Adapter (or Scripted Adapter for a
        # facility blood-bank API) — READ availability/crossmatch only. TODO: implement
        # VoceraBloodBankAdapter and return it. Keep it read-only; no order/reserve path.
        raise NotImplementedError(
            "Real LIS/blood-bank adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockBloodBankAdapter(config)


@traced_tool("blood_bank")
async def check_blood_bank(
    patient_ref: str,
    *,
    product: str | None = None,
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    adapter: BloodBankAdapter | None = None,
) -> BloodBankResult:
    """Read blood-product availability, bounded by the configured timeout.

    A slow LIS surfaces as a typed timeout result (``resolved=False``, ``error`` set)
    rather than hanging the orchestrator — never blocks the conversation.
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_blood_bank_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.check(patient_ref, product=product, correlation_id=correlation_id),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "blood_bank ref=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            patient_ref, correlation_id, elapsed_ms,
        )
        return BloodBankResult(
            patient_ref=patient_ref, resolved=False, patient_blood_type=None,
            crossmatch_status=None, crossmatch_units_ready=None,
            source_system=getattr(adapter, "source_system", "unknown"),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Blood-bank check timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the READ-ONLY ``blood_bank`` tool.

    Only a read/check tool is registered. No order/reserve/release tool exists — a human
    authorizes and performs any transfusion.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale Blood Bank (read-only)",
        instructions=(
            "Check blood-product availability and crossmatch status for a patient from the "
            "LIS/blood bank: units of PRBC/FFP/platelets/cryo on hand, patient blood type, "
            "crossmatch status, and whether the massive transfusion protocol is available. "
            "Read-only: it reports what's available; it never orders, reserves, releases, or "
            "transfuses product. A clinician authorizes any transfusion."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def blood_bank(
        patient_ref: str, product: str = "", correlation_id: str = ""
    ) -> str:
        """Check blood-product availability + crossmatch status (READ-ONLY).

        Args:
            patient_ref: Patient reference — bed/room (e.g. "bed 12") or MRN.
            product: Optional product filter, e.g. "prbc", "platelets", "ffp", "cryo".
            correlation_id: Optional envelope correlation id for tracing.

        Returns:
            JSON ``BloodBankResult``: patient_blood_type, crossmatch_status,
            crossmatch_units_ready, a ``units`` array (product/units_available/blood_type/
            location, verbatim), massive_transfusion_protocol_available, plus source_system
            and elapsed_ms. If empty ref/timed out, ``resolved`` is false and ``error`` set.
        """
        result = await check_blood_bank(
            patient_ref, product=product or None,
            correlation_id=correlation_id or None, config=config,
        )
        return json.dumps(result.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the blood-bank (read-only) MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
