$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir
New-Item -ItemType Directory -Force -Path "$RootDir\logs" | Out-Null

Write-Host "[AATM] Starting Backend API on 0.0.0.0:8000..."
$BackendProc = Start-Process -FilePath "$RootDir\.venv\Scripts\uvicorn.exe" `
    -ArgumentList "app.main:app", "--app-dir", "$RootDir\backend", "--host", "0.0.0.0", "--port", "8000" `
    -RedirectStandardOutput "$RootDir\logs\backend.log" `
    -RedirectStandardError "$RootDir\logs\backend.err.log" `
    -PassThru -WindowStyle Hidden
$BackendProc.Id | Out-File "$RootDir\logs\backend.pid" -Encoding ascii

Write-Host "[AATM] Starting Frontend Vite Server on 0.0.0.0:5173..."
$FrontendProc = Start-Process -FilePath "npm.cmd" `
    -ArgumentList "run", "dev" `
    -WorkingDirectory "$RootDir\frontend" `
    -RedirectStandardOutput "$RootDir\logs\frontend.log" `
    -RedirectStandardError "$RootDir\logs\frontend.err.log" `
    -PassThru -WindowStyle Hidden
$FrontendProc.Id | Out-File "$RootDir\logs\frontend.pid" -Encoding ascii

Write-Host "[AATM] Started! UI: http://localhost:5173 | API: http://localhost:8000/api/health"
