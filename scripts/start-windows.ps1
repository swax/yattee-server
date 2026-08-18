[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$startRepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$startVenvScripts = Join-Path $startRepoRoot ".venv\Scripts"
$startPython = Join-Path $startVenvScripts "python.exe"
$startProviderVersion = "1.3.1"
$startProviderServer = Join-Path $startRepoRoot ".tools\bgutil-ytdlp-pot-provider-$startProviderVersion\server"
$startProviderMain = Join-Path $startProviderServer "build\main.js"
$startProviderProcess = $null

if (-not (Test-Path -LiteralPath $startPython)) {
    throw "Yattee Server is not set up. Run .\scripts\setup-windows.ps1 first."
}

if (-not (Test-Path -LiteralPath $startProviderMain)) {
    throw "The YouTube PO-token provider is not built. Run .\scripts\setup-windows.ps1 again."
}

$startNode = Get-Command "node" -ErrorAction SilentlyContinue
if (-not $startNode) {
    throw "Node.js 20 or newer is required for the YouTube PO-token provider."
}

function Get-PotProviderVersion {
    try {
        $startPing = Invoke-RestMethod -Uri "http://127.0.0.1:4416/ping" -TimeoutSec 2
        return $startPing.version
    } catch {
        return $null
    }
}

Push-Location $startRepoRoot
try {
    $env:PATH = "$startVenvScripts;$env:PATH"
    $env:YATTEE_PO_TOKEN_PROVIDER = "1"

    $startRunningProviderVersion = Get-PotProviderVersion
    if ($startRunningProviderVersion) {
        if ($startRunningProviderVersion -ne $startProviderVersion) {
            throw "PO-token provider v$startRunningProviderVersion is already running on port 4416; v$startProviderVersion is required."
        }
        Write-Host "Using the running YouTube PO-token provider v$startRunningProviderVersion."
    } else {
        Write-Host "Starting YouTube PO-token provider v$startProviderVersion..."
        $startProviderProcess = Start-Process `
            -FilePath $startNode.Source `
            -ArgumentList @("build/main.js") `
            -WorkingDirectory $startProviderServer `
            -WindowStyle Hidden `
            -PassThru

        $startProviderReady = $false
        for ($startAttempt = 0; $startAttempt -lt 20; $startAttempt++) {
            Start-Sleep -Milliseconds 500
            if ($startProviderProcess.HasExited) {
                throw "The YouTube PO-token provider exited during startup with code $($startProviderProcess.ExitCode)."
            }
            if ((Get-PotProviderVersion) -eq $startProviderVersion) {
                $startProviderReady = $true
                break
            }
        }
        if (-not $startProviderReady) {
            throw "The YouTube PO-token provider did not become ready on http://127.0.0.1:4416."
        }
    }

    & $startPython "server.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Yattee Server exited with code $LASTEXITCODE."
    }
} finally {
    if ($startProviderProcess -and -not $startProviderProcess.HasExited) {
        Stop-Process -Id $startProviderProcess.Id
    }
    Pop-Location
}
