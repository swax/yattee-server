[CmdletBinding()]
param(
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$setupRepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$setupVenvPath = Join-Path $setupRepoRoot ".venv"
$setupVenvPython = Join-Path $setupVenvPath "Scripts\python.exe"

function Find-CompatiblePython {
    $setupCandidates = @(
        @{ Name = "py"; Arguments = @("-3") },
        @{ Name = "python"; Arguments = @() },
        @{ Name = "python3"; Arguments = @() }
    )

    foreach ($setupCandidate in $setupCandidates) {
        $setupCommand = Get-Command $setupCandidate.Name -ErrorAction SilentlyContinue
        if (-not $setupCommand) {
            continue
        }

        & $setupCommand.Source @($setupCandidate.Arguments) -c `
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{
                Path = $setupCommand.Source
                Arguments = $setupCandidate.Arguments
            }
        }
    }

    throw "Python 3.12 or newer was not found. Install it from https://www.python.org/downloads/windows/ and run this script again."
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Executable $($Arguments -join ' ')"
    }
}

Push-Location $setupRepoRoot
try {
    if (-not (Test-Path -LiteralPath $setupVenvPython)) {
        $setupPython = Find-CompatiblePython
        Write-Host "Creating Python virtual environment..."
        Invoke-CheckedCommand -Executable $setupPython.Path -Arguments @(
            $setupPython.Arguments + @("-m", "venv", $setupVenvPath)
        )
    }

    Write-Host "Installing Yattee Server dependencies..."
    Invoke-CheckedCommand -Executable $setupVenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-CheckedCommand -Executable $setupVenvPython -Arguments @("-m", "pip", "install", "-r", "requirements.txt")

    $setupEnvPath = Join-Path $setupRepoRoot ".env"
    if (-not (Test-Path -LiteralPath $setupEnvPath)) {
        $setupEnvTemplate = Join-Path $setupRepoRoot ".env.example"
        $setupEnvContent = (Get-Content -LiteralPath $setupEnvTemplate -Raw).Replace("HOST=0.0.0.0", "HOST=127.0.0.1")
        Set-Content -LiteralPath $setupEnvPath -Value $setupEnvContent -Encoding utf8
        Write-Host "Created .env with a loopback-only bind address."
    } else {
        Write-Host "Keeping existing .env configuration."
    }

    $setupMissingTools = @()
    if (-not (Get-Command "deno" -ErrorAction SilentlyContinue)) {
        $setupMissingTools += "Deno: https://docs.deno.com/runtime/getting_started/installation/"
    }
    if (-not (Get-Command "ffmpeg" -ErrorAction SilentlyContinue)) {
        $setupMissingTools += "FFmpeg: https://ffmpeg.org/download.html#build-windows"
    }

    if ($setupMissingTools.Count -gt 0) {
        Write-Warning "Some media features need additional tools on PATH:"
        foreach ($setupMissingTool in $setupMissingTools) {
            Write-Warning "  $setupMissingTool"
        }
    }

    Write-Host ""
    Write-Host "Setup complete. Start the server with:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1"
    Write-Host "Then open http://127.0.0.1:8085 to finish setup."

    if ($Start) {
        & (Join-Path $PSScriptRoot "start-windows.ps1")
    }
} finally {
    Pop-Location
}
