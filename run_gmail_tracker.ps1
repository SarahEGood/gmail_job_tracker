$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project ".venv\Scripts\python.exe"
$logDir = Join-Path $project "logs"
$logFile = Join-Path $logDir "gmail-job-tracker.log"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Follow the README setup first."
}

if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

Push-Location $project
try {
    & $python ".\gmail_job_tracker.py" *>&1 |
        Tee-Object -FilePath $logFile -Append
    if ($LASTEXITCODE -ne 0) {
        throw "Gmail job tracker exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
