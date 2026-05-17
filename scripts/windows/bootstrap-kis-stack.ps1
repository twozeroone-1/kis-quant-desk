param(
    [string]$RepoRoot = "",
    [string]$EnvFile = ".env.production",
    [string]$WslDistro = "Ubuntu-2",
    [switch]$SkipMcp,
    [switch]$EnableTailscaleServices
)

$ErrorActionPreference = "Stop"

function Assert-NativeSuccess {
    param([string]$CommandName)

    if ($LASTEXITCODE -ne 0) {
        throw "$CommandName failed with exit code $LASTEXITCODE"
    }
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandName,
        [Parameter(Mandatory = $true)]
        [scriptblock]$ScriptBlock
    )

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $ScriptBlock 2>&1 | ForEach-Object { Write-Output $_ }
        Assert-NativeSuccess $CommandName
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
}

function Wait-DockerReady {
    param([int]$TimeoutSeconds = 300)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $status = docker desktop status 2>$null
        if ($LASTEXITCODE -eq 0 -and $status -match "running") {
            return
        }

        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    throw "Docker Desktop did not become ready within $TimeoutSeconds seconds."
}

function ConvertTo-BashLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    return "'" + $Value.Replace("'", "'\''") + "'"
}

function ConvertFrom-WslUncPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Distro
    )

    $normalized = $Path.TrimEnd("\")
    $prefixes = @(
        "\\wsl.localhost\$Distro",
        "\\wsl$\$Distro"
    )

    foreach ($prefix in $prefixes) {
        if ($normalized.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $relative = $normalized.Substring($prefix.Length).Replace("\", "/")
            if (-not $relative.StartsWith("/")) {
                $relative = "/" + $relative
            }
            return $relative
        }
    }

    return $null
}

function Start-KisBacktestMcp {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$Distro
    )

    $linuxRepoRoot = ConvertFrom-WslUncPath -Path $RepoRoot -Distro $Distro
    if (-not $linuxRepoRoot) {
        Write-Output "Skipping MCP startup because RepoRoot is not a WSL UNC path: $RepoRoot"
        return
    }

    $repoArg = ConvertTo-BashLiteral $linuxRepoRoot
    $bashCommand = "cd $repoArg/backtester && if curl -fsS --max-time 2 http://127.0.0.1:3846/health >/dev/null 2>&1; then echo 'KIS backtest MCP already running.'; exit 0; fi; setsid bash scripts/start_mcp.sh > /tmp/kis-backtest-mcp.log 2>&1 < /dev/null & disown || true; for i in {1..30}; do if curl -fsS --max-time 2 http://127.0.0.1:3846/health >/dev/null 2>&1; then echo 'KIS backtest MCP started.'; exit 0; fi; sleep 1; done; echo 'KIS backtest MCP did not become healthy.' >&2; tail -n 80 /tmp/kis-backtest-mcp.log >&2 || true; exit 1"

    Invoke-NativeCommand "kis backtest mcp start" { & wsl.exe -d $Distro -- bash -lc $bashCommand }
}

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).ProviderPath
} else {
    $RepoRoot = (Resolve-Path $RepoRoot).ProviderPath
}

Set-Location $RepoRoot
$EnvPath = Join-Path $RepoRoot $EnvFile

Invoke-NativeCommand "docker desktop start" { & docker desktop start }
Wait-DockerReady

Invoke-NativeCommand "docker compose up" { & docker compose --env-file $EnvPath up -d --build }

if (-not $SkipMcp) {
    Start-KisBacktestMcp -RepoRoot $RepoRoot -Distro $WslDistro
}

if ($EnableTailscaleServices) {
    Invoke-NativeCommand "tailscale serve kis-builder" { & tailscale serve --service=svc:kis-builder --https=443 http://127.0.0.1:8081 }
    Invoke-NativeCommand "tailscale serve kis-backtest" { & tailscale serve --service=svc:kis-backtest --https=443 http://127.0.0.1:8082 }
}
