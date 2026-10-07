param(
    [switch]$DryRun,
    [switch]$FetchTradeRepublic
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Environnement Python absent. Crée .venv et installe requirements.txt avant de lancer l'import."
}

if ($FetchTradeRepublic) {
    & $python -m portfolio.trade_republic_fetch
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$arguments = @("-m", "portfolio.csv_import")
if ($DryRun) {
    $arguments += "--dry-run"
}

& $python @arguments
exit $LASTEXITCODE