<#
Runs the LOCAL-ONLY manual-login helpers for Mintos and Lande, one after
the other, for the CURRENT month, with no prompt. Each platform still runs
even if the other one fails.

Usage: .\run_manual_platform_current_month.ps1
#>

$MinDelayMinutes = 0
$MaxDelayMinutes = 2

if ($MinDelayMinutes -lt 0 -or $MaxDelayMinutes -lt $MinDelayMinutes) {
    Write-Error "Delay bounds must be non-negative and the maximum must be greater than or equal to the minimum."
    exit 1
}

Set-Location $PSScriptRoot

Remove-Item Env:\REPORT_DATE -ErrorAction SilentlyContinue
Remove-Item Env:\REPORT_DATE_MONTHS -ErrorAction SilentlyContinue

$delayMinutes = Get-Random -Minimum $MinDelayMinutes -Maximum ($MaxDelayMinutes + 1)
Write-Host ("Waiting {0} minute(s) before starting the platforms..." -f $delayMinutes) -ForegroundColor Yellow

if ($delayMinutes -gt 0) {
    # Start-Sleep does not count time spent while the PC is idle/asleep, so the wait
    # froze until a key press woke the machine. Block sleep and wait on the wall clock.
    Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
    $ES_CONTINUOUS = [uint32]2147483648
    $ES_SYSTEM_REQUIRED = [uint32]1
    [void][Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
    try {
        $deadline = [DateTime]::UtcNow.AddMinutes($delayMinutes)
        while (($remaining = $deadline - [DateTime]::UtcNow) -gt [TimeSpan]::Zero) {
            Write-Host ("`rStarting in {0:00}:{1:00} " -f [int][Math]::Floor($remaining.TotalMinutes), $remaining.Seconds) -NoNewline -ForegroundColor Yellow
            Start-Sleep -Seconds 1
        }
        Write-Host "`rStarting now.        "
    }
    finally {
        [void][Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS)
    }
}

Write-Host "=== Mintos ===" -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m diversification.mintos_get_session
$mintosExit = $LASTEXITCODE

Write-Host "`n=== Lande ===" -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m diversification.lande_get_session
$landeExit = $LASTEXITCODE

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
Write-Host ("Mintos: {0}" -f $(if ($mintosExit -eq 0) { "OK" } else { "FAILED (exit $mintosExit)" }))
Write-Host ("Lande:  {0}" -f $(if ($landeExit -eq 0) { "OK" } else { "FAILED (exit $landeExit)" }))
