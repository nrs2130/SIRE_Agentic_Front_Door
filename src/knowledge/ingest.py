"""Ingest sepsis protocol documents into an Azure AI Search index + Foundry IQ KB plan.

docs/01-architecture.md §6, foundry-iq-knowledge.prompt.md: index the seed corpus
(src/knowledge/protocol_documents.py) into an Azure AI Search index (reusing the existing
service from ``config.SearchConfig``), then expose it as a **Foundry IQ knowledge base** and
connect that KB to the sepsis agent as the ``knowledge_base_retrieve`` MCP tool.

Two stages:
1. **Index ingestion (executed for real):** create-or-update the index and upsert the docs
   over the **stable** Azure AI Search REST API (``api-version`` from ``SearchConfig``,
   default 2024-07-01). Idempotent — PUT index is create-or-update; docs use the
   ``mergeOrUpload`` action keyed on the stable ``id``, so re-running is safe.
2. **Foundry IQ KB + connection (printed as a plan):** the knowledge base and the Foundry
   RemoteTool project connection use **preview** APIs (KB MCP endpoint
   ``api-version=2026-05-01-preview``; ARM connection ``api-version=2025-10-01-preview``),
   verified against
   https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect (fetched
   2026-07-20). To avoid guessing preview SDK symbols, real creation is left as a documented
   plan; the exact REST shapes are emitted so an operator (or a follow-up) can apply them.

Usage:
    python -m src.knowledge.ingest --index sepsis-protocols --dry-run   # print, no Azure
    python -m src.knowledge.ingest --index sepsis-protocols             # real index ingest

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

    async def run(self, documents: tuple[ProtocolDocument, ...]) -> int:
        """Ensure the index then upsert all documents; returns the count uploaded."""
        import httpx  # noqa: PLC0415  (httpx>=0.27.0, already a dependency)

        async with httpx.AsyncClient(timeout=30.0) as client:
            await self.ensure_index(client)
            return await self.upload(client, documents)


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
            "foundry_iq_plan": plan,
        }, indent=2))
        print(
            f"\n[dry-run] Would upsert {len(documents)} document(s) into index "
            f"'{args.index}', then create Foundry IQ KB '{kb_name}' and connect it to "
            "the nightingale-sepsis agent as knowledge_base_retrieve."
        )
        return

    cfg = SearchConfig.from_env()  # requires AZURE_SEARCH_ENDPOINT + AZURE_SEARCH_API_KEY
    ingestor = SepsisKnowledgeIngestor(cfg, args.index)
    count = asyncio.run(ingestor.run(documents))
    print(f"Ingested {count} document(s) into '{args.index}'.")
    print(json.dumps(plan, indent=2))
    print(
        "\nIndex ingested. The Foundry IQ knowledge base + project connection above use "
        "preview APIs and are printed as a plan — apply them to finish connecting the KB to "
        "the sepsis agent (or run: python -m src.agents.register --capability sepsis)."
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO
    )
    main()
