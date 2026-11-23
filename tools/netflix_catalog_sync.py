"""Sync the US Netflix catalog from JustWatch GraphQL into an atomic local SQLite DB.

Pure Python standard library. Produces a compact (~1 MB) read-optimized SQLite
database with strictly regex-validated numeric Netflix IDs and precomputed O(1)
title aliases (subtitles before ':'/'-', compact hyphen-stripped, digits/words,
roman numerals).

Security invariants:
- Parent directory is enforced to mode 0700.
- Temporary database file is created exclusively with mode 0600 before SQLite opens it
  (never world-readable at any point) and atomically swapped via os.replace.
- Sanity check refuses to overwrite an existing DB if the new title count drops
  below 50% of the previous indexed_count (unless --force is passed).
- No raw third-party URLs are stored; only the validated 6-10 digit Netflix title ID.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

JUSTWATCH_URL = "https://apis.justwatch.com/graphql"
NETFLIX_ID_RE = re.compile(r"^https://www\.netflix\.com/(?:title|watch)/([0-9]{6,10})(?:[/?#].*)?$")
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "r2d2-catalog" / "netflix.db"

GRAPHQL_QUERY = """
query NetflixUSCatalog($after: String, $first: Int!, $objectTypes: [ObjectType!]) {
  popularTitles(
    country: US
    first: $first
    after: $after
    filter: { packages: ["nfx"], monetizationTypes: [FLATRATE], objectTypes: $objectTypes }
  ) {
    totalCount
    pageInfo {
      endCursor
      hasNextPage
    }
    edges {
      node {
        id
        objectType
        content(country: US, language: "en") {
          title
          originalReleaseYear
        }
        offers(country: US, platform: WEB, filter: { packages: ["nfx"] }) {
          monetizationType
          standardWebURL
          package {
            technicalName
          }
        }
      }
    }
  }
}
"""

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_SPACES_RE = re.compile(r"\s+")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")
_SUBTITLE_SPLIT_RE = re.compile(r"\s*(?::|\s[-–—]\s)\s*")
_HYPHEN_RE = re.compile(r"(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])")

_NUM_TO_WORD = {
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
}
_WORD_TO_NUM = {v: k for k, v in _NUM_TO_WORD.items()}
_ROMAN_TO_NUM = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_NUM_TO_ROMAN = {v: k for k, v in _ROMAN_TO_NUM.items()}


def normalize_title(text: str) -> str:
    """Case-fold, strip diacritics and punctuation, collapse whitespace."""
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _NON_ALNUM_RE.sub(" ", ascii_only.casefold().replace("&", " and "))
    return _SPACES_RE.sub(" ", cleaned).strip()


def strip_leading_article(norm: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", norm, count=1).strip()


def generate_aliases(raw_title: str, norm_title: str, bare_title: str) -> list[str]:
    """Generate deterministic O(1) aliases for a catalog title.

    Covers:
    - Subtitle stripping before ':' or ' - ' (e.g., 'Arcane: League of Legends' -> 'arcane')
    - Hyphen-joined compact form ('K-Pop Demon Hunters' -> 'kpop demon hunters')
    - '&' <-> 'and' variants (norm already expands '&' to 'and')
    - Digit <-> English word <-> Roman numeral token swaps ('2' <-> 'two' <-> 'ii')
    """
    out: set[str] = set()
    base_forms = {norm_title, bare_title}

    # 1. Primary title before ':' or ' - ' / ' – '
    parts = _SUBTITLE_SPLIT_RE.split(raw_title, maxsplit=1)
    if len(parts) == 2:
        primary_norm = normalize_title(parts[0])
        if len(primary_norm) >= 4:
            out.add(primary_norm)
            primary_bare = strip_leading_article(primary_norm)
            if len(primary_bare) >= 4:
                out.add(primary_bare)

    # 2. Compact hyphen-joined form ("K-Pop" -> "kpop", "Spider-Man" -> "spiderman")
    if "-" in raw_title:
        joined = _HYPHEN_RE.sub("", raw_title)
        j_norm = normalize_title(joined)
        if j_norm and j_norm not in base_forms:
            out.add(j_norm)
            j_bare = strip_leading_article(j_norm)
            if j_bare:
                out.add(j_bare)

    # 3. Digit / word / Roman numeral token substitutions (single-pass per variant map)
    seeds = set(base_forms) | set(out)
    for seed in list(seeds):
        toks = seed.split()
        if not toks:
            continue
        for mapping in (_NUM_TO_WORD, _WORD_TO_NUM, _ROMAN_TO_NUM, _NUM_TO_ROMAN):
            changed = False
            mapped = []
            for t in toks:
                if t in mapping:
                    mapped.append(mapping[t])
                    changed = True
                else:
                    mapped.append(t)
            if changed:
                cand = " ".join(mapped)
                if cand:
                    out.add(cand)

    out.discard("")
    out -= base_forms
    return sorted(out)


def extract_netflix_id(offers: list[dict]) -> str | None:
    """Extract the most canonical validated numeric Netflix title ID from JustWatch offers."""
    candidates: list[tuple[int, int, str]] = []
    for offer in offers or ():
        if not isinstance(offer, dict):
            continue
        if offer.get("monetizationType") != "FLATRATE":
            continue
        pkg = offer.get("package") or {}
        tech = pkg.get("technicalName") if isinstance(pkg, dict) else None
        if tech not in ("netflix", "netflixbasicwithads"):
            continue
        url = offer.get("standardWebURL")
        if not isinstance(url, str) or len(url) > 512:
            continue
        match = NETFLIX_ID_RE.fullmatch(url)
        if not match:
            continue
        nid = match.group(1)
        pkg_priority = 0 if tech == "netflix" else 1
        candidates.append((pkg_priority, int(nid), nid))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def _graphql_page(
    after: str | None,
    page_size: int,
    *,
    object_types: list[str] | None = None,
    timeout_s: float = 15.0,
) -> dict:
    variables: dict = {"after": after, "first": page_size}
    if object_types:
        variables["objectTypes"] = object_types
    payload = json.dumps({"query": GRAPHQL_QUERY, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        JUSTWATCH_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "r2d2-catalog-proto/1.0"},
        method="POST",
    )
    last_err: Exception | None = None
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read(4 * 1024 * 1024)
            doc = json.loads(body.decode("utf-8"))
            if "errors" in doc and doc["errors"]:
                raise RuntimeError(f"GraphQL error: {doc['errors'][0]}")
            return doc["data"]["popularTitles"]
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, RuntimeError) as exc:
            last_err = exc
            time.sleep(min(5.0, 0.75 * attempt))
    raise RuntimeError(f"JustWatch fetch failed after retries: {last_err}")


def fetch_catalog(*, max_titles: int = 4000, page_size: int = 100, delay_s: float = 0.05) -> tuple[list[dict], int]:
    """Fetch top US Netflix shows & movies ordered by JustWatch popularity."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    total_count = 0
    per_bucket = max(100, max_titles // 2)

    for bucket in (["SHOW"], ["MOVIE"]):
        after: str | None = None
        bucket_rank = 0
        bucket_total = 0
        while bucket_rank < per_bucket and len(rows) < max_titles:
            want = min(page_size, per_bucket - bucket_rank, max_titles - len(rows))
            page = _graphql_page(after, want, object_types=bucket)
            bucket_total = int(page.get("totalCount") or bucket_total)
            edges = page.get("edges") or []
            if not edges:
                break
            for edge in edges:
                node = (edge or {}).get("node") or {}
                content = node.get("content") or {}
                title = content.get("title")
                if not isinstance(title, str) or not title.strip() or len(title) > 200:
                    continue
                title = title.strip()
                nid = extract_netflix_id(node.get("offers") or [])
                if not nid or nid in seen_ids:
                    continue
                seen_ids.add(nid)
                bucket_rank += 1
                norm = normalize_title(title)
                if not norm:
                    continue
                year = content.get("originalReleaseYear")
                year_int = int(year) if isinstance(year, int) and 1888 <= year <= 2100 else None
                mtype = node.get("objectType")
                mtype = mtype if mtype in ("SHOW", "MOVIE") else bucket[0]
                rows.append({
                    "netflix_id": nid,
                    "title": title,
                    "norm_title": norm,
                    "bare_title": strip_leading_article(norm),
                    "year": year_int,
                    "media_type": mtype,
                    "popularity_rank": bucket_rank,
                })
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            after = str(page_info["endCursor"])
            if delay_s > 0:
                time.sleep(delay_s)
        total_count += bucket_total

    rows.sort(key=lambda r: (r["popularity_rank"], 0 if r["media_type"] == "SHOW" else 1, r["netflix_id"]))
    for i, r in enumerate(rows, start=1):
        r["popularity_rank"] = i
    return rows, total_count


def _previous_count(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'indexed_count'").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def write_catalog_db(
    db_path: Path,
    rows: list[dict],
    *,
    total_available: int,
    force: bool = False,
) -> dict:
    """Atomically build and replace the SQLite catalog DB with 0600 creation perms."""
    db_path = Path(db_path)
    if not rows:
        raise ValueError("Refusing to write empty catalog")

    prev_count = _previous_count(db_path)
    if not force and prev_count > 0 and len(rows) < int(prev_count * 0.5):
        raise ValueError(
            f"Sanity check refused swap: new count {len(rows)} < 50% of previous {prev_count}"
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(db_path.parent, 0o700)

    tmp_path = db_path.with_name(f".{db_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    # Create the temp file strictly with mode 0600 before SQLite opens it (no chmod race).
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)

    alias_rows: list[tuple[str, str]] = []
    for r in rows:
        for alias in generate_aliases(r["title"], r["norm_title"], r["bare_title"]):
            alias_rows.append((alias, r["netflix_id"]))

    synced_at = int(time.time())
    old_umask = os.umask(0o077)
    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA page_size=4096")
        conn.executescript("""
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE titles (
                netflix_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                norm_title TEXT NOT NULL,
                bare_title TEXT NOT NULL,
                year INTEGER,
                media_type TEXT NOT NULL,
                popularity_rank INTEGER NOT NULL
            );
            CREATE TABLE title_aliases (
                alias TEXT NOT NULL,
                netflix_id TEXT NOT NULL,
                PRIMARY KEY (alias, netflix_id)
            );
            CREATE INDEX idx_titles_norm ON titles(norm_title, popularity_rank);
            CREATE INDEX idx_titles_bare ON titles(bare_title, popularity_rank);
            CREATE INDEX idx_aliases_alias ON title_aliases(alias);
            CREATE VIRTUAL TABLE titles_fts USING fts5(
                norm_title,
                title,
                content='titles',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            );
        """)
        conn.executemany(
            """INSERT INTO titles(netflix_id, title, norm_title, bare_title, year, media_type, popularity_rank)
               VALUES (:netflix_id, :title, :norm_title, :bare_title, :year, :media_type, :popularity_rank)""",
            rows,
        )
        if alias_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO title_aliases(alias, netflix_id) VALUES (?, ?)",
                alias_rows,
            )
        conn.execute("INSERT INTO titles_fts(titles_fts) VALUES ('rebuild')")
        conn.execute("INSERT INTO titles_fts(titles_fts) VALUES ('optimize')")
        meta = {
            "schema_version": "2",
            "provider": "netflix",
            "country": "US",
            "synced_at": str(synced_at),
            "indexed_count": str(len(rows)),
            "alias_count": str(len(alias_rows)),
            "total_available": str(total_available),
        }
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", list(meta.items()))
        conn.commit()
        conn.execute("VACUUM")
        conn.close()
    except BaseException:
        conn.close()
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    finally:
        os.umask(old_umask)

    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, db_path)
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    return {
        "db_path": str(db_path),
        "indexed_count": len(rows),
        "alias_count": len(alias_rows),
        "total_available": total_available,
        "bytes": db_path.stat().st_size,
        "sha256": digest,
        "synced_at": synced_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync US Netflix catalog into local SQLite index.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Output SQLite DB path")
    parser.add_argument("--max-titles", type=int, default=4000, help="Max titles to index (default 4000)")
    parser.add_argument("--page-size", type=int, default=100, help="GraphQL page size (default 100)")
    parser.add_argument("--force", action="store_true", help="Bypass 50% drop sanity check")
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    rows, total = fetch_catalog(max_titles=args.max_titles, page_size=args.page_size)
    if not rows:
        print("ERROR: fetched 0 Netflix titles; refusing to overwrite DB", file=sys.stderr)
        return 1
    summary = write_catalog_db(args.db, rows, total_available=total, force=args.force)
    summary["elapsed_s"] = round(time.perf_counter() - t0, 2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
