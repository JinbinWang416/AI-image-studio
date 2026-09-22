Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & Replace(WScript.ScriptFullName, "启动FLUX本地验证.vbs", "tools\start_flux_local.ps1") & """", 0, False
