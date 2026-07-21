"""L4b Knowledge: sepsis **hour-1 bundle** retrieval (Foundry IQ, mock-backed locally).

docs/01-architecture.md §6: a **Foundry IQ** knowledge base over Azure AI Search gives
agents citation-backed protocol answers. The knowledge base is exposed to a hosted agent as
an MCP tool named ``knowledge_base_retrieve`` at
``{search_endpoint}/knowledgebases/{kb}/mcp?api-version=2026-05-01-preview`` and attached via a
RemoteTool project connection (verified against
https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect, fetched 2026-07-20;
azure-ai-projects>=2.0.0 — repo pins 2.3.0). See src/agents/sepsis.py
:func:`sepsis_hosted_agent_spec` for the hosted wiring.

Locally this module returns the **Surviving Sepsis Campaign Hour-1 Bundle** with source
citations from a mock, so the clinical sepsis agent can ground + cite with **no Azure**. The
real Foundry IQ provider is a config flip (``TOOLS_USE_REAL_ADAPTER``) that calls the
``knowledge_base_retrieve`` MCP tool instead.

SAFETY (docs/01-architecture.md, copilot-instructions.md): retrieval returns cited protocol
text only — it makes no clinical decision and orders nothing. Clinical agents must cite these
sources, keep a human in the loop, and never take autonomous action or override an alarm.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

from config import ToolsConfig

from .protocol_documents import HOUR1_ELEMENTS, SIRS_QSOFA_BCEHS, SSC_HOUR1_2018

logger = logging.getLogger("nightingale.knowledge.sepsis")

KNOWLEDGE_TOOL = "knowledge_base_retrieve"  # the MCP tool a Foundry IQ KB exposes


# --- Typed interface (identical for mock and real Foundry IQ provider) --------
@dataclass(frozen=True)
class ProtocolCitation:
    """A cited source backing a protocol step (Foundry IQ returns source references)."""

    source_id: str
    title: str
    snippet: str
    url: str | None = None  # search-index KB sources fall back to the KB MCP endpoint


@dataclass(frozen=True)
class ProtocolStep:
    """One hour-1 bundle step, tied to the source that grounds it."""

    order: int
    category: str  # "diagnostic" | "treatment"
    text: str
    source_id: str
    time_target: str = "within 1 hour"


@dataclass(frozen=True)
class ProtocolResult:
    """Typed output of a protocol retrieval (read-only; no clinical decision here)."""

    query: str
    grounded: bool  # True = retrieved live from the KB; False = cached/degraded fallback
    steps: list[ProtocolStep]
    citations: list[ProtocolCitation]
    source_system: str
    elapsed_ms: float
    correlation_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class SepsisKnowledgeProvider(Protocol):
    """Interface a real Foundry IQ provider must implement (same signature as the mock)."""

    source_system: str

    async def retrieve(
        self, query: str, *, correlation_id: str | None = None
    ) -> ProtocolResult:
        """Retrieve the sepsis hour-1 protocol for ``query`` with citations."""
        ...


# --- Canonical hour-1 bundle content, sourced from the KB seed corpus ---------
# Built from src/knowledge/protocol_documents.py so the local mock provider and the Azure AI
# Search index (the production Foundry IQ KB) cite identical content (SSC-HR1-2018; see the
# clinical-accuracy note there — # TODO: confirm against SSC 2021).
_SSC = ProtocolCitation(
    source_id=SSC_HOUR1_2018.source_id,
    title=SSC_HOUR1_2018.title,
    snippet=(
        "Begin the hour-1 bundle immediately on recognition: measure lactate; obtain blood "
        "cultures before antibiotics; give broad-spectrum antibiotics; begin rapid 30 mL/kg "
        "crystalloid for hypotension or lactate >=4 mmol/L; apply vasopressors for persistent "
        "hypotension to keep MAP >=65 mmHg."
    ),
    url=SSC_HOUR1_2018.url,
)
_QSOFA = ProtocolCitation(
    source_id=SIRS_QSOFA_BCEHS.source_id,
    title=SIRS_QSOFA_BCEHS.title,
    snippet=(
        "Suspect sepsis with >=2 of: respiratory rate >=22/min, systolic BP <=100 mmHg, "
        "altered mentation (qSOFA); or SIRS >=2 plus suspected/known new infection. A positive "
        "screen warrants escalation and clinician assessment."
    ),
    url=SIRS_QSOFA_BCEHS.url,
)

_HOUR1_STEPS: tuple[ProtocolStep, ...] = tuple(
    ProtocolStep(order, category, text, SSC_HOUR1_2018.source_id)
    for order, category, text in HOUR1_ELEMENTS
)


def fallback_sepsis_protocol(
    query: str = "sepsis hour-1 bundle", *, correlation_id: str | None = None
) -> ProtocolResult:
    """Cached hour-1 bundle used when live retrieval is unavailable (docs §4 breach rule).

    ``grounded=False`` so callers can flag the answer as cached/unverified. It still carries
    the same steps + citation, so a clinical agent degrades gracefully without fabricating.
    """
    return ProtocolResult(
        query=query, grounded=False, steps=list(_HOUR1_STEPS),
        citations=[_SSC, _QSOFA], source_system="CACHED (local hour-1 summary)",
        elapsed_ms=0.0, correlation_id=correlation_id,
    )


# --- Mock provider (realistic latency; stands in for Foundry IQ locally) ------
class MockSepsisKnowledgeProvider:
    """Simulates Foundry IQ returning the cited hour-1 bundle, with realistic latency."""

    def __init__(self, config: ToolsConfig | None = None) -> None:
        self._config = config or ToolsConfig.from_env()
        self.source_system = "MOCK (Foundry IQ — Sepsis Protocol KB)"

    async def _simulate_latency(self) -> None:
        base = self._config.mock_latency_ms
        jitter = random.uniform(0, self._config.mock_jitter_ms)
        await asyncio.sleep((base + jitter) / 1000.0)

    async def retrieve(
        self, query: str, *, correlation_id: str | None = None
    ) -> ProtocolResult:
        t0 = time.perf_counter()
        if not query.strip():
            return ProtocolResult(
                query=query, grounded=False, steps=[], citations=[],
                source_system=self.source_system,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
                correlation_id=correlation_id, error="query is required.",
            )
        await self._simulate_latency()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info(
            "sepsis KB retrieve query=%r grounded=True correlation_id=%s elapsed_ms=%s",
            query, correlation_id, elapsed_ms,
        )
        return ProtocolResult(
            query=query, grounded=True, steps=list(_HOUR1_STEPS),
            citations=[_SSC, _QSOFA], source_system=self.source_system,
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
        )


# --- Factory (mock now; real Foundry IQ is a config flip) ---------------------
def create_knowledge_provider(
    config: ToolsConfig | None = None,
) -> SepsisKnowledgeProvider:
    """Return the sepsis knowledge provider clinical agents attach to.

    Today this is the mock. When ``TOOLS_USE_REAL_ADAPTER=true`` a real Foundry IQ provider
    (same signature) that calls the ``knowledge_base_retrieve`` MCP tool is returned — no
    caller change.
    """
    config = config or ToolsConfig.from_env()
    if config.use_real_adapter:
        # Real provider: call the Foundry IQ knowledge base's knowledge_base_retrieve MCP
        # tool and map its cited results into ProtocolResult. Fail loudly until wired.
        raise NotImplementedError(
            "Real Foundry IQ sepsis provider not wired yet; unset TOOLS_USE_REAL_ADAPTER."
        )
    return MockSepsisKnowledgeProvider(config)


async def retrieve_sepsis_protocol(
    query: str = "sepsis hour-1 bundle",
    *,
    correlation_id: str | None = None,
    config: ToolsConfig | None = None,
    provider: SepsisKnowledgeProvider | None = None,
) -> ProtocolResult:
    """Retrieve the hour-1 protocol, bounded by the configured timeout.

    A slow or failing knowledge base surfaces as a degraded result (``grounded=False``,
    ``error`` set) rather than hanging — the clinical agent then falls back to the cached
    hour-1 summary and flags it as unverified (docs/01-architecture.md §4).
    """
    config = config or ToolsConfig.from_env()
    provider = provider or create_knowledge_provider(config)
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            provider.retrieve(query, correlation_id=correlation_id),
            timeout=config.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "sepsis KB retrieve query=%r TIMEOUT correlation_id=%s elapsed_ms=%s",
            query, correlation_id, elapsed_ms,
        )
        return ProtocolResult(
            query=query, grounded=False, steps=[], citations=[],
            source_system=getattr(provider, "source_system", "unknown"),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id,
            error=f"Knowledge retrieval timed out after {config.timeout_ms}ms.",
        )
    except Exception as exc:  # degrade, never crash the clinical path
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning(
            "sepsis KB retrieve query=%r ERROR=%s correlation_id=%s", query, exc, correlation_id
        )
        return ProtocolResult(
            query=query, grounded=False, steps=[], citations=[],
            source_system=getattr(provider, "source_system", "unknown"),
            elapsed_ms=elapsed_ms, correlation_id=correlation_id, error=str(exc),
        )
