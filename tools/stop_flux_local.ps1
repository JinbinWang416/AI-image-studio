[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8189 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -eq 0) {
    Write-Host 'FLUX local service is not running.'
    exit 0
}
foreach ($listener in $listeners) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($proc.CommandLine -notmatch 'app\.local_flux_service') {
        throw "Port 8189 belongs to a non-project process (PID $($listener.OwningProcess)); it was not stopped."
    }
    Stop-Process -Id $listener.OwningProcess -Force
    Write-Host "Stopped FLUX local service (PID $($listener.OwningProcess))."
}
