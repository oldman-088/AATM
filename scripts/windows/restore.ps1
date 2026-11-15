param(
    [Parameter(Mandatory=$true)]
    [string]$BackupZip
)
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
Expand-Archive -Path $BackupZip -DestinationPath $RootDir -Force
Write-Host "[AATM Restore] Restored backup from $BackupZip"
