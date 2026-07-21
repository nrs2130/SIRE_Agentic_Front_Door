"""L4 MCP tool: ``patient_context`` — look up a patient and their care team.

Real adapter: Vocera **Patient Context Adapter (REST)** (docs/01-architecture.md §5;
docs/02-stryker-workload-catalog.md Part C — "Find patients, find their care team,
retrieve patient details"). This tool is the thin MCP wrapper over that REST surface: it
resolves a patient reference to identity + demographics + assigned care team. The EHR/
Engage own the record; this tool only reads it.

SAFETY (docs/01-architecture.md, copilot-instructions.md):
- Tools do **I/O only** — this is a read: it returns who the patient is and who's on their
  care team. It makes no clinical decision and changes nothing.
- It enriches the Intent Envelope's ``patient_context`` field; a human still drives any
  clinical action — human-in-the-loop.

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

logger = logging.getLogger("nightingale.tools.patient_context")

TOOL_NAME = "patient_context"


# --- Typed interface (identical for mock and real adapter) -------------------
@dataclass(frozen=True)
class CareTeamMember:
    """A member of the patient's assigned care team."""

    role: str
    name: str
    provider_id: str
    contact: str  # pager or phone


@dataclass(frozen=True)
class PatientContextResult:
    """Typed output of a patient-context lookup (the tool's read-only I/O contract)."""

    patient_ref: str
    resolved: bool
    mrn: str | None
    name: str | None
    age: int | None
    sex: str | None
    location: str | None  # unit / bed
    code_status: str | None  # e.g. "Full Code", "DNR" — verbatim from the record
    allergies: list[str] = field(default_factory=list)
    care_team: list[CareTeamMember] = field(default_factory=list)
    source_system: str = ""
    retrieved_at: str = ""
    elapsed_ms: float = 0.0
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class PatientContextAdapter(Protocol):
    """Interface a real Vocera Patient Context REST adapter must implement.

    Mock and future real adapter share this signature exactly, so callers are unaffected
    by which one is wired.
    """

    source_system: str

    async def get_context(
        self, patient_ref: str, *, correlation_id: str | None = None
    ) -> PatientContextResult:
        """Resolve ``patient_ref`` to identity, demographics, and care team."""
        ...


# --- Mock adapter (realistic data + configurable latency) --------------------
# Realistic patient records keyed by a normalized location/ref. Synthetic data.
_PATIENTS: dict[str, PatientContextResult] = {
    "bed 12": PatientContextResult(
        patient_ref="bed 12", resolved=True, mrn="MRN-4820193",
        name="Robert Alvarez", age=67, sex="M", location="4 West, bed 12",
        code_status="Full Code", allergies=["penicillin"],
        care_team=[
            CareTeamMember("attending", "Dr. Priya Nadkarni", "prov-10231", "12045"),
            CareTeamMember("primary_rn", "Jordan Ellis, RN", "rn-55021", "12310"),
        ],
    ),
    "bed 7": PatientContextResult(
        patient_ref="bed 7", resolved=True, mrn="MRN-4820477",
        name="Susan Ito", age=54, sex="F", location="4 West, bed 7",
        code_status="DNR", allergies=[],
        care_team=[
            CareTeamMember("attending", "Dr. Marcus Webb", "prov-10244", "12088"),
            CareTeamMember("primary_rn", "Alex Park, RN", "rn-55098", "12377"),
        ],
    ),
    "room 4": PatientContextResult(
        patient_ref="room 4", resolved=True, mrn="MRN-4821050",
        name="Daniel Osei", age=72, sex="M", location="ICU, room 4",
        code_status="Full Code", allergies=["sulfa", "latex"],
        care_team=[
            CareTeamMember("intensivist", "Dr. Hana Kim", "prov-30512", "14005"),
            CareTeamMember("primary_rn", "Casey Lin, RN", "rn-56010", "14210"),
        ],
    ),
}


def _normalize_ref(patient_ref: str) -> str:
    return " ".join(patient_ref.strip().lower().split())


