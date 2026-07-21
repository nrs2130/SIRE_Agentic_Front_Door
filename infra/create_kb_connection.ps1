<#
.SYNOPSIS
  Create the Foundry IQ knowledge-base RemoteTool project connection (preview ARM API).

.DESCRIPTION
  Creates a `RemoteTool` / `ProjectManagedIdentity` connection on the Foundry project that
  targets the knowledge base's MCP endpoint, so the sepsis hosted agent can call the
  `knowledge_base_retrieve` tool with the project's managed identity. Mirrors
  https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect (fetched
  2026-07-21) and src/knowledge/ingest.py `kb_connection_plan()`.

  Uses `az rest` with DefaultAzureCredential (your `az login`). The API version
  (2025-10-01-preview) is a PREVIEW surface — verify it against your tenant before prod use.

.PARAMETER ProjectResourceId  ARM id of the Foundry project
  (/subscriptions/../resourceGroups/../providers/Microsoft.MachineLearningServices/workspaces/<acct>/projects/<proj>).
.PARAMETER SearchEndpoint      Azure AI Search service URL (https://<name>.search.windows.net).
.PARAMETER KbName              Knowledge base name (default sepsis-protocols-kb).
.PARAMETER ConnectionName      Project connection name (default nightingale-sepsis-kb-connection).

.EXAMPLE
  ./infra/create_kb_connection.ps1 -ProjectResourceId $pid -SearchEndpoint https://mysearch.search.windows.net
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ProjectResourceId,
  [Parameter(Mandatory = $true)][string]$SearchEndpoint,
  [string]$KbName = 'sepsis-protocols-kb',
  [string]$ConnectionName = 'nightingale-sepsis-kb-connection'
)

$ErrorActionPreference = 'Stop'
$kbApiVersion = '2026-05-01-preview'      # knowledge-base MCP endpoint
$connApiVersion = '2025-10-01-preview'    # ARM project connection
$mcpEndpoint = ('{0}/knowledgebases/{1}/mcp?api-version={2}' -f $SearchEndpoint.TrimEnd('/'), $KbName, $kbApiVersion)

$body = @{
  name       = $ConnectionName
  type       = 'Microsoft.MachineLearningServices/workspaces/connections'
  properties = @{
    authType     = 'ProjectManagedIdentity'
    category     = 'RemoteTool'
    target       = $mcpEndpoint
    isSharedToAll = $true
    audience     = 'https://search.azure.com/'
    metadata     = @{ ApiType = 'Azure' }
  }
} | ConvertTo-Json -Depth 8

$uri = ('https://management.azure.com{0}/connections/{1}?api-version={2}' -f $ProjectResourceId, $ConnectionName, $connApiVersion)

Write-Host "Creating project connection '$ConnectionName' -> $mcpEndpoint" -ForegroundColor Cyan
$tmp = New-TemporaryFile
[System.IO.File]::WriteAllText($tmp, $body, (New-Object System.Text.UTF8Encoding $false))  # BOM-less body
az rest --method PUT --uri $uri --body "@$tmp" --headers 'Content-Type=application/json'
Remove-Item $tmp -Force

Write-Host 'Done. Also grant the project managed identity **Search Index Data Reader** on the' -ForegroundColor Green
Write-Host 'search service (foundry-iq-connect troubleshooting), else retrieval returns 403.' -ForegroundColor Green
