#!/usr/bin/env python3
"""
openalex.py - OpenAlex literature search CLI.

Stdlib only. No third-party dependencies.

Design constraints (do not remove):
  * FREE TIER ONLY. This tool never calls a metered content endpoint.
    PDFs are fetched exclusively from open-access URLs served by the
    publisher or repository, which OpenAlex does not bill for.
  * Every response's rate-limit headers are read. When the remaining free
    daily budget drops below BUDGET_FLOOR_USD, the run stops cleanly with
    partial results already written to disk.

Auth: set OPENALEX_API_KEY (free key from https://openalex.org/settings/api).
      Without a key the API still works at 1/10th the daily budget.

Run `python openalex.py --help` for commands.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE = "https://api.openalex.org"
USER_AGENT = "openalex-search-skill/1.0 (stdlib urllib)"

# Stop making calls once the free daily budget falls below this. Leaves room
# for the user to do a singleton lookup without getting a hard 429.
BUDGET_FLOOR_USD = 0.005

MAX_PER_PAGE = 200          # verified 2026-07-30: 200 accepted, 250 rejected
SLEEP_BETWEEN_CALLS = 0.10  # polite pacing for list/search calls
SLEEP_SEMANTIC = 1.05       # semantic search is documented at 1 req/sec
MAX_RETRIES = 4

# Hosts this tool must never download from: they are metered per request.
METERED_HOSTS = {"content.openalex.org"}

# Fields pulled for every work. Keeping this tight cuts response size a lot.
WORK_SELECT = ",".join([
    "id", "doi", "title", "publication_year", "publication_date", "type",
    "language", "cited_by_count", "fwci", "is_retracted",
    "primary_location", "best_oa_location", "open_access", "locations",
    "authorships", "biblio", "primary_topic", "keywords",
    "referenced_works_count", "abstract_inverted_index",
])

ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", re.I)
ISSN_RE = re.compile(r"^\d{4}-\d{3}[\dX]$", re.I)
OA_ID_RE = re.compile(r"^[WASITKPFG]\d+$", re.I)


class BudgetExhausted(RuntimeError):
    """Raised when the free daily API budget is spent."""


class ApiError(RuntimeError):
    """Raised for non-retryable API errors."""


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Client:
    """Thin OpenAlex client with cost tracking and a hard budget stop."""

    def __init__(self, api_key: str | None = None, mailto: str | None = None,
                 verbose: bool = True):
        self.api_key = api_key or _find_api_key()
        self.mailto = mailto or os.environ.get("OPENALEX_MAILTO") or None
        self.verbose = verbose
        self.calls = 0
        self.session_cost = 0.0
        self.remaining_usd: float | None = None
        self.limit_usd: float | None = None
        self.prepaid_usd: float | None = None
        self._warned_no_key = False

    # -- internals ---------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    def _check_budget(self) -> None:
        if self.remaining_usd is not None and self.remaining_usd < BUDGET_FLOOR_USD:
            raise BudgetExhausted(
                f"Free daily OpenAlex budget is down to ${self.remaining_usd:.4f} "
                f"(floor ${BUDGET_FLOOR_USD}). Stopping so nothing gets charged. "
                "The budget resets at midnight UTC."
            )

    def _absorb_headers(self, headers: Any) -> None:
        def num(name: str) -> float | None:
            raw = headers.get(name)
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        cost = num("X-RateLimit-Cost-USD")
        if cost:
            self.session_cost += cost
        rem = num("X-RateLimit-Remaining-USD")
        if rem is not None:
            self.remaining_usd = rem
        lim = num("X-RateLimit-Limit-USD")
        if lim is not None:
            self.limit_usd = lim
        pre = num("X-RateLimit-Prepaid-Remaining-USD")
        if pre is not None:
            self.prepaid_usd = pre

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET an OpenAlex path. Raises BudgetExhausted rather than overspend."""
        self._check_budget()

        params = dict(params or {})
        if self.api_key:
            params["api_key"] = self.api_key
        elif not self._warned_no_key:
            self._warned_no_key = True
            self._log(
                "[warn] No OPENALEX_API_KEY set - running on the $0.10/day "
                "unkeyed budget. A free key gives 10x that: "
                "https://openalex.org/settings/api"
            )
        if self.mailto:
            params["mailto"] = self.mailto

        # '+' is deliberately NOT in safe: it must reach the server as %2B so
        # it is read as a literal plus, not as an encoded space.
        query = urllib.parse.urlencode(params, safe=":,|<>*/.")
        url = f"{BASE}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"

        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })

        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    self._absorb_headers(resp.headers)
                    self.calls += 1
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self._absorb_headers(exc.headers or {})
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass

                if exc.code == 429:
                    # 429 is overloaded: it means either the per-second rate
                    # limit (transient, has retryAfter) or the daily budget
                    # being spent (permanent until UTC midnight).
                    retry_after = _retry_after(exc.headers, body)
                    if retry_after is not None and attempt < MAX_RETRIES - 1:
                        wait = max(retry_after, 1.0) + 0.5
                        self._log(f"[retry] rate limited, waiting {wait:.1f}s")
                        time.sleep(wait)
                        last_err = exc
                        continue
                    raise BudgetExhausted(
                        "OpenAlex returned 429 (daily free budget spent). "
                        "Partial results have been kept. Resets at midnight UTC. "
                        f"Detail: {body}"
                    ) from exc
                if exc.code in (500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    self._log(f"[retry] HTTP {exc.code}, waiting {wait}s")
                    time.sleep(wait)
                    last_err = exc
                    continue
                raise ApiError(f"HTTP {exc.code} for {url}\n{body}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    self._log(f"[retry] {exc}, waiting {wait}s")
                    time.sleep(wait)
                    last_err = exc
                    continue
                raise ApiError(f"Network failure for {url}: {exc}") from exc

        raise ApiError(f"Exhausted retries for {url}: {last_err}")

    # -- helpers -----------------------------------------------------------

    def paginate(self, path: str, params: dict[str, Any], limit: int,
                 pace: float = SLEEP_BETWEEN_CALLS,
                 use_cursor: bool = True) -> Iterator[dict]:
        """Paginate a list endpoint, yielding at most `limit` records.

        Cursor paging is the default and is the only way past 10,000 results.
        Semantic search rejects cursors, so it falls back to page/per_page.
        """
        params = dict(params)
        params["per_page"] = min(MAX_PER_PAGE, max(1, limit))
        pulled = 0
        cursor = "*"
        page_no = 1

        while pulled < limit:
            if use_cursor:
                params["cursor"] = cursor
            else:
                params["page"] = page_no
            page = self.get(path, params)
            results = page.get("results") or []
            if not results:
                return
            for item in results:
                yield item
                pulled += 1
                if pulled >= limit:
                    return
            if use_cursor:
                cursor = (page.get("meta") or {}).get("next_cursor")
                if not cursor:
                    return
            else:
                page_no += 1
            time.sleep(pace)

    def count(self, path: str, params: dict[str, Any]) -> int:
        params = dict(params)
        params.update({"per_page": 1, "select": "id"})
        return int((self.get(path, params).get("meta") or {}).get("count") or 0)

    def budget_line(self) -> str:
        rem = f"${self.remaining_usd:.4f}" if self.remaining_usd is not None else "?"
        lim = f"${self.limit_usd:.2f}" if self.limit_usd is not None else "?"
        return (f"{self.calls} API calls, ${self.session_cost:.4f} spent this run; "
                f"{rem} of {lim} free daily budget left")


def _retry_after(headers: Any, body: str) -> float | None:
    """Seconds to wait, if the 429 was a transient per-second rate limit."""
    raw = None
    try:
        raw = (headers or {}).get("Retry-After")
    except AttributeError:
        pass
    if raw is None and body:
        try:
            raw = json.loads(body).get("retryAfter")
        except (ValueError, AttributeError):
            if "per second" in body.lower():
                raw = 1
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


PLACEHOLDER_HINTS = ("paste", "your", "here", "xxxx", "<", "changeme")


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    return any(hint in low for hint in PLACEHOLDER_HINTS)


def _find_api_key() -> str | None:
    for var in ("OPENALEX_API_KEY", "OPENALEX_KEY"):
        val = os.environ.get(var)
        if val and val.strip() and not _is_placeholder(val):
            return val.strip()
    # Several spellings accepted: Windows Explorer and Notepad both fight
    # dot-prefixed extensionless files, so a plain .txt works just as well.
    skill_dir = Path(__file__).resolve().parent.parent
    home = Path.home()
    for candidate in (home / ".openalex_key",
                      home / ".openalex_key.txt",
                      home / "openalex_key.txt",
                      home / "openalex_api_key.txt",
                      skill_dir / ".env",
                      skill_dir / "openalex_key.txt"):
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
                found = None
                match = re.search(r"OPENALEX_(?:API_)?KEY\s*=\s*(\S+)", text)
                if match:
                    found = match.group(1).strip().strip('"').strip("'")
                elif candidate.suffix != ".env":
                    # A bare key file: take it verbatim. Falling through to
                    # here also covers a key that itself contains '='.
                    stripped = "\n".join(
                        line for line in text.splitlines()
                        if line.strip() and not line.lstrip().startswith("#")
                    ).strip()
                    found = stripped or None
                if found and not _is_placeholder(found):
                    return found
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# Entity / record shaping
# --------------------------------------------------------------------------

# OpenAlex titles/abstracts carry publisher HTML (<sup>, <sub>, <i>, entities).
# Chemistry formulae read far better as real Unicode, in both Excel and markdown.
_SUP_MAP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴"
                                             "⁵⁶⁷⁸⁹"
                                             "⁺⁻⁼⁽⁾ⁿ")
_SUB_MAP = str.maketrans("0123456789+-=()", "₀₁₂₃₄"
                                            "₅₆₇₈₉"
                                            "₊₋₌₍₎")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_CHARS = re.escape(
    "".join(sorted(set(chr(v) for v in _SUP_MAP.values())
                   | set(chr(v) for v in _SUB_MAP.values())))
)


def _script(match: re.Match, table: dict) -> str:
    inner = match.group(1).strip()
    # Publishers use U+2212 MINUS and U+2013 EN DASH for the charge sign;
    # normalise to ASCII so anion charges convert too (e_aq⁻, Cl₂˙⁻).
    normalized = inner.replace("−", "-").replace("–", "-")
    # Only convert when every character has a Unicode equivalent; otherwise
    # hand back the original untouched (e.g. "•−" on a sulfate radical).
    if normalized and all(ord(c) in table for c in normalized):
        return normalized.translate(table)
    return inner


def clean_text(value: str | None) -> str:
    """Strip publisher HTML, converting sup/sub to Unicode where possible."""
    if not value:
        return ""
    text = re.sub(r"<sup>(.*?)</sup>", lambda m: _script(m, _SUP_MAP),
                  value, flags=re.I | re.S)
    text = re.sub(r"<sub>(.*?)</sub>", lambda m: _script(m, _SUB_MAP),
                  text, flags=re.I | re.S)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    # Publisher markup leaves a space before the now-inline script: "Zn ²⁺".
    # Note ¹²³ sit in Latin-1, not the U+2070 block, so list them explicitly.
    text = re.sub(rf"\s+([{_SCRIPT_CHARS}]+)", r"\1", text)
    return " ".join(text.split())


def short_id(value: str | None) -> str:
    """'https://openalex.org/W123' -> 'W123'."""
    if not value:
        return ""
    return value.rstrip("/").split("/")[-1]


def invert_abstract(inverted: dict[str, list[int]] | None) -> str:
    """Rebuild plaintext from OpenAlex's inverted abstract index."""
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))


