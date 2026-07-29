<#
Runs the LOCAL-ONLY manual-login helpers for Mintos and Lande, one after
the other. Both open a real browser window and may need you to solve a
CAPTCHA/Turnstile challenge by hand - just follow the on-screen prompts.
Each platform still runs even if the other one fails.
#>

Set-Location $PSScriptRoot

Write-Host "=== Mintos ===" -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m diversification.mintos_get_session
$mintosExit = $LASTEXITCODE

Write-Host "`n=== Lande ===" -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m diversification.lande_get_session
$landeExit = $LASTEXITCODE

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
Write-Host ("Mintos: {0}" -f $(if ($mintosExit -eq 0) { "OK" } else { "FAILED (exit $mintosExit)" }))
Write-Host ("Lande:  {0}" -f $(if ($landeExit -eq 0) { "OK" } else { "FAILED (exit $landeExit)" }))
