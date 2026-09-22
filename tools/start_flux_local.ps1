[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ModelRoot = 'E:\AIModels\flux2-klein-4b'
$Python = Join-Path $ModelRoot '.venv\Scripts\python.exe'
$Manifest = Join-Path $ModelRoot 'model_manifest.json'
$LogDir = Join-Path $ProjectRoot 'logs'

if (-not (Test-Path $Python)) { throw "FLUX environment missing: $Python. Run tools/setup_flux_local.ps1 first." }
if (-not (Test-Path $Manifest)) { throw "Model manifest missing: $Manifest. Run tools/setup_flux_local.ps1 first." }

$listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8189 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    Write-Host 'FLUX local service is already listening on 127.0.0.1:8189.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:FLUX_LOCAL_HOME = $ModelRoot
$env:FLUX_LOCAL_SOURCE = Join-Path $ModelRoot 'source'
$env:FLUX_LOCAL_WEIGHTS = Join-Path $ModelRoot 'weights'
$env:KLEIN_4B_MODEL_PATH = Join-Path $ModelRoot 'weights\flux-2-klein-4b.safetensors'
$env:AE_MODEL_PATH = Join-Path $ModelRoot 'weights\ae.safetensors'
$env:HF_HOME = Join-Path $ModelRoot 'huggingface'
$env:HF_HUB_CACHE = $env:HF_HOME
$env:HUGGINGFACE_HUB_CACHE = $env:HF_HOME
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$stdout = Join-Path $LogDir "flux_local_$stamp.out.log"
$stderr = Join-Path $LogDir "flux_local_$stamp.err.log"
$proc = Start-Process -FilePath $Python -ArgumentList @('-m', 'app.local_flux_service') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
Write-Host "FLUX local service is starting (PID $($proc.Id)). Use the web Test button after model load completes."
Write-Host "Log: $stdout"