def pick_oa_pdf(work: dict) -> str:
    """Best free PDF URL for a work, or '' if none is openly available."""
    candidates = []
    for loc in (work.get("best_oa_location"), work.get("primary_location")):
        if isinstance(loc, dict) and loc.get("is_oa"):
            candidates.append(loc.get("pdf_url"))
    oa = work.get("open_access") or {}
    if oa.get("is_oa"):
        candidates.append(oa.get("oa_url"))
    for loc in work.get("locations") or []:
        if isinstance(loc, dict) and loc.get("is_oa"):
            candidates.append(loc.get("pdf_url"))
    for url in candidates:
        if url and _host_of(url) not in METERED_HOSTS:
            return url
    return ""


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def flatten(work: dict, rank: int = 0) -> dict:
    """Turn a Work into one flat row suitable for CSV / report rendering."""
    authorships = work.get("authorships") or []
    names = [(a.get("author") or {}).get("display_name") or ""
             for a in authorships]
    names = [n for n in names if n]

    first_inst = ""
    if authorships:
        insts = authorships[0].get("institutions") or []
        if insts:
            first_inst = insts[0].get("display_name") or ""

    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    biblio = work.get("biblio") or {}
    oa = work.get("open_access") or {}
    topic = work.get("primary_topic") or {}

    pages = ""
    if biblio.get("first_page"):
        pages = str(biblio["first_page"])
        if biblio.get("last_page"):
            pages += f"-{biblio['last_page']}"

    doi = work.get("doi") or ""
    landing = primary.get("landing_page_url") or ""
    best_oa = work.get("best_oa_location") or {}

    return {
        "rank": rank,
        "id": short_id(work.get("id")),
        "doi": doi.replace("https://doi.org/", "") if doi else "",
        "title": clean_text(work.get("title") or work.get("display_name")),
        "first_author": names[0] if names else "",
        "first_author_institution": first_inst,
        "all_authors": "; ".join(names),
        "n_authors": len(names),
        "year": work.get("publication_year") or "",
        "date": work.get("publication_date") or "",
        "type": work.get("type") or "",
        "journal": clean_text(source.get("display_name")),
        "publisher": source.get("host_organization_name") or "",
        "volume": biblio.get("volume") or "",
        "issue": biblio.get("issue") or "",
        "pages": pages,
        "cited_by_count": work.get("cited_by_count") or 0,
        "fwci": work.get("fwci") if work.get("fwci") is not None else "",
        "is_oa": bool(oa.get("is_oa")),
        "oa_status": oa.get("oa_status") or "",
        "pdf_url": pick_oa_pdf(work),
        "landing_page_url": best_oa.get("landing_page_url") or landing,
        "doi_url": doi,
        "topic": topic.get("display_name") or "",
        "subfield": (topic.get("subfield") or {}).get("display_name") or "",
        "field": (topic.get("field") or {}).get("display_name") or "",
        "keywords": "; ".join(
            k.get("display_name") or "" for k in (work.get("keywords") or [])
        ),
        "n_references": work.get("referenced_works_count") or 0,
        "is_retracted": bool(work.get("is_retracted")),
        "relevance_score": work.get("relevance_score", ""),
        "abstract": clean_text(invert_abstract(work.get("abstract_inverted_index"))),
        "pdf_file": "",
    }


