param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $root "data"
$pidFile = Join-Path $dataDir "app.pid"
$logFile = Join-Path $dataDir "app.log"
$errorLogFile = Join-Path $dataDir "app-error.log"
$port = 8000
$url = "http://127.0.0.1:$port"

function Get-AppProcess {
    if (-not (Test-Path $pidFile)) { return $null }
    $savedPid = (Get-Content $pidFile -Raw).Trim()
    if (-not ($savedPid -as [int])) { return $null }
    return Get-Process -Id ([int]$savedPid) -ErrorAction SilentlyContinue
}

function Remove-StalePid {
    if (Test-Path $pidFile) { Remove-Item -LiteralPath $pidFile -Force }
}

function Test-PortInUse {
    return @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
}

function Start-App {
    $existing = Get-AppProcess
    if ($existing) {
        Write-Host "Application is already running. PID: $($existing.Id)"
        Start-Process $url
        return
    }
    Remove-StalePid
    if (Test-PortInUse) {
        throw "Port $port is already in use."
    }

    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw "Python was not found. Run install.ps1 first." }
        $python = $pythonCommand.Source
    }
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $process = Start-Process -FilePath $python -ArgumentList @("python_login_app.py") `
        -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $logFile `
        -RedirectStandardError $errorLogFile -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

    $ready = $false
    1..15 | ForEach-Object {
        Start-Sleep -Seconds 1
        if (Test-PortInUse) { $ready = $true }
    }
    if (-not $ready) {
        Remove-StalePid
        throw "Application did not start. Check data\app-error.log."
    }
    Write-Host "Application started. PID: $($process.Id)"
    Write-Host "Console: $url"
    Start-Process $url
}

function Stop-App {
    $process = Get-AppProcess
    if (-not $process) {
        Remove-StalePid
        Write-Host "Application is not running."
        return
    }
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$url/close" -Method Post -TimeoutSec 5 | Out-Null
    } catch {
        Write-Host "Could not close the browser through the app API."
    }
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Remove-StalePid
    Write-Host "Application stopped."
}

function Show-Status {
    $process = Get-AppProcess
    if ($process) {
        Write-Host "Running. PID: $($process.Id), URL: $url"
    } else {
        Remove-StalePid
        Write-Host "Not running."
    }
}

switch ($Action) {
    "start" { Start-App }
    "stop" { Stop-App }
    "restart" { Stop-App; Start-App }
    "status" { Show-Status }
}
