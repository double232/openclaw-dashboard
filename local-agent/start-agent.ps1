$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logFile = Join-Path $scriptDir "agent.log"
$python = if ($env:OPENCLAW_PYTHON) { $env:OPENCLAW_PYTHON } else { "python" }

Set-Location $scriptDir

while ($true) {
    try {
        & $python agent.py 2>&1 |
            ForEach-Object { $_ | Out-File -FilePath $logFile -Append -Encoding utf8 }
    } catch {}
    "$(Get-Date -Format o) Agent exited. Restarting in 5 seconds..." |
        Out-File -FilePath $logFile -Append -Encoding utf8
    Start-Sleep -Seconds 5
}
