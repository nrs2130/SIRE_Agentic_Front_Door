"""Seed corpus for the Foundry IQ **sepsis-protocols** knowledge base.

Single source of truth for the grounded protocol content (docs/01-architecture.md §6,
docs/02-stryker-workload-catalog.md Part D). The same documents are:

* indexed into an Azure AI Search index by src/knowledge/ingest.py (the production KB), and
* served by the local mock Foundry IQ provider in src/knowledge/sepsis_protocol.py, so the
  sepsis agent grounds + cites identically with **no Azure** during local dev.

Every chunk carries a **source citation** (:class:`SourceCitation`) so retrieval is
citation-backed. The structure (a per-protocol tuple registered in :data:`PROTOCOLS`) lets you
add more protocols (code blue, RRT, fall) without touching the retriever or the ingester — see
:data:`PROTOCOLS` at the bottom.

CLINICAL ACCURACY (foundry-iq-knowledge.prompt.md step 4): the hour-1 elements below are the
**2018 SSC hour-1 update** (reproduced in a peer-reviewed nursing article). They remain the
standard bedside actions but MUST be confirmed against the current guideline before any
patient-facing clinical use. This content is **decision support, human-in-the-loop** — an agent
must never present it as a medical order.
# TODO: confirm against SSC 2021 (Surviving Sepsis Campaign 2021 guideline) before clinical use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceCitation:
    """A citable source backing a protocol chunk (the citation field on every document)."""

    source_id: str
    title: str
    url: str
    guideline_version: str


# --- Citations (2018 SSC hour-1 update + BCEHS screening scores) --------------
SSC_HOUR1_2018 = SourceCitation(
    source_id="SSC-HR1-2018",
    title="Surviving Sepsis Campaign Hour-1 Bundle (2018 update; Schorr, American Nurse Journal)",
    url="https://www.myamericannurse.com/wp-content/uploads/2018/08/ant9-Sepsis-822a.pdf",
    guideline_version="2018 SSC hour-1 update",
)
SIRS_QSOFA_BCEHS = SourceCitation(
    source_id="SIRS-QSOFA-BCEHS",
    title="SIRS / qSOFA sepsis screening scores (BCEHS clinical scores)",
    url="https://handbook.bcehs.ca/clinical-resources/clinical-scores/sirs-qsofa-sepsis-scores/",
    guideline_version="Sepsis-3 (qSOFA, 2016); SIRS",
)


# --- The 5 hour-1 elements (shared by the KB corpus AND the agent's mock) ------
# (order, category, text). category: "diagnostic" (measure/collect) | "treatment" (give).
HOUR1_ELEMENTS: tuple[tuple[int, str, str], ...] = (
    (1, "diagnostic", "Measure lactate; remeasure if the initial lactate is > 2 mmol/L."),
    (2, "diagnostic", "Obtain blood cultures before antibiotics (do not delay antibiotics if cultures are hard to get)."),
    (3, "treatment", "Administer broad-spectrum antibiotics."),
    (4, "treatment", "Begin rapid crystalloid — 30 mL/kg for hypotension or lactate >= 4 mmol/L."),
    (5, "treatment", "Apply vasopressors if hypotensive during/after fluids to maintain MAP >= 65 mmHg."),
)


@dataclass(frozen=True)
class ProtocolDocument:
    """One retrievable, citation-carrying chunk (a row in the Azure AI Search index)."""

    id: str  # stable key — makes re-ingestion idempotent (mergeOrUpload on this id)
    protocol: str  # e.g. "sepsis" — filter/group facet; new protocols reuse the schema
    section: str  # e.g. "hour1_bundle" | "screening"
    title: str
    content: str
    source: SourceCitation
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def to_search_document(self) -> dict:
        """Flatten to the Azure AI Search document shape (citation fields inlined).

        Field names match the index schema in src/knowledge/ingest.py. The citation lives in
        ``source_id`` / ``source_title`` / ``source_url`` / ``guideline_version`` so every hit
        is attributable.
        """
        return {
            "id": self.id,
            "protocol": self.protocol,
            "section": self.section,
            "title": self.title,
            "content": self.content,
            "source_id": self.source.source_id,
            "source_title": self.source.title,
            "source_url": self.source.url,
            "guideline_version": self.source.guideline_version,
            "keywords": list(self.keywords),
        }


def _bundle_overview() -> str:
    lines = ["Surviving Sepsis Campaign Hour-1 Bundle — begin immediately, even if some steps take longer than an hour:"]
    lines += [f"{order}. {text}" for order, _cat, text in HOUR1_ELEMENTS]
    return "\n".join(lines)


# --- Sepsis seed documents (hour-1 bundle + SIRS/qSOFA screening) -------------
SEPSIS_DOCUMENTS: tuple[ProtocolDocument, ...] = (
    ProtocolDocument(
        id="sepsis-hour1-bundle",
        protocol="sepsis",
        section="hour1_bundle",
        title="Sepsis Hour-1 Bundle (5 elements)",
        content=_bundle_overview(),
        source=SSC_HOUR1_2018,
        keywords=("sepsis", "hour-1", "hour 1", "bundle", "hour1", "5 elements", "lactate",
                  "blood cultures", "antibiotics", "crystalloid", "fluids", "vasopressors"),
    ),
    ProtocolDocument(
        id="sepsis-hour1-1-lactate", protocol="sepsis", section="hour1_bundle",
        title="Hour-1 element 1 — Measure lactate",
        content="Measure lactate. Remeasure lactate if the initial value is greater than 2 mmol/L.",
        source=SSC_HOUR1_2018, keywords=("lactate", "remeasure", "hour-1", "sepsis"),
    ),
    ProtocolDocument(
        id="sepsis-hour1-2-cultures", protocol="sepsis", section="hour1_bundle",
        title="Hour-1 element 2 — Blood cultures before antibiotics",
        content="Obtain blood cultures before administering antibiotics. Do not delay antibiotics if cultures are difficult to obtain.",
        source=SSC_HOUR1_2018, keywords=("blood cultures", "cultures", "antibiotics", "hour-1", "sepsis"),
    ),
    ProtocolDocument(
        id="sepsis-hour1-3-antibiotics", protocol="sepsis", section="hour1_bundle",
        title="Hour-1 element 3 — Broad-spectrum antibiotics",
        content="Administer broad-spectrum antibiotics.",
        source=SSC_HOUR1_2018, keywords=("antibiotics", "broad-spectrum", "hour-1", "sepsis"),
    ),
    ProtocolDocument(
        id="sepsis-hour1-4-crystalloid", protocol="sepsis", section="hour1_bundle",
        title="Hour-1 element 4 — Rapid crystalloid",
        content="Begin rapid crystalloid: 30 mL/kg for hypotension or for a lactate >= 4 mmol/L.",
        source=SSC_HOUR1_2018, keywords=("crystalloid", "fluids", "30 mL/kg", "hypotension", "hour-1", "sepsis"),
    ),
    ProtocolDocument(
        id="sepsis-hour1-5-vasopressors", protocol="sepsis", section="hour1_bundle",
        title="Hour-1 element 5 — Vasopressors",
        content="Apply vasopressors if the patient is hypotensive during or after fluid resuscitation, to maintain a mean arterial pressure (MAP) >= 65 mmHg.",
        source=SSC_HOUR1_2018, keywords=("vasopressors", "MAP", "hypotension", "hour-1", "sepsis"),
    ),
    ProtocolDocument(
        id="sepsis-screening-suspicion",
        protocol="sepsis",
        section="screening",
        title="Suspicion of sepsis — bedside screening (SIRS and qSOFA)",
        content=(
            "Suspicion of sepsis is triggered by a positive bedside screen in a patient with "
            "suspected or known new infection. Two standard screens are used: SIRS (sensitive) "
            "and qSOFA (Sepsis-3, specific, no labs). A positive screen launches the hour-1 bundle."
        ),
        source=SIRS_QSOFA_BCEHS,
        keywords=("suspicion of sepsis", "suspicion", "screening", "screen", "sirs", "qsofa", "sepsis"),
    ),
    ProtocolDocument(
        id="sepsis-screening-sirs",
        protocol="sepsis",
        section="screening",
        title="SIRS screening criteria (sensitive)",
        content=(
            "SIRS (Systemic Inflammatory Response Syndrome), sensitive: >= 2 of — temperature "
            "> 38.3 C or < 36 C, heart rate > 90, respiratory rate > 20, WBC > 12k or < 4k. "
            "SIRS criteria plus a suspected or known new infection indicate sepsis."
        ),
        source=SIRS_QSOFA_BCEHS,
        keywords=("sirs", "screening", "suspicion", "temperature", "heart rate", "respiratory rate", "wbc", "sepsis"),
    ),
    ProtocolDocument(
        id="sepsis-screening-qsofa",
        protocol="sepsis",
        section="screening",
        title="qSOFA screening criteria (Sepsis-3, specific)",
        content=(
            "qSOFA (quick SOFA, Sepsis-3), specific and requires no labs: >= 2 of — respiratory "
            "rate >= 22/min, systolic blood pressure <= 100 mmHg, altered mental status. Designed "
            "to flag high-risk infected patients outside the ICU."
        ),
        source=SIRS_QSOFA_BCEHS,
        keywords=("qsofa", "screening", "suspicion", "respiratory rate", "systolic", "altered mental status", "sepsis"),
    ),
)


# --- Protocol registry — add the next protocol here ---------------------------
# To add code blue / RRT / fall: define its SourceCitation + a tuple of ProtocolDocument
# (reuse the same fields/schema) and register it below. ingest.py and retrieve_local pick it
# up automatically; the index schema does not change (protocol is just a filterable facet).
PROTOCOLS: dict[str, tuple[ProtocolDocument, ...]] = {
    "sepsis": SEPSIS_DOCUMENTS,
}


def all_documents() -> tuple[ProtocolDocument, ...]:
    """Every seed document across all registered protocols (what ingest.py indexes)."""
    docs: list[ProtocolDocument] = []
    for protocol_docs in PROTOCOLS.values():
        docs.extend(protocol_docs)
    return tuple(docs)


_STOPWORDS = frozenset(
    {"what", "is", "the", "a", "an", "of", "to", "for", "and", "or", "as",
     "qualifies", "in", "on", "does", "do", "with", "how", "are", "be", "that"}
)


def _terms(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1 and t not in _STOPWORDS]


def retrieve_local(
    query: str,
    documents: tuple[ProtocolDocument, ...] | None = None,
    *,
    top: int = 3,
) -> list[ProtocolDocument]:
    """Keyword retriever over the seed corpus (stands in for KB retrieval, no Azure).

    Scores each document by query-term overlap (keywords weighted highest, then title, then
    content) and returns the top matches. Used by the retrieval smoke test and by the local
    mock Foundry IQ provider.
    """
    docs = documents if documents is not None else all_documents()
    terms = _terms(query)
    if not terms:
        return []

    scored: list[tuple[float, int, ProtocolDocument]] = []
    for idx, doc in enumerate(docs):
        kw = " ".join(doc.keywords).lower()
        title = doc.title.lower()
        content = doc.content.lower()
        score = 0.0
        for term in terms:
            if term in kw:
                score += 3.0
            if term in title:
                score += 2.0
            if term in content:
                score += 1.0
        if score > 0:
            scored.append((score, idx, doc))  # idx keeps the sort stable/deterministic
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [doc for _score, _idx, doc in scored[:top]]
