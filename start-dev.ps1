<#
.SYNOPSIS
    Start the Fabric Dev AI backend (FastAPI) and frontend (Streamlit) together.

.DESCRIPTION
    Launches both local dev servers from the repo root and wires the frontend to
    the backend via FABRIC_API_BASE_URL.

    Default ports avoid 8000, which on many Windows machines is reserved by
    Hyper-V / WinNAT and raises "WinError 10013: An attempt was made to access a
    socket in a way forbidden by its access permissions".

    Each server runs in its own window. Close the windows (or press Ctrl+C in
    them) to stop. Press Ctrl+C in this script to stop both.

.PARAMETER ApiPort
    Port for the FastAPI backend (uvicorn). Default 8001.

.PARAMETER WebPort
    Port for the Streamlit frontend. Default 8501.

.EXAMPLE
    ./start-dev.ps1

.EXAMPLE
    ./start-dev.ps1 -ApiPort 8010 -WebPort 8600
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8001,
    [int]$WebPort = 8501
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$apiBaseUrl = "http://localhost:$ApiPort"

function Test-PortFree([int]$Port) {
    return -not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

foreach ($p in @($ApiPort, $WebPort)) {
    if (-not (Test-PortFree $p)) {
        Write-Error "Port $p is already in use. Pick another with -ApiPort / -WebPort."
    }
}

# Local dev should authenticate to Azure / Microsoft Foundry as the developer
# (your `az login` identity), not as any stray service-principal env vars
# (AZURE_CLIENT_ID/SECRET/TENANT_ID) or a dev-box managed identity that may be
# left over from other projects. `AZURE_TOKEN_CREDENTIALS=dev` (azure-identity
# >= 1.23) makes DefaultAzureCredential try only developer credentials
# (Azure CLI / VS Code / azd / Azure PowerShell), skipping EnvironmentCredential
# and ManagedIdentityCredential. Production (Container Apps) does not use this
# script, so its managed-identity auth is unaffected. Override by pre-setting
# AZURE_TOKEN_CREDENTIALS before invoking this script.
if (-not $env:AZURE_TOKEN_CREDENTIALS) {
    $env:AZURE_TOKEN_CREDENTIALS = "dev"
    Write-Host "Auth: AZURE_TOKEN_CREDENTIALS=dev (using your az login identity)." -ForegroundColor DarkGray
}

Write-Host "Starting backend (FastAPI) on $apiBaseUrl ..." -ForegroundColor Cyan
$api = Start-Process -FilePath "uvicorn" `
    -ArgumentList @(
        "fabric_api.main:app",
        "--reload",
        "--host", "127.0.0.1",
        "--port", "$ApiPort"
    ) `
    -WorkingDirectory $root `
    -PassThru

Write-Host "Starting frontend (Streamlit) on http://localhost:$WebPort ..." -ForegroundColor Cyan
$env:FABRIC_API_BASE_URL = $apiBaseUrl
$web = Start-Process -FilePath "streamlit" `
    -ArgumentList @(
        "run", "fabric_app/streamlit_app.py",
        "--server.port", "$WebPort"
    ) `
    -WorkingDirectory $root `
    -PassThru

Write-Host ""
Write-Host "Backend:  $apiBaseUrl/docs   (pid $($api.Id))" -ForegroundColor Green
Write-Host "Frontend: http://localhost:$WebPort   (pid $($web.Id))" -ForegroundColor Green
Write-Host "FABRIC_API_BASE_URL=$apiBaseUrl" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Press Ctrl+C to stop both." -ForegroundColor Yellow

try {
    while (-not $api.HasExited -and -not $web.HasExited) {
        Start-Sleep -Seconds 1
    }
}
finally {
    foreach ($proc in @($api, $web)) {
        if ($proc -and -not $proc.HasExited) {
            Write-Host "Stopping pid $($proc.Id) ..." -ForegroundColor DarkYellow
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
