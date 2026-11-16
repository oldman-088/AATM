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
    Write-Host "[AATM Windows 11 Install] Generating .env with cryptographically random secrets..."
    $GeneratedAdminPw = & .\.venv\Scripts\python.exe -c @"
import secrets
from pathlib import Path

secret_key = secrets.token_hex(32)
admin_pw = secrets.token_urlsafe(18)
bridge_tok = secrets.token_hex(24)
pg_pw = secrets.token_hex(20)

content = Path(".env.example").read_text(encoding="utf-8")
for old, new in {
    "SECRET_KEY=change-this-to-a-secure-random-32-plus-char-secret": f"SECRET_KEY={secret_key}",
    "DEFAULT_ADMIN_PASSWORD=change-me-immediately": f"DEFAULT_ADMIN_PASSWORD={admin_pw}",
    "ARENA_BRIDGE_TOKEN=": f"ARENA_BRIDGE_TOKEN={bridge_tok}",
    "POSTGRES_PASSWORD=": f"POSTGRES_PASSWORD={pg_pw}",
}.items():
    content = content.replace(old, new)

Path(".env").write_text(content, encoding="utf-8")
print(admin_pw)
"@
    Write-Host "[AATM Windows 11 Install] Initial admin account: admin@aatm.local"
    Write-Host "[AATM Windows 11 Install] Generated admin password (shown once - save it now): $GeneratedAdminPw" -ForegroundColor Yellow
}

if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "[AATM Windows 11 Install] Building sandbox image aatm-sandbox:latest..."
    docker build -t aatm-sandbox:latest -f docker/sandbox.Dockerfile .
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Docker sandbox image build failed (sandbox will report SANDBOX_UNAVAILABLE until built)."
    }
} else {
    Write-Warning "docker not found - sandbox image aatm-sandbox:latest was not built (SANDBOX_UNAVAILABLE in docker mode)."
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
