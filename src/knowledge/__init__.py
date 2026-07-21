"""L4b Knowledge: Foundry IQ knowledge bases over Azure AI Search for citation-backed protocol retrieval."""

from .ingest import (
    SepsisKnowledgeIngestor,
    index_schema,
    kb_connection_plan,
    upload_payload,
)
from .protocol_documents import (
    HOUR1_ELEMENTS,
    PROTOCOLS,
    SEPSIS_DOCUMENTS,
    SIRS_QSOFA_BCEHS,
    SSC_HOUR1_2018,
    ProtocolDocument,
    SourceCitation,
    all_documents,
    retrieve_local,
)
from .sepsis_protocol import (
    KNOWLEDGE_TOOL,
    MockSepsisKnowledgeProvider,
    ProtocolCitation,
    ProtocolResult,
    ProtocolStep,
    SepsisKnowledgeProvider,
    create_knowledge_provider,
    fallback_sepsis_protocol,
    retrieve_sepsis_protocol,
)

__all__ = [
    # seed corpus + retriever
    "ProtocolDocument",
    "SourceCitation",
    "SEPSIS_DOCUMENTS",
    "PROTOCOLS",
    "HOUR1_ELEMENTS",
    "SSC_HOUR1_2018",
    "SIRS_QSOFA_BCEHS",
    "all_documents",
    "retrieve_local",
    # ingestion + Foundry IQ KB plan
    "SepsisKnowledgeIngestor",
    "index_schema",
    "upload_payload",
    "kb_connection_plan",
    # runtime provider (mock Foundry IQ + agent-facing retrieval)
    "KNOWLEDGE_TOOL",
    "MockSepsisKnowledgeProvider",
    "ProtocolCitation",
    "ProtocolResult",
    "ProtocolStep",
    "SepsisKnowledgeProvider",
    "create_knowledge_provider",
    "fallback_sepsis_protocol",
    "retrieve_sepsis_protocol",
]
