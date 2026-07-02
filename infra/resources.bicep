// ---------------------------------------------------------------------------
// fabric-autopilot — resources (resource-group scope).
//
// Provisions: Log Analytics + App Insights, Container Registry, user-assigned
// Managed Identity, Storage (blob) for artifacts, Key Vault, a Container Apps
// Environment, and three Container Apps (api, mcp, web). All app auth uses the
// user-assigned Managed Identity (no secrets). The existing Foundry project is
// reused via FOUNDRY_* env vars — no AI resource is created here.
// ---------------------------------------------------------------------------


@description('Azure region for all resources.')
param location string

@description('Unique-ish token used to name globally-unique resources. uniqueString() always yields 13 chars.')
@minLength(13)
param resourceToken string

@description('Tags applied to all resources.')
param tags object

param foundryProjectEndpoint string
param foundryModel string
param foundryAgentName string

@description('Optional principal id for local-dev data-plane role grants.')
param principalId string = ''

var abbrs = {
  containerRegistry: 'cr'
  keyVault: 'kv'
  storage: 'st'
  identity: 'id'
  logAnalytics: 'log'
  appInsights: 'appi'
  containerAppsEnv: 'cae'
}

var artifactContainerName = 'artifacts'

// --- Observability ---------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${abbrs.logAnalytics}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${abbrs.appInsights}-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// --- Identity --------------------------------------------------------------

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${abbrs.identity}-${resourceToken}'
  location: location
  tags: tags
}

// --- Container Registry ----------------------------------------------------

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: '${abbrs.containerRegistry}${resourceToken}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

// --- Storage (artifacts) ---------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${abbrs.storage}${resourceToken}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource artifactContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: artifactContainerName
  properties: {
    publicAccess: 'None'
  }
}

// --- Key Vault -------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${abbrs.keyVault}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
  }
}

// --- Role assignments (data-plane, no secrets) -----------------------------
// The GUIDs below are well-known public Azure built-in role definition IDs.
// They are not secrets. See: https://learn.microsoft.com/azure/role-based-access-control/built-in-roles

var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // gitleaks:allow
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6' // gitleaks:allow
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // gitleaks:allow

resource miStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource miAcrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Optional: grant the developer principal data-plane access for local runs.
resource devStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(storage.id, principalId, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: principalId
    principalType: 'User'
  }
}

// --- Container Apps Environment --------------------------------------------

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${abbrs.containerAppsEnv}-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// --- Shared container app config -------------------------------------------

var blobAccountUrl = 'https://${storage.name}.blob.${environment().suffixes.storage}'
var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var commonEnv = [
  {
    name: 'AZURE_CLIENT_ID'
    value: identity.properties.clientId
  }
  {
    name: 'FABRIC_ARTIFACT_STORE'
    value: 'blob'
  }
  {
    name: 'FABRIC_BLOB_ACCOUNT_URL'
    value: blobAccountUrl
  }
  {
    name: 'FABRIC_BLOB_CONTAINER'
    value: artifactContainerName
  }
  {
    name: 'FOUNDRY_PROJECT_ENDPOINT'
    value: foundryProjectEndpoint
  }
  {
    name: 'FOUNDRY_MODEL'
    value: foundryModel
  }
  {
    name: 'FOUNDRY_AGENT_NAME'
    value: foundryAgentName
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsights.properties.ConnectionString
  }
  // The Power BI Modeling MCP (@microsoft/powerbi-modeling-mcp) is the local
  // Microsoft server that edits semantic models over stdio (Node/npx). The
  // container images bake Node.js 20+, so it runs headlessly here. Because the
  // server's interactive browser sign-in cannot work in a container, the chat
  // surface mints a Power BI / XMLA bearer token from this app's managed
  // identity and injects it (POWERBI_MODELING_MCP_HEADLESS=true). NOTE: the
  // managed identity must be granted Member/Contributor (build & write) access
  // on the target Fabric workspace for token-based editing to succeed; service
  // principal auth does not enforce row-level security.
  {
    name: 'POWERBI_MODELING_MCP_ENABLED'
    value: 'true'
  }
  {
    name: 'POWERBI_MODELING_MCP_HEADLESS'
    value: 'true'
  }
]

// --- API container app (internal ingress) ----------------------------------

resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-api-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: placeholderImage
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: commonEnv
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [
    miAcrPullRole
  ]
}

// --- MCP container app (internal ingress) ----------------------------------

resource mcpApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-mcp-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'mcp' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'mcp'
          image: placeholderImage
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: commonEnv
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [
    miAcrPullRole
  ]
}

// --- Web container app (external ingress) ----------------------------------

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-web-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: placeholderImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            {
              name: 'FABRIC_API_BASE_URL'
              value: 'https://${apiApp.properties.configuration.ingress.fqdn}'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [
    miAcrPullRole
  ]
}

// --- Outputs ---------------------------------------------------------------

output registryLoginServer string = registry.properties.loginServer
output keyVaultEndpoint string = keyVault.properties.vaultUri
output storageBlobEndpoint string = blobAccountUrl
output apiUri string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
output mcpUri string = 'https://${mcpApp.properties.configuration.ingress.fqdn}'
output webUri string = 'https://${webApp.properties.configuration.ingress.fqdn}'
output managedIdentityClientId string = identity.properties.clientId
