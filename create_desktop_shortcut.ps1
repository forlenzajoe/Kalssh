# Creates a "Kalshi Weather Scanner" shortcut on your Desktop that launches the
# dashboard as a native desktop window (no browser, no localhost typing).
# Re-run this any time to recreate the shortcut.

$ErrorActionPreference = "Stop"
$proj    = Split-Path -Parent $MyInvocation.MyCommand.Path
# The venv may live inside the project or outside OneDrive (per-machine).
$pyw = @(
    (Join-Path $proj ".venv\Scripts\pythonw.exe"),
    (Join-Path $env:USERPROFILE "venvs\kalssh\Scripts\pythonw.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
$target  = Join-Path $proj "desktop_app.py"
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop "Kalshi Weather Scanner.lnk"

if (-not $pyw) {
    throw "No virtual environment found (.venv or ~\venvs\kalssh). Create one first (see README)."
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath       = $pyw
$sc.Arguments        = '"' + $target + '"'
$sc.WorkingDirectory = $proj
$sc.WindowStyle      = 1
$sc.IconLocation     = "$env:SystemRoot\System32\imageres.dll,109"  # a weather-ish icon
$sc.Description      = "Kalshi Weather Mispricing Scanner (paper-only)"
$sc.Save()

Write-Output "Created shortcut: $lnkPath"
