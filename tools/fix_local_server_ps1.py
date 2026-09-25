# -*- coding: utf-8 -*-
"""
修复 local-server.ps1 的编码与就绪检测。

问题一（编码）：脚本含中文注释但保存为「无 BOM 的 UTF-8」，
  Windows PowerShell 5.1 会按 ANSI 解析，中文变成乱码并吞掉后续代码，
  报出 "The Try statement is missing its Catch or Finally block"。
  修法：改写为 **UTF-8 with BOM**。

问题二（就绪检测）：原先调用 /api/state，而该接口需要 batch.read 权限，
  命令行场景没有登录会话 → 401 → 正常启动的服务被误判为"启动失败"。
  修法：改用公开接口 /api/auth/status。

问题三（误判退出）：原先用 $taskChild.HasExited 判定失败，
  但该进程会派生子进程后自身退出，HasExited=true 时服务往往已正常监听。
  修法：只依据 HTTP 就绪探测。

用法：
    .\\.venv\\Scripts\\python.exe tools\\fix_local_server_ps1.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "local-server.ps1"

# 替换片段：(原文, 新文)
REPLACEMENTS: list[tuple[str, str]] = [
    (
        """    if (-not $taskProcess -or $taskProcess.Name -ne 'python.exe' -or
        -not $taskProcess.CommandLine -or
        $taskProcess.CommandLine.IndexOf(('"' + $taskMain + '"'), [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Port 8000 is occupied by another process. It was left running.'
    }""",
        """    if (-not $taskProcess -or $taskProcess.Name -ne 'python.exe' -or
        -not $taskProcess.CommandLine) {
        throw 'Port 8000 is occupied by another process. It was left running.'
    }
    # Require both "main.py" and the "web" subcommand. Do NOT require the full
    # quoted path: the spawned child process runs with a relative path
    # (main.py web --host ...), so a strict match misreports a healthy server
    # as "Port 8000 is occupied by another process".
    $taskCmd = $taskProcess.CommandLine
    if ($taskCmd.IndexOf('main.py', [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
        $taskCmd.IndexOf(' web', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Port 8000 is occupied by another process. It was left running.'
    }""",
    ),
    (
        """            $taskState = Invoke-RestMethod "$taskUrl/api/state" -TimeoutSec 5
            if ($taskState.running) {
                throw 'Image generation is running. Stop it on the web page before closing the server.'
            }""",
        """            # /api/state requires login; a command-line run has no session cookie.
            # If it is unavailable, do not block the stop flow - just say so.
            $taskRunning = $null
            try {
                $taskState = Invoke-RestMethod "$taskUrl/api/state" -TimeoutSec 5
                $taskRunning = [bool]$taskState.running
            } catch {
                Write-Output 'Note: /api/state needs login; skipping the running-task check.'
            }
            if ($taskRunning) {
                throw 'Image generation is running. Stop it on the web page before closing the server.'
            }""",
    ),
    (
        """        for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
            if ($taskChild.HasExited) { throw "Server exited. See $taskErr" }
            try {
                $taskState = Invoke-RestMethod "$taskUrl/api/state" -TimeoutSec 2
                if ($taskState.stores.Count -eq 23) { $taskReady = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 500
        }""",
        """        for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
            # Do NOT fail on $taskChild.HasExited: the launcher spawns a child and
            # exits, so HasExited can be true while the server is already listening.
            try {
                # Readiness must use a PUBLIC endpoint. /api/state needs batch.read
                # and returns 401 without a session, which used to make a healthy
                # server look like a failed start.
                $taskState = Invoke-RestMethod "$taskUrl/api/auth/status" -TimeoutSec 2
                if ($null -ne $taskState.needs_setup) { $taskReady = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 500
        }""",
    ),
]


def main() -> int:
    raw = TARGET.read_bytes()
    # 统一解码（兼容 BOM / 无 BOM / CRLF）
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")

    changed = 0
    for old, new in REPLACEMENTS:
        old_n = old.replace("\r\n", "\n")
        if old_n in text:
            text = text.replace(old_n, new, 1)
            changed += 1
            print(f"  ✅ 已替换片段（{old_n.splitlines()[0].strip()[:40]}…）")
        else:
            print(f"  ⏭  未找到片段（可能已修复）：{old_n.splitlines()[0].strip()[:40]}…")

    # 关键：写成 UTF-8 with BOM，Windows PowerShell 5.1 才能正确解析中文注释
    TARGET.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
    print(f"\n✅ 已写回 local-server.ps1（UTF-8 with BOM，替换 {changed} 处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
