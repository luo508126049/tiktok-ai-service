$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    & py -3 -m venv .venv
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python 3.10 or newer is required. Install Python and run this script again."
    }
    & python -m venv .venv
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt
& $venvPython -m playwright install chromium

Write-Host "Environment installed. Start the app with:"
Write-Host ".\.venv\Scripts\python.exe python_login_app.py"
