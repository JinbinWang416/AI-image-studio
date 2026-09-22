[CmdletBinding()]
param([switch]$Stop, [switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskMain = Join-Path $taskRoot 'main.py'
$taskUrl = 'http://127.0.0.1:8000'
$taskLogs = Join-Path $taskRoot 'logs'

function Get-AppListener {
    Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Assert-AppProcess($connection) {
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
    if (-not $taskProcess -or $taskProcess.Name -ne 'python.exe' -or
        -not $taskProcess.CommandLine) {
        throw 'Port 8000 is occupied by another process. It was left running.'
    }
    # 只要求命令行同时含 main.py 与 web 子命令即可。
    # 不能要求「完整路径 + 引号」：uvicorn 派生的子进程命令行使用相对路径
    # （main.py web --host ...），严格匹配会把它误判为外部程序占端口，
    # 表现为服务明明在正常运行却报 "Port 8000 is occupied"。
    $taskCmd = $taskProcess.CommandLine
    if ($taskCmd.IndexOf('main.py', [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
        $taskCmd.IndexOf(' web', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Port 8000 is occupied by another process. It was left running.'
    }
}

try {
    $taskConnection = Get-AppListener
    if ($Stop) {
        if ($taskConnection) {
            Assert-AppProcess $taskConnection
            # /api/state 需要登录；命令行场景通常没有会话 cookie。
            # 拿不到状态时不阻断停止流程，只提示无法确认是否有任务在跑。
            $taskRunning = $null
            try {
                $taskState = Invoke-RestMethod "$taskUrl/api/state" -TimeoutSec 5
                $taskRunning = [bool]$taskState.running
            } catch {
                Write-Output 'Note: could not read /api/state (login required); skipping the running-task check.'
            }
            if ($taskRunning) {
                throw 'Image generation is running. Stop it on the web page before closing the server.'
            }
            Stop-Process -Id $taskConnection.OwningProcess
            Write-Output 'Local server stopped.'
        } else {
            Write-Output 'Local server is already stopped.'
        }
        exit 0
    }

    if ($taskConnection) {
        Assert-AppProcess $taskConnection
    } else {
        if (-not (Test-Path -LiteralPath $taskPython)) {
            throw 'Missing .venv. See LOCAL-DEPLOYMENT.md for environment setup.'
        }
        New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
        $taskStamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
        $taskOut = Join-Path $taskLogs "web_$taskStamp.stdout.log"
        $taskErr = Join-Path $taskLogs "web_$taskStamp.stderr.log"
        $taskChild = Start-Process -FilePath $taskPython -WorkingDirectory $taskRoot -WindowStyle Hidden `
            -ArgumentList @('-X', 'utf8', '-u', ('"' + $taskMain + '"'), 'web', '--host', '127.0.0.1', '--port', '8000') `
            -RedirectStandardOutput $taskOut -RedirectStandardError $taskErr -PassThru
        $taskReady = $false
        for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
            # 注意：不能用 $taskChild.HasExited 判定失败。
            # 该进程会派生子进程后自身退出（父子模型），HasExited 为 true 时
            # 服务往往已经在正常监听 —— 早先据此抛错，表现为"明明能用却报启动失败"。
            try {
                # 就绪检测必须用**公开**接口：/api/state 需要 batch.read 权限，
                # 未携带登录会话时返回 401，会把这个正常启动的服务误判为"启动失败"。
                $taskState = Invoke-RestMethod "$taskUrl/api/auth/status" -TimeoutSec 2
                if ($null -ne $taskState.needs_setup) { $taskReady = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 500
        }
        if (-not $taskReady) { throw "Server did not become ready. See $taskErr" }
        Assert-AppProcess (Get-AppListener)
    }
    if (-not $NoBrowser) { Start-Process $taskUrl }
    Write-Output "Ready: $taskUrl"
} catch {
    $taskError = $_.Exception.Message
    if (-not $NoBrowser) {
        $taskShell = New-Object -ComObject WScript.Shell
        $null = $taskShell.Popup($taskError, 0, 'Image Agent', 16)
    }
    Write-Error $taskError -ErrorAction Continue
    exit 1
}
