[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$startRepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$startPython = Join-Path $startRepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $startPython)) {
    throw "Yattee Server is not set up. Run .\scripts\setup-windows.ps1 first."
}

Push-Location $startRepoRoot
try {
    & $startPython "server.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Yattee Server exited with code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
