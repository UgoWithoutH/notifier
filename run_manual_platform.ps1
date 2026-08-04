<#
Runs the LOCAL-ONLY manual-login helpers for Mintos and Lande, one after
the other. Both open a real browser window and may need you to solve a
CAPTCHA/Turnstile challenge by hand - just follow the on-screen prompts.
Each platform still runs even if the other one fails.

By default, fetches the CURRENT month. To fetch an OLDER month instead,
enter a date somewhere in that month when prompted at startup (French
format JJ/MM/AAAA, e.g. "30/06/2026" to get June 2026's full totals) -
just press Enter to leave it blank and use today. It's forwarded as the
REPORT_DATE environment variable (see shared/report_date.py). You can also
pass -Date to skip the prompt (e.g. for a non-interactive call).

Usage: .\run_manual_platform.ps1
       .\run_manual_platform.ps1 -Date "30/06/2026"
#>

param(
    [string]$Date = ""
)

Set-Location $PSScriptRoot

if (-not $Date) {
    $Date = Read-Host "Date (JJ/MM/AAAA) pour un mois précédent, ou laissez vide pour aujourd'hui"
}

if ($Date) {
    $env:REPORT_DATE = $Date
    Write-Host "Using REPORT_DATE=$Date (instead of today)" -ForegroundColor Yellow
} else {
    Remove-Item Env:\REPORT_DATE -ErrorAction SilentlyContinue
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
