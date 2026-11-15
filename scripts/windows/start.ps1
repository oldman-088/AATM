# AATM - start the backend API and the frontend dev server (Windows 11).
#
# The backend is health-checked before success is reported, so a failed start is
# detected immediately instead of leaving a dead process behind.
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [int]$HealthRetries = 30
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir
New-Item -ItemType Directory -Force -Path "$RootDir\logs" | Out-Null

$Uvicorn = "$RootDir\.venv\Scripts\uvicorn.exe"
if (-not (Test-Path $Uvicorn)) {
    Write-Host "[AATM] ERROR: .venv not found - run scripts\windows\install.ps1 first." -ForegroundColor Red
    exit 1
}

$backendPidFile = "$RootDir\logs\backend.pid"
$alreadyRunning = (Test-Path $backendPidFile) -and (Get-Process -Id ([int](Get-Content $backendPidFile)) -ErrorAction SilentlyContinue)
if ($alreadyRunning) {
    Write-Host "[AATM] Backend already running (PID $(Get-Content $backendPidFile))."
} else {
    Write-Host "[AATM] Starting Backend API on 0.0.0.0:${BackendPort}..."
    $backendProc = Start-Process -FilePath $Uvicorn `
        -ArgumentList "app.main:app", "--app-dir", "$RootDir\backend", "--host", "0.0.0.0", "--port", "$BackendPort" `
        -RedirectStandardOutput "$RootDir\logs\backend.log" `
        -RedirectStandardError "$RootDir\logs\backend.err.log" `
        -PassThru -WindowStyle Hidden
    $backendProc.Id | Out-File $backendPidFile -Encoding ascii
}

Write-Host "[AATM] Waiting for the backend health endpoint..."
$ready = $false
for ($i = 0; $i -lt $HealthRetries; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:${BackendPort}/api/health" -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Host "[AATM] ERROR: backend did not become healthy. Last log lines:" -ForegroundColor Red
    if (Test-Path "$RootDir\logs\backend.err.log") { Get-Content "$RootDir\logs\backend.err.log" -Tail 30 | Write-Host }
    exit 1
}
Write-Host "[AATM] Backend healthy: http://localhost:${BackendPort}/api/health"

if (Get-Command npm -ErrorAction SilentlyContinue) {
    $frontendPidFile = "$RootDir\logs\frontend.pid"
    $frontendRunning = (Test-Path $frontendPidFile) -and (Get-Process -Id ([int](Get-Content $frontendPidFile)) -ErrorAction SilentlyContinue)
    if ($frontendRunning) {
        Write-Host "[AATM] Frontend already running (PID $(Get-Content $frontendPidFile))."
    } else {
        Write-Host "[AATM] Starting Frontend Vite Server on 0.0.0.0:${FrontendPort}..."
        $frontendProc = Start-Process -FilePath "npm.cmd" `
            -ArgumentList "run", "--silent", "dev", "--", "--port", "$FrontendPort" `
            -WorkingDirectory "$RootDir\frontend" `
            -RedirectStandardOutput "$RootDir\logs\frontend.log" `
            -RedirectStandardError "$RootDir\logs\frontend.err.log" `
            -PassThru -WindowStyle Hidden
        $frontendProc.Id | Out-File $frontendPidFile -Encoding ascii
    }
}

Write-Host "[AATM] Started! UI: http://localhost:${FrontendPort} | API: http://localhost:${BackendPort}/api/health"
