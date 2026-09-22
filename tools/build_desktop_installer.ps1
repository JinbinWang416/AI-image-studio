[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskDesktop = Join-Path $taskRoot 'desktop'
$taskDist = Join-Path $taskDesktop 'dist'
$taskBuild = Join-Path $taskDesktop 'build'
$taskAppName = '门店贴纸智能体'
$taskInstallerName = '门店贴纸图片生成智能体安装程序'
$taskIcon = Join-Path $taskDesktop 'assets\store-sticker-ai-icon.ico'

if (-not (Test-Path -LiteralPath $taskPython)) {
    throw "Missing .venv Python: $taskPython"
}

if (-not $SkipInstall) {
    & $taskPython -m pip install --disable-pip-version-check pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller install failed.' }
}

if (-not (Test-Path -LiteralPath (Join-Path $taskDesktop 'launcher.py'))) {
    throw 'Missing desktop launcher source.'
}
if (-not (Test-Path -LiteralPath $taskIcon)) {
    throw "Missing application icon: $taskIcon"
}

New-Item -ItemType Directory -Force -Path $taskDist, $taskBuild | Out-Null

& $taskPython -m PyInstaller --noconfirm --clean --onedir --noconsole `
    --name $taskAppName `
    --icon $taskIcon `
    --distpath (Join-Path $taskDist 'app') `
    --workpath (Join-Path $taskBuild 'app') `
    --specpath (Join-Path $taskBuild 'spec') `
    --add-data "$taskRoot\app;app" `
    --add-data "$taskRoot\data;data" `
    (Join-Path $taskDesktop 'launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'Desktop app build failed.' }

$taskPayload = Join-Path (Join-Path $taskDist 'app') $taskAppName
if (-not (Test-Path -LiteralPath (Join-Path $taskPayload "$taskAppName.exe"))) {
    throw "Desktop app executable not found: $taskPayload"
}

Copy-Item -LiteralPath (Join-Path $taskDesktop 'README-桌面版.txt') -Destination $taskPayload -Force

& $taskPython -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name $taskInstallerName `
    --icon $taskIcon `
    --distpath (Join-Path $taskDist 'installer') `
    --workpath (Join-Path $taskBuild 'installer') `
    --specpath (Join-Path $taskBuild 'spec') `
    --add-data "$taskPayload;payload" `
    (Join-Path $taskDesktop 'installer.py')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed.' }

$taskInstaller = Join-Path (Join-Path $taskDist 'installer') "$taskInstallerName.exe"
if (-not (Test-Path -LiteralPath $taskInstaller)) {
    throw "Installer executable not found: $taskInstaller"
}

$taskHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $taskInstaller).Hash.ToLower()
$taskInstallerHash = "$taskInstaller.sha256"
"$taskHash  $(Split-Path -Leaf $taskInstaller)" | Set-Content -LiteralPath $taskInstallerHash -Encoding utf8

Write-Output "Installer: $taskInstaller"
Write-Output "SHA256: $taskInstallerHash"
