"""Ingest sepsis protocol documents into an Azure AI Search index + Foundry IQ KB plan.

docs/01-architecture.md §6, foundry-iq-knowledge.prompt.md: index the seed corpus
(src/knowledge/protocol_documents.py) into an Azure AI Search index (reusing the existing
service from ``config.SearchConfig``), then expose it as a **Foundry IQ knowledge base** and
connect that KB to the sepsis agent as the ``knowledge_base_retrieve`` MCP tool.

Three stages:
1. **Index ingestion (executed for real):** create-or-update the index (now with a
   **semantic configuration**, required for agentic retrieval) and upsert the docs over the
   **stable** Azure AI Search REST API (``api-version`` from ``SearchConfig``, default
   2024-07-01). Idempotent — PUT index is create-or-update; docs use the ``mergeOrUpload``
   action keyed on the stable ``id``, so re-running is safe.
2. **Foundry IQ knowledge base (executed for real with --with-kb):** create-or-update a
   ``searchIndex`` **knowledge source** over the index, then a **knowledge base** with
   ``outputMode=answerSynthesis`` grounded by an Azure OpenAI model. These use the agentic
   retrieval REST API (``api-version=2026-05-01-preview``), verified against
   https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-create-knowledge-base
   and .../agentic-knowledge-source-how-to-search-index (fetched 2026-07-21). The model block
   carries **no apiKey**, so answer synthesis uses the search service's **managed identity**
   (granted ``Cognitive Services User`` on the Foundry account). Inbound REST here uses the
   admin **api-key** (the shared service is api-key-only for inbound data-plane auth).
3. **Foundry RemoteTool project connection (printed as a plan):** binding the KB's MCP
   endpoint to the sepsis agent uses the ARM connection API
   (``api-version=2025-10-01-preview``); emitted as a plan for the register step to apply.

Usage:
    python -m src.knowledge.ingest --index sepsis-protocols --dry-run   # print, no Azure
    python -m src.knowledge.ingest --index sepsis-protocols             # index ingest only
    python -m src.knowledge.ingest --index sepsis-protocols --with-kb   # index + Foundry IQ KB

SDKs: no new dependency — uses ``httpx`` (already pinned >=0.27.0), matching the existing
SIRE search client. ``azure-ai-projects==2.3.0`` is the SDK for the (planned) KB connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from config import SearchConfig
from src.knowledge.protocol_documents import ProtocolDocument, all_documents

logger = logging.getLogger("nightingale.knowledge.ingest")

# Preview API versions for the Foundry IQ KB + Foundry project connection (see module docstring).
_KB_MCP_API_VERSION = "2026-05-01-preview"
_CONNECTION_API_VERSION = "2025-10-01-preview"
# Agentic-retrieval REST API version for knowledge sources + knowledge bases (answerSynthesis is
# a 2026-05-01-preview feature). See agentic-retrieval-how-to-create-knowledge-base (fetched 2026-07-21).
_AGENTIC_API_VERSION = "2026-05-01-preview"
# Named semantic configuration on the index (required by agentic retrieval; referenced by the KB source).
_SEMANTIC_CONFIG_NAME = "sepsis-semantic"
# Index fields returned as grounding + citation data by the knowledge source at retrieve time.
_SOURCE_DATA_FIELDS = (
    "id", "title", "content", "source_id", "source_title",
    "source_url", "guideline_version", "protocol", "section",
)


def index_schema(index_name: str) -> dict:
    """Azure AI Search index schema for protocol chunks (citation fields included).

    ``id`` is the key (stable → idempotent upserts). ``protocol``/``section`` are filterable
    facets so more protocols share one index. ``source_*``/``guideline_version`` carry the
    citation on every document.
    """
    return {
        "name": index_name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "protocol", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "section", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "title", "type": "Edm.String", "searchable": True},
            {"name": "content", "type": "Edm.String", "searchable": True},
            {"name": "source_id", "type": "Edm.String", "filterable": True},
            {"name": "source_title", "type": "Edm.String", "searchable": True},
            {"name": "source_url", "type": "Edm.String"},
            {"name": "guideline_version", "type": "Edm.String", "filterable": True},
            {"name": "keywords", "type": "Collection(Edm.String)", "searchable": True},
        ],
        # Agentic retrieval requires a semantic configuration (L2 reranking over the chunks).
        "semantic": {
            "defaultConfiguration": _SEMANTIC_CONFIG_NAME,
            "configurations": [
                {
                    "name": _SEMANTIC_CONFIG_NAME,
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [{"fieldName": "keywords"}],
                    },
                }
            ],
        },
    }


def knowledge_source_schema(ks_name: str, index_name: str) -> dict:
    """``searchIndex`` knowledge source over the protocol index (agentic retrieval).

    ``sourceDataFields`` are the fields surfaced as grounding data + citations at retrieve time.
    """
    return {
        "name": ks_name,
        "kind": "searchIndex",
        "description": (
            "Sepsis clinical protocol chunks (hour-1 bundle, SIRS/qSOFA screening) with "
            "per-chunk citations to the source guideline."
        ),
        "searchIndexParameters": {
            "searchIndexName": index_name,
            "semanticConfigurationName": _SEMANTIC_CONFIG_NAME,
            "sourceDataFields": [{"name": name} for name in _SOURCE_DATA_FIELDS],
        },
    }


def knowledge_base_schema(
    kb_name: str,
    ks_name: str,
    *,
    model_endpoint: str,
    model_deployment: str,
    model_name: str,
) -> dict:
    """Foundry IQ knowledge base over the source with citation-backed answer synthesis.

    The model block carries **no apiKey** on purpose: answer synthesis authenticates to Azure
    OpenAI with the search service's **managed identity** (``Cognitive Services User`` on the
    Foundry account). ``retrievalReasoningEffort=low`` keeps retrieval latency down.
    """
    return {
        "name": kb_name,
        "description": (
            "Foundry IQ knowledge base over the sepsis protocol index; synthesizes "
            "citation-backed answers for the nightingale-sepsis agent."
        ),
        "outputMode": "answerSynthesis",
        "knowledgeSources": [{"name": ks_name}],
        "models": [
            {
                "kind": "azureOpenAI",
                "azureOpenAIParameters": {
                    "resourceUri": model_endpoint.rstrip("/"),
                    "deploymentId": model_deployment,
                    "modelName": model_name,
                },
            }
        ],
        "retrievalReasoningEffort": {"kind": "low"},
    }


def upload_payload(documents: tuple[ProtocolDocument, ...]) -> dict:
    """Build the docs/index payload using the idempotent ``mergeOrUpload`` action."""
    return {
        "value": [
            {"@search.action": "mergeOrUpload", **doc.to_search_document()}
            for doc in documents
        ]
    }


def kb_connection_plan(
    *,
    search_endpoint: str,
    kb_name: str,
    index_name: str,
    project_endpoint: str | None,
    connection_name: str,
) -> dict:
    """Documented plan to build the Foundry IQ KB over the index + connect it to the agent.

    Mirrors the foundry-iq-connect doc: a knowledge source over the index, a knowledge base,
    a RemoteTool/ProjectManagedIdentity project connection targeting the KB's MCP endpoint,
    and the agent's ``knowledge_base_retrieve`` MCP tool bound to that connection.
    """
    mcp_endpoint = (
        f"{search_endpoint.rstrip('/')}/knowledgebases/{kb_name}/mcp"
        f"?api-version={_KB_MCP_API_VERSION}"
    )
    return {
        "action": "create_foundry_iq_knowledge_base",
        "knowledge_source": {
            "over_index": index_name,
            "service": search_endpoint or "<AZURE_SEARCH_ENDPOINT unset>",
            "note": "Create a knowledge source over the index in Azure AI Search (agentic retrieval).",
        },
        "knowledge_base": {
            "name": kb_name,
            "mcp_endpoint": mcp_endpoint,
            "exposes_tool": "knowledge_base_retrieve",
        },
        "foundry_project_connection": {
            "name": connection_name,
            "arm_api_version": _CONNECTION_API_VERSION,
            "properties": {
                "authType": "ProjectManagedIdentity",
                "category": "RemoteTool",
                "target": mcp_endpoint,
                "isSharedToAll": True,
                "audience": "https://search.azure.com/",
                "metadata": {"ApiType": "Azure"},
            },
        },
        "agent_tool": {
            "server_label": "nightingale_sepsis_kb",
            "server_url": mcp_endpoint,
            "require_approval": "never",
            "allowed_tools": ["knowledge_base_retrieve"],
            "project_connection_id": connection_name,
            "attached_to_agent": "nightingale-sepsis",
        },
        "project_endpoint": project_endpoint or "<FOUNDRY_PROJECT_ENDPOINT unset>",
        "auth": "DefaultAzureCredential",
    }


class SepsisKnowledgeIngestor:
    """Idempotent ingester for protocol documents into an Azure AI Search index (REST)."""

    def __init__(self, cfg: SearchConfig, index_name: str) -> None:
        self._cfg = cfg
        self._index = index_name
        self._base = cfg.endpoint.rstrip("/")
        self._headers = {"api-key": cfg.api_key, "Content-Type": "application/json"}

    async def ensure_index(self, client) -> None:
        """Create-or-update the index (PUT is idempotent on Azure AI Search)."""
        url = f"{self._base}/indexes/{self._index}?api-version={self._cfg.api_version}"
        resp = await client.put(url, headers=self._headers, json=index_schema(self._index))
        resp.raise_for_status()
        logger.info("index %r ensured (status=%s)", self._index, resp.status_code)

    async def upload(self, client, documents: tuple[ProtocolDocument, ...]) -> int:
        """Upsert documents via the ``mergeOrUpload`` action (idempotent, re-runnable)."""
        url = f"{self._base}/indexes/{self._index}/docs/index?api-version={self._cfg.api_version}"
        resp = await client.post(url, headers=self._headers, json=upload_payload(documents))
        resp.raise_for_status()
        results = resp.json().get("value", [])
        failed = [r for r in results if not r.get("status", True)]
        if failed:
            raise RuntimeError(f"{len(failed)} document(s) failed to index: {failed}")
        logger.info("upserted %d document(s) into %r", len(documents), self._index)
        return len(documents)

    async def ensure_knowledge_source(self, client, ks_name: str) -> None:
        """Create-or-update the ``searchIndex`` knowledge source (PUT is idempotent)."""
        url = f"{self._base}/knowledgesources/{ks_name}?api-version={_AGENTIC_API_VERSION}"
        resp = await client.put(
            url, headers=self._headers, json=knowledge_source_schema(ks_name, self._index)
        )
        resp.raise_for_status()
        logger.info("knowledge source %r ensured (status=%s)", ks_name, resp.status_code)

    async def ensure_knowledge_base(
        self,
        client,
        kb_name: str,
        ks_name: str,
        *,
        model_endpoint: str,
        model_deployment: str,
        model_name: str,
    ) -> None:
        """Create-or-update the knowledge base with answer synthesis (PUT is idempotent)."""
        url = f"{self._base}/knowledgebases/{kb_name}?api-version={_AGENTIC_API_VERSION}"
        resp = await client.put(
            url,
            headers=self._headers,
            json=knowledge_base_schema(
                kb_name,
                ks_name,
                model_endpoint=model_endpoint,
                model_deployment=model_deployment,
                model_name=model_name,
            ),
        )
        resp.raise_for_status()
        logger.info("knowledge base %r ensured (status=%s)", kb_name, resp.status_code)

    async def run(
        self, documents: tuple[ProtocolDocument, ...], *, kb: dict | None = None
    ) -> int:
        """Ensure the index then upsert all documents; optionally build the Foundry IQ KB.

        When ``kb`` is provided it must carry ``ks_name``, ``kb_name``, ``model_endpoint``,
        ``model_deployment`` and ``model_name``; the knowledge source + knowledge base are
        then created-or-updated after the docs are indexed.
        """
        import httpx  # noqa: PLC0415  (httpx>=0.27.0, already a dependency)

        async with httpx.AsyncClient(timeout=60.0) as client:
            await self.ensure_index(client)
            count = await self.upload(client, documents)
            if kb is not None:
                await self.ensure_knowledge_source(client, kb["ks_name"])
                await self.ensure_knowledge_base(
                    client,
                    kb["kb_name"],
                    kb["ks_name"],
                    model_endpoint=kb["model_endpoint"],
                    model_deployment=kb["model_deployment"],
                    model_name=kb["model_name"],
                )
            return count


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Ingest sepsis protocol docs into Azure AI Search + print the Foundry IQ KB plan."
    )
    parser.add_argument("--index", default="sepsis-protocols", help="Azure AI Search index name.")
    parser.add_argument(
        "--kb-name", default=None,
        help="Foundry IQ knowledge base name (default: <index>-kb).",
    )
    parser.add_argument(
        "--ks-name", default=None,
        help="Foundry IQ knowledge source name (default: <index>-ks).",
    )
    parser.add_argument(
        "--with-kb", action="store_true",
        help="Also create-or-update the Foundry IQ knowledge source + knowledge base.",
    )
    parser.add_argument(
        "--model-endpoint",
        default=os.getenv("FOUNDRY_OPENAI_ENDPOINT") or os.getenv("AZURE_OPENAI_ENDPOINT", ""),
        help="Azure OpenAI resource URI for KB answer synthesis (env FOUNDRY_OPENAI_ENDPOINT).",
    )
    parser.add_argument(
        "--model-deployment",
        default=os.getenv("FOUNDRY_MODEL_NAME", "gpt-5-mini"),
        help="Azure OpenAI deployment id for KB answer synthesis (env FOUNDRY_MODEL_NAME).",
    )
    parser.add_argument(
        "--model-name", default=None,
        help="Azure OpenAI model name for KB answer synthesis (default: --model-deployment).",
    )
    parser.add_argument(
        "--connection-name",
        default=os.getenv("FOUNDRY_IQ_KB_CONNECTION", "nightingale-sepsis-kb-connection"),
        help="Foundry project connection name for the KB MCP tool.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the index schema, documents, and KB plan without calling Azure.",
    )
    args = parser.parse_args(argv)

    documents = all_documents()
    kb_name = args.kb_name or f"{args.index}-kb"
    ks_name = args.ks_name or f"{args.index}-ks"
    model_name = args.model_name or args.model_deployment
    search_endpoint = os.getenv("AZURE_SEARCH_ENDPOINT", "")
    project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or None
    plan = kb_connection_plan(
        search_endpoint=search_endpoint, kb_name=kb_name, index_name=args.index,
        project_endpoint=project_endpoint, connection_name=args.connection_name,
    )

    if args.dry_run:
        print(json.dumps({
            "index_schema": index_schema(args.index),
            "documents": [d.to_search_document() for d in documents],
            "knowledge_source_schema": knowledge_source_schema(ks_name, args.index),
            "knowledge_base_schema": knowledge_base_schema(
                kb_name, ks_name,
                model_endpoint=args.model_endpoint or "<FOUNDRY_OPENAI_ENDPOINT unset>",
                model_deployment=args.model_deployment, model_name=model_name,
            ),
            "foundry_iq_plan": plan,
        }, indent=2))
        print(
            f"\n[dry-run] Would upsert {len(documents)} document(s) into index "
            f"'{args.index}', then create Foundry IQ knowledge source '{ks_name}' + "
            f"knowledge base '{kb_name}' and connect it to the nightingale-sepsis agent as "
            "knowledge_base_retrieve."
        )
        return

    cfg = SearchConfig.from_env()  # requires AZURE_SEARCH_ENDPOINT + AZURE_SEARCH_API_KEY
    ingestor = SepsisKnowledgeIngestor(cfg, args.index)
    kb = None
    if args.with_kb:
        if not args.model_endpoint:
            parser.error(
                "--with-kb requires a model endpoint: set FOUNDRY_OPENAI_ENDPOINT (or "
                "AZURE_OPENAI_ENDPOINT) or pass --model-endpoint."
            )
        kb = {
            "ks_name": ks_name, "kb_name": kb_name,
            "model_endpoint": args.model_endpoint,
            "model_deployment": args.model_deployment, "model_name": model_name,
        }
    count = asyncio.run(ingestor.run(documents, kb=kb))
    print(f"Ingested {count} document(s) into '{args.index}'.")
    if kb is not None:
        kb_mcp = (
            f"{cfg.endpoint.rstrip('/')}/knowledgebases/{kb_name}/mcp"
            f"?api-version={_KB_MCP_API_VERSION}"
        )
        print(
            f"Created Foundry IQ knowledge source '{ks_name}' + knowledge base '{kb_name}'.\n"
            f"KB MCP endpoint: {kb_mcp}"
        )
    print(json.dumps(plan, indent=2))
    print(
        "\nIndex ingested" + (" and Foundry IQ KB created" if kb else "") + ". The Foundry "
        "RemoteTool project connection above uses a preview ARM API and is printed as a plan "
        "— apply it to bind the KB to the sepsis agent (or run: python -m src.agents.register "
        "--capability sepsis)."
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    main()
