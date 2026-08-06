<#
Runs the LOCAL-ONLY manual-login helpers for Mintos and Lande, one after
the other. Both open a real browser window and may need you to solve a
CAPTCHA/Turnstile challenge by hand - just follow the on-screen prompts.
Each platform still runs even if the other one fails.

By default (nothing passed, or everything left blank at the prompt),
fetches the CURRENT month. To backfill a single past/future month, or a
range of several months (mirrors the "Diversification Reports" GitHub
workflow's start_month/end_month inputs), pass -StartMonth/-EndMonth
(MM/AAAA, inclusive range) - Mintos and Lande each log in/open the
browser only ONCE, then reuse that same session for every month in the
range (REPORT_DATE set to the LAST day of each month in turn). Passing
just one of the two treats it as a single month.

Usage: .\run_manual_platform.ps1
       .\run_manual_platform.ps1 -StartMonth "06/2026"
       .\run_manual_platform.ps1 -StartMonth "01/2026" -EndMonth "06/2026"
#>

param(
    [string]$StartMonth = "",
    [string]$EndMonth = ""
)

Set-Location $PSScriptRoot

if (-not $StartMonth -and -not $EndMonth) {
    $StartMonth = Read-Host "Mois de début (MM/AAAA), ou laissez vide pour le mois en cours"
    if ($StartMonth) {
        $EndMonth = Read-Host "Mois de fin (MM/AAAA, inclus), ou laissez vide pour ne traiter que le mois de début"
    }
}

# A single month passed either way is a valid 1-month range.
if ($StartMonth -and -not $EndMonth) { $EndMonth = $StartMonth }
if ($EndMonth -and -not $StartMonth) { $StartMonth = $EndMonth }

function Invoke-Platforms {
    Write-Host "=== Mintos ===" -ForegroundColor Cyan
    .\.venv\Scripts\python.exe -m diversification.mintos_get_session
    $script:mintosExit = $LASTEXITCODE

    Write-Host "`n=== Lande ===" -ForegroundColor Cyan
    .\.venv\Scripts\python.exe -m diversification.lande_get_session
    $script:landeExit = $LASTEXITCODE
}

if (-not $StartMonth -and -not $EndMonth) {
    Remove-Item Env:\REPORT_DATE -ErrorAction SilentlyContinue
    Invoke-Platforms

    Write-Host "`n=== Summary ===" -ForegroundColor Cyan
    Write-Host ("Mintos: {0}" -f $(if ($mintosExit -eq 0) { "OK" } else { "FAILED (exit $mintosExit)" }))
    Write-Host ("Lande:  {0}" -f $(if ($landeExit -eq 0) { "OK" } else { "FAILED (exit $landeExit)" }))
    exit 0
}

$monthPattern = '^(0[1-9]|1[0-2])/\d{4}$'
if ($StartMonth -notmatch $monthPattern) {
    Write-Host "StartMonth '$StartMonth' n'est pas au format MM/AAAA." -ForegroundColor Red
    exit 1
}
if ($EndMonth -notmatch $monthPattern) {
    Write-Host "EndMonth '$EndMonth' n'est pas au format MM/AAAA." -ForegroundColor Red
    exit 1
}

$startParts = $StartMonth -split '/'
$endParts = $EndMonth -split '/'
$current = Get-Date -Year ([int]$startParts[1]) -Month ([int]$startParts[0]) -Day 1
$end = Get-Date -Year ([int]$endParts[1]) -Month ([int]$endParts[0]) -Day 1

if ($current -gt $end) {
    Write-Host "StartMonth ($StartMonth) est après EndMonth ($EndMonth)." -ForegroundColor Red
    exit 1
}

$reportDates = @()
while ($current -le $end) {
    $lastDay = $current.AddMonths(1).AddDays(-1)
    $reportDates += $lastDay.ToString("dd/MM/yyyy")
    $current = $current.AddMonths(1)
}

# One login/browser session, reused for every month (see mintos_get_session.py/
# lande_get_session.py's REPORT_DATE_MONTHS handling) instead of relaunching
# the browser and logging in again for each month.
$env:REPORT_DATE_MONTHS = $reportDates -join ","
Write-Host "`n=== $($reportDates.Count) mois ($StartMonth -> $EndMonth), une seule connexion réutilisée ===" -ForegroundColor Yellow
Invoke-Platforms

Remove-Item Env:\REPORT_DATE_MONTHS -ErrorAction SilentlyContinue
Remove-Item Env:\REPORT_DATE -ErrorAction SilentlyContinue

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
Write-Host ("Mintos: {0}" -f $(if ($mintosExit -eq 0) { "OK" } else { "FAILED (exit $mintosExit)" }))
Write-Host ("Lande:  {0}" -f $(if ($landeExit -eq 0) { "OK" } else { "FAILED (exit $landeExit)" }))
