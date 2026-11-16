# AATM - restore a backup archive (Windows 11).
param(
    [Parameter(Mandatory=$true)]
    [string]$BackupZip
)
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path

if (-not (Test-Path $BackupZip)) {
    Write-Host "[AATM Restore] ERROR: archive not found: $BackupZip" -ForegroundColor Red
    exit 1
}

# Validate the archive before extracting anything.
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $BackupZip))
    $entries = $zip.Entries | ForEach-Object { $_.FullName }
    $zip.Dispose()
} catch {
    Write-Host "[AATM Restore] ERROR: archive is corrupt or not a valid zip." -ForegroundColor Red
    exit 1
}

# Reject absolute paths and parent-directory traversal inside the archive.
$unsafe = $entries | Where-Object { $_ -match '(^/|^[A-Za-z]:|(^|/)\.\.(/|$)|\\)' }
if ($unsafe) {
    Write-Host "[AATM Restore] ERROR: archive contains unsafe member paths:" -ForegroundColor Red
    $unsafe | ForEach-Object { Write-Host "  $_" }
    exit 1
}

Write-Host "[AATM Restore] Restoring backup from $BackupZip..."
Expand-Archive -Path $BackupZip -DestinationPath $RootDir -Force
Write-Host "[AATM Restore] Restored backup from $BackupZip"
Write-Host "[AATM Restore] Run scripts\windows\verify.ps1 to confirm the restored state is healthy."
