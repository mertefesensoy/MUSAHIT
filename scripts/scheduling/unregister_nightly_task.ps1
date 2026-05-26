<#
.SYNOPSIS
    Remove the MUSAHIT_Nightly scheduled task from this machine.

.DESCRIPTION
    Stops the task if currently running, unregisters it, and verifies removal.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "MUSAHIT_Nightly"

# ── Check if task exists ───────────────────────────────────────────────────

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Output "Task '$TaskName' does not exist. Nothing to remove."
    exit 0
}

# ── Stop if running ────────────────────────────────────────────────────────

if ($task.State -eq "Running") {
    Write-Output "Task is currently running. Stopping..."
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

# ── Unregister ─────────────────────────────────────────────────────────────

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "Task '$TaskName' unregistered."

# ── Verify ─────────────────────────────────────────────────────────────────

try {
    $null = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Error "Verification failed: task '$TaskName' still exists."
    exit 1
} catch {
    Write-Output "Verified: task '$TaskName' is gone."
}
