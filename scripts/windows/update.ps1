$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $RootDir

& .\.venv\Scripts\pip.exe install -r backend\requirements.txt
Push-Location backend
& ..\.venv\Scripts\alembic.exe upgrade head
Pop-Location
Push-Location frontend
npm install
npm run build
Pop-Location
Write-Host "[AATM Windows 11 Update] Complete."