CSV_COLUMNS = [
    "rank", "id", "doi", "title", "first_author", "first_author_institution",
    "all_authors", "n_authors", "year", "date", "type", "journal", "publisher",
    "volume", "issue", "pages", "cited_by_count", "fwci", "is_oa", "oa_status",
    "pdf_url", "landing_page_url", "doi_url", "topic", "subfield", "field",
    "keywords", "n_references", "is_retracted", "relevance_score",
    "pdf_file", "abstract",
]


# --------------------------------------------------------------------------
# Output writers
# --------------------------------------------------------------------------

def write_csv(rows: list[dict], path: Path) -> None:
    """UTF-8 with BOM so Excel opens accented author names correctly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_jsonl(works: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for work in works:
            fh.write(json.dumps(work, ensure_ascii=False) + "\n")


def _abstract_snippet(text: str, limit: int) -> str:
    if not text:
        return "_No abstract in OpenAlex._"
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


def write_report(rows: list[dict], path: Path, *, title: str,
                 provenance: dict[str, Any], abstract_chars: int = 700) -> None:
    """Screening-oriented markdown report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    years = [r["year"] for r in rows if isinstance(r.get("year"), int)]
    n_oa = sum(1 for r in rows if r.get("is_oa"))
    n_pdf = sum(1 for r in rows if r.get("pdf_file"))
    n_retracted = sum(1 for r in rows if r.get("is_retracted"))

    out: list[str] = []
    out.append(f"# {title}")
    out.append("")
    out.append(f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} "
               "from [OpenAlex](https://openalex.org)._")
    out.append("")
    out.append("## Query")
    out.append("")
    for key, value in provenance.items():
        if value in (None, "", []):
            continue
        out.append(f"- **{key}**: {value}")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- Records in this report: **{len(rows)}**")
    if years:
        out.append(f"- Year range: **{min(years)}-{max(years)}**")
    out.append(f"- Open access: **{n_oa}/{len(rows)}**")
    if n_pdf:
        out.append(f"- PDFs downloaded: **{n_pdf}**")
    if n_retracted:
        out.append(f"- :warning: Retracted: **{n_retracted}**")
    out.append("")
    out.append("## Results")
    out.append("")

    for row in rows:
        flag = " :warning: **RETRACTED**" if row.get("is_retracted") else ""
        out.append(f"### {row['rank']}. {row['title']}{flag}")
        out.append("")

        authors = row["all_authors"].split("; ") if row["all_authors"] else []
        if len(authors) > 8:
            shown = ", ".join(authors[:8]) + f", ... (+{len(authors) - 8} more)"
        else:
            shown = ", ".join(authors)
        out.append(f"- **Authors**: {shown or '_unknown_'}")

        venue = row["journal"] or "_no journal_"
        bits = [venue]
        if row["year"]:
            bits.append(str(row["year"]))
        if row["volume"]:
            vol = str(row["volume"])
            if row["issue"]:
                vol += f"({row['issue']})"
            bits.append(vol)
        if row["pages"]:
            bits.append(row["pages"])
        out.append(f"- **Published in**: {', '.join(bits)}")

        metrics = [f"{row['cited_by_count']} citations"]
        if row["fwci"] != "":
            try:
                metrics.append(f"FWCI {float(row['fwci']):.2f}")
            except (TypeError, ValueError):
                pass
        if row["type"]:
            metrics.append(row["type"])
        out.append(f"- **Metrics**: {' | '.join(metrics)}")

        if row["topic"]:
            hier = " > ".join(x for x in (row["field"], row["subfield"],
                                          row["topic"]) if x)
            out.append(f"- **Topic**: {hier}")

        # Access line: always give the user a way to reach the paper.
        access: list[str] = []
        if row.get("pdf_file"):
            access.append(f"PDF downloaded -> `{row['pdf_file']}`")
        elif row["pdf_url"]:
            access.append(f"[Open-access PDF]({row['pdf_url']})")
        if row["doi"]:
            access.append(f"[DOI: {row['doi']}](https://doi.org/{row['doi']})")
        if row["landing_page_url"]:
            access.append(f"[Publisher page]({row['landing_page_url']})")
        access.append(f"[OpenAlex](https://openalex.org/{row['id']})")
        oa_tag = f"OA ({row['oa_status']})" if row["is_oa"] else "closed access"
        out.append(f"- **Access** [{oa_tag}]: " + " | ".join(access))

        out.append("")
        out.append(_abstract_snippet(row["abstract"], abstract_chars))
        out.append("")

    path.write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------
