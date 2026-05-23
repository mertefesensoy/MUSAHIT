<#
.SYNOPSIS
    MÜŞAHİT first end-to-end smoke run.

.DESCRIPTION
    Runs the full nightly pipeline against real Turkish sources and real
    Ollama / Piper models for the first time. Performs five pre-flight
    checks before invoking ``python -m musahit.pipeline run --date <date>``;
    if every check passes, runs the pipeline with a tee-captured log; on
    completion prints a summary and a pointer to the briefing artifacts;
    on failure prints the structured-log tail and suggests next-step
    diagnostics.

    This is intentionally verbose · the first run is the operator's
    primary learning opportunity. Subsequent runs use Task Scheduler
    (ADR-007) and need none of this hand-holding.

.PARAMETER Date
    The TR-local date to process. ``"today"`` (default) or YYYY-MM-DD.
    Passed straight through to ``python -m musahit.pipeline``.

.PARAMETER DataDir
    Directory the DuckDB file lives in. Used for the disk-space check.
    Default: ``"data"`` (relative to repo root).

.PARAMETER VoicePath
    Absolute path to the Piper voice ONNX. Default reads from
    ``$env:LOCALAPPDATA\piper\voices\tr_TR-dfki-medium.onnx``.

.PARAMETER MinFreeGB
    Disk-space floor. The pipeline's own pre-flight check uses the same
    threshold via ``Settings.min_free_disk_gb``; we check here so the
    operator sees the gap before the pipeline starts.

.PARAMETER SkipPreflight
    Skip the five pre-flight checks. Use only when re-running after a
    failure and the operator has already confirmed the environment is
    healthy.

.EXAMPLE
    .\scripts\run_first_smoke.ps1

.EXAMPLE
    .\scripts\run_first_smoke.ps1 -Date 2026-05-23

.EXAMPLE
    .\scripts\run_first_smoke.ps1 -SkipPreflight
#>

[CmdletBinding()]
param(
    [string]$Date = "today",
    [string]$DataDir = "data",
    [string]$VoicePath = "$env:LOCALAPPDATA\piper\voices\tr_TR-dfki-medium.onnx",
    [int]$MinFreeGB = 5,
    [switch]$SkipPreflight
)

$ErrorActionPreference = "Stop"

# Models we expect Ollama to have. Names match Settings.{worker,writer,embed}_model.
$RequiredModels = @(
    "qwen2.5:7b-instruct-q4_K_M",
    "serkandyck/trendyol-llm-7b-chat-v1.8-gguf",
    "bge-m3"
)

# Log file capture · tee pipeline stdout here so the on-failure tail has data.
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logPath = Join-Path $logDir "smoke-$timestamp.jsonl"

function Write-Section($title) {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
    Write-Host "❯ $title" -ForegroundColor Cyan
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
}

function Write-Check($label, $ok, $detail = "") {
    if ($ok) {
        Write-Host "  [PASS] $label" -ForegroundColor Green -NoNewline
    } else {
        Write-Host "  [FAIL] $label" -ForegroundColor Red -NoNewline
    }
    if ($detail) { Write-Host "  ($detail)" -ForegroundColor DarkGray }
    else { Write-Host "" }
}

# ── Pre-flight checks ──────────────────────────────────────────────────────

$preflightPassed = $true

if (-not $SkipPreflight) {
    Write-Section "Pre-flight checks"

    # Check 1: Ollama models present
    try {
        $ollamaOutput = ollama list 2>&1 | Out-String
        foreach ($model in $RequiredModels) {
            $found = $ollamaOutput -match [regex]::Escape($model)
            Write-Check "Ollama model present: $model" $found
            if (-not $found) {
                Write-Host "      Pull with: ollama pull $model" -ForegroundColor Yellow
                $preflightPassed = $false
            }
        }
    } catch {
        Write-Check "Ollama is reachable" $false "is 'ollama serve' running?"
        $preflightPassed = $false
    }

    # Check 2: Piper voice ONNX exists
    $voiceOk = Test-Path $VoicePath
    Write-Check "Piper voice ONNX exists at $VoicePath" $voiceOk
    if (-not $voiceOk) {
        Write-Host "      Download via scripts/install_windows.ps1 or place the file manually" -ForegroundColor Yellow
        $preflightPassed = $false
    }

    # Check 3: Disk space
    $dataDirFull = if (Test-Path $DataDir) { (Resolve-Path $DataDir).Path } else { (Get-Location).Path }
    $drive = (Get-Item $dataDirFull).PSDrive
    if ($null -eq $drive) {
        # Fall back to drive of cwd.
        $drive = (Get-Item (Get-Location)).PSDrive
    }
    $freeGB = [math]::Round($drive.Free / 1GB, 2)
    $diskOk = $freeGB -ge $MinFreeGB
    Write-Check "Free disk on $($drive.Name): ≥ $MinFreeGB GB" $diskOk "$freeGB GB free"
    if (-not $diskOk) { $preflightPassed = $false }

    # Check 4: DuckDB migration version is current.
    # Expected: 3 (initial schema, article metadata, failed_stages).
    $expectedMigrationVersion = 3
    $dbPath = Join-Path $DataDir "musahit.duckdb"
    if (-not (Test-Path $dbPath)) {
        Write-Check "DuckDB present at $dbPath" $false "run: python scripts/init_db.py"
        $preflightPassed = $false
    } else {
        try {
            $migrationVersion = python -c "import duckdb; conn = duckdb.connect('$($dbPath -replace '\\','/')'); r = conn.execute('SELECT MAX(version) FROM schema_version').fetchone(); print(r[0] if r and r[0] is not None else 0)" 2>&1
            $migrationVersionInt = [int]$migrationVersion
            $migrationOk = $migrationVersionInt -eq $expectedMigrationVersion
            Write-Check "DuckDB migration version = $expectedMigrationVersion" $migrationOk "current: $migrationVersionInt"
            if (-not $migrationOk) {
                Write-Host "      Apply pending migrations: python scripts/init_db.py" -ForegroundColor Yellow
                $preflightPassed = $false
            }
        } catch {
            Write-Check "DuckDB readable" $false "$($_.Exception.Message)"
            $preflightPassed = $false
        }
    }

    # Check 5: data dir writable (smoke check; mkdir is idempotent)
    try {
        $null = New-Item -ItemType Directory -Force -Path $DataDir -ErrorAction Stop
        Write-Check "$DataDir writable" $true
    } catch {
        Write-Check "$DataDir writable" $false "$($_.Exception.Message)"
        $preflightPassed = $false
    }

    if (-not $preflightPassed) {
        Write-Host ""
        Write-Host "Pre-flight checks failed · fix the [FAIL] items and rerun." -ForegroundColor Red
        Write-Host "Skip pre-flight (only if you've fixed things by hand):" -ForegroundColor DarkGray
        Write-Host "    .\scripts\run_first_smoke.ps1 -SkipPreflight" -ForegroundColor DarkGray
        exit 1
    }

    Write-Host ""
    Write-Host "All pre-flight checks passed." -ForegroundColor Green
} else {
    Write-Section "Pre-flight checks · SKIPPED via -SkipPreflight"
}

