<#
.SYNOPSIS
    Entry point for the MUSAHIT nightly pipeline. Invoked by Task Scheduler.

.DESCRIPTION
    1. Resolves the project root from this script's location.
    2. Ensures Ollama is running (starts it if not, waits up to 30 s).
    3. Creates the log directory and log file.
    4. Runs  python -m musahit.pipeline run --date today
    5. Captures stdout+stderr to both console and log file.
    6. Exits with the pipeline's exit code.

.PARAMETER LogDir
    Override the log directory. Defaults to <project_root>\logs.
#>

param(
    [string]$LogDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Resolve paths ──────────────────────────────────────────────────────────

# scripts/scheduling/run_nightly.ps1 → project root is two levels up.
$ProjectRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $ProjectRoot

if (-not $LogDir) {
    $LogDir = Join-Path $ProjectRoot "logs"
}
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogFile   = Join-Path $LogDir "nightly-$Timestamp.jsonl"

# ── Ensure Ollama is running ───────────────────────────────────────────────

function Test-OllamaReady {
    try {
        $null = & ollama list 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (-not (Test-OllamaReady)) {
    Write-Output "[run_nightly] Ollama not responding. Starting ollama serve..."
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden

    $ready = $false
    for ($i = 1; $i -le 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-OllamaReady) {
            $ready = $true
            Write-Output "[run_nightly] Ollama ready after ${i}s."
            break
        }
    }
    if (-not $ready) {
        $msg = "[run_nightly] ERROR: Ollama failed to start within 30 seconds."
        Write-Output $msg
        $msg | Out-File -FilePath $LogFile -Encoding utf8
        exit 1
    }
}

# ── Run pipeline ───────────────────────────────────────────────────────────

Write-Output "[run_nightly] Starting pipeline at $(Get-Date -Format o)"
Write-Output "[run_nightly] Log file: $LogFile"
Write-Output "[run_nightly] Project root: $ProjectRoot"

& python -m musahit.pipeline run --date today 2>&1 | Tee-Object -FilePath $LogFile

$PipelineExit = $LASTEXITCODE

Write-Output "[run_nightly] Pipeline exited with code $PipelineExit at $(Get-Date -Format o)"

exit $PipelineExit
