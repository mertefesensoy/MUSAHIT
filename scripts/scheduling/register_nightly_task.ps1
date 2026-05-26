<#
.SYNOPSIS
    Register the MUSAHIT_Nightly scheduled task on this machine.

.DESCRIPTION
    Reads nightly_task.xml.template, substitutes placeholders with values
    derived from the current environment, and registers the task via
    Register-ScheduledTask (falls back to schtasks /Create /XML).

    Run from any directory; paths are computed from $PSScriptRoot.

.NOTES
    May require elevation for WakeToRun capability. The script checks and
    warns if not elevated.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "MUSAHIT_Nightly"

# ── Resolve paths ──────────────────────────────────────────────────────────

$ScriptDir   = $PSScriptRoot
$ProjectRoot = (Get-Item $ScriptDir).Parent.Parent.FullName
$RunScript   = Join-Path $ScriptDir "run_nightly.ps1"
$TemplatePath = Join-Path $ScriptDir "nightly_task.xml.template"
$LogDir      = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $TemplatePath)) {
    Write-Error "Template not found: $TemplatePath"
    exit 1
}
if (-not (Test-Path $RunScript)) {
    Write-Error "Run script not found: $RunScript"
    exit 1
}

# ── Check elevation ────────────────────────────────────────────────────────

$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $IsAdmin) {
    Write-Warning "Not running as Administrator. WakeToRun may fail to register."
    Write-Warning "If registration fails, re-run this script from an elevated terminal."
}

# ── Resolve pwsh.exe absolute path ─────────────────────────────────────────
# Two paths on Windows contain "WindowsApps" but only one is the problem:
#   - alias stub      C:\Users\<user>\AppData\Local\Microsoft\WindowsApps\pwsh.exe
#   - real Store bin  C:\Program Files\WindowsApps\Microsoft.PowerShell_*\pwsh.exe
# Both substring-match "WindowsApps". Only the first is unusable from Task
# Scheduler (zero-byte AppX stub → 0x80070002). The real Store binary works
# fine. Filter on the user-local alias subpath only so probe A keeps the
# Store binary in the candidate set.
# Probe order: MSI/winget install first, then Store binary, then error.

$PwshExe = $null

# Probe A: any pwsh.exe that is NOT the user-local execution alias stub.
$candidates = Get-Command pwsh.exe -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -notlike "*\AppData\Local\Microsoft\WindowsApps\*" } |
    Select-Object -First 1
if ($candidates) {
    $PwshExe = $candidates.Source
}

# Probe B/C: well-known MSI install paths.
if (-not $PwshExe -and (Test-Path "C:\Program Files\PowerShell\7\pwsh.exe")) {
    $PwshExe = "C:\Program Files\PowerShell\7\pwsh.exe"
}
if (-not $PwshExe -and (Test-Path "C:\Program Files\PowerShell\7-preview\pwsh.exe")) {
    $PwshExe = "C:\Program Files\PowerShell\7-preview\pwsh.exe"
}

# Probe D: Store binary — the real EXE under Program Files\WindowsApps\
# (not the alias stub under AppData\Local\Microsoft\WindowsApps\).
# The path is version-pinned and changes on update; resolve dynamically.
if (-not $PwshExe) {
    $storeCandidate = Get-Command pwsh.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -like "*\Program Files\WindowsApps\*" } |
        Select-Object -First 1
    if ($storeCandidate) {
        $PwshExe = $storeCandidate.Source
    }
}

if (-not $PwshExe) {
    Write-Error @"
Cannot find a real pwsh.exe binary (only the WindowsApps alias was found).
Install PowerShell 7 via winget or the MSI installer:
  winget install Microsoft.PowerShell
Then re-run this script.
"@
    exit 1
}

# ── Resolve user ID ────────────────────────────────────────────────────────

$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# ── Compute start boundary (tomorrow at 02:00) ────────────────────────────

$Tomorrow = (Get-Date).AddDays(1).Date.AddHours(2)
$StartBoundary = $Tomorrow.ToString("yyyy-MM-ddTHH:mm:ss")

# ── Substitute placeholders ────────────────────────────────────────────────

$Xml = Get-Content -Path $TemplatePath -Raw -Encoding utf8
$Xml = $Xml -replace '\{\{PWSH_EXE\}\}',         [System.Security.SecurityElement]::Escape($PwshExe)
$Xml = $Xml -replace '\{\{PROJECT_ROOT\}\}',    [System.Security.SecurityElement]::Escape($ProjectRoot)
$Xml = $Xml -replace '\{\{RUN_SCRIPT\}\}',       [System.Security.SecurityElement]::Escape($RunScript)
$Xml = $Xml -replace '\{\{USER_ID\}\}',           [System.Security.SecurityElement]::Escape($UserId)
$Xml = $Xml -replace '\{\{LOG_DIR\}\}',            [System.Security.SecurityElement]::Escape($LogDir)
$Xml = $Xml -replace '\{\{START_BOUNDARY\}\}',    $StartBoundary

Write-Output "=== MUSAHIT Nightly Task Registration ==="
Write-Output ""
Write-Output "  pwsh.exe     : $PwshExe"
Write-Output "  Project root : $ProjectRoot"
Write-Output "  Run script   : $RunScript"
Write-Output "  Log directory : $LogDir"
Write-Output "  User          : $UserId"
Write-Output "  First trigger : $StartBoundary (then daily at 02:00)"
Write-Output ""

# ── Register ───────────────────────────────────────────────────────────────

# Remove existing task if present (idempotent re-registration).
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Output "  Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
} catch {
    # Task doesn't exist yet — normal path.
}

try {
    Register-ScheduledTask -TaskName $TaskName -Xml $Xml | Out-Null
} catch {
    Write-Warning "Register-ScheduledTask failed: $_"
    Write-Output "  Falling back to schtasks /Create /XML..."

    $TempXml = Join-Path $env:TEMP "musahit_nightly_task.xml"
    $Xml | Out-File -FilePath $TempXml -Encoding utf8
    & schtasks /Create /TN $TaskName /XML $TempXml /F
    Remove-Item $TempXml -ErrorAction SilentlyContinue

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Both registration methods failed. Run as Administrator and retry."
        exit 1
    }
}

# ── Verify ─────────────────────────────────────────────────────────────────

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Error "Registration failed: task '$TaskName' not found after registration attempt."
    exit 1
}

Write-Output ""
Write-Output "  [OK] Task '$TaskName' registered and verified, state = $($task.State)"
Write-Output ""
Write-Output "  To test-fire immediately:"
Write-Output "    schtasks /Run /TN `"$TaskName`""
Write-Output ""
Write-Output "  To check the log after test-fire:"
Write-Output "    Get-ChildItem `"$LogDir`" | Sort-Object LastWriteTime -Descending | Select-Object -First 1"
Write-Output ""