# PDF fetching (open access only, never a metered endpoint)
# --------------------------------------------------------------------------

def _safe_stem(row: dict) -> str:
    last = (row.get("first_author") or "anon").split()[-1] if row.get("first_author") else "anon"
    last = re.sub(r"[^A-Za-z0-9]", "", last) or "anon"
    year = row.get("year") or "nd"
    return f"{last}_{year}_{row['id']}"


def download_pdfs(rows: list[dict], out_dir: Path, *, verbose: bool = True,
                  pace: float = 0.5) -> tuple[int, int]:
    """Download open-access PDFs. Returns (downloaded, skipped)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    got = skipped = 0

    for row in rows:
        url = row.get("pdf_url") or ""
        if not url:
            skipped += 1
            continue
        host = _host_of(url)
        if host in METERED_HOSTS:
            # Defensive: this tool must stay on the free tier.
            if verbose:
                print(f"[skip] {row['id']}: metered host {host}", file=sys.stderr)
            skipped += 1
            continue

        dest = out_dir / f"{_safe_stem(row)}.pdf"
        # Stored relative to the run directory so the report stays portable.
        rel = f"{out_dir.name}/{dest.name}"
        if dest.exists() and dest.stat().st_size > 1024:
            row["pdf_file"] = rel
            got += 1
            continue

        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,*/*",
            })
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
            if not data.startswith(b"%PDF"):
                if verbose:
                    print(f"[skip] {row['id']}: not a PDF (likely a landing page)",
                          file=sys.stderr)
                skipped += 1
                continue
            dest.write_bytes(data)
            row["pdf_file"] = rel
            got += 1
            if verbose:
                print(f"[pdf] {dest.name} ({len(data) // 1024} KB)", file=sys.stderr)
        except Exception as exc:  # network/parse issues must not kill the run
            if verbose:
                print(f"[skip] {row['id']}: {exc}", file=sys.stderr)
            skipped += 1
        time.sleep(pace)

    return got, skipped


# --------------------------------------------------------------------------
# Filter construction
# --------------------------------------------------------------------------

def build_work_filters(args: argparse.Namespace) -> list[str]:
    filters: list[str] = []

    if args.from_year and args.to_year:
        filters.append(f"publication_year:{args.from_year}-{args.to_year}")
    elif args.from_year:
        filters.append(f"publication_year:>{args.from_year - 1}")
    elif args.to_year:
        filters.append(f"publication_year:<{args.to_year + 1}")

    if args.min_citations:
        filters.append(f"cited_by_count:>{args.min_citations - 1}")
    if args.type:
        filters.append(f"type:{args.type}")
    if args.oa_only:
        filters.append("open_access.is_oa:true")
    if args.has_abstract:
        filters.append("has_abstract:true")
    if getattr(args, "exclude_retracted", False):
        filters.append("is_retracted:false")

    if args.journal:
        value = args.journal.strip()
        if OA_ID_RE.match(value) and value.upper().startswith("S"):
            filters.append(f"primary_location.source.id:{value.upper()}")
        elif ISSN_RE.match(value):
            filters.append(f"primary_location.source.issn:{value}")
        else:
            raise SystemExit(
                f"--journal expects an OpenAlex source ID (S...) or an ISSN, got '{value}'. "
                "Use `resolve sources \"<journal name>\"` to look it up first."
            )

    if args.author:
        filters.append(f"authorships.author.id:{_normalize_author(args.author)}")
    if args.institution:
        filters.append(f"authorships.institutions.id:{_normalize_ror(args.institution)}")
    if args.topic:
        filters.append(f"topics.id:{args.topic.strip().upper()}")
    if args.filter:
        filters.append(args.filter.strip())

    return filters


def _normalize_author(value: str) -> str:
    value = value.strip()
    if ORCID_RE.match(value):
        return f"https://orcid.org/{value}"
    if "orcid.org" in value:
        return value if value.startswith("http") else f"https://{value}"
    return short_id(value).upper()


def _normalize_ror(value: str) -> str:
    value = value.strip()
    if "ror.org" in value:
        return value if value.startswith("http") else f"https://{value}"
    if OA_ID_RE.match(value):
        return value.upper()
    return f"https://ror.org/{value.lstrip('/')}"


def normalize_work_ref(value: str) -> str:
    """Accept a bare DOI, DOI URL, or OpenAlex work ID -> API path segment."""
    value = value.strip()
    if value.lower().startswith("10."):
        return f"doi:{value}"
    if "doi.org/" in value:
        return f"doi:{value.split('doi.org/', 1)[1]}"
    if value.lower().startswith("doi:"):
        return value
    return short_id(value).upper()


# --------------------------------------------------------------------------
# Shared run finalisation
# --------------------------------------------------------------------------

def finalize(client: Client, works: list[dict], args: argparse.Namespace, *,
             title: str, provenance: dict[str, Any],
             truncated: bool = False) -> None:
    """Flatten, optionally fetch PDFs, write all outputs, print a summary."""
    out_dir = Path(args.out).expanduser().resolve()
    rows = [flatten(w, i) for i, w in enumerate(works, start=1)]

    if getattr(args, "pdfs", False) and rows:
        got, skipped = download_pdfs(rows, out_dir / "pdfs", verbose=True)
        print(f"[pdfs] {got} downloaded, {skipped} unavailable "
              "(no open-access PDF - report links to the DOI instead)",
              file=sys.stderr)

    provenance = dict(provenance)
    provenance["Retrieved"] = f"{len(rows)} records"
    if truncated:
        provenance["Note"] = ("Run stopped early on the free daily budget - "
                              "these are partial results.")

    write_csv(rows, out_dir / "results.csv")
    write_jsonl(works, out_dir / "results.jsonl")
    write_report(rows, out_dir / "report.md", title=title,
                 provenance=provenance, abstract_chars=args.abstract_chars)

    print(f"\n[done] {len(rows)} records -> {out_dir}", file=sys.stderr)
    print(f"  report.md     screening report (markdown)", file=sys.stderr)
    print(f"  results.csv   Excel-ready (UTF-8 BOM)", file=sys.stderr)
    print(f"  results.jsonl raw OpenAlex records", file=sys.stderr)
    print(f"[budget] {client.budget_line()}", file=sys.stderr)

    # Compact stdout summary: this is what the agent reads back.
    print(json.dumps({
        "out_dir": str(out_dir),
        "n_records": len(rows),
        "truncated": truncated,
        "api_calls": client.calls,
        "cost_usd": round(client.session_cost, 5),
        "remaining_usd": client.remaining_usd,
        "top": [
            {"rank": r["rank"], "title": r["title"][:120], "year": r["year"],
             "cited_by_count": r["cited_by_count"], "doi": r["doi"],
             "is_oa": r["is_oa"]}
            for r in rows[:10]
        ],
    }, ensure_ascii=False, indent=2))


def collect(client: Client, path: str, params: dict, limit: int,
            pace: float = SLEEP_BETWEEN_CALLS,
            use_cursor: bool = True) -> tuple[list[dict], bool]:
    """Paginate, tolerating a budget stop so partial results survive."""
    works: list[dict] = []
    truncated = False
    try:
        for work in client.paginate(path, params, limit, pace=pace,
                                    use_cursor=use_cursor):
            works.append(work)
    except BudgetExhausted as exc:
        truncated = True
        print(f"\n[budget-stop] {exc}", file=sys.stderr)
    return works, truncated


def fetch_by_ids(client: Client, ids: list[str], limit: int) -> tuple[list[dict], bool]:
    """Batch-hydrate work IDs, 100 per request via the OR filter."""
    works: list[dict] = []
    truncated = False
    ids = ids[:limit]
    try:
        for start in range(0, len(ids), 100):
            chunk = [short_id(i) for i in ids[start:start + 100]]
            page = client.get("works", {
                "filter": "openalex_id:" + "|".join(chunk),
                "per_page": 100,
                "select": WORK_SELECT,
            })
            works.extend(page.get("results") or [])
            time.sleep(SLEEP_BETWEEN_CALLS)
    except BudgetExhausted as exc:
        truncated = True
        print(f"\n[budget-stop] {exc}", file=sys.stderr)
    return works, truncated


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace) -> None:
    client = Client()
    filters = build_work_filters(args)

    params: dict[str, Any] = {"select": WORK_SELECT}
    pace = SLEEP_BETWEEN_CALLS
    mode = "filter only"
    limit = args.limit

    if args.query:
        if args.semantic:
            params["search.semantic"] = args.query
            mode = "semantic (embeddings)"
            pace = SLEEP_SEMANTIC
            if limit > 50:
                print("[note] semantic search caps at 50 results; limiting to 50",
                      file=sys.stderr)
                limit = 50
        elif args.exact:
            params["search.exact"] = args.query
            mode = "exact (unstemmed)"
        else:
            params["search"] = args.query
            mode = "full-text (stemmed)"
    elif not filters:
        raise SystemExit("Give a query, or at least one filter. See --help.")

    if filters:
        params["filter"] = ",".join(filters)

    # Relevance ranking only exists for a search; otherwise sort explicitly.
    sort_map = {
        "relevance": None,
        "citations": "cited_by_count:desc",
        "date": "publication_date:desc",
        "date-asc": "publication_date:asc",
    }
    sort = sort_map.get(args.sort, args.sort)
    if args.sort == "relevance" and not args.query:
        sort = "cited_by_count:desc"
    if sort:
        params["sort"] = sort
    if args.query and not args.semantic:
        params["select"] = WORK_SELECT + ",relevance_score"

    if args.include_xpac:
        params["include_xpac"] = "true"

    # Semantic search always reports 50 matches and is capped at 1 req/sec, so
    # a separate count call would cost $0.001 and tell us nothing new.
    if args.semantic:
        total = min(50, limit)
        print(f"[query] {mode} | retrieving up to {limit} (semantic caps at 50)",
              file=sys.stderr)
    else:
        try:
            total = client.count("works", {k: v for k, v in params.items()
                                           if k not in ("select", "sort")})
        except BudgetExhausted as exc:
            raise SystemExit(str(exc))
        print(f"[query] {mode} | {total:,} works match | retrieving up to {limit}",
              file=sys.stderr)

    # Semantic search rejects cursor pagination (max 50 results anyway).
    works, truncated = collect(client, "works", params, limit, pace=pace,
                               use_cursor=not args.semantic)

    finalize(client, works, args,
             title=f"OpenAlex literature search: {args.query or 'filtered set'}",
             provenance={
                 "Search": f"`{args.query}`" if args.query else "_none (filters only)_",
                 "Mode": mode,
                 "Filters": f"`{params.get('filter', '')}`" if filters else "_none_",
                 "Sort": sort or "relevance",
                 "Total matches": ("_n/a for semantic search_" if args.semantic
                                   else f"{total:,}"),
             },
             truncated=truncated)


def cmd_cited_by(args: argparse.Namespace) -> None:
    """Forward citation chase: everything citing the seed work(s)."""
    client = Client()
    seeds = [normalize_work_ref(s) for s in args.seed]

    seed_ids, seed_titles = [], []
    for seed in seeds:
        work = client.get(f"works/{seed}", {"select": "id,title,publication_year"})
        seed_ids.append(short_id(work.get("id")))
        seed_titles.append(f"{work.get('title')} ({work.get('publication_year')})")

    # Repeated filters = AND. NOTE: `cites:A+B` does NOT and-combine; it
    # silently returns only A's results. Verified against the live API.
    filter_str = ",".join(f"cites:{sid}" for sid in seed_ids)
    params = {"filter": filter_str, "select": WORK_SELECT,
              "sort": {"citations": "cited_by_count:desc",
                       "date": "publication_date:desc"}.get(args.sort, args.sort)}
    params = {k: v for k, v in params.items() if v}

    total = client.count("works", {"filter": filter_str})
    label = "co-citing works" if len(seed_ids) > 1 else "citing works"
    print(f"[cited-by] {total:,} {label} | retrieving up to {args.limit}",
          file=sys.stderr)

    works, truncated = collect(client, "works", params, args.limit)

    finalize(client, works, args,
             title=("Co-citation: works citing all seeds" if len(seed_ids) > 1
                    else "Forward citations"),
             provenance={
                 "Seed work(s)": "; ".join(seed_titles),
                 "Seed ID(s)": ", ".join(seed_ids),
                 "Filter": f"`{filter_str}`",
                 "Total matches": f"{total:,}",
             },
             truncated=truncated)


def cmd_references(args: argparse.Namespace) -> None:
    """Backward chase: the reference list of a seed work."""
    client = Client()
    seed = normalize_work_ref(args.seed)
    work = client.get(f"works/{seed}",
                      {"select": "id,title,publication_year,referenced_works"})
    refs = work.get("referenced_works") or []
    print(f"[references] {len(refs)} referenced works | hydrating up to {args.limit}",
          file=sys.stderr)

    works, truncated = fetch_by_ids(client, refs, args.limit)
    works.sort(key=lambda w: w.get("cited_by_count") or 0, reverse=True)

    finalize(client, works, args,
             title=f"References of: {work.get('title')}",
             provenance={
                 "Seed work": f"{work.get('title')} ({work.get('publication_year')})",
                 "Seed ID": short_id(work.get("id")),
                 "References in record": len(refs),
             },
             truncated=truncated)


def cmd_coupling(args: argparse.Namespace) -> None:
    """Bibliographic coupling: references shared by two or more seeds."""
    client = Client()
    ref_sets, labels = [], []
    for raw in args.seed:
        work = client.get(f"works/{normalize_work_ref(raw)}",
                          {"select": "id,title,publication_year,referenced_works"})
        ref_sets.append(set(work.get("referenced_works") or []))
        labels.append(f"{work.get('title')} ({work.get('publication_year')})")

    shared = set.intersection(*ref_sets) if ref_sets else set()
    print(f"[coupling] {len(shared)} shared references across {len(ref_sets)} seeds",
          file=sys.stderr)

    works, truncated = fetch_by_ids(client, sorted(shared), args.limit)
    works.sort(key=lambda w: w.get("cited_by_count") or 0, reverse=True)

    finalize(client, works, args,
             title="Bibliographic coupling: shared references",
             provenance={
                 "Seeds": " || ".join(labels),
                 "Reference counts": ", ".join(str(len(s)) for s in ref_sets),
                 "Shared references": len(shared),
             },
             truncated=truncated)


def cmd_related(args: argparse.Namespace) -> None:
    """OpenAlex's own 'related works' neighbours for a seed."""
    client = Client()
    seed = normalize_work_ref(args.seed)
    work = client.get(f"works/{seed}", {"select": "id,title,publication_year"})
    seed_id = short_id(work.get("id"))

    params = {"filter": f"related_to:{seed_id}", "select": WORK_SELECT,
              "sort": "cited_by_count:desc"}
    works, truncated = collect(client, "works", params, args.limit)

    finalize(client, works, args,
             title=f"Related to: {work.get('title')}",
             provenance={
                 "Seed work": f"{work.get('title')} ({work.get('publication_year')})",
                 "Seed ID": seed_id,
                 "Filter": f"`related_to:{seed_id}`",
             },
             truncated=truncated)


