# openalex-search

A universal skill and CLI for literature search over [OpenAlex](https://openalex.org) (~300M scholarly works, fully open, free to query).

Designed for **any AI coding assistant** ([Claude Code](https://claude.com/claude-code), [Google Antigravity / Gemini CLI](https://github.com/google-gemini), [Cursor](https://cursor.com), Windsurf, Roo Code, Cline) or as a **standalone command-line tool**.

Ask in plain English; get back screening reports, interactive HTML dashboards, 1-click EndNote libraries, universal RIS references, Excel-ready tables, and open-access PDFs:

> *"find papers on radiolysis of molten chlorides since 2020 with --html and --citations, and grab the OA PDFs"*
>
> *"generate an EndNote database and interactive HTML report for my search results"*
>
> *"who has cited 10.1039/d3cp01477k?"*
>
> *"here's my draft abstract: … — what existing work is closest to it?"*

---

## Key Features

- **No hallucinated citations**: Every single record is a verified OpenAlex entity with a DOI, author list, publication venue, and metric history. Nothing is AI-generated.
- **EndNote & RIS Citation Export**: Generates native EndNote tagged files (`references.enw`) for 1-click library import on Windows/macOS, plus universal RIS (`references.ris`) for Zotero, Mendeley, and Reference Manager.
- **Interactive HTML Reports**: Generates standalone, responsive HTML dashboards (`report.html`) with instant client-side search, open-access and publication type filters, multi-column sorting (citations, year, title, FWCI), and collapsible abstract drawers.
- **Tidy Directory Structure**: Deliverables (`report.md`, `report.html`, `references.ris`, `references.enw`, `pdfs/`) stay in the root folder, while data dumps (`results.csv`, `results.jsonl`) are neatly organized in `raw/`.
- **Offline Re-Export (`report` subcommand)**: Re-generate reports or citation files from any existing search directory offline with 0 API calls and $0 cost.
- **Strict Cost Safety**: Free-tier safe. Automatically tracks rate-limit headers and enforces a hard stop before hitting budget limits.
- **Zero Third-Party Dependencies**: Pure Python standard library only. Nothing to `pip install`.

---

## Requirements

- **Python 3.9+** (standard library only)
- **Any AI Assistant** (Claude Code, Gemini CLI, Antigravity, Cursor, Roo Code, Cline) or a standard terminal shell.
- **An OpenAlex API key** (optional, free, 30 seconds) — [openalex.org/settings/api](https://openalex.org/settings/api). Raises free daily quota from $0.10/day to $1.00/day (~100 → 1,000 searches/day).

---

## Installation for Any AI

Clone the repository:

```bash
git clone https://github.com/gabzsi/openalex-search-skill.git
cd openalex-search-skill
```

### Option 1: Universal Auto-Detect Installer

The installer automatically detects which AI assistants are installed on your system (`~/.claude`, `~/.gemini`) and installs/updates the skill into all detected environments:

**Windows (PowerShell):**
```powershell
.\install.ps1
```

**macOS / Linux:**
```bash
bash install.sh
```

### Option 2: Targeting Specific AI Assistants

#### Google Antigravity / Gemini CLI
```powershell
# Windows
.\install.ps1 -Gemini

# macOS / Linux
bash install.sh --gemini
```
Installs into `$HOME/.gemini/config/skills/openalex-search/`.

#### Claude Code
```powershell
# Windows
.\install.ps1 -Claude

# macOS / Linux
bash install.sh --claude
```
Installs into `$HOME/.claude/skills/openalex-search/`.

#### Cursor / Windsurf / Roo Code / Cline / Custom
```powershell
# Windows
.\install.ps1 -Path "C:\path\to\your\agent\skills\openalex-search"

# macOS / Linux
bash install.sh --path "/path/to/your/agent/skills/openalex-search"
```
Or simply point your assistant's system instructions or rules file to `scripts/openalex.py`.

---

## Setting up Your API Key (Optional)

Everything works out of the box without a key on OpenAlex's free tier. Adding a free key raises your daily limit from $0.10 to $1.00 (~1,000 searches/day).

1. Get a key in 30 seconds at **[openalex.org/settings/api](https://openalex.org/settings/api)**.
2. Save it to `openalex_key.txt` in your user home directory:
   - **Windows:** `echo YOUR_KEY_HERE>"%USERPROFILE%\openalex_key.txt"`
   - **macOS / Linux:** `echo "YOUR_KEY_HERE" > ~/.openalex_key`
3. Check remaining budget anytime:
   ```bash
   python scripts/openalex.py budget
   ```

---

## Output Structure

Each search or citation run outputs a clean, uncluttered directory:

```
my_literature_search/
├── report.md           # Markdown screening report
├── report.html         # Interactive HTML report (with --html or --all)
├── references.ris      # Universal RIS citation library (with --ris, --citations, or --all)
├── references.enw      # Native EndNote tagged library (with --enw, --citations, or --all)
├── pdfs/               # Open-access PDFs (with --pdfs)
└── raw/                # Structured data archives
    ├── results.csv     # Excel-ready spreadsheet (UTF-8 BOM, 32 columns)
    └── results.jsonl   # Full raw OpenAlex JSON entities
```

---

## Usage Guide

### 1. In AI Chat (Claude Code, Gemini CLI, Cursor, etc.)

Just speak naturally:
- *"Search for recent papers on pulse radiolysis of amides and create an HTML report and EndNote database."*
- *"Find all papers citing 10.1021/ja00716a011 with citations export."*
- *"Re-generate the HTML report and RIS file for my previous search in `results/amides` without re-querying OpenAlex."*

### 2. Standalone CLI Usage

```bash
# Path to script
OA="scripts/openalex.py"

# Topic screening with HTML report & EndNote export
python "$OA" search "solvated electron acetamide" --from-year 2010 --limit 50 --all --out results/acetamide

# Fast citation chasing
python "$OA" cited-by 10.1021/ja00716a011 --limit 100 --citations --out results/cited_hayon
python "$OA" references 10.1021/ja00716a011 --all --out results/hayon_refs

# Co-citation (papers citing BOTH seeds)
python "$OA" cited-by 10.1021/ja00716a011 10.1063/1.1678690 --all --out results/cocitation

# Bibliographic coupling (shared references)
python "$OA" coupling 10.1021/ja00716a011 10.1063/1.1678690 --out results/coupling

# Offline report generation (0 API calls, reads existing results.jsonl / results.csv)
python "$OA" report results/acetamide --all
```

---

## CLI Flags Reference

| Flag | Purpose |
| --- | --- |
| `--html` | Generate interactive standalone HTML report (`report.html`) |
| `--ris` | Generate universal RIS reference library (`references.ris`) |
| `--enw` | Generate native EndNote tagged library (`references.enw`) |
| `--citations` | Shortcut to generate both `references.ris` and `references.enw` |
| `--all` | Generate all deliverables (Markdown, HTML, RIS, ENW, CSV, JSONL) |
| `--flat` | Keep raw CSV and JSONL in root directory instead of `raw/` |
| `--pdfs` | Download open-access PDFs into `pdfs/` |
| `--limit N` | Max records to retrieve (default: 50) |
| `--from-year Y` | Minimum publication year |
| `--to-year Y` | Maximum publication year |
| `--min-citations N`| Minimum citation threshold |
| `--oa-only` | Filter to open-access works only |
| `--has-abstract` | Filter to records with an abstract |
| `--semantic` | Vector embedding search for abstracts/paragraphs (max 50) |
| `--exact` | Exact unstemmed search (required for wildcards like `radiol*`) |

---

## Importing into Reference Managers

### EndNote
- **Method 1 (Instant):** Double-click `references.enw` in Windows Explorer or macOS Finder. EndNote launches and imports all records into your current library.
- **Method 2:** In EndNote, choose `File > Import > File...`, select `references.ris`, and choose import option **Reference Manager (RIS)**.

### Zotero & Mendeley
- Select `File > Import...` and select `references.ris`.

---

## Cost Safety & Free Tier Details

| Operation | Cost | Free Tier Capacity (with key) |
| --- | --- | --- |
| `get`, seed lookups | **Free** | Unlimited |
| `report` (offline) | **Free** | Unlimited (0 API calls) |
| `cited-by`, `references`, `coupling`, `related` | ~$0.0001 / call | ~10,000 / day |
| `search`, `resolve` | ~$0.001 / call | ~1,000 / day |

The tool never connects to metered content endpoints and stops automatically before daily budget exhaustion.

---

## Running Tests

Verify the installation and export functions anytime:

```bash
python tests/test_exports.py
```

---

## License

MIT — see [LICENSE](LICENSE).
