<#
.SYNOPSIS
    Install the openalex-search skill into ~/.claude/skills/.
.DESCRIPTION
    Copies SKILL.md, reference.md, .env.example and scripts/ into
    $HOME\.claude\skills\openalex-search. Safe to re-run: it overwrites the
    skill files but never touches a .env you have already filled in.
#>

$ErrorActionPreference = "Stop"

$source = $PSScriptRoot
$target = Join-Path $HOME ".claude\skills\openalex-search"

Write-Host "Installing openalex-search" -ForegroundColor Cyan
Write-Host "  from: $source"
Write-Host "  to:   $target"

# --- Python check -----------------------------------------------------------
try {
    $pythonVersion = (python --version 2>&1) -join ""
} catch {
    Write-Host "`nERROR: Python was not found on PATH." -ForegroundColor Red
    Write-Host "Install Python 3.9 or newer from https://python.org and re-run."
    exit 1
}
Write-Host "  python: $pythonVersion"

# --- Copy -------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path (Join-Path $target "scripts") | Out-Null

foreach ($item in @("SKILL.md", "reference.md", ".env.example")) {
    $from = Join-Path $source $item
    if (Test-Path $from) {
        Copy-Item $from (Join-Path $target $item) -Force
        Write-Host "  + $item"
    }
}
Copy-Item (Join-Path $source "scripts\openalex.py") `
          (Join-Path $target "scripts\openalex.py") -Force
Write-Host "  + scripts/openalex.py"

# --- Verify -----------------------------------------------------------------
$cli = Join-Path $target "scripts\openalex.py"
Write-Host "`nVerifying..." -ForegroundColor Cyan
$null = python $cli --help
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: the CLI did not run correctly." -ForegroundColor Red
    exit 1
}
Write-Host "  CLI runs."

# --- API key ----------------------------------------------------------------
$keyFiles = @(
    (Join-Path $HOME "openalex_key.txt"),
    (Join-Path $HOME ".openalex_key"),
    (Join-Path $HOME ".openalex_key.txt"),
    (Join-Path $HOME "openalex_api_key.txt"),
    (Join-Path $target ".env")
)
$haveKey = $env:OPENALEX_API_KEY -or ($keyFiles | Where-Object { Test-Path $_ })

Write-Host "`nDone." -ForegroundColor Green
if (-not $haveKey) {
    Write-Host ""
    Write-Host "No API key found. It is optional, but a free key raises your" -ForegroundColor Yellow
    Write-Host "daily budget from `$0.10 to `$1.00." -ForegroundColor Yellow
    Write-Host "  1. Get one at https://openalex.org/settings/api"
    Write-Host "  2. Save it, and nothing else, to: $(Join-Path $HOME 'openalex_key.txt')"
}
Write-Host ""
Write-Host "Restart Claude Code so it picks up the new skill, then just ask:"
Write-Host '  "find recent papers on <your topic> and grab the open-access PDFs"'
Write-Host ""
Write-Host "Or run it directly:"
Write-Host "  python `"$cli`" --help"