RESOLVE_FIELDS = {
    "authors": "id,display_name,orcid,works_count,cited_by_count,"
               "last_known_institutions,topics",
    "institutions": "id,display_name,ror,country_code,type,works_count,cited_by_count",
    "sources": "id,display_name,issn_l,issn,type,host_organization_name,"
               "works_count,cited_by_count,is_oa,is_in_doaj",
    "topics": "id,display_name,description,works_count,subfield,field,domain",
    "funders": "id,display_name,country_code,works_count,cited_by_count,awards_count",
    "publishers": "id,display_name,country_codes,works_count",
}


def cmd_resolve(args: argparse.Namespace) -> None:
    """Name -> OpenAlex ID. Always do this before filtering by an entity."""
    client = Client()
    entity = args.entity
    page = client.get(entity, {
        "search": args.name,
        "per_page": args.limit,
        "select": RESOLVE_FIELDS[entity],
    })
    results = page.get("results") or []

    out = []
    for item in results:
        row = {
            "id": short_id(item.get("id")),
            "display_name": item.get("display_name"),
            "works_count": item.get("works_count"),
            "cited_by_count": item.get("cited_by_count"),
        }
        if entity == "authors":
            row["orcid"] = (item.get("orcid") or "").replace("https://orcid.org/", "")
            insts = item.get("last_known_institutions") or []
            row["institution"] = "; ".join(i.get("display_name", "") for i in insts)
            row["top_topics"] = "; ".join(
                (t.get("display_name") or "") for t in (item.get("topics") or [])[:3]
            )
        elif entity == "institutions":
            row["ror"] = (item.get("ror") or "").replace("https://ror.org/", "")
            row["country"] = item.get("country_code")
            row["type"] = item.get("type")
        elif entity == "sources":
            row["issn_l"] = item.get("issn_l")
            row["type"] = item.get("type")
            row["publisher"] = item.get("host_organization_name")
            row["is_oa"] = item.get("is_oa")
        elif entity == "topics":
            row["field"] = (item.get("field") or {}).get("display_name")
            row["description"] = (item.get("description") or "")[:160]
        out.append(row)

    print(json.dumps({
        "entity": entity,
        "query": args.name,
        "n_candidates": len(out),
        "candidates": out,
        "note": ("Names are ambiguous. Confirm which ID is correct before "
                 "running a search filtered on it."),
    }, ensure_ascii=False, indent=2))
    print(f"[budget] {client.budget_line()}", file=sys.stderr)


