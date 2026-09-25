Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & Replace(WScript.ScriptFullName, "停止FLUX本地验证.vbs", "tools\stop_flux_local.ps1") & """", 0, False
