# OpenAlex query grammar reference

Loaded on demand — only needed when building a raw `--filter` string or
debugging an unexpected result set. Verified against the live API 2026-07-30.

Base URL: `https://api.openalex.org` · Docs: <https://developers.openalex.org>

## Filter syntax

| Pattern | Meaning | Example |
| --- | --- | --- |
| `a:b` | equals | `type:article` |
| `a:b,c:d` | **AND** across fields | `is_oa:true,publication_year:2024` |
| `a:b,a:c` | **AND** within one field | `cites:W1,cites:W2` |
| `a:b\|c` | **OR** (max 100 values) | `type:article\|review` |
| `a:!b` | NOT | `type:!paratext` |
| `a:>n` / `a:<n` | inequality (exclusive) | `cited_by_count:>100` |
| `a:n-m` | inclusive range | `publication_year:2020-2024` |
| `a:b+c` | AND within field (**most** fields) | `institutions.country_code:fr+gb` |

> `+` does **not** work for `cites`, nor for search/boolean/numeric filters.
> Repeat the filter instead — that always intersects correctly.

Filters are case-insensitive. OR works *within* a filter, never *between* two
different filters.

## Frequently used Works filters

**Identity / access**
`doi` · `ids.pmid` · `ids.pmcid` · `openalex_id` (batch: `openalex_id:W1|W2|…`)
`is_oa` · `open_access.oa_status` (gold/green/hybrid/bronze/closed/diamond)
`best_oa_location.license` · `has_pdf_url` · `has_fulltext` · `has_abstract`

**Bibliographic**
`publication_year` · `publication_date` · `from_publication_date` · `to_publication_date`
`from_created_date` (new records — good for "what appeared this week")
`type` (article, review, preprint, book-chapter, dataset, dissertation, report…)
`language` (ISO 639-1) · `is_retracted` · `is_paratext` · `biblio.volume`

**Entities** — always via ID, never a name
`authorships.author.id` (also accepts an ORCID URL)
`authorships.author.orcid` · `authorships.is_corresponding`
`authorships.institutions.id` (also accepts a ROR URL)
`authorships.institutions.country_code` · `authorships.institutions.type`
`primary_location.source.id` · `primary_location.source.issn`
`locations.source.id` (any location, not just primary)
`primary_location.source.is_in_doaj` · `primary_location.source.is_core`
`topics.id` · `primary_topic.id` · `primary_topic.field.id` · `primary_topic.subfield.id`
`keywords.id` · `funders.id` · `awards.funder_id` · `sustainable_development_goals.id`

**Citation graph**
`cites:W123` — works citing W123 (forward)
`cited_by:W123` — the works W123 cites (backward, equals its reference list)
`related_to:W123` — algorithmic neighbours
`cited_by_count` · `fwci` · `citation_normalized_percentile.is_in_top_1_percent`
`referenced_works_count` · `has_references`

**Byline matching** (the one search filter with no `search=` equivalent)
`raw_author_name.search:"jane smith"` — quote it, or tokens match *different*
authors on the paper. `~1` / `~2` allows middle names: `"jane smith"~2`.

## Search parameters

Only **one** of these per request:

| Param | Behaviour |
| --- | --- |
| `search=` | stemmed, over title + abstract + fulltext. Default. |
| `search.exact=` | unstemmed. Required for wildcards. |
| `search.semantic=` | embeddings; max 2000 input chars, 50 results, 1 req/s. |

**Boolean:** `AND` / `OR` / `NOT` uppercase; unseparated words imply AND.
`(elmo AND "sesame street") NOT (cookie OR monster)`

**Phrase & proximity:** `"climate change"` exact; `"climate change"~5` allows 5
positions of slop; `"machine learning"~5~"neural network"` keeps each phrase
intact but requires them within 5 words.

**Wildcards** (require `search.exact`): `machin*` (≥3 chars before `*`),
`wom?n`. Leading wildcards are unsupported.

**Fuzzy:** `machin~1` — edit distance 0–2, ≥3 chars before `~`.

**URL length cap ~4 KB.** A big Boolean OR-list (systematic reviews) will 400.
Split the OR list, run each chunk, union the IDs client-side — the result is
identical, but each chunk bills separately.

## Sorting

`sort=cited_by_count:desc` · `publication_date:desc` · `relevance_score:desc`
(only meaningful with a search) · `:asc` for ascending.

**`sort=-field` returns 400** despite appearing in the official docs.

## Pagination

- `per_page` max **200** (docs say 100; 250 rejected).
- `page=` works to a depth of 10,000 results.
- `cursor=*`, then follow `meta.next_cursor`, for anything deeper.
- **Semantic search rejects cursors** — use `page`/`per_page`.
- `sample=N&seed=K` for a reproducible random sample (max 10,000; cannot
  combine with `sort` or `page`).

## Aggregation

`group_by=<field>` returns counts instead of records — one cheap call answers
"how many per year / country / journal / topic". Combine with `filter`.
`group_by=publication_year`, `group_by=primary_topic.field.id`,
`group_by=authorships.institutions.country_code`, `group_by=open_access.oa_status`.

Not exposed by the CLI; use `--filter` plus a direct call only if truly needed.

## Response shape

```json
{ "meta": { "count": 286750097, "page": 1, "per_page": 25,
            "next_cursor": "…", "cost_usd": 0.001 },
  "results": [ … ],
  "group_by": [] }
```

`meta.count` is the total matching set, not what was returned.

## Abstracts

OpenAlex ships `abstract_inverted_index` (word → positions), never plaintext,
for legal reasons. The CLI reconstructs it. Not every work has one — filter
with `has_abstract:true` when abstracts matter.

## Entity ID prefixes

`W` work · `A` author · `S` source · `I` institution · `T` topic · `K` keyword
· `P` publisher · `F` funder · `G` award · `C` concept (deprecated — use topics)

Canonical external IDs: works→DOI, authors→ORCID, sources→ISSN-L,
institutions→ROR, topics/publishers→Wikidata.

Merged entities return `301` to their new ID; urllib follows this automatically.

## Topic hierarchy

4 domains → 26 fields → 254 subfields → ~4,500 topics. Every work carries a
`primary_topic` with the full path, so you can filter at any level.

## Pricing (per 1,000 calls)

| Operation | Cost |
| --- | --- |
| Singleton `/works/W123` | free |
| List + filter | $0.10 |
| Full-text or semantic search | $1.00 |
| Content download (**never used by this skill**) | $10.00 |

Daily free budget: $1 with a key, $0.10 without. Resets midnight UTC.
With no prepaid balance, exceeding it returns 429 — it cannot create a charge.

Headers on every response: `X-RateLimit-Remaining-USD`, `X-RateLimit-Cost-USD`,
`X-RateLimit-Limit-USD`, `X-RateLimit-Prepaid-Remaining-USD`, `X-RateLimit-Reset`.

## Error codes

`200` ok · `301` merged, follow redirect · `400` bad filter/param ·
`403` throttled · `404` no such entity · `429` **either** per-second rate limit
(has `retryAfter` — wait and retry) **or** daily budget spent (wait for UTC
midnight) · `500`/`502`/`503` transient, retry with backoff.

## Deprecated

`/concepts` → topics · `/text` → removed · `host_venue` → `primary_location` ·
`grants` → `funders` + `awards` · `filter=field.search:` → the `search` param
(still functional, but `raw_author_name.search` remains the exception).
