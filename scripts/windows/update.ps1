# AATM - update dependencies, apply migrations and re-verify (Windows 11).
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir

if (-not (Test-Path "$RootDir\.venv\Scripts\pip.exe")) {
    Write-Host "[AATM Update] ERROR: .venv not found - run scripts\windows\install.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "[AATM Update] Pulling latest dependencies and applying migrations..."
& .\.venv\Scripts\pip.exe install -r backend\requirements.txt
Push-Location backend
& ..\.venv\Scripts\alembic.exe upgrade head
Pop-Location
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Push-Location frontend
    npm install
    npm run build
    Pop-Location
}

Write-Host "[AATM Update] Re-running the verification suite..."
& "$RootDir\scripts\windows\verify.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[AATM Update] ERROR: verification failed after the update." -ForegroundColor Red
    exit 1
}
Write-Host "[AATM Windows 11 Update] Complete."
