// Phase 2 of the Nightingale deploy: host the L4 MCP tool servers on Azure Container Apps so
// the Foundry hosted agents can attach them as hosted MCP tool endpoints. Provisions one
// Container App per tool (same image, different args), a user-assigned managed identity with
// AcrPull, a Log Analytics workspace, and the Container Apps managed environment.
//
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/main.bicepparam \
//     -p containerImage=<acr>.azurecr.io/nightingale-mcp-tools:<tag> acrName=<acr>
//
// The Foundry IQ knowledge base + its RemoteTool project connection are provisioned by
// deploy.ps1 (Azure AI Search + `az rest`, preview APIs) — not here — so this template stays
// on stable, verifiable ARM resource types. api-versions pinned + verified 2026-07-21.

@description('Azure region.')
param location string = resourceGroup().location

@description('Short prefix for resource names (e.g. "nightingale").')
param namePrefix string = 'nightingale'

@description('Existing ACR name (created by acr.bicep) that holds the tool image.')
param acrName string

@description('Full tool image reference: <acr>.azurecr.io/nightingale-mcp-tools:<tag>.')
param containerImage string

@description('The L4 MCP tools to publish; one Container App each (same image, per-tool args).')
param tools array = [
  'oncall_lookup'
  'comms_page'
  'labs_hl7'
  'patient_context'
  'bed_telemetry'
  'monitor_alarm'
  'blood_bank'
  'equipment_locate'
]

@description('Demo latency knobs passed to every tool container (see config.ToolsConfig).')
param mockLatencyMs string = '250'
param mockJitterMs string = '150'
param toolTimeoutMs string = '3000'

var uamiName = '${namePrefix}-mcp-id'
var lawName = '${namePrefix}-law'
var envName = '${namePrefix}-cae'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull built-in role

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

// Managed identity the Container Apps use to pull the image (passwordless, no admin user).
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
}

// Grant the identity AcrPull on the registry (scoped to the ACR only).
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// Log Analytics backs the Container Apps environment (container logs / console).
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// One Container App per tool: same image, args=[<tool>, '--http'] serve it at /mcp.
// Pinned to a single replica: FastMCP streamable-HTTP is session-stateful, so multiple
// replicas would drop cross-replica sessions (see src/tools/__main__.py note).
resource toolApps 'Microsoft.App/containerApps@2024-03-01' = [for tool in tools: {
  name: '${namePrefix}-mcp-${replace(tool, '_', '-')}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'mcp-${replace(tool, '_', '-')}'
          image: containerImage
          args: [
            tool
            '--http'
          ]
          env: [
            { name: 'PORT', value: '8080' }
            { name: 'TOOLS_USE_REAL_ADAPTER', value: 'false' }
            { name: 'TOOLS_MOCK_LATENCY_MS', value: mockLatencyMs }
            { name: 'TOOLS_MOCK_JITTER_MS', value: mockJitterMs }
            { name: 'TOOLS_TIMEOUT_MS', value: toolTimeoutMs }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    acrPull
  ]
}]

@description('tool -> hosted MCP endpoint URL (feed these into the agent MCP_* env vars).')
output mcpEndpoints array = [for (tool, i) in tools: {
  tool: tool
  url: 'https://${toolApps[i].properties.configuration.ingress.fqdn}/mcp'
}]

output managedIdentityClientId string = uami.properties.clientId
output managedIdentityPrincipalId string = uami.properties.principalId
