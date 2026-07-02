# Deployment Guide

This guide covers deploying **fabric-autopilot** to Azure using the
[Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/).

A single `azd up` provisions all infrastructure, builds the three container
images, pushes them to Azure Container Registry, and deploys them to Azure
Container Apps.

---

## Architecture

Three containerized services run on a shared **Azure Container Apps** environment:

| Service | Image | Ingress | Port | Purpose |
|---------|-------|---------|------|---------|
| `api` | `Dockerfile.api` | Internal | 8000 | FastAPI backend (REST). Bakes the MS ODBC Driver 18 for Fabric SQL access. |
| `mcp` | `Dockerfile.mcp` | Internal | 8000 | Agent-facing MCP server (streamable-http). Calls the service layer in-process. |
| `web` | `Dockerfile.web` | **External** | 8000 | Streamlit frontend. Reaches the platform only via the API (`FABRIC_API_BASE_URL`). |

Supporting resources provisioned by `infra/`:

- **Azure Container Registry** (Basic) — image store
- **User-assigned Managed Identity** — all app auth, no secrets
- **Azure Storage (Blob)** — artifact store (`artifacts` container)
- **Azure Key Vault** (RBAC) — secret store
- **Log Analytics + Application Insights** — observability
- **Container Apps Environment** — hosts the three apps

Authentication is fully secretless: the managed identity is granted
**Storage Blob Data Contributor**, **Key Vault Secrets User**, and **AcrPull**
roles via Bicep.

> The Microsoft Foundry project (agentic layer) is **reused**, not created. It
> is wired in through `FOUNDRY_*` environment variables (see below).

---

## Prerequisites

| Tool | Minimum | Check |
|------|---------|-------|
| Azure Developer CLI | 1.25+ | `azd version` |
| Docker (running) | 24+ | `docker --version` |
| Azure CLI (optional, for inspection) | 2.60+ | `az version` |
| An Azure subscription | — | Contributor + User Access Administrator on the target scope |

Role assignments in `resources.bicep` require permission to create role
assignments (Owner or User Access Administrator) on the resource group scope.

### Sign in

```powershell
azd auth login
# (optional, for az inspection commands)
az login
```

---

## Configuration

Settings are 12-factor / environment-driven (`fabric_api/settings.py`, prefix
`FABRIC_`). The Bicep wires the container apps automatically; the values you may
want to set yourself are the **Foundry** parameters.

| azd env var | Bicep param | Required | Default | Notes |
|-------------|-------------|----------|---------|-------|
| `AZURE_ENV_NAME` | `environmentName` | yes | set by `azd env new` | Drives resource names (`rg-<name>`). |
| `AZURE_LOCATION` | `location` | yes | prompted by `azd up` | Region for all resources. |
| `AZURE_PRINCIPAL_ID` | `principalId` | auto | your object id | Grants you data-plane access for local dev. |
| `FOUNDRY_PROJECT_ENDPOINT` | `foundryProjectEndpoint` | optional | `""` | Existing Foundry project endpoint. |
| `FOUNDRY_MODEL` | `foundryModel` | optional | `""` | Model deployment name. |
| `FOUNDRY_AGENT_NAME` | `foundryAgentName` | optional | `""` | Agent name. |

Empty `FOUNDRY_*` values mean the agentic layer uses its baked-in defaults
(`app.intelligence.agent`). The REST/web/MCP tiers deploy and run regardless;
the Foundry-backed design/suggestion/audit features stay dormant until set.