def cmd_get(args: argparse.Namespace) -> None:
    """Single-work lookup. Singleton requests are free on OpenAlex."""
    client = Client()
    work = client.get(f"works/{normalize_work_ref(args.seed)}",
                      {"select": WORK_SELECT})
    row = flatten(work, 1)
    if args.full_abstract:
        pass
    else:
        row["abstract"] = _abstract_snippet(row["abstract"], 900)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    print(f"[budget] {client.budget_line()}", file=sys.stderr)


def cmd_budget(args: argparse.Namespace) -> None:
    client = Client()
    client.get("works", {"per_page": 1, "select": "id",
                         "filter": "publication_year:2024"})
    key = "set" if client.api_key else "NOT set"
    print(json.dumps({
        "api_key": key,
        "daily_limit_usd": client.limit_usd,
        "remaining_usd": client.remaining_usd,
        "prepaid_remaining_usd": client.prepaid_usd,
        "note": ("Free tier only. With no prepaid balance, exhausting the daily "
                 "budget returns HTTP 429 - it cannot incur a charge."),
        "resets": "midnight UTC",
    }, indent=2))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_output_args(parser: argparse.ArgumentParser, default_dir: str) -> None:
    parser.add_argument("--out", default=default_dir,
                        help=f"output directory (default: {default_dir})")
    parser.add_argument("--limit", type=int, default=50,
                        help="max records to retrieve (default: 50)")
    parser.add_argument("--pdfs", action="store_true",
                        help="download open-access PDFs into <out>/pdfs/ "
                             "(free sources only; never a metered endpoint)")
    parser.add_argument("--abstract-chars", type=int, default=700,
                        help="abstract characters per entry in report.md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openalex.py",
        description="OpenAlex literature search - free tier only, no charges.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # topic screening
  openalex.py search "pulse radiolysis molten salt" --from-year 2015 --limit 60 --pdfs
  openalex.py search "hydrated electron molar absorptivity" --semantic --limit 40

  # resolve first, then filter (names are ambiguous)
  openalex.py resolve sources "Journal of Physical Chemistry A"
  openalex.py search "solvated electron" --journal S123456789

  # citation chasing
  openalex.py cited-by 10.1021/j100447a010 --limit 100
  openalex.py cited-by 10.1021/jp1001434 10.1063/1.4941829     # co-citation
  openalex.py references 10.1021/j100447a010
  openalex.py coupling 10.1021/jp1001434 10.1063/1.4941829
  openalex.py related 10.1021/j100447a010

  openalex.py budget
""")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="search works by text and/or filters")
    p.add_argument("query", nargs="?", help="search terms; supports AND/OR/NOT, "
                                            '"quoted phrases", wildcards with --exact')
    p.add_argument("--semantic", action="store_true",
                   help="embedding search - best for a pasted abstract or "
                        "paragraph; max 50 results")
    p.add_argument("--exact", action="store_true",
                   help="unstemmed search; required for wildcards (machin*)")
    p.add_argument("--from-year", type=int)
    p.add_argument("--to-year", type=int)
    p.add_argument("--min-citations", type=int)
    p.add_argument("--type", help="article, review, preprint, book-chapter, dataset...")
    p.add_argument("--oa-only", action="store_true", help="open access only")
    p.add_argument("--has-abstract", action="store_true",
                   help="only works with an abstract (better for screening)")
    p.add_argument("--exclude-retracted", action="store_true")
    p.add_argument("--journal", help="OpenAlex source ID (S...) or ISSN")
    p.add_argument("--author", help="OpenAlex author ID (A...) or ORCID")
    p.add_argument("--institution", help="OpenAlex institution ID (I...) or ROR")
    p.add_argument("--topic", help="OpenAlex topic ID (T...)")
    p.add_argument("--filter", help="raw extra OpenAlex filter string, appended with AND")
    p.add_argument("--sort", default="relevance",
                   choices=["relevance", "citations", "date", "date-asc"])
    p.add_argument("--include-xpac", action="store_true",
                   help="include XPAC works (datasets/repository records, "
                        "lower metadata quality, excluded by default)")
    add_output_args(p, "results/search")
    p.set_defaults(func=cmd_search)

    # cited-by
    p = sub.add_parser("cited-by",
                       help="works citing the seed; 2+ seeds = co-citation (AND)")
    p.add_argument("seed", nargs="+", help="DOI(s) or OpenAlex work ID(s)")
    p.add_argument("--sort", default="citations",
                   choices=["citations", "date"])
    add_output_args(p, "results/cited_by")
    p.set_defaults(func=cmd_cited_by)

    # references
    p = sub.add_parser("references", help="the seed's own reference list")
    p.add_argument("seed")
    add_output_args(p, "results/references")
    p.set_defaults(func=cmd_references)

    # coupling
    p = sub.add_parser("coupling",
                       help="bibliographic coupling: references shared by all seeds")
    p.add_argument("seed", nargs="+")
    add_output_args(p, "results/coupling")
    p.set_defaults(func=cmd_coupling)

    # related
    p = sub.add_parser("related", help="OpenAlex's algorithmic related works")
    p.add_argument("seed")
    add_output_args(p, "results/related")
    p.set_defaults(func=cmd_related)

    # resolve
    p = sub.add_parser("resolve", help="name -> OpenAlex ID (do this first)")
    p.add_argument("entity", choices=sorted(RESOLVE_FIELDS))
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_resolve)

    # get
    p = sub.add_parser("get", help="one work by DOI or ID (free singleton call)")
    p.add_argument("seed")
    p.add_argument("--full-abstract", action="store_true")
    p.set_defaults(func=cmd_get)

    # budget
    p = sub.add_parser("budget", help="show remaining free daily API budget")
    p.set_defaults(func=cmd_budget)

    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args()
    try:
        args.func(args)
    except BudgetExhausted as exc:
        print(f"[budget] {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[abort] interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
