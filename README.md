# openalex-search

A [Claude Code](https://claude.com/claude-code) skill for literature search over
[OpenAlex](https://openalex.org) — ~300M scholarly works, fully open, free to query.

Ask in plain English; get back a screening report, an Excel-ready CSV, and the
open-access PDFs.

> *"find papers on radiolysis of molten chlorides since 2020, grab the OA PDFs"*
>
> *"who has cited 10.1039/d3cp01477k?"*
>
> *"here's my draft abstract: … — what existing work is closest to it?"*

It also works as a standalone CLI with no Claude involved.

---

## Why this instead of asking an LLM directly

- **No hallucinated citations.** Every record is a real OpenAlex entity with a
  DOI. Nothing is generated.
- **Cheap in tokens.** The CLI does the paginating and file-writing; only a
  compact summary re-enters the model's context. Result sets of hundreds of
  papers cost roughly the same in tokens as a handful.
- **Costs you nothing.** OpenAlex's free tier is enforced in code — see
  [Cost safety](#cost-safety).
- **Reproducible.** Every run records the exact query, filters, and match count
  in the report, and keeps the raw JSON so re-analysis needs no new API calls.

## Requirements

- **Python 3.9+** — standard library only, nothing to `pip install`
- **Claude Code** (optional — the CLI works on its own)
- **An OpenAlex API key** (optional, free, 30 seconds) —
  [openalex.org/settings/api](https://openalex.org/settings/api).
  Without one you get $0.10/day of free usage instead of $1.00/day.

## Install

```bash
git clone https://github.com/gabzsi/openalex-search-skill.git
cd openalex-search-skill
```

**Windows (PowerShell):**

```bash
.\install.ps1
```

**macOS / Linux:**

```bash
bash install.sh
```

Either one copies the skill into `~/.claude/skills/openalex-search/`. Restart
Claude Code afterwards so it picks up the new skill.

<details>
<summary>Manual install, or installing without the script</summary>

Copy `SKILL.md`, `reference.md`, `.env.example`, and `scripts/` into
`~/.claude/skills/openalex-search/`. That directory *is* the skill — there is no
build step and no registration to do.

You can also clone this repo directly into place, which makes `git pull` the
update mechanism:

```bash
git clone https://github.com/gabzsi/openalex-search-skill.git ~/.claude/skills/openalex-search
```
</details>

## Add your API key

Optional but recommended — it raises your daily budget 10×.

Save the key as a one-line text file at **`~/openalex_key.txt`** (on Windows:
`C:\Users\<you>\openalex_key.txt`). Just the key, nothing else — no
`OPENALEX_API_KEY=`, no quotes.

Any of these locations work, so it's hard to get wrong:

| Location | Notes |
| --- | --- |
| `OPENALEX_API_KEY` env var | Takes precedence over the files |
| `~/openalex_key.txt` | Simplest on Windows |
| `~/.openalex_key` | Conventional on macOS/Linux |
| `<skill dir>/.env` | Copy `.env.example` and edit |

Verify:

```bash
python ~/.claude/skills/openalex-search/scripts/openalex.py budget
```

You want `"api_key": "set"` and `"daily_limit_usd": 1.0`.

> **Note for PowerShell users:** `~` is not expanded in arguments to a native
> executable. Use `$HOME\.claude\skills\openalex-search\scripts\openalex.py`
> instead. In `cmd.exe`, use `%USERPROFILE%\...`.

## Usage

### Through Claude Code

Just ask. The skill auto-triggers on literature-search phrasing, in any project
directory. If a request is ambiguous, name it explicitly:

> use the openalex-search skill to find …

### As a CLI

```bash
OA=~/.claude/skills/openalex-search/scripts/openalex.py

# Topic screening
python $OA search "solvated electron molten salt" --from-year 2020 --limit 30 --pdfs --out results/melts

# Semantic search — for a pasted abstract or paragraph, not keywords
python $OA search "We measure the decay kinetics of solvated electrons in
  high-temperature chloride melts after an electron pulse." --semantic --limit 40

# Citation chasing
python $OA cited-by   10.1039/d3cp01477k --limit 100   # who cites this
python $OA references 10.1039/d3cp01477k               # what it cites
python $OA cited-by   W1926950498 W2116507044          # co-citation (cites BOTH)
python $OA coupling   W1926950498 W3008264207          # shared references
python $OA related    10.1039/d3cp01477k

# Names are ambiguous — resolve to an ID first, then filter on it
python $OA resolve sources "Journal of Physical Chemistry A"
python $OA search "solvated electron" --journal S123456789

python $OA budget
```

`python $OA --help` documents every flag.

### Commands

| Command | Does |
| --- | --- |
| `search` | Text and/or filtered search over works |
| `cited-by` | Works citing the seed; 2+ seeds gives co-citation |
| `references` | The seed's own reference list |
| `coupling` | References shared by all seeds (bibliographic coupling) |
| `related` | OpenAlex's algorithmic neighbours |
| `resolve` | Name → OpenAlex ID for authors/sources/institutions/topics/funders/publishers |
| `get` | One work by DOI or ID (free) |
| `budget` | Remaining free daily budget |

### Search modes

| Mode | When |
| --- | --- |
| default | Stemmed full-text. Supports `AND`/`OR`/`NOT`, `"phrases"`, `"a b"~5` proximity |
| `--semantic` | You have an **abstract or paragraph**, not keywords. Finds conceptually related work whose wording differs. Max 50 results |
| `--exact` | Unstemmed. **Required** for wildcards (`radiol*`, `wom?n`) |

### Filters

`--from-year` `--to-year` `--min-citations` `--type` `--oa-only`
`--has-abstract` `--exclude-retracted` `--journal` `--author` `--institution`
`--topic` `--filter` (raw passthrough) `--include-xpac`

## Output

Every run writes to `--out`:

| File | Contents |
| --- | --- |
| `report.md` | Screening report — authors, venue, metrics, topic hierarchy, access links, abstract |
| `results.csv` | 32 columns, UTF-8 with BOM so Excel handles accented names |
| `results.jsonl` | Raw OpenAlex records, for re-analysis without re-querying |
| `pdfs/` | Open-access PDFs (with `--pdfs`) |

Titles are cleaned of publisher HTML, with chemistry rendered as Unicode:
`Zn <sup>2+</sup>` → `Zn²⁺`, `Cl <sub>2</sub>` → `Cl₂`.

## Cost safety

OpenAlex is freemium. This tool is built to stay inside the free tier:

- It **never** calls a metered content endpoint. PDFs come only from
  open-access publisher/repository links, which are not billed.
- It reads `X-RateLimit-Remaining-USD` on every response and **stops before the
  daily budget reaches zero**, keeping whatever it has already written.
- With no prepaid balance on your account, exceeding the daily budget returns
  HTTP 429 — it *cannot* create a charge.

Costs, for calibration:

| Operation | Per call | Free tier/day (with key) |
| --- | --- | --- |
| `get`, seed lookups | free | unlimited |
| `cited-by`, `references`, `coupling`, `related` | $0.0001 | ~10,000 |
| `search`, `resolve` | $0.001 | ~1,000 |

So citation chasing is ~10× cheaper than search — iterate there freely.
Budget resets at midnight UTC.

## API quirks worth knowing

Found by testing against the live API (2026-07-30). Several contradict the
official documentation, and are handled in the code:

| Quirk | Reality |
| --- | --- |
| `sort=-cited_by_count` (per the docs) | Returns **400**. Use `field:desc` |
| `cites:A+B` | Does **not** AND — silently returns only A's results. Use repeated filters `cites:A,cites:B` |
| `per_page` max | **200**, not the documented 100 |
| Semantic search | Rejects cursor pagination; capped at 1 req/sec and 50 results |
| API key "required" | Not actually — it only raises the daily budget 10× |
| `429` | Means **either** per-second throttling (has `retryAfter`, retry it) **or** daily budget spent (wait for UTC midnight) |
| XPAC works | Bulk repository/DataCite records, excluded by default; `--include-xpac` roughly doubles counts |
| Abstracts | Delivered as an inverted index, never plaintext. Reconstructed here. Some are genuinely truncated in OpenAlex |

## Limitations

- Metadata quality is OpenAlex's. Author disambiguation is good but not
  perfect — always sanity-check an author profile before trusting a
  publication list. Use `resolve authors` and inspect the candidates.
- Abstract coverage is incomplete; `--has-abstract` filters to what exists.
- `--pdfs` typically retrieves roughly half a result set. The rest are closed
  access, or the OA link points at a landing page rather than a PDF. Those get
  a DOI link in the report instead.
- No BibTeX export yet. `results.csv` imports into Zotero/Excel fine.
- `search` is limited to ~4 KB of URL, so very large Boolean queries (systematic
  reviews) need splitting into chunks and unioning the IDs.

## Credits

Data from [OpenAlex](https://openalex.org), CC0. If you use it in research,
cite:

> Priem, J., Piwowar, H., & Orr, R. (2022). *OpenAlex: A fully-open index of
> scholarly works, authors, venues, institutions, and concepts.*
> arXiv:[2205.01833](https://arxiv.org/abs/2205.01833)

## License

MIT — see [LICENSE](LICENSE).
