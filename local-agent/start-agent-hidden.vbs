Set objShell = CreateObject("WScript.Shell")
objShell.Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & Replace(WScript.ScriptFullName, "start-agent-hidden.vbs", "start-agent.ps1") & """", 0, False
