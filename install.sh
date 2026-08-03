#!/usr/bin/env bash
# Install the openalex-search skill into ~/.claude/skills/.
# Safe to re-run: overwrites the skill files, never touches an existing .env.

set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target_dir="$HOME/.claude/skills/openalex-search"

echo "Installing openalex-search"
echo "  from: $source_dir"
echo "  to:   $target_dir"

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
echo "  python: $("$python_bin" --version 2>&1)"

# --- Copy -------------------------------------------------------------------
mkdir -p "$target_dir/scripts"
for item in SKILL.md reference.md .env.example; do
    if [ -f "$source_dir/$item" ]; then
        cp "$source_dir/$item" "$target_dir/$item"
        echo "  + $item"
    fi
done
cp "$source_dir/scripts/openalex.py" "$target_dir/scripts/openalex.py"
echo "  + scripts/openalex.py"

# --- Verify -----------------------------------------------------------------
cli="$target_dir/scripts/openalex.py"
echo ""
echo "Verifying..."
if ! "$python_bin" "$cli" --help >/dev/null; then
    echo "ERROR: the CLI did not run correctly."
    exit 1
fi
echo "  CLI runs."

# --- API key ----------------------------------------------------------------
have_key=""
[ -n "${OPENALEX_API_KEY:-}" ] && have_key=1
for f in "$HOME/.openalex_key" "$HOME/openalex_key.txt" \
         "$HOME/.openalex_key.txt" "$HOME/openalex_api_key.txt" \
         "$target_dir/.env"; do
    [ -f "$f" ] && have_key=1
done

echo ""
echo "Done."
if [ -z "$have_key" ]; then
    cat <<EOF

No API key found. It is optional, but a free key raises your daily
budget from \$0.10 to \$1.00.
  1. Get one at https://openalex.org/settings/api
  2. Save it, and nothing else, to: $HOME/.openalex_key
EOF
fi
cat <<EOF

Restart Claude Code so it picks up the new skill, then just ask:
  "find recent papers on <your topic> and grab the open-access PDFs"

Or run it directly:
  $python_bin "$cli" --help
EOF
