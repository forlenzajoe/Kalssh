# Creates a "Kalshi Weather Scanner" shortcut on your Desktop that launches the
# dashboard as a native desktop window (no browser, no localhost typing).
# Re-run this any time to recreate the shortcut.

$ErrorActionPreference = "Stop"
$proj    = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw     = Join-Path $proj ".venv\Scripts\pythonw.exe"
$target  = Join-Path $proj "desktop_app.py"
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop "Kalshi Weather Scanner.lnk"

if (-not (Test-Path $pyw)) {
    throw "Virtual environment not found at $pyw. Create it first (see README)."
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
