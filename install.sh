#!/usr/bin/env bash
# Universal installer for the openalex-search skill across AI assistants.
# Safe to re-run: overwrites the skill files, never touches an existing .env.

set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================================="
echo "   OpenAlex Literature Search Skill - Universal Installer"
echo "=========================================================="
echo "Source: $source_dir"

# --- Python check -----------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    python_bin=python3
elif command -v python >/dev/null 2>&1; then
    python_bin=python
else
    echo ""
    echo "ERROR: Python was not found on PATH."
    echo "Install Python 3.9 or newer and re-run."
    exit 1
fi
echo "Python: $("$python_bin" --version 2>&1)"

# --- Determine targets ------------------------------------------------------
target_dirs=()

if [ "${1:-}" = "--claude" ]; then
    target_dirs+=("$HOME/.claude/skills/openalex-search")
elif [ "${1:-}" = "--gemini" ]; then
    target_dirs+=("$HOME/.gemini/config/skills/openalex-search")
elif [ "${1:-}" = "--path" ] && [ -n "${2:-}" ]; then
    target_dirs+=("$2")
elif [ "${1:-}" = "--all" ]; then
    target_dirs+=("$HOME/.claude/skills/openalex-search")
    target_dirs+=("$HOME/.gemini/config/skills/openalex-search")
else
    # Auto-detection
    if [ -d "$HOME/.claude" ]; then
        target_dirs+=("$HOME/.claude/skills/openalex-search")
    fi
    if [ -d "$HOME/.gemini" ]; then
        target_dirs+=("$HOME/.gemini/config/skills/openalex-search")
    fi
    if [ ${#target_dirs[@]} -eq 0 ]; then
        target_dirs+=("$HOME/.claude/skills/openalex-search")
    fi
fi

# --- Install to targets -----------------------------------------------------
for target in "${target_dirs[@]}"; do
    echo ""
    echo "Installing to: $target"
    mkdir -p "$target/scripts"
    mkdir -p "$target/tests"

    for item in SKILL.md reference.md README.md LICENSE .env.example; do
        if [ -f "$source_dir/$item" ]; then
            cp "$source_dir/$item" "$target/$item"
            echo "  + $item"
        fi
    done

    cp "$source_dir/scripts/openalex.py" "$target/scripts/openalex.py"
    echo "  + scripts/openalex.py"

    if [ -f "$source_dir/tests/test_exports.py" ]; then
        cp "$source_dir/tests/test_exports.py" "$target/tests/test_exports.py"
        echo "  + tests/test_exports.py"
    fi

    # Verify
    cli="$target/scripts/openalex.py"
    if ! "$python_bin" "$cli" --help >/dev/null; then
        echo "ERROR: CLI verification failed at $cli"
        exit 1
    fi
    echo "  [OK] CLI verified successfully."
done

# --- API Key check ----------------------------------------------------------
have_key=""
[ -n "${OPENALEX_API_KEY:-}" ] && have_key=1
for f in "$HOME/.openalex_key" "$HOME/openalex_key.txt" \
         "$HOME/.openalex_key.txt" "$HOME/openalex_api_key.txt"; do
    [ -f "$f" ] && have_key=1
done

echo ""
echo "=========================================================="
echo "Installation Complete!"
echo "=========================================================="
if [ -z "$have_key" ]; then
    echo ""
    echo "[Note] No OpenAlex API key found."
    echo "The tool works completely free without a key (\$0.10/day budget)."
    echo "A free key raises your daily budget 10x (\$1.00/day):"
    echo "  1. Get a key at https://openalex.org/settings/api"
    echo "  2. Save it to: $HOME/.openalex_key"
fi

echo ""
echo "Usage with AI assistants:"
echo "  Claude Code:           Ask 'Search papers on <topic> with --html and --citations'"
echo "  Gemini / Antigravity:  Ask 'Search papers on <topic> with --html and --citations'"
echo "  Cursor / Roo / Cline:  Reference $cli in rules or prompt"
echo "  Terminal CLI:          $python_bin \"${target_dirs[0]}/scripts/openalex.py\" --help"
