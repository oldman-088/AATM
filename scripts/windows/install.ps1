$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir

Write-Host "[AATM Windows 11 Install] Creating Python virtual environment..."
python -m venv .venv
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

Write-Host "[AATM Windows 11 Install] Installing & building Frontend..."
Push-Location frontend
npm install
npm run build
Pop-Location

Write-Host "[AATM Windows 11 Install] Complete! Run scripts\windows\start.ps1 to start."
