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


# Distinctive multi-word markers only. Bare "your"/"here" would false-positive
# on a random alphanumeric key that happens to contain those letters.
PLACEHOLDER_HINTS = ("paste", "changeme", "xxxx", "your_key", "yourkey",
                     "your-key", "key_here", "keyhere", "key-here", "example")


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    # Real keys are alphanumeric with the odd dash/underscore; anything with
    # whitespace or angle brackets is instruction text, not a key.
    if any(ch.isspace() or ch in "<>" for ch in value):
        return True
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


def clean_num_str(val: Any) -> str:
    """Format volume, issue, year without floating point .0 artifacts."""
    if val is None:
        return ""
    s = str(val).strip()
    if s in ("nan", "NaN", "None", "<NA>"):
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def format_author_for_citation(name: str) -> str | None:
    """Format an author name into standard citation format (Last, First M.).

    Protects corporate/institutional authors by adding a trailing comma, which
    prevents EndNote and reference managers from reversing words.
    """
    if not name:
        return None
    name = name.strip()
    if not name or name.lower() in ("nan", "none", "unknown"):
        return None

    name = (name.replace("\u2010", "-")
                .replace("\u2013", "-")
                .replace("\u2014", "-")
                .replace("\xa0", " "))

    inst_keywords = (
        "univ", "lab", "inc", "dept", "center", "centre", "institute",
        "institution", "national", "office", "ind. (usa)", "radiation",
        "ministry", "association", "organization", "organisation", "committee"
    )
    if any(k in name.lower() for k in inst_keywords):
        return name.rstrip(",") + ","

    if "," in name:
        return name

    suffix = ""
    m_suf = re.search(r"\b(Jr|Sr|II|III|IV)\b\.?", name, re.IGNORECASE)
    if m_suf:
        raw_suf = m_suf.group(0).strip()
        if raw_suf.lower() in ("jr", "sr"):
            raw_suf += "."
        suffix = " " + raw_suf
        name = (name[:m_suf.start()].strip() + " " + name[m_suf.end():].strip()).strip()
        name = re.sub(r"\s+", " ", name)

    parts = name.split()
    if len(parts) == 1:
        return parts[0] + suffix

    surname = parts[-1] + suffix
    given = " ".join(parts[:-1])
    return f"{surname}, {given}"


def map_work_type_ris(oa_type: str) -> str:
    t = (oa_type or "").lower().strip()
    if "journal" in t or "article" in t:
        return "JOUR"
    if "book-chapter" in t or "chapter" in t or "section" in t:
        return "CHAP"
    if "book" in t or "monograph" in t:
        return "BOOK"
    if "dissertation" in t or "thesis" in t:
        return "THES"
    if "report" in t or "memorandum" in t:
        return "RPRT"
    if "dataset" in t:
        return "DATA"
    if "preprint" in t:
        return "ELEC"
    return "GEN"


def map_work_type_enw(oa_type: str) -> str:
    t = (oa_type or "").lower().strip()
    if "journal" in t or "article" in t:
        return "Journal Article"
    if "book-chapter" in t or "chapter" in t or "section" in t:
        return "Book Section"
    if "book" in t or "monograph" in t:
        return "Book"
    if "dissertation" in t or "thesis" in t:
        return "Thesis"
    if "report" in t or "memorandum" in t:
        return "Report"
    if "dataset" in t:
        return "Dataset"
    if "preprint" in t:
        return "Electronic Article"
    return "Generic"


