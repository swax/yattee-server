[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"

$stopRepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$stopEnvPath = Join-Path $stopRepoRoot ".env"

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory)]
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $stopEnvPath)) {
        return $null
    }

    $escapedName = [regex]::Escape($Name)
    foreach ($line in Get-Content -LiteralPath $stopEnvPath) {
        if ($line -notmatch "^\s*$escapedName\s*=\s*(.*?)\s*$") {
            continue
        }

        $value = $Matches[1].Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        return $value
    }

    return $null
}

function Get-ListeningProcessIds {
    param(
        [Parameter(Mandatory)]
        [int]$Port
    )

    $netstatPath = Join-Path $env:SystemRoot "System32\netstat.exe"
    $listenerPattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(?<pid>\d+)\s*$"
    $processIds = foreach ($line in & $netstatPath "-ano" "-p" "TCP") {
        if ($line -match $listenerPattern) {
            [int]$Matches["pid"]
        }
    }

    return @($processIds | Sort-Object -Unique | Where-Object { $_ -gt 0 })
}

function Stop-VerifiedListener {
    param(
        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        [int]$Port,

        [Parameter(Mandatory)]
        [scriptblock]$Verify
    )

    $processIds = @(Get-ListeningProcessIds -Port $Port)
    if ($processIds.Count -eq 0) {
        Write-Host "$Name is already stopped."
        return
    }
    if ($processIds.Count -ne 1) {
        throw "Refusing to stop ${Name}: port $Port has multiple listener processes ($($processIds -join ', '))."
    }

    if (-not (& $Verify)) {
        throw "Refusing to stop process $($processIds[0]) on port $Port because it did not identify as $Name."
    }

    $processId = $processIds[0]
    if (-not $PSCmdlet.ShouldProcess("$Name process $processId on port $Port", "Stop")) {
        return
    }

    Stop-Process -Id $processId -ErrorAction Stop
    $stopDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (
        (Get-Process -Id $processId -ErrorAction SilentlyContinue) -and
        [DateTime]::UtcNow -lt $stopDeadline
    ) {
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        throw "$Name process $processId did not stop within 10 seconds."
    }

    Write-Host "Stopped $Name (process $processId)."
}

$stopPortText = if ($env:PORT) { $env:PORT } else { Get-DotEnvValue -Name "PORT" }
if (-not $stopPortText) {
    $stopPortText = "8080"
}

$stopYatteePort = 0
if (-not [int]::TryParse($stopPortText, [ref]$stopYatteePort) -or $stopYatteePort -lt 1 -or $stopYatteePort -gt 65535) {
    throw "Invalid Yattee Server port: $stopPortText"
}

$stopHost = if ($env:HOST) { $env:HOST } else { Get-DotEnvValue -Name "HOST" }
if (-not $stopHost -or $stopHost -in @("0.0.0.0", "localhost")) {
    $stopProbeHost = "127.0.0.1"
} elseif ($stopHost -eq "::") {
    $stopProbeHost = "[::1]"
} elseif ($stopHost.Contains(":") -and -not $stopHost.StartsWith("[")) {
    $stopProbeHost = "[$stopHost]"
} else {
    $stopProbeHost = $stopHost
}

$stopYatteeVerify = {
    try {
        $info = Invoke-RestMethod -Uri "http://${stopProbeHost}:$stopYatteePort/info" -TimeoutSec 10
        return $info.name -eq "Yattee Server"
    } catch {
        return $false
    }
}

$stopProviderPort = 4416
$stopProviderVerify = {
    try {
        $ping = Invoke-RestMethod -Uri "http://127.0.0.1:$stopProviderPort/ping" -TimeoutSec 5
        return -not [string]::IsNullOrWhiteSpace([string]$ping.version)
    } catch {
        return $false
    }
}

Stop-VerifiedListener `
    -Name "Yattee Server" `
    -Port $stopYatteePort `
    -Verify $stopYatteeVerify

# If start-windows.ps1 owns the provider, stopping Yattee gives its finally
# block a moment to clean the provider up before we check for an orphan.
Start-Sleep -Milliseconds 500

Stop-VerifiedListener `
    -Name "YouTube PO-token provider" `
    -Port $stopProviderPort `
    -Verify $stopProviderVerify