class MockPatientContextAdapter:
    """Simulates the Vocera Patient Context REST adapter, with realistic latency."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (Vocera Patient Context REST)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def get_context(
        self, patient_ref: str, *, correlation_id: str | None = None
    ) -> PatientContextResult:
        t0 = time.perf_counter()
        retrieved_at = datetime.now(timezone.utc).isoformat()

        if not patient_ref.strip():
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            return PatientContextResult(
                patient_ref=patient_ref, resolved=False, mrn=None, name=None,
                age=None, sex=None, location=None, code_status=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error="patient_ref is required.",
            )

        await self._simulate_latency()
        record = _PATIENTS.get(_normalize_ref(patient_ref))
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if record is None:
            logger.info(
                "patient_context ref=%r UNRESOLVED correlation_id=%s elapsed_ms=%s",
                patient_ref, correlation_id, elapsed_ms,
            )
            return PatientContextResult(
                patient_ref=patient_ref, resolved=False, mrn=None, name=None,
                age=None, sex=None, location=None, code_status=None,
                source_system=self.source_system, retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms, correlation_id=correlation_id,
                error=f"No patient found for reference '{patient_ref}'.",
            )
        logger.info(
            "patient_context ref=%r -> %s correlation_id=%s elapsed_ms=%s",
            patient_ref, record.mrn, correlation_id, elapsed_ms,
        )
        # Re-stamp the request-scoped fields on the stored record.
        return replace(
            record,
            patient_ref=patient_ref,
            source_system=self.source_system,
            retrieved_at=retrieved_at,
            elapsed_ms=elapsed_ms,
            correlation_id=correlation_id,
        )


# --- Factory (mock now; real adapter is a config flip) -----------------------
def create_patient_context_adapter(
    config: ToolsConfig | None = None,
) -> PatientContextAdapter:
    """Return the patient-context adapter L3 agents / hosted-agent registration attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real adapter
    implementing :class:`PatientContextAdapter` (same signature) is returned — no caller
    change.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real adapter: Vocera Patient Context Adapter (REST) — find patient / care team.
        # TODO: implement VoceraPatientContextAdapter against the REST surface and return
        # it here. Until then, fail loudly rather than silently mocking.
        raise NotImplementedError(
            "Real Vocera Patient Context adapter not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockPatientContextAdapter(config)


@traced_tool("patient_context")
async def get_patient_context(
    patient_ref: str,
    *,
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    adapter: PatientContextAdapter | None = None,
) -> PatientContextResult:
    """Look up patient context, bounded by the configured timeout.

    A slow REST endpoint surfaces as a typed timeout result (``resolved=False``, ``error``
    set) rather than hanging the orchestrator — the standard path proceeds without context
    and notes it (docs/01-architecture.md §4).
    """
    config = config or ToolsConfig.from_env()
    adapter = adapter or create_patient_context_adapter(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            adapter.get_context(patient_ref, correlation_id=correlation_id),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "patient_context ref=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            patient_ref, correlation_id, elapsed_ms,
        )
        return PatientContextResult(
            patient_ref=patient_ref, resolved=False, mrn=None, name=None,
            age=None, sex=None, location=None, code_status=None,
            source_system=getattr(adapter, "source_system", "unknown"),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Patient context lookup timed out after {config.timeout_ms}ms.",
        )


# --- MCP server factory ------------------------------------------------------
def build_server(config: ToolsConfig | None = None):
    """Build a FastMCP server exposing the ``patient_context`` tool.

    Returned so both local dev (stdio via ``python -m src.tools patient_context``) and the
    Foundry hosted-agent registration (attach the server endpoint as a hosted MCP tool)
    can mount the same tool.
    """
    from mcp.server.fastmcp import FastMCP  # noqa: PLC0415  (pinned mcp==1.28.1)

    config = config or ToolsConfig.from_env()
    mcp = FastMCP(
        "Nightingale Patient Context",
        instructions=(
            "Look up a patient by reference (bed/room, MRN) and return identity, key "
            "demographics, code status, allergies, and the assigned care team. Read-only: "
            "this retrieves the record; it makes no clinical decision and changes nothing."
        ),
    )

    @mcp.tool(name=TOOL_NAME)
    async def patient_context(patient_ref: str, correlation_id: str = "") -> str:
        """Retrieve a patient's context and care team (read-only).

        Args:
            patient_ref: Patient reference — bed/room (e.g. "bed 12", "room 4") or MRN.
            correlation_id: Optional envelope correlation id for tracing.

        Returns:
            JSON ``PatientContextResult``: mrn, name, age, sex, location, code_status,
            allergies, and a ``care_team`` array (role, name, provider_id, contact), plus
            source_system and elapsed_ms. If not found/timed out, ``resolved`` is false
            and ``error`` is set.
        """
        result = await get_patient_context(
            patient_ref, correlation_id=correlation_id or None, config=config
        )
        return json.dumps(result.to_dict(), indent=2, default=str)

    return mcp


def main() -> None:  # pragma: no cover - process entrypoint
    """Run the patient context MCP server over stdio (local dev)."""
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    build_server().run()
