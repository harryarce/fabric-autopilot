// ---------------------------------------------------------------------------
// fabric-autopilot — main deployment (subscription scope, azd convention).
//
// Creates the resource group and delegates all resources to resources.bicep.
// Provision + deploy with:  azd up
// ---------------------------------------------------------------------------

targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment (azd env). Used to derive resource names.')
param environmentName string

@minLength(1)
@description('Primary Azure region for all resources.')
param location string

@description('Existing Microsoft Foundry project endpoint to reuse (no new AI resource is created).')
param foundryProjectEndpoint string = ''

@description('Foundry model deployment name for the agentic layer.')
param foundryModel string = ''

@description('Foundry agent name for the agentic layer.')
param foundryAgentName string = ''

@description('Object id of the user/principal to grant data-plane roles for local development. Leave empty in CI.')
param principalId string = ''

// Deterministic, unique-ish resource token derived from the environment.
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    foundryProjectEndpoint: foundryProjectEndpoint
    foundryModel: foundryModel
    foundryAgentName: foundryAgentName
    principalId: principalId
  }
}

// Outputs consumed by azd to push images and wire env.
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_RESOURCE_GROUP string = rg.name
output SERVICE_API_URI string = resources.outputs.apiUri
output SERVICE_MCP_URI string = resources.outputs.mcpUri
output SERVICE_WEB_URI string = resources.outputs.webUri
output AZURE_KEY_VAULT_ENDPOINT string = resources.outputs.keyVaultEndpoint
output AZURE_STORAGE_BLOB_ENDPOINT string = resources.outputs.storageBlobEndpoint
