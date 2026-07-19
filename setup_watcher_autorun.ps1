# Sets up the Kalshi edge watcher to start automatically at every logon, with no
# admin rights required, by placing a shortcut in your Startup folder. The
# watcher scans live markets in the background, logs edges to data/edge_alerts.csv,
# and sends phone (ntfy) + desktop notifications when a genuine edge appears.
#
# Run once:  powershell -ExecutionPolicy Bypass -File setup_watcher_autorun.ps1
# Remove:    Remove-Item "$([Environment]::GetFolderPath('Startup'))\KalshiEdgeWatcher.lnk"

$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $MyInvocation.MyCommand.Path
# The venv may live inside the project or outside OneDrive (per-machine).
$pyw = @(
    (Join-Path $proj ".venv\Scripts\pythonw.exe"),
    (Join-Path $env:USERPROFILE "venvs\kalssh\Scripts\pythonw.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pyw) { throw "No venv found (.venv or ~\venvs\kalssh); see README setup." }

$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup "KalshiEdgeWatcher.lnk"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $pyw
$sc.Arguments  = "-m src.cli watch --interval 120"
$sc.WorkingDirectory = $proj
$sc.WindowStyle = 7   # minimized / hidden (pythonw shows no window anyway)
$sc.Description = "Kalshi edge watcher (paper/research; notifies on genuine edges)."
$sc.Save()

Write-Output "Auto-start shortcut created: $lnk"
Write-Output "The watcher will launch automatically at every logon."
Write-Output "To start it right now without logging out:"
Write-Output "    Start-Process '$pyw' -ArgumentList '-m src.cli watch --interval 120' -WorkingDirectory '$proj' -WindowStyle Hidden"
