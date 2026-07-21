"""Foundry IQ knowledge base: retrieval smoke test + idempotent ingestion (no Azure).

Runs against the local seed corpus / retriever (src/knowledge/protocol_documents.py), which is
the same content indexed into Azure AI Search and served by the local mock Foundry IQ provider.
"""

from __future__ import annotations

import json

from config import ToolsConfig
from src.agents.sepsis import SepsisAgent
from src.gateway.intent_envelope import IntentEnvelope, Urgency
from src.knowledge import ingest
from src.knowledge.protocol_documents import (
    SEPSIS_DOCUMENTS,
    SSC_HOUR1_2018,
    all_documents,
    retrieve_local,
)

_FAST = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)

# The 5 hour-1 elements, by a distinctive keyword each.
_HOUR1_MARKERS = ["lactate", "blood cultures", "antibiotics", "crystalloid", "vasopressors"]


def _combined(docs) -> str:
    return " ".join(f"{d.title} {d.content} {' '.join(d.keywords)}" for d in docs).lower()


# --- Retrieval smoke test (prompt step 3) ------------------------------------
def test_hour1_bundle_query_returns_five_elements_with_citation() -> None:
    """'What is the sepsis hour-1 bundle?' returns the 5 elements, each citation-backed."""
    hits = retrieve_local("What is the sepsis hour-1 bundle?", top=3)
    assert hits, "expected retrieval hits for the hour-1 bundle query"

    text = _combined(hits)
    for marker in _HOUR1_MARKERS:
        assert marker in text, f"hour-1 element '{marker}' missing from retrieval"

    # Every hit carries a citation; the bundle is grounded in the 2018 SSC hour-1 update.
    assert all(h.source.source_id and h.source.url for h in hits)
    assert any(h.source.source_id == SSC_HOUR1_2018.source_id for h in hits)
    assert any("2018" in h.source.guideline_version for h in hits)


def test_suspicion_query_returns_sirs_and_qsofa() -> None:
    """'What qualifies as suspicion of sepsis?' returns SIRS and qSOFA, citation-backed."""
    hits = retrieve_local("What qualifies as suspicion of sepsis?", top=3)
    assert hits

    text = _combined(hits)
    assert "sirs" in text
    assert "qsofa" in text
    assert all(h.source.source_id and h.source.url for h in hits)


# --- Citation metadata (acceptance: documents carry citation metadata) --------
def test_every_document_carries_citation_metadata() -> None:
    for doc in all_documents():
        assert doc.source.source_id
        assert doc.source.title
        assert doc.source.url.startswith("http")
        assert doc.source.guideline_version
        # to_search_document() inlines the citation for the index.
        row = doc.to_search_document()
        for field in ("id", "source_id", "source_title", "source_url", "guideline_version"):
            assert row[field], f"{doc.id} missing {field}"


def test_clinical_currency_marked_2018_with_todo() -> None:
    """Guardrail: seeded hour-1 content is marked as the 2018 SSC update (confirm vs 2021)."""
    assert SSC_HOUR1_2018.guideline_version == "2018 SSC hour-1 update"
    # The module documents the confirm-against-2021 TODO.
    import src.knowledge.protocol_documents as pd

    assert "confirm against SSC 2021" in (pd.__doc__ or "")


# --- Ingestion is idempotent + carries citations (acceptance) -----------------
def test_upload_payload_uses_merge_or_upload() -> None:
    """Idempotent, re-runnable: every action is mergeOrUpload keyed on the stable id."""
    payload = ingest.upload_payload(SEPSIS_DOCUMENTS)
    actions = payload["value"]
    assert len(actions) == len(SEPSIS_DOCUMENTS)
    assert all(a["@search.action"] == "mergeOrUpload" for a in actions)
    assert all(a["id"] and a["source_id"] for a in actions)  # keyed + cited
    ids = [a["id"] for a in actions]
    assert len(ids) == len(set(ids)), "document ids must be unique (idempotent upsert)"


def test_index_schema_has_key_and_citation_fields() -> None:
    schema = ingest.index_schema("sepsis-protocols")
    fields = {f["name"]: f for f in schema["fields"]}
    assert fields["id"]["key"] is True
    for cited in ("source_id", "source_title", "source_url", "guideline_version"):
        assert cited in fields


def test_ingest_dry_run_prints_plan_and_touches_no_azure(capsys) -> None:
    """--dry-run prints schema + docs + the Foundry IQ KB connection plan, no Azure calls."""
    ingest.main(["--index", "sepsis-protocols", "--dry-run"])
    out = capsys.readouterr().out
    payload = json.loads(out.split("\n[dry-run]")[0])

    assert payload["index_schema"]["name"] == "sepsis-protocols"
    assert len(payload["documents"]) == len(all_documents())
    plan = payload["foundry_iq_plan"]
    assert plan["knowledge_base"]["exposes_tool"] == "knowledge_base_retrieve"
    assert "2026-05-01-preview" in plan["knowledge_base"]["mcp_endpoint"]
    assert plan["agent_tool"]["attached_to_agent"] == "nightingale-sepsis"
    assert plan["foundry_project_connection"]["properties"]["authType"] == "ProjectManagedIdentity"


# --- Connection to the sepsis agent (prompt step 2) ---------------------------
async def test_sepsis_agent_grounds_and_cites_from_this_corpus() -> None:
    """The sepsis agent's answer is grounded and cites a source from this KB corpus."""
    agent = SepsisAgent(_FAST)
    env = IntentEnvelope.create(
        "sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"}, "utterance"
    )
    result = await agent.handle(env)

    assert result.grounded is True
    assert result.citations, "agent answer must include citations"
    corpus_source_ids = {d.source.source_id for d in all_documents()}
    assert any(c.source_id in corpus_source_ids for c in result.citations)
    assert any(c.source_id == SSC_HOUR1_2018.source_id for c in result.citations)
