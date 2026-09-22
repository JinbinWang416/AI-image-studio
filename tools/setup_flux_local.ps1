[CmdletBinding()]
param(
    [switch]$SkipWeights
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ModelRoot = 'E:\AIModels\flux2-klein-4b'
$SourceDir = Join-Path $ModelRoot 'source'
$VenvDir = Join-Path $ModelRoot '.venv'
$Python = Join-Path $VenvDir 'Scripts\python.exe'

New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null
if (-not (Test-Path (Join-Path $SourceDir '.git'))) {
    git clone --depth 1 https://github.com/black-forest-labs/flux2.git $SourceDir
}

if (-not (Test-Path $Python)) {
    $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $PythonLauncher) { throw 'Python Launcher (py) was not found. Install Python 3.10 through 3.12 and retry.' }
    & py -3.12 -m venv $VenvDir
}

& $Python -m pip install --upgrade pip wheel setuptools
# The official project specifies torch 2.8 with cu129. CUDA 13.1 drivers support it.
& $Python -m pip install -e $SourceDir --extra-index-url https://download.pytorch.org/whl/cu129 --no-cache-dir
# Native Windows needs this CUDA Triton build for Qwen3 FP8 text-encoder kernels.
& $Python -m pip install fastapi 'uvicorn[standard]' huggingface_hub hf-xet 'triton-windows==3.4.0.post21' --no-cache-dir

$LockPath = Join-Path $ModelRoot 'requirements-lock.txt'
& $Python -m pip freeze | Set-Content -Path $LockPath -Encoding utf8

if (-not $SkipWeights) {
    $env:HF_HOME = Join-Path $ModelRoot 'huggingface'
    & $Python (Join-Path $ProjectRoot 'tools\download_flux_local_weights.py') --home $ModelRoot
}

Write-Host "FLUX local validation environment is ready: $ModelRoot"
Write-Host "Start the service with tools\\start_flux_local.ps1"