def write_ris(rows: list[dict], path: Path) -> None:
    """Write RIS citation file (UTF-8 with BOM for reference managers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[str] = []
    for r in rows:
        lines: list[str] = []
        rec_type = map_work_type_ris(r.get("type", ""))
        lines.append(f"TY  - {rec_type}")

        title = str(r.get("title") or "").strip()
        if title:
            lines.append(f"TI  - {title}")
            lines.append(f"T1  - {title}")

        authors_raw = str(r.get("all_authors") or "").strip()
        if authors_raw:
            for a in authors_raw.split(";"):
                fa = format_author_for_citation(a)
                if fa:
                    lines.append(f"AU  - {fa}")

        journal = str(r.get("journal") or "").strip()
        if journal:
            lines.append(f"JO  - {journal}")
            lines.append(f"JF  - {journal}")
        elif rec_type == "RPRT" and r.get("publisher"):
            lines.append(f"PB  - {r['publisher']}")

        year = clean_num_str(r.get("year"))
        if year:
            lines.append(f"PY  - {year}")
            lines.append(f"Y1  - {year}")

        vol = clean_num_str(r.get("volume"))
        if vol:
            lines.append(f"VL  - {vol}")

        issue = clean_num_str(r.get("issue"))
        if issue:
            lines.append(f"IS  - {issue}")

        pages = str(r.get("pages") or "").strip()
        if pages:
            if "-" in pages:
                sp, ep = pages.split("-", 1)
                lines.append(f"SP  - {sp.strip()}")
                lines.append(f"EP  - {ep.strip()}")
            else:
                lines.append(f"SP  - {pages}")

        doi = str(r.get("doi") or "").strip()
        if doi:
            lines.append(f"DO  - {doi}")

        doi_url = str(r.get("doi_url") or "").strip()
        if not doi_url and doi:
            doi_url = f"https://doi.org/{doi}"
        if not doi_url:
            doi_url = str(r.get("landing_page_url") or "").strip()
        if doi_url:
            lines.append(f"UR  - {doi_url}")

        abstract = str(r.get("abstract") or "").strip()
        if abstract and not abstract.startswith("_No abstract"):
            abstract_clean = " ".join(abstract.split())
            lines.append(f"AB  - {abstract_clean}")

        for kw_field in ("topic", "subfield", "field"):
            val = str(r.get(kw_field) or "").strip()
            if val:
                lines.append(f"KW  - {val}")
        keywords = str(r.get("keywords") or "").strip()
        if keywords:
            for kw in keywords.split(";"):
                kw_clean = kw.strip()
                if kw_clean:
                    lines.append(f"KW  - {kw_clean}")

        if r.get("publisher") and rec_type != "RPRT":
            lines.append(f"PB  - {r['publisher']}")

        lines.append("ER  - ")
        records.append("\n".join(lines))

    content = "\n\n".join(records) + "\n"
    with path.open("w", encoding="utf-8-sig") as fh:
        fh.write(content)


def write_enw(rows: list[dict], path: Path) -> None:
    """Write EndNote tagged format (.enw) file (UTF-8 BOM for 1-click import)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[str] = []
    for r in rows:
        lines: list[str] = []
        rec_type = map_work_type_enw(r.get("type", ""))
        lines.append(f"%0 {rec_type}")

        title = str(r.get("title") or "").strip()
        if title:
            lines.append(f"%T {title}")

        authors_raw = str(r.get("all_authors") or "").strip()
        if authors_raw:
            for a in authors_raw.split(";"):
                fa = format_author_for_citation(a)
                if fa:
                    lines.append(f"%A {fa}")

        journal = str(r.get("journal") or "").strip()
        if journal:
            lines.append(f"%J {journal}")
        elif rec_type == "Report" and r.get("publisher"):
            lines.append(f"%I {r['publisher']}")

        year = clean_num_str(r.get("year"))
        if year:
            lines.append(f"%D {year}")

        vol = clean_num_str(r.get("volume"))
        if vol:
            lines.append(f"%V {vol}")

        issue = clean_num_str(r.get("issue"))
        if issue:
            lines.append(f"%N {issue}")

        pages = str(r.get("pages") or "").strip()
        if pages:
            if "-" in pages:
                sp, ep = pages.split("-", 1)
                if sp.strip() == ep.strip():
                    pages = sp.strip()
            lines.append(f"%P {pages}")

        doi = str(r.get("doi") or "").strip()
        if doi:
            lines.append(f"%R {doi}")

        doi_url = str(r.get("doi_url") or "").strip()
        if not doi_url and doi:
            doi_url = f"https://doi.org/{doi}"
        if not doi_url:
            doi_url = str(r.get("landing_page_url") or "").strip()
        if doi_url:
            lines.append(f"%U {doi_url}")

        abstract = str(r.get("abstract") or "").strip()
        if abstract and not abstract.startswith("_No abstract"):
            abstract_clean = " ".join(abstract.split())
            lines.append(f"%X {abstract_clean}")

        for kw_field in ("topic", "subfield", "field"):
            val = str(r.get(kw_field) or "").strip()
            if val:
                lines.append(f"%K {val}")
        keywords = str(r.get("keywords") or "").strip()
        if keywords:
            for kw in keywords.split(";"):
                kw_clean = kw.strip()
                if kw_clean:
                    lines.append(f"%K {kw_clean}")

        if r.get("publisher") and rec_type != "Report":
            lines.append(f"%I {r['publisher']}")

        records.append("\n".join(lines))

    content = "\n\n".join(records) + "\n"
    with path.open("w", encoding="utf-8-sig") as fh:
        fh.write(content)


