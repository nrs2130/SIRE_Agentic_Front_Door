// Phase 1 of the Nightingale deploy: the Azure Container Registry that holds the MCP tool
// image. Deployed FIRST so `az acr build` can build+push the image before main.bicep wires the
// Container Apps to it (resolves the image chicken-and-egg without a placeholder image).
//
//   az deployment group create -g <rg> -f infra/acr.bicep -p acrName=<name> location=<region>
//
// api-version pinned to a current stable ACR ARM version (2023-11-01-preview is GA-stable in
// the 2023-11 line; use 2023-07-01 if your tenant lacks the preview). Verified 2026-07-21.

@description('Globally-unique Azure Container Registry name (5-50 alphanumerics).')
param acrName string

@description('Azure region for the registry.')
param location string = resourceGroup().location

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    // Pull is via the Container Apps managed identity (AcrPull) — no admin user needed.
    adminUserEnabled: false
  }
}

output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
