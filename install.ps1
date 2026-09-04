<#
.SYNOPSIS
    Install or update the openalex-search skill for any AI assistant.
.DESCRIPTION
    Installs openalex-search into Claude Code, Google Antigravity / Gemini CLI,
    or custom agent directory. Supports auto-detecting all installed AI assistants.
    Safe to re-run: updates skill files without touching existing .env or API keys.
.PARAMETER TargetAll
    Install to all detected AI assistants (Claude Code, Gemini CLI / Antigravity).
.PARAMETER Claude
    Install specifically into ~/.claude/skills/openalex-search.
.PARAMETER Gemini
    Install specifically into ~/.gemini/config/skills/openalex-search.
.PARAMETER Path
    Install into a custom target directory.
#>
[CmdletBinding()]
param(
    [switch]$TargetAll,
    [switch]$Claude,
    [switch]$Gemini,
    [string]$Path = ""
)

$ErrorActionPreference = "Stop"
$source = $PSScriptRoot

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "     OpenAlex Literature Search Skill - Universal Installer" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Source: $source"

# --- Python check -----------------------------------------------------------
try {
    $pythonVersion = (python --version 2>&1) -join ""
} catch {
    Write-Host "`nERROR: Python was not found on PATH." -ForegroundColor Red
    Write-Host "Install Python 3.9 or newer from https://python.org and re-run."
    exit 1
}
Write-Host "Python: $pythonVersion" -ForegroundColor Green

# --- Resolve target directories --------------------------------------------
$targetDirs = @()

if ($Path) {
    $targetDirs += (Resolve-Path -Path $Path -ErrorAction SilentlyContinue).Path
    if (-not $targetDirs[0]) {
        $targetDirs = @($Path)
    }
} elseif ($Claude) {
    $targetDirs += (Join-Path $HOME ".claude\skills\openalex-search")
} elseif ($Gemini) {
    $targetDirs += (Join-Path $HOME ".gemini\config\skills\openalex-search")
} elseif ($TargetAll) {
    $targetDirs += (Join-Path $HOME ".claude\skills\openalex-search")
    $targetDirs += (Join-Path $HOME ".gemini\config\skills\openalex-search")
} else {
    # Auto-detect installed AI environments
    $claudeBase = Join-Path $HOME ".claude"
    $geminiBase = Join-Path $HOME ".gemini"
    
    if (Test-Path $claudeBase) {
        $targetDirs += (Join-Path $HOME ".claude\skills\openalex-search")
    }
    if (Test-Path $geminiBase) {
        $targetDirs += (Join-Path $HOME ".gemini\config\skills\openalex-search")
    }
    
    # If neither directory exists yet, default to Claude Code standard
    if ($targetDirs.Count -eq 0) {
        $targetDirs += (Join-Path $HOME ".claude\skills\openalex-search")
    }
}

# Remove duplicates if any
$targetDirs = $targetDirs | Select-Object -Unique

# --- Perform installation for each target ------------------------------------
$resolvedSource = (Resolve-Path $source).Path

foreach ($target in $targetDirs) {
    Write-Host "`nInstalling to: $target" -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path (Join-Path $target "scripts") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $target "tests") | Out-Null

    $resolvedTarget = (Resolve-Path $target -ErrorAction SilentlyContinue)
    $isSelf = $resolvedTarget -and ($resolvedSource -eq $resolvedTarget.Path)

    if (-not $isSelf) {
        # Copy metadata and docs
        foreach ($item in @("SKILL.md", "reference.md", "README.md", "LICENSE", ".env.example")) {
            $from = Join-Path $source $item
            if (Test-Path $from) {
                Copy-Item $from (Join-Path $target $item) -Force
                Write-Host "  + $item"
            }
        }

        # Copy script
        Copy-Item (Join-Path $source "scripts\openalex.py") `
                  (Join-Path $target "scripts\openalex.py") -Force
        Write-Host "  + scripts/openalex.py"

        # Copy tests if present
        if (Test-Path (Join-Path $source "tests\test_exports.py")) {
            Copy-Item (Join-Path $source "tests\test_exports.py") `
                      (Join-Path $target "tests\test_exports.py") -Force
            Write-Host "  + tests/test_exports.py"
        }
    } else {
        Write-Host "  (Target is current source directory; verified in-place)"
    }

    # Verify execution
    $cli = Join-Path $target "scripts\openalex.py"
    $null = python $cli --help
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: CLI verification failed at $cli" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [OK] CLI verified successfully." -ForegroundColor Green
}

# --- API key check ----------------------------------------------------------
$keyFiles = @(
    (Join-Path $HOME "openalex_key.txt"),
    (Join-Path $HOME ".openalex_key"),
    (Join-Path $HOME ".openalex_key.txt"),
    (Join-Path $HOME "openalex_api_key.txt")
)
$haveKey = $env:OPENALEX_API_KEY -or ($keyFiles | Where-Object { Test-Path $_ })

Write-Host "`n==========================================================" -ForegroundColor Cyan
Write-Host "Installation Complete!" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

if (-not $haveKey) {
    Write-Host "`n[Note] No OpenAlex API key found." -ForegroundColor Yellow
    Write-Host "The tool runs completely free without a key ($0.10/day budget)."
    Write-Host "A free key raises your daily budget 10x ($1.00/day):"
    Write-Host "  1. Get a key at https://openalex.org/settings/api"
    Write-Host "  2. Save it to: $(Join-Path $HOME 'openalex_key.txt')"
}

Write-Host "`nUsage with your AI Assistants:" -ForegroundColor Cyan
Write-Host "  Claude Code:           Ask 'Search papers on <topic> with --html and --citations'"
Write-Host "  Gemini / Antigravity:  Ask 'Search papers on <topic> with --html and --citations'"
Write-Host "  Cursor / Roo / Cline:  Reference $cli in rules or prompt"
Write-Host "  Terminal CLI:          python `"$($targetDirs[0])\scripts\openalex.py`" --help"
