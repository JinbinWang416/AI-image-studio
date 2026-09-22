[CmdletBinding()]
param(
    [string]$PythonLauncher = "py",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$deployRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $deployRoot ".venv\Scripts\python.exe"

Push-Location $deployRoot
try {
    if (-not (Get-Command $PythonLauncher -ErrorAction SilentlyContinue)) {
        throw "找不到 Python 启动器 '$PythonLauncher'。请安装 Python 3.12 后重试，或传入 -PythonLauncher python。"
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $PythonLauncher -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "创建 Python 3.12 虚拟环境失败。" }
    }

    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "升级 pip 失败。" }
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "安装 requirements.txt 失败。" }
    & $venvPython main.py check
    if ($LASTEXITCODE -ne 0) { throw "门店数据校验失败。" }
    if (-not $SkipTests) {
        & $venvPython -m unittest discover -s tests -q
        if ($LASTEXITCODE -ne 0) { throw "自动化测试失败。" }
    }
    Write-Output "部署环境就绪。执行 .\local-server.ps1 -NoBrowser 启动网页。"
} finally {
    Pop-Location
}
