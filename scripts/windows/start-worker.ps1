# AATM - start a remote worker daemon on this Windows 11 host.
#
# Required environment variables (or entries in .env):
#   AATM_SERVER_URL, AATM_DEVICE_ID, AATM_DEVICE_TOKEN
# Optional: AATM_WORKER_WORKSPACE, AATM_WORKER_POLL_SECONDS, AATM_WORKER_MAX_CONCURRENT
param(
    [string]$ServerUrl = $env:AATM_SERVER_URL,
    [string]$DeviceId = $env:AATM_DEVICE_ID,
    [string]$DeviceToken = $env:AATM_DEVICE_TOKEN,
    [string]$WorkspaceRoot = $env:AATM_WORKER_WORKSPACE,
    [double]$PollSeconds = 0,
    [int]$MaxConcurrent = 0
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir

# Load .env when present (simple KEY=VALUE parsing, no execution of file content).
$EnvFile = "$RootDir\.env"
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        $trimmed = $line.Trim()
        if ($trimmed -and -not $trimmed.StartsWith("#") -and $trimmed.Contains("=")) {
            $key, $value = $trimmed.Split("=", 2)
            $key = $key.Trim()
            $value = $value.Trim().Trim('"').Trim("'")
            if (-not [Environment]::GetEnvironmentVariable($key)) {
                [Environment]::SetEnvironmentVariable($key, $value)
            }
        }
    }
}

if (-not $ServerUrl) { $ServerUrl = $env:AATM_SERVER_URL }
if (-not $DeviceId) { $DeviceId = $env:AATM_DEVICE_ID }
if (-not $DeviceToken) { $DeviceToken = $env:AATM_DEVICE_TOKEN }
if (-not $WorkspaceRoot) { $WorkspaceRoot = $env:AATM_WORKER_WORKSPACE }
if ($PollSeconds -le 0) { $PollSeconds = [double]($env:AATM_WORKER_POLL_SECONDS ?? 5) }
if ($MaxConcurrent -le 0) { $MaxConcurrent = [int]($env:AATM_WORKER_MAX_CONCURRENT ?? 2) }

if (-not $ServerUrl) { Write-Host "[AATM Worker] ERROR: AATM_SERVER_URL is required." -ForegroundColor Red; exit 1 }
if (-not $DeviceId) { Write-Host "[AATM Worker] ERROR: AATM_DEVICE_ID is required." -ForegroundColor Red; exit 1 }
if (-not $DeviceToken) { Write-Host "[AATM Worker] ERROR: AATM_DEVICE_TOKEN is required." -ForegroundColor Red; exit 1 }

$Python = "$RootDir\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "[AATM Worker] ERROR: $Python not found - run scripts\windows\install.ps1 on this host first." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path "$RootDir\logs" | Out-Null

$SandboxMode = if ($env:AATM_SANDBOX_MODE) { $env:AATM_SANDBOX_MODE.ToLower() } else { "docker" }
if ($SandboxMode -eq "docker") {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        docker image inspect aatm-sandbox:latest *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[AATM Worker] Building sandbox image aatm-sandbox:latest from docker/sandbox.Dockerfile..."
            docker build -t aatm-sandbox:latest -f docker/sandbox.Dockerfile .
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Sandbox image build failed (SANDBOX_UNAVAILABLE; not falling back to process mode)."
            }
        }
    } else {
        Write-Warning "docker not found while AATM_SANDBOX_MODE=docker (SANDBOX_UNAVAILABLE)."
    }
}

if (-not $WorkspaceRoot) { $WorkspaceRoot = "$RootDir\data\remote_workspaces" }
$Workspace = Join-Path $WorkspaceRoot $DeviceId
$LogFile = "$RootDir\logs\worker-$DeviceId.log"
$PidFile = "$RootDir\logs\worker-$DeviceId.pid"

if (Test-Path $PidFile) {
    $existing = [int](Get-Content $PidFile)
    if (Get-Process -Id $existing -ErrorAction SilentlyContinue) {
        Write-Host "[AATM Worker] Already running (PID $existing)."
        exit 0
    }
    Remove-Item $PidFile -Force
}

Write-Host "[AATM Worker] Starting daemon for device '$DeviceId' against $ServerUrl..."
$env:PYTHONPATH = "$RootDir\backend" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
$proc = Start-Process -FilePath $Python `
    -ArgumentList "-m", "app.runtime.worker_daemon", "--server-url", $ServerUrl, "--device-id", $DeviceId, "--token", $DeviceToken, "--workspace-root", $Workspace `
    -WorkingDirectory "$RootDir\backend" `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError "$RootDir\logs\worker-$DeviceId.err.log" `
    -PassThru -WindowStyle Hidden
$proc.Id | Out-File $PidFile -Encoding ascii

Start-Sleep -Seconds 2
if ($proc.HasExited) {
    Write-Host "[AATM Worker] ERROR: the daemon exited immediately. Last log lines:" -ForegroundColor Red
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 20 | Write-Host }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "[AATM Worker] Started (PID $($proc.Id)). Log: $LogFile"
