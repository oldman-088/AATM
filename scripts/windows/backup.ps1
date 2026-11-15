# AATM - back up the runtime data and configuration (Windows 11).
# The archive may contain credentials from .env; it is written to backups\ which is
# git-ignored, and access is restricted to the current user.
$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$OutDir = "$RootDir\backups"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$ZipPath = "$OutDir\aatm-backup-$Stamp.zip"

$items = @()
if (Test-Path "$RootDir\data") { $items += "$RootDir\data" }
if (Test-Path "$RootDir\.env") { $items += "$RootDir\.env" }

if ($items.Count -eq 0) {
    Write-Host "[AATM Backup] ERROR: nothing to back up (no data\ directory and no .env)." -ForegroundColor Red
    exit 1
}

Compress-Archive -Path $items -DestinationPath $ZipPath -Force

# Verify the archive is readable.
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    $entryCount = $zip.Entries.Count
    $zip.Dispose()
} catch {
    Write-Host "[AATM Backup] ERROR: the created archive failed verification." -ForegroundColor Red
    Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "[AATM Backup] Created $ZipPath ($entryCount entries)."
Write-Host "[AATM Backup] Restrict access to this file - it may contain credentials from .env."

# Restrict to the current user only.
try {
    $acl = Get-Acl $ZipPath
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
        "FullControl", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -Path $ZipPath -AclObject $acl
} catch {
    Write-Warning "Could not restrict ACL on the backup archive: $($_.Exception.Message)"
}
