# AATM Windows 11 Install - dependencies, migrations and verification.
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir

Write-Host "[AATM Windows 11 Install] Checking prerequisites..."
foreach ($tool in @("python", "git")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: $tool is required." -ForegroundColor Red
        exit 1
    }
}

Write-Host "[AATM Windows 11 Install] Creating Python virtual environment..."
if (-not (Test-Path "$RootDir\.venv")) {
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -r backend\requirements.txt

Write-Host "[AATM Windows 11 Install] Installing Playwright Chromium..."
try {
    & .\.venv\Scripts\playwright.exe install chromium
} catch {
    Write-Warning "Chromium install skipped (check network connectivity)."
}

Write-Host "[AATM Windows 11 Install] Running Alembic database migrations..."
Push-Location backend
& ..\.venv\Scripts\alembic.exe upgrade head
Pop-Location

if (-not (Test-Path "$RootDir\.env")) {
    Write-Host "[AATM Windows 11 Install] Creating .env from .env.example (review it before production use)."
    Copy-Item "$RootDir\.env.example" "$RootDir\.env"
}

if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Host "[AATM Windows 11 Install] Installing & building Frontend..."
    Push-Location frontend
    npm install
    npm run build
    Pop-Location
} else {
    Write-Warning "npm not found - skipping the frontend build."
}

Write-Host "[AATM Windows 11 Install] Verifying the installation..."
& "$RootDir\scripts\windows\verify.ps1" -SkipFrontend
if ($LASTEXITCODE -ne 0) {
    Write-Host "[AATM Windows 11 Install] ERROR: verification failed." -ForegroundColor Red
    exit 1
}

Write-Host "[AATM Windows 11 Install] Complete! Run scripts\windows\start.ps1 to start."