The multi-agent **orchestration** layer (`/api/v1/agents/orchestrate`, MCP
`run_agent_team` / `design_and_publish_model` / `improve_report`, and the web
**Agent Studio** tab) also reuses `FOUNDRY_*`. It runs a Foundry-backed agent
team when available and falls back to the same deterministic pipeline otherwise,
so it needs no extra configuration. Optional tuning knobs (all have safe
defaults): `FABRIC_AGENT_MANAGER_NAME`, `FABRIC_AGENT_MAX_STEPS`,
`FABRIC_AGENT_NARRATE`, `FABRIC_AGENT_REQUIRE_APPROVAL`, and the Power BI
modeling MCP bridge (`POWERBI_MODELING_MCP_ENABLED`,
`POWERBI_MODELING_MCP_HEADLESS`, `POWERBI_MODELING_MCP_TRANSPORT`,
`POWERBI_MODELING_MCP_COMMAND`, `POWERBI_MODELING_MCP_URL`,
`POWERBI_MODELING_MCP_READONLY`, `PBI_MODELING_MCP_ACCESS_TOKEN`). See
[docs/deployment.md](docs/deployment.md#agentic-orchestration-optional-tuning).

> **Power BI Modeling MCP in the cloud.** The bridge is **enabled by default in
> code** and now also runs in the cloud. The Microsoft modeling server is
> **stdio-only** (`npx @microsoft/powerbi-modeling-mcp` over Node) — there is no
> self-hostable HTTP variant for *editing* models, so the `api`/`mcp` container
> images bake **Node.js 20+** to launch it headlessly. Because its interactive
> browser sign-in cannot work in a container, the chat surface mints a Power BI
> / XMLA bearer token from the app's **managed identity** and injects it
> (`infra/resources.bicep` sets `POWERBI_MODELING_MCP_ENABLED=true` and
> `POWERBI_MODELING_MCP_HEADLESS=true`). Local dev leaves
> `POWERBI_MODELING_MCP_HEADLESS` unset, so it keeps the interactive sign-in.
>
> **Required manual grant:** the managed identity must be a **Member/Contributor
> (build & write)** on the target Fabric workspace for token-based editing to
> succeed; without it the surface degrades to its deterministic advisory.
> Service-principal / token auth does **not** enforce row-level security.

Environment variables injected into the containers (from `resources.bicep`):

```
AZURE_CLIENT_ID                        # managed identity client id
FABRIC_ARTIFACT_STORE = blob
FABRIC_BLOB_ACCOUNT_URL                # storage blob endpoint
FABRIC_BLOB_CONTAINER = artifacts
FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_MODEL / FOUNDRY_AGENT_NAME
APPLICATIONINSIGHTS_CONNECTION_STRING
POWERBI_MODELING_MCP_ENABLED = true    # api/mcp: run the npx/stdio modeling server
POWERBI_MODELING_MCP_HEADLESS = true   # inject a managed-identity XMLA token (no browser)
FABRIC_API_BASE_URL                    # web → api (internal FQDN)
```

---

## Permissions

There are **two distinct permission layers**. The Bicep grants the Azure
resource-plane roles automatically; the **Fabric** and **Foundry** data-plane
grants are *manual* and are the usual reason a freshly deployed app shows
**0 workspaces** or fails agent/AI calls.

### 1. Azure resource roles (granted by Bicep — no action needed)

The user-assigned managed identity (`id-<token>`) receives these via
`infra/resources.bicep`:

| Role | Scope | Why |
|------|-------|-----|
| **AcrPull** | Container Registry | Pull the app images. |
| **Storage Blob Data Contributor** | Storage account | Read/write the `artifacts` container. |
| **Key Vault Secrets User** | Key Vault | Read secrets (secretless auth). |

> The deploying user also needs **Owner** or **User Access Administrator** on
> the subscription/resource group, because creating the role assignments above
> is itself a privileged operation.

### 2. Microsoft Fabric access (manual — required to list/create workspaces)

In Azure the app authenticates as its **managed identity**, which is a *new
principal that belongs to no Fabric workspace*. The non-admin
`GET /v1/workspaces` endpoint only returns workspaces the caller is a member of,
so until the identity is granted access the app correctly shows **0 workspaces**.
(Locally you see workspaces because `DefaultAzureCredential` falls back to your
`az login` user.)

Grant all three:

1. **Enable service principals to call Fabric APIs** (tenant setting).
   Fabric Admin Portal → **Tenant settings** → **Developer settings** →
   *"Service principals can use Fabric APIs"* → **Enabled**, scoped to a
   security group. Add the managed identity's service principal (enterprise
   application named `id-<token>`) to that group.
2. **Add the identity to each workspace.** Workspace → **Manage access** →
   **Add people or groups** → search `id-<token>` → assign a role:
   - **Viewer** — list/read only
   - **Contributor** (or **Member**) — **required**, because the app creates and
     updates semantic models and reports
   - **Admin** — full control
3. **(Optional) Capacity access** — only needed if the app provisions *new*
   workspaces on a Fabric capacity; not required to use existing ones.

> The app uses the membership-scoped `/workspaces` endpoint by design. The
> bundled `list_workspaces.py` uses the **admin** endpoint (`/admin/workspaces`),
> which instead requires the identity to be a **Fabric Administrator** or hold
> `Tenant.Read.All`.

Look up the identity to search for in Fabric:

```powershell
az identity show --name id-<token> --resource-group rg-<env> `
  --query "{name:name, clientId:clientId, principalId:principalId}" -o table
