# AATM - full verification suite (Windows 11 PowerShell).
#
# Gates: prerequisites, alembic migrations, ruff (backend + bridge), pytest (backend +
# E2E + bridge), static secret scan, and (when node_modules exists) tsc / vitest / vite build.
#
# Usage:  .\scripts\windows\verify.ps1 [-SkipFrontend] [-Install]
param(
    [switch]$Install,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir

$script:Passed = 0
$script:Failed = 0

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  PASS $msg" -ForegroundColor Green; $script:Passed++ }
function Fail($msg) { Write-Host "  FAIL $msg" -ForegroundColor Red;  $script:Failed++ }

function Invoke-Gate {
    param([string]$Label, [scriptblock]$Command)
    try {
        & $Command
        if ($LASTEXITCODE -ne 0) { Fail $Label } else { Ok $Label }
    } catch {
        Fail "$Label ($($_.Exception.Message))"
    }
}

Step "1/6 Prerequisites"
foreach ($tool in @("python", "git")) {
    if (Get-Command $tool -ErrorAction SilentlyContinue) { Ok "$tool present" } else { Fail "$tool missing" }
}
if (Get-Command npm -ErrorAction SilentlyContinue) { Ok "npm present" } else { Fail "npm missing" }

$Py = "$RootDir\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    if ($Install) {
        Step "Installing Python dependencies"
        python -m venv .venv
        & .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
        & .\.venv\Scripts\pip.exe install -r backend\requirements.txt | Out-Null
        Ok "python dependencies installed"
    } else {
        Write-Host "  .venv missing - run scripts\windows\install.ps1 first (or pass -Install)" -ForegroundColor Red
        exit 1
    }
}

Step "2/6 Database migrations"
Push-Location backend
& ..\.venv\Scripts\alembic.exe upgrade head
if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head" } else { Ok "alembic upgrade head" }
Pop-Location

Step "3/6 Backend & Bridge lint"
Invoke-Gate "ruff check backend" { & .\.venv\Scripts\ruff.exe check backend }
Invoke-Gate "ruff check bridge"  { & .\.venv\Scripts\ruff.exe check --config backend\pyproject.toml bridge }

Step "4/6 Backend & Bridge tests"
Push-Location backend
& $Py -m pytest -q
if ($LASTEXITCODE -ne 0) { Fail "pytest (backend)" } else { Ok "pytest (backend + E2E verification suite)" }
Pop-Location
& $Py -m pytest -v bridge\tests\test_bridge.py
if ($LASTEXITCODE -ne 0) { Fail "pytest (bridge suite)" } else { Ok "pytest (bridge suite)" }

Step "5/6 Static secret scan"
Push-Location backend
& $Py -m app.qa.engine --repo-root "$RootDir"
if ($LASTEXITCODE -ne 0) { Fail "secret scan" } else { Ok "secret scan" }
Pop-Location

if (-not $SkipFrontend) {
    Step "6/6 Frontend gates"
    if (-not (Test-Path "$RootDir\frontend\node_modules")) {
        Fail "frontend\node_modules missing - run npm install in frontend\ (or use -SkipFrontend)"
    } else {
        Push-Location frontend
        Invoke-Gate "tsc --noEmit" { npm run --silent lint }
        Invoke-Gate "vitest run"    { npm run --silent test }
        Invoke-Gate "vite build"    { npm run --silent build }
        Pop-Location
    }
} else {
    Step "6/6 Frontend gates (skipped)"
}

Write-Host "`n==> Verification summary" -ForegroundColor Cyan
Write-Host "  passed: $script:Passed"
Write-Host "  failed: $script:Failed"
if ($script:Failed -gt 0) {
    Write-Host "  VERIFICATION FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "  VERIFICATION PASSED" -ForegroundColor Green
