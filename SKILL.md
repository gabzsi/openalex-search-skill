---
name: openalex-search
description: Search the scholarly literature via OpenAlex - find papers on a topic, screen a candidate set, chase citations forward or backward, pull an author's or journal's output, resolve DOIs/ORCIDs, and download open-access PDFs. Produces a markdown screening report plus an Excel-ready CSV. Use whenever the user wants to find, screen, or trace academic papers, references, citations, or bibliographies.
---

# OpenAlex literature search

A CLI wrapper over the OpenAlex API (~300M works). It fetches, paginates, and
writes files; only a compact summary comes back to you. **Never hand-build
OpenAlex URLs or use WebFetch against api.openalex.org** — use this CLI, so
cost tracking and the free-tier guard stay in force.

```
OA = ~/.claude/skills/openalex-search/scripts/openalex.py
```

Run as `python "<OA>" <command> ...`. Substitute the right home-directory form
for the shell you are using — **verified, they differ**:

| Shell | Path to use |
| --- | --- |
| bash / zsh | `~/.claude/skills/openalex-search/scripts/openalex.py` |
| PowerShell | `$HOME\.claude\skills\openalex-search\scripts\openalex.py` |
| cmd.exe | `"%USERPROFILE%\.claude\skills\openalex-search\scripts\openalex.py"` |

PowerShell does **not** expand `~` in arguments to a native executable — it
passes the tilde through literally and Python fails to find the file.

Quoting warning for PowerShell: it strips embedded double quotes when calling
a native exe, which breaks Boolean queries like `"pulse radiolysis" OR x`.
Escape them as `\"` there, or use the Bash tool instead.

## Cost discipline — non-negotiable

The user is on the **free tier and must never be charged.** The CLI enforces
this: it reads the rate-limit headers, stops before the daily budget hits zero
(keeping partial results), and refuses to touch metered content endpoints.
Your part:

| Operation | Cost | Notes |
| --- | --- | --- |
| `get`, seed lookups | **free** | singleton requests are not billed |
| `cited-by`, `references`, `coupling`, `related` | ~$0.0001/call | cheap — chase freely |
| `search`, `resolve` | **$0.001/call** | 10× the cost. Think before re-running |

Budget is ~$1/day with a key, $0.10/day without. So: **~1000 searches/day, or
~100 without a key.** Don't burn searches on trial and error — get the query
right, and prefer `--limit` over pulling thousands of records you won't read.
Run `python "<OA>" budget` if you need to check what's left.

PDFs come only from open-access publisher/repository links, which cost nothing.
Non-OA papers get a DOI link in the report instead — that is expected, not a
failure. Never suggest paying for the OpenAlex content endpoint.

## The one rule that prevents wrong answers

**Names are ambiguous; IDs are not.** Before filtering by an author, journal,
institution, or topic, resolve the name to an ID and confirm which one is right:

```bash
python "<OA>" resolve sources "Journal of Physical Chemistry A"
python "<OA>" resolve authors "Gabor Szabo"
```

Then pass the ID: `--journal S123456789`, `--author A5012345678`. If several
candidates are plausible, show the user the table and ask — don't guess. ORCIDs,
RORs, ISSNs and DOIs can be passed directly, no resolve step needed.

## Commands

```bash
# Topic search and screening
python "<OA>" search "QUERY" [--from-year Y] [--to-year Y] [--min-citations N]
      [--type article] [--oa-only] [--has-abstract] [--exclude-retracted]
      [--journal S…|ISSN] [--author A…|ORCID] [--institution I…|ROR] [--topic T…]
      [--semantic] [--exact] [--filter "raw:filter"] [--include-xpac]
      [--sort relevance|citations|date|date-asc] [--limit N] [--pdfs] --out DIR

# Citation chasing
python "<OA>" cited-by   SEED [SEED2 …]   # works citing the seed; 2+ seeds = co-citation
python "<OA>" references SEED             # the seed's own reference list
python "<OA>" coupling   SEED SEED2       # references shared by both (bibliographic coupling)
python "<OA>" related    SEED             # OpenAlex's algorithmic neighbours

# Utilities
python "<OA>" resolve {authors|sources|institutions|topics|funders|publishers} "NAME"
python "<OA>" get SEED                    # one work, free
python "<OA>" budget
```

`SEED` accepts a bare DOI (`10.1039/d3cp01477k`), a DOI URL, or a work ID (`W123…`).

### Search modes

- **default** — stemmed full-text over title + abstract + fulltext. Supports
  `AND` / `OR` / `NOT` (uppercase), `"quoted phrases"`, and `"a b"~5` proximity.
- `--semantic` — embedding search. Reach for this when the user gives an
  **abstract, a paragraph, or a grant aim** rather than keywords; it finds
  conceptually related work whose wording differs. Capped at 50 results.
- `--exact` — unstemmed; **required** for wildcards (`radiol*`, `wom?n`).

## Output

Every run writes to `--out`:

| File | Use |
| --- | --- |
| `report.md` | Screening report: authors, venue, metrics, topic, access links, abstract |
| `results.csv` | Excel-ready (UTF-8 BOM), 32 columns |
| `results.jsonl` | Raw records, for re-analysis without re-querying |
| `pdfs/` | Open-access PDFs, with `--pdfs` |

Default `--out` is `results/<command>`; set something descriptive per project.

**Token discipline:** stdout gives you the count, cost, and top 10 — usually
enough to report back. Don't cat `results.jsonl`. If you need detail, grep
`report.md` or read a slice of it. To re-analyse, read `results.csv` with
pandas rather than re-running the query.

## Workflows

**Topic screening.** Start broad, check the match count in the `[query]` line,
then narrow with filters rather than re-searching different phrasings. Add
`--has-abstract` when the user will actually read the results — abstract-less
records are noise for screening. Use `--min-citations` to skim the established
literature, or `--from-year` + `--sort date` for the recent edge.

**Citation chasing.** From one or more seed papers:
`references` for the intellectual roots, `cited-by` for who built on it,
`coupling` for papers with a shared evidence base, and `cited-by A B` for the
works that cite both seeds (a strong signal that two lines of work connect).
These are ~10× cheaper than search, so iterate here freely.

## Reporting back

Summarise in prose — the key papers, what the set looks like (year spread, how
much is OA, notable clusters), and anything odd (retractions, a suspiciously
small match count). Link the report with a markdown link. Flag retracted works
explicitly. Don't paste the JSON dump or a wall of titles.

## Verified gotchas

These were tested against the live API on 2026-07-30; some contradict the
official docs.

- `sort=-cited_by_count` (in the docs) **returns 400.** Use `field:desc`.
- `cites:A+B` does **not** AND — it silently returns only A's results. The CLI
  uses repeated filters (`cites:A,cites:B`), which does intersect correctly.
- `per_page` max is **200**, not the documented 100.
- Semantic search rejects cursor pagination and is limited to 1 request/second.
- An API key is *not* required, despite what some sources say — it just raises
  the daily budget 10×.
- XPAC works (bulk DataCite/repository records, lower metadata quality) are
  excluded by default. `--include-xpac` adds them; it roughly doubles counts and
  is usually not what you want for a literature review.

For full filter/search grammar when you need `--filter`, read `reference.md` in
this skill directory.
