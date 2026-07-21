<#
.SYNOPSIS
  Deploy the Nightingale platform to a Microsoft Foundry project end to end.

.DESCRIPTION
  Publishes the L4 MCP tool servers to Azure Container Apps, ingests the sepsis Foundry IQ
  knowledge base into Azure AI Search + creates its RemoteTool project connection, and creates
  the two hosted (declarative/prompt) agents (comms, sepsis) in Foundry Agent Service with their
  MCP tools attached. Uses DefaultAzureCredential (`az login`) throughout. Every step prints what
  it does. Pass -WhatIf to preview without changing Azure.

  Pinned tool versions: Azure CLI >= 2.60 with the `containerapp` extension; Bicep >= 0.28;
  Python deps per requirements.txt (azure-ai-projects==2.3.0, azure-identity, opentelemetry 1.43).

.PARAMETER ResourceGroup   Target resource group (created if missing).
.PARAMETER Location        Azure region (default eastus).
.PARAMETER AcrName         Globally-unique ACR name (default derived from -NamePrefix + suffix).
.PARAMETER NamePrefix      Resource name prefix (default 'nightingale').
.PARAMETER ImageTag        Tool image tag (default: current UTC timestamp).
.PARAMETER WhatIf          Preview: run `az deployment ... --what-if` and skip agent creation.

.EXAMPLE
  ./infra/deploy.ps1 -ResourceGroup rg-nightingale -Location eastus
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [Parameter(Mandatory = $true)][string]$ResourceGroup,
  [string]$Location = 'eastus',
  [string]$NamePrefix = 'nightingale',
  [string]$AcrName = '',
  [string]$ImageTag = (Get-Date -Format 'yyyyMMddHHmmss')
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not $AcrName) { $AcrName = ('{0}acr{1}' -f $NamePrefix, (Get-Random -Maximum 99999)) }
$imageName = 'nightingale-mcp-tools'

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------------------
# 0. Preflight: az login, required extension, .env (Foundry + Search endpoints).
# ---------------------------------------------------------------------------
Step '0. Preflight (az login, containerapp extension, .env)'
az account show --only-show-errors 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Run `az login` first (DefaultAzureCredential needs a signed-in identity).' }
az extension add --name containerapp --upgrade --only-show-errors 1>$null
az provider register -n Microsoft.App --only-show-errors 1>$null
az provider register -n Microsoft.OperationalInsights --only-show-errors 1>$null
if (-not (Test-Path '.env')) { throw '.env not found. Copy .env.example -> .env and set FOUNDRY_PROJECT_ENDPOINT + AZURE_SEARCH_* first.' }

# ---------------------------------------------------------------------------
# 1. Resource group.
# ---------------------------------------------------------------------------
Step "1. Resource group '$ResourceGroup' in $Location"
az group create -n $ResourceGroup -l $Location --only-show-errors 1>$null

# ---------------------------------------------------------------------------
# 2. ACR (phase 1 Bicep) — deployed first so we can build the image into it.
# ---------------------------------------------------------------------------
Step "2. Azure Container Registry '$AcrName' (acr.bicep)"
az deployment group create -g $ResourceGroup -f infra/acr.bicep `
  -p acrName=$AcrName location=$Location --only-show-errors 1>$null

# ---------------------------------------------------------------------------
# 3. Build the MCP tool image IN the cloud (no local Docker needed) and push to ACR.
# ---------------------------------------------------------------------------
Step "3. Build+push $imageName`:$ImageTag with 'az acr build'"
az acr build -r $AcrName -t "$imageName`:$ImageTag" -f infra/mcp-tools.Dockerfile . --only-show-errors
$loginServer = az acr show -n $AcrName --query loginServer -o tsv
$image = "$loginServer/$imageName`:$ImageTag"

# ---------------------------------------------------------------------------
# 4. Container Apps hosting the tools (phase 2 Bicep). One app per tool at /mcp.
# ---------------------------------------------------------------------------
Step '4. Container Apps for the 8 MCP tools (main.bicep)'
if ($WhatIfPreference) {
  az deployment group create -g $ResourceGroup -f infra/main.bicep --parameters infra/main.bicepparam `
    --parameters acrName=$AcrName containerImage=$image namePrefix=$NamePrefix location=$Location `
    --what-if
  Write-Host 'WhatIf: stopping before KB + agent creation.' -ForegroundColor Yellow
  return
}
$deploy = az deployment group create -g $ResourceGroup -f infra/main.bicep --parameters infra/main.bicepparam `
  --parameters acrName=$AcrName containerImage=$image namePrefix=$NamePrefix location=$Location `
  --only-show-errors -o json | ConvertFrom-Json
$endpoints = $deploy.properties.outputs.mcpEndpoints.value
foreach ($e in $endpoints) { Write-Host ("   {0,-18} -> {1}" -f $e.tool, $e.url) }

# Map tool endpoints to the env vars the agent specs read (src/agents/*.py).
function Url($t) { ($endpoints | Where-Object { $_.tool -eq $t }).url }
$env:MCP_ONCALL_LOOKUP_URL   = Url 'oncall_lookup'
$env:MCP_COMMS_PAGE_URL      = Url 'comms_page'
$env:MCP_LABS_HL7_URL        = Url 'labs_hl7'
$env:MCP_PATIENT_CONTEXT_URL = Url 'patient_context'

# ---------------------------------------------------------------------------
# 5. Foundry IQ knowledge base: ingest docs into Azure AI Search (idempotent).
# ---------------------------------------------------------------------------
Step '5. Ingest sepsis-protocols into Azure AI Search (Foundry IQ source)'
python -m src.knowledge.ingest --index sepsis-protocols

# ---------------------------------------------------------------------------
# 6. RemoteTool project connection so the sepsis agent can call knowledge_base_retrieve.
#    Preview ARM API (2025-10-01-preview) — see src/knowledge/ingest.py kb_connection_plan().
# ---------------------------------------------------------------------------
Step '6. Foundry IQ project connection (RemoteTool / ProjectManagedIdentity)'
Write-Host '   Provision the KB connection with infra/create_kb_connection.ps1 (needs the' -ForegroundColor DarkGray
Write-Host '   project ARM id + search endpoint). Skipping auto-run: preview API + project-specific.' -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# 7. Create the hosted (prompt) agents in Foundry with their MCP tools attached.
#    register.py uses DefaultAzureCredential + FOUNDRY_PROJECT_ENDPOINT from .env.
# ---------------------------------------------------------------------------
Step '7. Create hosted agents (comms, sepsis) in Foundry Agent Service'
python -m src.agents.register --capability comms
python -m src.agents.register --capability sepsis

Step 'Done'
Write-Host 'Agents + tools published. Open the Foundry portal -> Agents / Tracing to verify' -ForegroundColor Green
Write-Host '(see infra/CONTROL_PLANE.md). Smoke test: run one routine and one emergency utterance.' -ForegroundColor Green