def write_html_report(rows: list[dict], path: Path, *, title: str,
                      provenance: dict[str, Any], abstract_chars: int = 700) -> None:
    """Standalone, interactive HTML screening report with search, filter, and sort."""
    path.parent.mkdir(parents=True, exist_ok=True)

    years = []
    for r in rows:
        y = r.get("year")
        if isinstance(y, int):
            years.append(y)
        elif str(y).isdigit():
            years.append(int(y))

    year_range_str = f"{min(years)}–{max(years)}" if years else "N/A"
    n_oa = sum(1 for r in rows if r.get("is_oa"))
    pct_oa = round((n_oa / len(rows)) * 100) if rows else 0
    n_pdf = sum(1 for r in rows if r.get("pdf_file"))
    n_retracted = sum(1 for r in rows if r.get("is_retracted"))

    cites_list = []
    for r in rows:
        try:
            cites_list.append(int(r.get("cited_by_count") or 0))
        except (ValueError, TypeError):
            cites_list.append(0)
    total_cites = sum(cites_list)
    avg_cites = round(total_cites / len(rows), 1) if rows else 0
    max_cites = max(cites_list) if cites_list else 0

    all_types = sorted({str(r.get("type") or "other").lower().strip() for r in rows if r.get("type")})

    prov_items = []
    for k, v in provenance.items():
        if v not in (None, "", []):
            prov_items.append(f"<div><span class='prov-label'>{html.escape(str(k))}:</span> <span class='prov-val'>{html.escape(str(v))}</span></div>")
    prov_html = "\n".join(prov_items) if prov_items else "<div>No query parameters recorded.</div>"

    type_options = "\n".join(f'<option value="{html.escape(t)}">{html.escape(t.replace("-", " ").title())}</option>' for t in all_types)

    table_rows = []
    for r in rows:
        rank = r.get("rank", 0)
        raw_title = str(r.get("title") or "Untitled").strip()
        t_esc = html.escape(raw_title)

        authors_raw = str(r.get("all_authors") or "").strip()
        auth_list = [a.strip() for a in authors_raw.split(";") if a.strip()]
        if len(auth_list) > 8:
            auth_shown = ", ".join(auth_list[:8]) + f", ... (+{len(auth_list) - 8} more)"
        else:
            auth_shown = ", ".join(auth_list) if auth_list else "Unknown authors"
        auth_esc = html.escape(auth_shown)

        journal = str(r.get("journal") or "").strip()
        if not journal or journal.lower() in ("nan", "none"):
            journal = str(r.get("type") or "Scholarly Work").title()
        j_esc = html.escape(journal)

        year_val = clean_num_str(r.get("year"))
        vol_val = clean_num_str(r.get("volume"))
        iss_val = clean_num_str(r.get("issue"))
        pages_val = str(r.get("pages") or "").strip()

        pub_bits = []
        if year_val:
            pub_bits.append(year_val)
        if vol_val:
            vol_str = f"Vol. {vol_val}"
            if iss_val:
                vol_str += f"({iss_val})"
            pub_bits.append(vol_str)
        if pages_val:
            pub_bits.append(f"pp. {pages_val}")
        pub_info = html.escape(" • ".join(pub_bits))

        cites = int(r.get("cited_by_count") or 0)
        fwci_raw = r.get("fwci")
        fwci_val = ""
        if fwci_raw not in (None, "", "nan"):
            try:
                fwci_val = f"{float(fwci_raw):.2f}"
            except (ValueError, TypeError):
                fwci_val = ""

        is_oa = bool(r.get("is_oa"))
        oa_status = str(r.get("oa_status") or ("oa" if is_oa else "closed")).lower().strip()
        oa_badge_class = f"badge-oa-{oa_status}" if is_oa else "badge-closed"
        oa_badge_text = f"OA {oa_status.capitalize()}" if is_oa else "Closed Access"

        doi = str(r.get("doi") or "").strip()
        doi_url = str(r.get("doi_url") or "").strip()
        if not doi_url and doi:
            doi_url = f"https://doi.org/{doi}"
        if not doi_url:
            doi_url = str(r.get("landing_page_url") or "").strip()

        pdf_url = str(r.get("pdf_file") or r.get("pdf_url") or "").strip()
        openalex_url = f"https://openalex.org/{r.get('id', '')}" if r.get("id") else ""

        abstract_text = str(r.get("abstract") or "").strip()
        has_abstract = bool(abstract_text and not abstract_text.startswith("_No abstract"))
        abs_esc = html.escape(abstract_text)

        w_type = str(r.get("type") or "other").lower().strip()

        actions = []
        if doi_url:
            actions.append(f'<a href="{html.escape(doi_url)}" target="_blank" class="btn btn-doi" title="Open DOI">DOI &rarr;</a>')
        if pdf_url:
            actions.append(f'<a href="{html.escape(pdf_url)}" target="_blank" class="btn btn-pdf" title="Open PDF">PDF &darr;</a>')
        if openalex_url:
            actions.append(f'<a href="{html.escape(openalex_url)}" target="_blank" class="btn btn-oa" title="View OpenAlex Record">OpenAlex</a>')
        actions_html = " ".join(actions)

        abstract_html = ""
        if has_abstract:
            abstract_html = f"""
            <div class="abstract-wrapper">
                <button class="btn-abstract" onclick="toggleRowAbstract(this)">Show Abstract</button>
                <div class="abstract-content">{abs_esc}</div>
            </div>
            """

        fwci_html = f'<div class="metric-sub">FWCI: <strong>{fwci_val}</strong></div>' if fwci_val else ""
        retracted_badge = '<span class="badge-retracted">:warning: RETRACTED</span>' if r.get("is_retracted") else ""

        row_html = f"""
        <tr data-rank="{rank}"
            data-title="{t_esc}"
            data-authors="{auth_esc}"
            data-journal="{j_esc}"
            data-year="{year_val}"
            data-cites="{cites}"
            data-fwci="{fwci_val}"
            data-oa="{'oa' if is_oa else 'closed'}"
            data-type="{html.escape(w_type)}">
            <td class="col-rank">{rank}</td>
            <td class="col-details">
                <div class="title-line">
                    <a href="{html.escape(doi_url or openalex_url)}" target="_blank" class="paper-title">{t_esc}</a>
                    {retracted_badge}
                </div>
                <div class="paper-authors">{auth_esc}</div>
                {abstract_html}
            </td>
            <td class="col-venue">
                <div class="venue-name">{j_esc}</div>
                <div class="pub-details">{pub_info}</div>
            </td>
            <td class="col-metrics">
                <div class="cites-pill"><strong>{cites}</strong> cites</div>
                {fwci_html}
                <div class="type-pill">{html.escape(w_type.replace('-', ' ').title())}</div>
            </td>
            <td class="col-access">
                <span class="badge {oa_badge_class}">{oa_badge_text}</span>
                <div class="actions-group">
                    {actions_html}
                </div>
            </td>
        </tr>
        """
        table_rows.append(row_html)

    body_rows_html = "\n".join(table_rows)

    retracted_stat_html = f'<div class="stat-card stat-alert"><div class="stat-num">{n_retracted}</div><div class="stat-lbl">Retracted Works</div></div>' if n_retracted else ""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(title)}</title>
    <style>
        :root {{
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --bg: #f8fafc;
            --card: #ffffff;
            --text: #0f172a;
            --text-muted: #64748b;
            --border: #e2e8f0;
            --radius: 8px;
            --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
            --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.5;
            padding-bottom: 60px;
        }}
        .hero {{
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #1e3a8a 100%);
            color: #ffffff;
            padding: 40px 32px 36px;
            border-bottom: 1px solid #334155;
        }}
        .hero-container {{ max-width: 1400px; margin: 0 auto; }}
        .hero-tag {{
            display: inline-block;
            background: rgba(255, 255, 255, 0.12);
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 3px 10px;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            color: #93c5fd;
            margin-bottom: 12px;
        }}
        .hero h1 {{ font-size: 1.85rem; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.02em; }}
        .hero-sub {{ font-size: 0.95rem; color: #94a3b8; margin-bottom: 20px; }}
        .hero-sub a {{ color: #60a5fa; text-decoration: none; }}
        .hero-sub a:hover {{ text-decoration: underline; }}
        .prov-box {{
            background: rgba(0, 0, 0, 0.2);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: var(--radius);
            padding: 12px 16px;
            display: flex;
            flex-wrap: wrap;
            gap: 16px 24px;
            font-size: 0.85rem;
        }}
        .prov-label {{ color: #94a3b8; font-weight: 500; }}
        .prov-val {{ color: #f1f5f9; font-weight: 600; }}
        .stats-grid {{
            max-width: 1400px;
            margin: -20px auto 28px;
            padding: 0 32px;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            position: relative;
            z-index: 10;
        }}
        .stat-card {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 16px 20px;
            box-shadow: var(--shadow-sm);
        }}
        .stat-num {{ font-size: 1.5rem; font-weight: 700; color: var(--primary); font-family: monospace; }}
        .stat-lbl {{ font-size: 0.75rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px; }}
        .stat-alert .stat-num {{ color: #dc2626; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 0 32px; }}
        .toolbar {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 14px 18px;
            margin-bottom: 20px;
            box-shadow: var(--shadow-sm);
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 12px;
        }}
        .search-box {{ flex: 1; min-width: 260px; }}
        .search-box input {{
            width: 100%;
            padding: 8px 12px;
            border: 1px solid var(--border);
            border-radius: var(--radius);
            font-size: 0.9rem;
            outline: none;
            transition: border-color 0.2s;
        }}
        .search-box input:focus {{ border-color: var(--primary); }}
        .toolbar select {{
            padding: 8px 12px;
            border: 1px solid var(--border);
            border-radius: var(--radius);
            font-size: 0.85rem;
            background: #fff;
            color: var(--text);
            cursor: pointer;
            outline: none;
        }}
        .btn-toggle-all {{
            padding: 8px 12px;
            border: 1px solid var(--border);
            border-radius: var(--radius);
            font-size: 0.85rem;
            background: #f1f5f9;
            color: #334155;
            cursor: pointer;
            font-weight: 500;
        }}
        .btn-toggle-all:hover {{ background: #e2e8f0; }}
        .count-badge {{ font-size: 0.85rem; font-weight: 600; color: var(--text-muted); margin-left: auto; }}
        .table-wrap {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow-sm);
            overflow-x: auto;
        }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 0.88rem; }}
        th {{
            background: #f8fafc;
            padding: 12px 16px;
            font-weight: 600;
            color: #475569;
            border-bottom: 1px solid var(--border);
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        td {{ padding: 14px 16px; border-bottom: 1px solid var(--border); vertical-align: top; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover {{ background: #fbfcfe; }}
        .col-rank {{ width: 44px; color: var(--text-muted); font-weight: 600; text-align: center; }}
        .col-details {{ min-width: 440px; }}
        .col-venue {{ min-width: 220px; }}
        .col-metrics {{ width: 140px; white-space: nowrap; }}
        .col-access {{ width: 170px; }}
        .paper-title {{ font-size: 0.98rem; font-weight: 600; color: #1e3a8a; text-decoration: none; line-height: 1.4; }}
        .paper-title:hover {{ color: var(--primary); text-decoration: underline; }}
        .paper-authors {{ font-size: 0.82rem; color: #475569; margin-top: 4px; }}
        .venue-name {{ font-weight: 600; color: #334155; }}
        .pub-details {{ font-size: 0.8rem; color: var(--text-muted); margin-top: 2px; }}
        .cites-pill {{
            display: inline-block;
            background: #eff6ff;
            color: #1e40af;
            border: 1px solid #bfdbfe;
            padding: 2px 8px;
            border-radius: 9999px;
            font-size: 0.78rem;
        }}
        .metric-sub {{ font-size: 0.76rem; color: var(--text-muted); margin-top: 3px; }}
        .type-pill {{
            display: inline-block;
            background: #f1f5f9;
            color: #475569;
            border: 1px solid #cbd5e1;
            padding: 1px 6px;
            border-radius: 4px;
            font-size: 0.72rem;
            margin-top: 4px;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            margin-bottom: 6px;
        }}
        .badge-oa-gold {{ background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }}
        .badge-oa-green {{ background: #dcfce7; color: #166534; border: 1px solid #bbf7d0; }}
        .badge-oa-hybrid {{ background: #e0f2fe; color: #075985; border: 1px solid #bae6fd; }}
        .badge-oa-bronze {{ background: #ffedd5; color: #9a3412; border: 1px solid #fed7aa; }}
        .badge-oa-oa {{ background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }}
        .badge-closed {{ background: #f1f5f9; color: #475569; border: 1px solid #e2e8f0; }}
        .badge-retracted {{ background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; font-size: 0.72rem; padding: 2px 6px; border-radius: 4px; font-weight: 700; margin-left: 6px; }}
        .actions-group {{ display: flex; flex-wrap: wrap; gap: 4px; }}
        .btn {{
            display: inline-block;
            padding: 3px 7px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            text-decoration: none;
            transition: background 0.15s;
        }}
        .btn-doi {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }}
        .btn-doi:hover {{ background: #dbeafe; }}
        .btn-pdf {{ background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }}
        .btn-pdf:hover {{ background: #d1fae5; }}
        .btn-oa {{ background: #f8fafc; color: #64748b; border: 1px solid #cbd5e1; }}
        .btn-oa:hover {{ background: #e2e8f0; color: #334155; }}
        .abstract-wrapper {{ margin-top: 6px; }}
        .btn-abstract {{
            background: none;
            border: 1px solid var(--border);
            border-radius: 4px;
            color: var(--primary);
            font-size: 0.75rem;
            padding: 2px 6px;
            cursor: pointer;
            font-weight: 500;
        }}
        .btn-abstract:hover, .btn-abstract.active {{ background: #eff6ff; border-color: #bfdbfe; }}
        .abstract-content {{
            display: none;
            margin-top: 6px;
            padding: 8px 12px;
            background: #f8fafc;
            border-left: 3px solid var(--primary);
            border-radius: 0 4px 4px 0;
            font-size: 0.83rem;
            color: #334155;
            line-height: 1.55;
        }}
    </style>
</head>
<body>
    <header class="hero">
        <div class="hero-container">
            <span class="hero-tag">Literature Screening Report</span>
            <h1>{html.escape(title)}</h1>
            <div class="hero-sub">
                Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} from <a href="https://openalex.org" target="_blank">OpenAlex</a>
            </div>
            <div class="prov-box">
                {prov_html}
            </div>
        </div>
    </header>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-num">{len(rows)}</div>
            <div class="stat-lbl">Total Records</div>
        </div>
        <div class="stat-card">
            <div class="stat-num">{pct_oa}%</div>
            <div class="stat-lbl">Open Access ({n_oa}/{len(rows)})</div>
        </div>
        <div class="stat-card">
            <div class="stat-num">{total_cites}</div>
            <div class="stat-lbl">Total Citations (Avg {avg_cites})</div>
        </div>
        <div class="stat-card">
            <div class="stat-num">{year_range_str}</div>
            <div class="stat-lbl">Publication Span</div>
        </div>
        {retracted_stat_html}
    </div>

    <main class="container">
        <div class="toolbar">
            <div class="search-box">
                <input type="text" id="searchInput" placeholder="Filter by title, author, journal, abstract..." oninput="applyFiltersAndSort()">
            </div>
            <select id="accessFilter" onchange="applyFiltersAndSort()">
                <option value="all">All Access</option>
                <option value="oa">Open Access Only</option>
                <option value="closed">Closed Access Only</option>
            </select>
            <select id="typeFilter" onchange="applyFiltersAndSort()">
                <option value="all">All Work Types</option>
                {type_options}
            </select>
            <select id="sortSelect" onchange="applyFiltersAndSort()">
                <option value="citations-desc">Citations (High &rarr; Low)</option>
                <option value="year-desc">Year (Newest First)</option>
                <option value="year-asc">Year (Oldest First)</option>
                <option value="title-asc">Title (A &rarr; Z)</option>
                <option value="fwci-desc">FWCI (High &rarr; Low)</option>
            </select>
            <button class="btn-toggle-all" onclick="toggleAllAbstracts()">Toggle All Abstracts</button>
            <span class="count-badge" id="countDisplay">Showing {len(rows)} of {len(rows)} records</span>
        </div>

        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th class="col-rank">#</th>
                        <th class="col-details">Work Details</th>
                        <th class="col-venue">Venue &amp; Date</th>
                        <th class="col-metrics">Metrics</th>
                        <th class="col-access">Access &amp; Links</th>
                    </tr>
                </thead>
                <tbody id="recordsTbody">
                    {body_rows_html}
                </tbody>
            </table>
        </div>
    </main>

    <script>
        function applyFiltersAndSort() {{
            const q = document.getElementById("searchInput").value.toLowerCase().trim();
            const access = document.getElementById("accessFilter").value;
            const type = document.getElementById("typeFilter").value;
            const sort = document.getElementById("sortSelect").value;
            const tbody = document.getElementById("recordsTbody");
            const rows = Array.from(tbody.querySelectorAll("tr"));

            let visible = 0;
            rows.forEach(row => {{
                const title = (row.dataset.title || "").toLowerCase();
                const authors = (row.dataset.authors || "").toLowerCase();
                const journal = (row.dataset.journal || "").toLowerCase();
                const absElem = row.querySelector(".abstract-content");
                const abstract = absElem ? absElem.innerText.toLowerCase() : "";

                const matchesQuery = !q || title.includes(q) || authors.includes(q) || journal.includes(q) || abstract.includes(q);
                const matchesAccess = access === "all" || row.dataset.oa === access;
                const matchesType = type === "all" || row.dataset.type.toLowerCase() === type.toLowerCase();

                if (matchesQuery && matchesAccess && matchesType) {{
                    row.style.display = "";
                    visible++;
                }} else {{
                    row.style.display = "none";
                }}
            }});

            rows.sort((a, b) => {{
                if (sort === "citations-desc") {{
                    return (parseFloat(b.dataset.cites) || 0) - (parseFloat(a.dataset.cites) || 0);
                }} else if (sort === "year-desc") {{
                    return (parseInt(b.dataset.year) || 0) - (parseInt(a.dataset.year) || 0);
                }} else if (sort === "year-asc") {{
                    return (parseInt(a.dataset.year) || 0) - (parseInt(b.dataset.year) || 0);
                }} else if (sort === "title-asc") {{
                    return (a.dataset.title || "").localeCompare(b.dataset.title || "");
                }} else if (sort === "fwci-desc") {{
                    return (parseFloat(b.dataset.fwci) || 0) - (parseFloat(a.dataset.fwci) || 0);
                }}
                return (parseInt(a.dataset.rank) || 0) - (parseInt(b.dataset.rank) || 0);
            }});

            rows.forEach(r => tbody.appendChild(r));
            document.getElementById("countDisplay").innerText = `Showing ${{visible}} of ${{rows.length}} records`;
        }}

        function toggleRowAbstract(btn) {{
            const content = btn.nextElementSibling;
            if (!content) return;
            const isHidden = content.style.display === "none" || !content.style.display;
            if (isHidden) {{
                content.style.display = "block";
                btn.innerText = "Hide Abstract";
                btn.classList.add("active");
            }} else {{
                content.style.display = "none";
                btn.innerText = "Show Abstract";
                btn.classList.remove("active");
            }}
        }}

        let allExpanded = false;
        function toggleAllAbstracts() {{
            allExpanded = !allExpanded;
            const buttons = document.querySelectorAll(".btn-abstract");
            buttons.forEach(btn => {{
                const content = btn.nextElementSibling;
                if (content) {{
                    content.style.display = allExpanded ? "block" : "none";
                    btn.innerText = allExpanded ? "Hide Abstract" : "Show Abstract";
                    if (allExpanded) btn.classList.add("active"); else btn.classList.remove("active");
                }}
            }});
        }}
    </script>
</body>
</html>
"""
    path.write_text(html_content, encoding="utf-8")


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

    use_raw_dir = not getattr(args, "flat", False)
    data_dir = out_dir / "raw" if use_raw_dir else out_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    csv_path = data_dir / "results.csv"
    jsonl_path = data_dir / "results.jsonl"
    md_path = out_dir / "report.md"

    write_csv(rows, csv_path)
    write_jsonl(works, jsonl_path)
    write_report(rows, md_path, title=title,
                 provenance=provenance, abstract_chars=args.abstract_chars)

    gen_html = getattr(args, "html", False) or getattr(args, "all", False)
    gen_ris = getattr(args, "ris", False) or getattr(args, "citations", False) or getattr(args, "all", False)
    gen_enw = getattr(args, "enw", False) or getattr(args, "citations", False) or getattr(args, "all", False)

    html_path = out_dir / "report.html" if gen_html else None
    ris_path = out_dir / "references.ris" if gen_ris else None
    enw_path = out_dir / "references.enw" if gen_enw else None

    if gen_html:
        write_html_report(rows, html_path, title=title,
                          provenance=provenance, abstract_chars=args.abstract_chars)
    if gen_ris:
        write_ris(rows, ris_path)
    if gen_enw:
        write_enw(rows, enw_path)

    print(f"\n[done] {len(rows)} records -> {out_dir}", file=sys.stderr)
    print(f"  report.md        screening report (markdown)", file=sys.stderr)
    if html_path:
        print(f"  report.html      interactive screening report (HTML)", file=sys.stderr)
    if ris_path:
        print(f"  references.ris   universal RIS citation file", file=sys.stderr)
    if enw_path:
        print(f"  references.enw   EndNote tagged citation file", file=sys.stderr)
    try:
        csv_rel = csv_path.relative_to(out_dir)
        jsonl_rel = jsonl_path.relative_to(out_dir)
    except ValueError:
        csv_rel = csv_path.name
        jsonl_rel = jsonl_path.name
    print(f"  {csv_rel}  Excel-ready (UTF-8 BOM)", file=sys.stderr)
    print(f"  {jsonl_rel} raw OpenAlex records", file=sys.stderr)
    print(f"[budget] {client.budget_line()}", file=sys.stderr)

    # Compact stdout summary: this is what the agent reads back.
    print(json.dumps({
        "out_dir": str(out_dir),
        "n_records": len(rows),
        "truncated": truncated,
        "api_calls": client.calls,
        "cost_usd": round(client.session_cost, 5),
        "remaining_usd": client.remaining_usd,
        "files": {
            "report_md": str(md_path),
            "report_html": str(html_path) if html_path else None,
            "references_ris": str(ris_path) if ris_path else None,
            "references_enw": str(enw_path) if enw_path else None,
            "raw_csv": str(csv_path),
            "raw_jsonl": str(jsonl_path),
        },
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


def cmd_report(args: argparse.Namespace) -> None:
    """Offline report generation from an existing results directory."""
    target_dir = Path(args.dir).expanduser().resolve()
    if not target_dir.is_dir():
        print(f"ERROR: {target_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    candidates = [
        target_dir / "raw" / "results.jsonl",
        target_dir / "results.jsonl",
        target_dir / "raw" / "results.csv",
        target_dir / "results.csv",
    ]
    data_file = None
    for c in candidates:
        if c.is_file():
            data_file = c
            break

    if not data_file:
        print(f"ERROR: No results.jsonl or results.csv found in {target_dir} or {target_dir}/raw", file=sys.stderr)
        sys.exit(1)

    rows: list[dict] = []
    if data_file.suffix == ".jsonl":
        works = []
        with data_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    works.append(json.loads(line))
        rows = [flatten(w, i) for i, w in enumerate(works, start=1)]
    else:
        with data_file.open("r", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for i, r in enumerate(reader, start=1):
                r["rank"] = i
                r["is_oa"] = str(r.get("is_oa", "")).lower() in ("true", "1")
                r["is_retracted"] = str(r.get("is_retracted", "")).lower() in ("true", "1")
                try:
                    r["cited_by_count"] = int(r.get("cited_by_count") or 0)
                except (ValueError, TypeError):
                    r["cited_by_count"] = 0
                rows.append(r)

    title = args.title or f"Literature Report: {target_dir.name.replace('_', ' ').title()}"
    provenance = {"Source Directory": str(target_dir), "Records": len(rows), "Mode": "Offline Report"}

    has_specific = any([args.html, args.ris, args.enw, args.citations, args.md])
    do_all = args.all or not has_specific

    gen_html = args.html or args.all or do_all
    gen_ris = args.ris or args.citations or args.all or do_all
    gen_enw = args.enw or args.citations or args.all or do_all
    gen_md = args.md or args.all or do_all

    if getattr(args, "organize_raw", False):
        raw_dir = target_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for fn in ("results.csv", "results.jsonl"):
            src = target_dir / fn
            if src.is_file():
                dest = raw_dir / fn
                if not dest.exists():
                    src.replace(dest)

    if gen_md:
        write_report(rows, target_dir / "report.md", title=title,
                     provenance=provenance, abstract_chars=args.abstract_chars)
    if gen_html:
        write_html_report(rows, target_dir / "report.html", title=title,
                          provenance=provenance, abstract_chars=args.abstract_chars)
    if gen_ris:
        write_ris(rows, target_dir / "references.ris")
    if gen_enw:
        write_enw(rows, target_dir / "references.enw")

    print(f"\n[report done] {len(rows)} records processed in {target_dir}", file=sys.stderr)
    if gen_md:
        print(f"  report.md        screening report (markdown)", file=sys.stderr)
    if gen_html:
        print(f"  report.html      interactive screening report (HTML)", file=sys.stderr)
    if gen_ris:
        print(f"  references.ris   universal RIS citation file", file=sys.stderr)
    if gen_enw:
        print(f"  references.enw   EndNote tagged citation file", file=sys.stderr)


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
    parser.add_argument("--html", action="store_true",
                        help="generate interactive HTML screening report (<out>/report.html)")
    parser.add_argument("--ris", action="store_true",
                        help="generate universal RIS citation file (<out>/references.ris)")
    parser.add_argument("--enw", action="store_true",
                        help="generate EndNote tagged citation file (<out>/references.enw)")
    parser.add_argument("--citations", action="store_true",
                        help="shortcut: generate both RIS and ENW citation files")
    parser.add_argument("--all", action="store_true",
                        help="generate all report and citation formats (HTML, RIS, ENW)")
    parser.add_argument("--flat", action="store_true",
                        help="keep raw results.csv and results.jsonl in root output directory instead of raw/ subdirectory")


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

    # report
    p = sub.add_parser("report",
                       help="generate HTML/citation reports offline from an existing results directory")
    p.add_argument("dir", help="existing results directory containing results.jsonl or results.csv")
    p.add_argument("--title", help="report title (defaults to directory name)")
    p.add_argument("--abstract-chars", type=int, default=700,
                   help="abstract characters per entry in report.md")
    p.add_argument("--html", action="store_true",
                   help="generate interactive HTML screening report")
    p.add_argument("--ris", action="store_true",
                   help="generate universal RIS citation file")
    p.add_argument("--enw", action="store_true",
                   help="generate EndNote tagged citation file")
    p.add_argument("--citations", action="store_true",
                   help="shortcut: generate both RIS and ENW citation files")
    p.add_argument("--md", action="store_true",
                   help="regenerate markdown report (report.md)")
    p.add_argument("--all", action="store_true",
                   help="generate all report formats (HTML, RIS, ENW, MD)")
    p.add_argument("--organize-raw", action="store_true",
                   help="move existing results.csv/results.jsonl into raw/ subdirectory if in root")
    p.set_defaults(func=cmd_report)

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
