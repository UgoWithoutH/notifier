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

Add-Type -Namespace Win32 -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern System.IntPtr GetStdHandle(int nStdHandle);
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern bool GetConsoleMode(System.IntPtr hConsole, out uint mode);
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern bool SetConsoleMode(System.IntPtr hConsole, uint mode);
'@

# Console QuickEdit freezes the whole process as soon as the window is clicked
# (text selection) until a key is pressed. Turn it off for this console.
$stdin = [Win32.Native]::GetStdHandle(-10)
$consoleMode = [uint32]0
if ([Win32.Native]::GetConsoleMode($stdin, [ref]$consoleMode)) {
    $ENABLE_QUICK_EDIT = [uint32]0x40
    $ENABLE_EXTENDED_FLAGS = [uint32]0x80
    [void][Win32.Native]::SetConsoleMode($stdin, (($consoleMode -bor $ENABLE_EXTENDED_FLAGS) -band (-bnot $ENABLE_QUICK_EDIT)))
}

# Start-Sleep does not count time spent while the PC is idle/asleep, so everything
# froze until a key press woke the machine. Block sleep for the whole run and
# wait on the wall clock.
$ES_CONTINUOUS = [uint32]2147483648
$ES_SYSTEM_REQUIRED = [uint32]1
[void][Win32.Native]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)

try {
    $delayMinutes = Get-Random -Minimum $MinDelayMinutes -Maximum ($MaxDelayMinutes + 1)
    Write-Host ("Waiting {0} minute(s) before starting the platforms..." -f $delayMinutes) -ForegroundColor Yellow

    if ($delayMinutes -gt 0) {
        $deadline = [DateTime]::UtcNow.AddMinutes($delayMinutes)
        while (($remaining = $deadline - [DateTime]::UtcNow) -gt [TimeSpan]::Zero) {
            Write-Host ("`rStarting in {0:00}:{1:00} " -f [int][Math]::Floor($remaining.TotalMinutes), $remaining.Seconds) -NoNewline -ForegroundColor Yellow
            Start-Sleep -Seconds 1
        }
        Write-Host "`rStarting now.        "
    }

    Write-Host "=== Mintos ===" -ForegroundColor Cyan
    .\.venv\Scripts\python.exe -m diversification.mintos_get_session
    $mintosExit = $LASTEXITCODE

    Write-Host "`n=== Lande ===" -ForegroundColor Cyan
    .\.venv\Scripts\python.exe -m diversification.lande_get_session
    $landeExit = $LASTEXITCODE
}
finally {
    [void][Win32.Native]::SetThreadExecutionState($ES_CONTINUOUS)
}

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
Write-Host ("Mintos: {0}" -f $(if ($mintosExit -eq 0) { "OK" } else { "FAILED (exit $mintosExit)" }))
Write-Host ("Lande:  {0}" -f $(if ($landeExit -eq 0) { "OK" } else { "FAILED (exit $landeExit)" }))