```

Workspaces are cached in-process (~30s TTL). After granting access, restart the
revision to clear the cache immediately:

```powershell
az containerapp revision restart -n ca-api-<token> -g rg-<env> --revision <active-revision>
```

### 3. Microsoft Foundry access (manual — required for AI features)

For the agentic layer (design/suggestions/audit enrichment) the **same managed
identity** must be allowed to run agents on the reused Foundry project. Grant it
the **Azure AI Developer** role (or any role permitting
`Microsoft.MachineLearningServices/workspaces/agents/action`) on the Foundry
project. Without it the app surfaces a 403 / "isn't authorized to run agents"
message. See <https://aka.ms/azureml-auth-troubleshooting>.

---

## Deploy

### 1. Create the azd environment

```powershell
azd env new fabric-dev
```

### 2. (Optional) wire up the Foundry agentic layer

```powershell
azd env set FOUNDRY_PROJECT_ENDPOINT "https://<your-foundry>.services.ai.azure.com/api/projects/<project>"
azd env set FOUNDRY_MODEL "gpt-4o"
azd env set FOUNDRY_AGENT_NAME "fabric-agent"
```

Skip this to deploy infra + REST/web/MCP only; set them and re-run
`azd deploy` later.

### 3. Provision + build + deploy

```powershell
azd up
```

You will be prompted for the **subscription** and **region**. Choose a region
that supports Azure Container Apps and is close to your Foundry resource
(e.g. `eastus2`). `azd up`:

1. Provisions the resource group and all resources (`infra/main.bicep`).
2. Builds `Dockerfile.api`, `Dockerfile.mcp`, `Dockerfile.web`.
3. Pushes images to ACR.
4. Deploys the three container apps and wires environment + identity.

### 4. Get the public URL

```powershell
azd env get-values | Select-String SERVICE_WEB_URI
```

Only `web` has external ingress; `api` and `mcp` are internal to the Container
Apps environment.

---

## Preview before deploying (optional)

Validate the Bicep against your subscription without creating anything:

```powershell
azd provision --preview
```

---

## Common operations

| Task | Command |
|------|---------|
| Redeploy a single service after a code change | `azd deploy web` (or `api` / `mcp`) |
| Re-apply infrastructure only | `azd provision` |
| Push code + infra changes | `azd up` |
| Open App Insights dashboard | `azd monitor` |
| Show all environment outputs | `azd env get-values` |
| Stream container logs | `az containerapp logs show -n ca-web-<token> -g rg-<env> --follow` |
| Tear everything down | `azd down` |

---

## Outputs

After a successful `azd up`, these outputs are written to the azd environment
(`main.bicep`):

| Output | Description |
|--------|-------------|
| `SERVICE_WEB_URI` | Public Streamlit URL |
| `SERVICE_API_URI` | Internal API FQDN |
| `SERVICE_MCP_URI` | Internal MCP FQDN |
| `AZURE_CONTAINER_REGISTRY_ENDPOINT` | ACR login server |
| `AZURE_KEY_VAULT_ENDPOINT` | Key Vault URI |
| `AZURE_STORAGE_BLOB_ENDPOINT` | Blob endpoint for artifacts |
| `AZURE_RESOURCE_GROUP` | `rg-<environmentName>` |

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `azd up` fails creating role assignments | You lack **User Access Administrator** / **Owner** on the scope. Use a subscription where you can assign roles. |
| **App shows 0 workspaces** | The managed identity has no Fabric access. Grant the three Fabric items in [Permissions → Microsoft Fabric access](#2-microsoft-fabric-access-manual--required-to-listcreate-workspaces), then restart the `api` revision. |
| Schema extraction fails: `Can't open lib '…libmsodbcsql-18….so' file not found` | Stale image where `apt-get autoremove` pruned the ODBC driver's runtime deps. Rebuild with the current Dockerfiles (`azd deploy api` / `azd deploy mcp`). |
| Image build fails on `msodbcsql18` | Transient apt/network issue during Docker build — re-run `azd deploy`. Ensure Docker Desktop is running. |
| Web app loads but API calls fail | Check `FABRIC_API_BASE_URL` on the web app and that `api` revision is healthy: `az containerapp revision list -n ca-api-<token> -g rg-<env>`. |
| Agentic features inactive | `FOUNDRY_*` not set. Set them with `azd env set ...` and run `azd deploy api mcp`. |
| Agent calls return 403 / "isn't authorized to run agents" | Grant the identity **Azure AI Developer** on the Foundry project — see [Permissions → Microsoft Foundry access](#3-microsoft-foundry-access-manual--required-for-ai-features). |
| Container won't start | Inspect logs: `az containerapp logs show -n ca-<svc>-<token> -g rg-<env> --follow`, and App Insights via `azd monitor`. |
| Storage / Key Vault access denied | Managed-identity role assignments may still be propagating (can take a few minutes). Restart the revision if needed. |

---

## Clean up

```powershell
azd down --purge
```

`--purge` also purges the soft-deleted Key Vault so the name can be reused
immediately.
