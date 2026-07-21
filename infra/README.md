# infra/ — deploy Nightingale to a Microsoft Foundry project

Deployment assets for **Step 10** of [docs/03-copilot-runbook.md](../docs/03-copilot-runbook.md):
publish the hosted agents, attach their **MCP tool endpoints**, and connect the **Foundry IQ**
knowledge base. Everything authenticates with **DefaultAzureCredential** (`az login`).

Verified against the current Microsoft Learn docs (fetched 2026-07-21):
[Foundry Agent Service overview](https://learn.microsoft.com/azure/ai-foundry/agents/overview),
[hosted MCP tools](https://learn.microsoft.com/agent-framework/agents/tools/hosted-mcp-tools),
[Foundry IQ connect](https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect).
The Container Apps hosting pattern (ACR + managed identity + AcrPull) is adapted from the
standard Foundry/azd agent-hosting infrastructure (the `foundry-samples/infrastructure` Bicep
layout) to our layers.

## What gets deployed

| # | Asset | File | Why |
|---|---|---|---|
| 1 | **Azure Container Registry** | `acr.bicep` | Holds the MCP tool image; deployed first so `az acr build` can push into it. |
| 2 | **MCP tool image** | `mcp-tools.Dockerfile` + `requirements-mcp-tools.txt` | One slim image; each tool runs it with `args=[<tool>, --http]` → streamable-HTTP at `/mcp`. |
| 3 | **8 Container Apps** (one per tool) | `main.bicep` | The **hosted MCP tool endpoints** the agents attach to. UAMI + AcrPull, single replica. |
| 4 | **Foundry IQ knowledge base** | `../src/knowledge/ingest.py` | Ingests `sepsis-protocols` into Azure AI Search (idempotent). |
| 5 | **RemoteTool project connection** | `create_kb_connection.ps1` | Lets the sepsis agent call `knowledge_base_retrieve` with the project MI. |
| 6 | **Hosted agents** (comms, sepsis) | `../src/agents/register.py` | Created in Foundry Agent Service (declarative prompt agents + MCP tools). |
| — | **Control Plane guide** | `CONTROL_PLANE.md` | How to view agents / tools / traces in the portal. |

## Why hosted agents here are declarative (prompt) agents

Our `comms`/`sepsis` specs are **model + instructions + MCP tool definitions** (see
`src/agents/hosted.py`). Foundry runs them as declarative agents — no container to build for the
agent itself. Only the **MCP tool servers** need hosting (that's what the Container Apps are
for). `register_hosted_agent()` creates each via the verified
`client.agents.create_version(agent_name=..., definition=PromptAgentDefinition(model, instructions, tools=[MCPTool(...)]))`
pattern (`azure-ai-projects==2.3.0`).

## One-shot deploy

```powershell
az login
# .env must have FOUNDRY_PROJECT_ENDPOINT + AZURE_SEARCH_ENDPOINT + AZURE_SEARCH_API_KEY
./infra/deploy.ps1 -ResourceGroup rg-nightingale -Location eastus
```

`deploy.ps1` runs the steps in order and explains each:

0. **Preflight** — verify `az login`, add the `containerapp` CLI extension, register the
   `Microsoft.App` / `Microsoft.OperationalInsights` providers, confirm `.env`.
1. **`az group create`** — the resource group.
2. **`az deployment group create -f acr.bicep`** — the registry (phase 1).
3. **`az acr build`** — build the tool image in the cloud (no local Docker) and push to ACR.
4. **`az deployment group create -f main.bicep`** — the 8 Container Apps, UAMI + AcrPull, env.
   Prints each `tool -> https://.../mcp` URL and exports them as the `MCP_*` env vars the agent
   specs read. Pass `-WhatIf` to preview this deployment and stop.
5. **`python -m src.knowledge.ingest --index sepsis-protocols`** — idempotent KB ingest.
6. **`create_kb_connection.ps1`** — the RemoteTool project connection (preview API; run
   separately with your project ARM id + search endpoint).
7. **`python -m src.agents.register --capability {comms,sepsis}`** — create the hosted agents
   with their MCP tools attached (DefaultAzureCredential).

## RBAC the deploy assumes

- **You** (the deployer): `Contributor` on the resource group + `Foundry Project Manager` (to
  create the project connection) on the Foundry resource.
- **Container Apps UAMI**: `AcrPull` on the registry (granted by `main.bicep`).
- **Foundry project managed identity**: `Search Index Data Reader` on the Azure AI Search
  service (grant after `create_kb_connection.ps1`, else retrieval 403s).
- **Each hosted agent's instance identity**: `Azure AI User` + `Cognitive Services OpenAI User`
  on the account scope (azd/Foundry grants automatically on create).

## Pinned versions

- Bicep api-versions pinned + commented in each `.bicep` (ACR `2023-07-01`, Container Apps
  `2024-03-01`, managed identity `2023-01-31`, role assignment `2022-04-01`).
- Preview APIs (KB MCP `2026-05-01-preview`, project connection `2025-10-01-preview`) are
  flagged in `create_kb_connection.ps1` — verify before production.
- Python SDKs pinned in `requirements.txt` (`azure-ai-projects==2.3.0`, `azure-identity`,
  `mcp[cli]==1.28.1`, OpenTelemetry `1.43.0`).
- Azure CLI >= 2.60 with the `containerapp` extension.

## Smoke test after deploy

Run one routine and one emergency flow, then check the Foundry portal (**Agents** +
**Tracing**, see [CONTROL_PLANE.md](CONTROL_PLANE.md)). The two hosted agents should appear with
their MCP tool calls, and a single trace should span gateway → orchestrator → agents → tools.
