$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$OutDir = "$RootDir\backups"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$ZipPath = "$OutDir\aatm-backup-$Stamp.zip"

Compress-Archive -Path "$RootDir\data" -DestinationPath $ZipPath -Force
Write-Host "[AATM Backup] Created $ZipPath"
