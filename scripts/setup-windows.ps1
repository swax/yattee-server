[CmdletBinding()]
param(
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$setupRepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$setupVenvPath = Join-Path $setupRepoRoot ".venv"
$setupVenvPython = Join-Path $setupVenvPath "Scripts\python.exe"
$setupProviderVersion = "1.3.2"
$setupProviderCommit = "7511309af023b09788dc8f2efc96cc3671291e6c"
$setupProviderRoot = Join-Path $setupRepoRoot ".tools\bgutil-ytdlp-pot-provider-$setupProviderVersion"
$setupProviderServer = Join-Path $setupProviderRoot "server"
$setupProviderPatch = Join-Path $PSScriptRoot "patches\bgutil-ytdlp-pot-provider-1.3.2-loopback.patch"

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
    Invoke-CheckedCommand -Executable $setupVenvPython -Arguments @("-m", "pip", "install", "-r", "requirements.txt")

    $setupNode = Get-Command "node" -ErrorAction SilentlyContinue
    if (-not $setupNode) {
        throw "Node.js 20 or newer is required for the YouTube PO-token provider. Install it from https://nodejs.org/ and run this script again."
    }

    $setupNodeVersionText = (& $setupNode.Source "--version").Trim().TrimStart("v")
    if ($LASTEXITCODE -ne 0 -or ([version]$setupNodeVersionText).Major -lt 20) {
        throw "Node.js 20 or newer is required for the YouTube PO-token provider (found $setupNodeVersionText)."
    }

    $setupGit = Get-Command "git" -ErrorAction SilentlyContinue
    if (-not $setupGit) {
        throw "Git is required to install the YouTube PO-token provider. Install it from https://git-scm.com/ and run this script again."
    }

    if (-not (Test-Path -LiteralPath $setupProviderRoot)) {
        $setupToolsRoot = Split-Path -Parent $setupProviderRoot
        New-Item -ItemType Directory -Path $setupToolsRoot -Force | Out-Null
        Write-Host "Downloading YouTube PO-token provider v$setupProviderVersion..."
        Invoke-CheckedCommand -Executable $setupGit.Source -Arguments @(
            "clone",
            "--depth", "1",
            "--branch", $setupProviderVersion,
            "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git",
            $setupProviderRoot
        )
    }

    $setupProviderHead = (& $setupGit.Source "-C" $setupProviderRoot "rev-parse" "HEAD").Trim()
    if ($LASTEXITCODE -ne 0 -or $setupProviderHead -ne $setupProviderCommit) {
        throw "Unexpected PO-token provider revision at $setupProviderRoot. Expected $setupProviderCommit, found $setupProviderHead."
    }

    $setupProviderMainSource = Join-Path $setupProviderServer "src\main.ts"
    $setupProviderMainSourceContent = Get-Content -LiteralPath $setupProviderMainSource -Raw
    $setupProviderPackage = Join-Path $setupProviderServer "package.json"
    $setupProviderPackageContent = Get-Content -LiteralPath $setupProviderPackage -Raw
    if (
        $setupProviderMainSourceContent -notmatch 'host:\s*"127\.0\.0\.1"' -or
        $setupProviderPackageContent -notmatch '"allowScripts"'
    ) {
        Write-Host "Restricting the PO-token provider to the loopback interface..."
        Invoke-CheckedCommand -Executable $setupGit.Source -Arguments @(
            "-C", $setupProviderRoot, "apply", "--whitespace=nowarn", $setupProviderPatch
        )
    }

    $setupNpm = Get-Command "npm" -ErrorAction SilentlyContinue
    if (-not $setupNpm) {
        throw "npm was not found even though Node.js is installed. Repair the Node.js installation and run this script again."
    }

    Write-Host "Building YouTube PO-token provider v$setupProviderVersion..."
    Push-Location $setupProviderServer
    try {
        Invoke-CheckedCommand -Executable $setupNpm.Source -Arguments @("ci")
        $setupTypeScript = Join-Path $setupProviderServer "node_modules\.bin\tsc.cmd"
        Invoke-CheckedCommand -Executable $setupTypeScript -Arguments @("--pretty")
    } finally {
        Pop-Location
    }

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
