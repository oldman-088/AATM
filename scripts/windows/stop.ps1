$ErrorActionPreference = "SilentlyContinue"
$RootDir = (Resolve-Path "$PSScriptRoot\..\..").Path

foreach ($name in @("backend", "frontend")) {
    $pidFile = "$RootDir\logs\$name.pid"
    if (Test-Path $pidFile) {
        $procId = [int](Get-Content $pidFile)
        Stop-Process -Id $procId -Force
        Remove-Item $pidFile -Force
        Write-Host "[AATM] Stopped $name (PID $procId)"
    }
}