# ── Run the pipeline ───────────────────────────────────────────────────────

Write-Section "Running pipeline (date=$Date)"
Write-Host "  command: python -m musahit.pipeline run --date $Date"
Write-Host "  log file: $logPath"
Write-Host ""

$pipelineStart = Get-Date
python -m musahit.pipeline run --date $Date 2>&1 | Tee-Object -FilePath $logPath
$pipelineExit = $LASTEXITCODE
$pipelineEnd = Get-Date
$elapsed = $pipelineEnd - $pipelineStart

# ── Summary or diagnostics ────────────────────────────────────────────────

Write-Section "Pipeline finished"
Write-Host "  exit code      : $pipelineExit"
Write-Host "  total elapsed  : $([int]$elapsed.TotalMinutes) min $([int]$elapsed.Seconds) sec"
Write-Host "  log captured   : $logPath"

if ($pipelineExit -eq 0) {
    Write-Host ""
    Write-Host "Pipeline COMPLETED · ✓" -ForegroundColor Green
    Write-Host ""

    # Locate today's briefing artifacts.
    if ($Date -eq "today") {
        $isoDate = (Get-Date).ToString("yyyy-MM-dd")
    } else {
        $isoDate = $Date
    }
    $yyyy, $mm, $dd = $isoDate -split "-"
    $briefingDir = Join-Path "briefings" "$yyyy\$mm\$dd"

    Write-Host "Briefing artifacts (expected location: $briefingDir):"
    if (Test-Path $briefingDir) {
        Get-ChildItem -Path $briefingDir -File | ForEach-Object {
            Write-Host "    $($_.FullName) ($([int]($_.Length / 1024)) KB)" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "    (directory not found · pipeline may have failed mid-stage)" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Next:"
    Write-Host "  1. Read briefings/$yyyy/$mm/$dd/briefing.md"
    Write-Host "  2. Listen to briefings/$yyyy/$mm/$dd/briefing.mp3"
    Write-Host "  3. Run quick DB checks:"
    Write-Host "       python -m musahit.pipeline status --date $Date"
    Write-Host "  4. File any surprises to memory/operator-tasks.md"
    Write-Host ""
    exit 0
} elseif ($pipelineExit -eq 2) {
    Write-Host ""
    Write-Host "Pipeline aborted via SIGINT (Ctrl-C) · stages_done preserved" -ForegroundColor Yellow
    Write-Host "Resume with: python -m musahit.pipeline resume --date $Date" -ForegroundColor DarkGray
    exit 2
} else {
    # exit_code == 1 (FAILED) or anything else.
    Write-Host ""
    Write-Host "Pipeline FAILED (exit code $pipelineExit)" -ForegroundColor Red
    Write-Host ""

    Write-Host "Structured-log tail (last 50 lines of $logPath):" -ForegroundColor Cyan
    Write-Host "──────────────────────────────────────────────────────────────"
    Get-Content $logPath -Tail 50 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    Write-Host "──────────────────────────────────────────────────────────────"
    Write-Host ""

    Write-Host "Next-step diagnostics:" -ForegroundColor Cyan
    Write-Host "  1. Check which stages completed: python -m musahit.pipeline status --date $Date"
    Write-Host "  2. Inspect failed_stages JSON in pipeline_runs row"
    Write-Host "  3. Search log for 'stage_failed' or 'Traceback':"
    Write-Host "       Select-String -Path $logPath -Pattern 'stage_failed|Traceback'"
    Write-Host "  4. Targeted retry of a single stage:"
    Write-Host "       python -m musahit.pipeline run --date $Date --stage <name>"
    Write-Host "  5. Resume from last completed stage:"
    Write-Host "       python -m musahit.pipeline resume --date $Date"
    Write-Host "  6. File the finding to memory/operator-tasks.md before moving on"
    Write-Host ""
    exit 1
}
