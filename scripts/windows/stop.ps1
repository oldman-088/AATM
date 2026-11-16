# AATM - stop the backend, frontend and any remote worker daemons (Windows 11).
#
# Each PID is validated against its process name before Stop-Process is called, so a
# recycled PID belonging to an unrelated process is never terminated.
$ErrorActionPreference = "SilentlyContinue"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path

function Stop-AatmProcess {
    param([string]$Name, [string]$ExpectedPattern)

    $pidFile = "$RootDir\logs\$Name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Host "[AATM] ${Name}: no PID file, nothing to stop."
        return
    }
    $raw = (Get-Content $pidFile | Select-Object -First 1)
    if (-not $raw -or -not ($raw -match '^\d+$')) {
        Write-Host "[AATM] ${Name}: invalid PID file, removing."
        Remove-Item $pidFile -Force
        return
    }
    $procId = [int]$raw
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "[AATM] ${Name}: not running (stale PID $procId), removing PID file."
        Remove-Item $pidFile -Force
        return
    }
    if ($ExpectedPattern -and $proc.ProcessName -notmatch $ExpectedPattern) {
        Write-Host "[AATM] ${Name}: PID $procId is '$($proc.ProcessName)' and does not look like ours; skipping." -ForegroundColor Yellow
        Remove-Item $pidFile -Force
        return
    }
    Stop-Process -Id $procId -Force
    Remove-Item $pidFile -Force
    Write-Host "[AATM] ${Name}: stopped (PID $procId)."
}

Stop-AatmProcess -Name "backend"  -ExpectedPattern "python"
Stop-AatmProcess -Name "frontend" -ExpectedPattern "node"

Get-ChildItem "$RootDir\logs\worker-*.pid" -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-AatmProcess -Name $_.BaseName -ExpectedPattern "python"
}
