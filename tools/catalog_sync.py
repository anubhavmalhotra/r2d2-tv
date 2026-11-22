"""Unified offline catalog syncer for R2D2 P5 (Netflix, Apple TV+, Tubi, Apple Music).

Pure Python standard library. Produces compact per-provider SQLite DBs in
~/.local/share/r2d2-catalog/{provider}.db with strictly regex-validated IDs
(never raw third-party URLs) and precomputed O(1) title aliases.

Security invariants:
- Parent directory enforced to mode 0700.
- Temporary database file created exclusively with mode 0600 before SQLite opens it
  (never world-readable at any point) and atomically swapped via os.replace.
- Sanity check refuses to overwrite an existing DB if the new title count drops
  below 50% of the previous indexed_count (unless --force is passed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CATALOG_DIR = Path.home() / ".local" / "share" / "r2d2-catalog"
ALL_PROVIDERS = ("netflix", "apple_tv", "tubi", "apple_music")

JUSTWATCH_URL = "https://apis.justwatch.com/graphql"
USER_AGENT = "r2d2-catalog-sync/2.0"

NETFLIX_ID_URL_RE = re.compile(r"^https://www\.netflix\.com/(?:title|watch)/([0-9]{6,10})(?:[/?#].*)?$")
APPLE_TV_MOVIE_URL_RE = re.compile(
    r"https://tv\.apple\.com/us/movie/([a-z0-9-]{1,80})/(umc\.cmc\.[a-z0-9]{16,32})(?:[/?#].*)?$"
)
APPLE_TV_SHOW_URL_RE = re.compile(
    r"https://tv\.apple\.com/us/show/([a-z0-9-]{1,80})/(umc\.cmc\.[a-z0-9]{16,32})(?:[/?#].*)?$"
)
APPLE_TV_SHOW_ID_QS_RE = re.compile(r"(?:[?&])showId=(umc\.cmc\.[a-z0-9]{16,32})(?:&|$)")
APPLE_TV_ID_STRICT = re.compile(r"^umc\.cmc\.[a-z0-9]{16,32}$")
APPLE_TV_SLUG_STRICT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")

TUBI_MOVIE_URL_RE = re.compile(r"^https://tubitv\.com/movies/([0-9]{4,12})(?:[/?#].*)?$")
TUBI_SHOW_URL_RE = re.compile(r"^https://tubitv\.com/series/([0-9]{4,12})(?:[/?#].*)?$")

APPLE_MUSIC_SONG_URL_RE = re.compile(
    r"^https://music\.apple\.com/us/album/(?:[^/?#]+/)?([0-9]{4,14})\?i=([0-9]{4,14})(?:&.*)?$"
)
APPLE_MUSIC_ALBUM_URL_RE = re.compile(
    r"^https://music\.apple\.com/us/album/(?:[^/?#]+/)?([0-9]{4,14})(?:[?#].*)?$"
)
APPLE_MUSIC_ARTIST_URL_RE = re.compile(
    r"^https://music\.apple\.com/us/artist/(?:[^/?#]+/)?([0-9]{4,14})(?:[?#].*)?$"
)
NUMERIC_MUSIC_ID_RE = re.compile(r"^[0-9]{4,14}$")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_SPACES_RE = re.compile(r"\s+")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")
_SUBTITLE_SPLIT_RE = re.compile(r"\s*(?::|\s[-–—]\s)\s*")
_HYPHEN_RE = re.compile(r"(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])")
_MUSIC_PAREN_RE = re.compile(r"\s*[\(\[][^\)\]]{1,80}[\)\]]\s*")

_NUM_TO_WORD = {
    "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
}
_WORD_TO_NUM = {v: k for k, v in _NUM_TO_WORD.items()}
_ROMAN_TO_NUM = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_NUM_TO_ROMAN = {v: k for k, v in _ROMAN_TO_NUM.items()}

JUSTWATCH_QUERY = """
query CatalogUS($after: String, $first: Int!, $filter: TitleFilter) {
  popularTitles(
    country: US
    first: $first
    after: $after
    filter: $filter
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
        offers(country: US, platform: WEB) {
          monetizationType
          standardWebURL
          package {
            shortName
            technicalName
          }
        }
      }
    }
  }
}
"""

# iTunes US RSS genre IDs for broad Apple Music chart coverage (zero API keys)
ITUNES_GENRES: tuple[int | None, ...] = (
    None,  # Overall US Top Charts
    14,    # Pop
    18,    # Hip-Hop/Rap
    21,    # Rock
    20,    # Alternative
    15,    # R&B/Soul
    6,     # Country
    7,     # Electronic
    17,    # Dance
    11,    # Jazz
    5,     # Classical
    12,    # Latin
    16,    # Soundtrack
    1153,  # Metal
    10,    # Singer/Songwriter
    24,    # Reggae
    2,     # Blues
    19,    # World
)

# Iconic / evergreen Apple Music artist IDs so classic voice queries always resolve offline
CLASSIC_ARTIST_IDS: tuple[str, ...] = (
    "32940",       # Michael Jackson
    "136975",      # The Beatles
    "3296287",     # Queen
    "487143",      # Pink Floyd
    "159260351",   # Taylor Swift
    "479756766",   # The Weeknd
    "271256",      # Drake
    "368183298",   # Kendrick Lamar
    "1419227",     # Beyonce
    "262836961",   # Adele
    "471744",      # Coldplay
    "657515",      # Radiohead
    "5468295",     # Daft Punk
    "158038",      # Fleetwood Mac
    "112018",      # Nirvana
    "505264",      # Bob Marley & The Wailers
    "44984",       # Miles Davis
    "1065981054",  # Billie Eilish
    "278873078",   # Bruno Mars
    "183313439",   # Ed Sheeran
    "63346553",    # Rihanna
    "111051",      # Eminem
    "2715720",     # Ye / Kanye West
    "442122051",   # Frank Ocean
    "605800394",   # SZA
    "1126808565",  # Bad Bunny
    "829142092",   # Morgan Wallen
    "966309175",   # Post Malone
    "549236696",   # Travis Scott
    "1031397873",  # Dua Lipa
    "464296584",   # Lana Del Rey
    "31617",       # Stevie Wonder
    "155814",      # Prince
    "551695",      # David Bowie
    "994656",      # Led Zeppelin
    "54657",       # Elton John
    "41812",       # Marvin Gaye
    "13108398",    # Amy Winehouse
    "62820413",    # Arctic Monkeys
    "390647681",   # Sabrina Carpenter
    "1479439873",  # Chappell Roan
    "979458609",   # Olivia Rodrigo
    "3996865",     # Metallica
    "5040714",     # AC/DC
    "149020",      # Hans Zimmer
    "156301",      # John Williams
    "3249567",     # A.R. Rahman
    "178834",      # Bruce Springsteen
    "485953",      # Red Hot Chili Peppers
    "17904",       # Linkin Park
    "283051",      # Eagles
    "199043",      # Mariah Carey
    "156026",      # Whitney Houston
    "518462",      # Louis Armstrong
    "52172",       # Frank Sinatra
    "35315",       # Johnny Cash
    "148662",      # Dolly Parton
    "217005",      # Justin Bieber
    "412778295",   # Ariana Grande
    "1171421960",  # Tyler, The Creator
    "883131348",   # Tame Impala
    "98556",       # Gorillaz
    "617154241",   # Hozier
    "5723",        # Madonna
    "20044",       # Madonna / U2 fallback
)


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


def make_ascii_slug(text: str, *, fallback: str = "title") -> str:
    norm = normalize_title(text)
    slug = norm.replace(" ", "-")[:80].strip("-")
    if slug and APPLE_TV_SLUG_STRICT.fullmatch(slug):
        return slug
    return fallback


def generate_aliases(
    raw_title: str,
    norm_title: str,
    bare_title: str,
    *,
    subtitle: str = "",
    provider: str = "netflix",
) -> list[str]:
    """Generate deterministic O(1) aliases for a catalog item."""
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

    # 2. Music parenthetical stripping: "Blinding Lights (Remix)" / "Song (feat. X)" -> "blinding lights"
    if provider == "apple_music" and ("(" in raw_title or "[" in raw_title):
        stripped = _MUSIC_PAREN_RE.sub(" ", raw_title).strip()
        s_norm = normalize_title(stripped)
        if len(s_norm) >= 3:
            out.add(s_norm)
            s_bare = strip_leading_article(s_norm)
            if len(s_bare) >= 3:
                out.add(s_bare)

    # 3. Compact hyphen-joined form ("K-Pop" -> "kpop", "Spider-Man" -> "spiderman")
    if "-" in raw_title:
        joined = _HYPHEN_RE.sub("", raw_title)
        j_norm = normalize_title(joined)
        if j_norm and j_norm not in base_forms:
            out.add(j_norm)
            j_bare = strip_leading_article(j_norm)
            if j_bare:
                out.add(j_bare)

    # 4. Digit / word / Roman numeral token substitutions
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

    # 5. Music "title by artist" & "artist title" aliases for O(1) disambiguated voice queries
    if provider == "apple_music" and subtitle:
        sub_norm = normalize_title(subtitle)
        if sub_norm and sub_norm != norm_title:
            title_forms = {norm_title}
            if bare_title and bare_title != norm_title:
                title_forms.add(bare_title)
            if "(" in raw_title or "[" in raw_title:
                s_norm = normalize_title(_MUSIC_PAREN_RE.sub(" ", raw_title).strip())
                if len(s_norm) >= 3:
                    title_forms.add(s_norm)
            for tf in title_forms:
                if tf:
                    out.add(f"{tf} by {sub_norm}")
                    out.add(f"{sub_norm} {tf}")

    out.discard("")
    out -= base_forms
    return sorted(out)


def _http_get_json(url: str, *, timeout_s: float = 15.0, retries: int = 3, max_bytes: int = 8 * 1024 * 1024) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read(max_bytes)
            return json.loads(body.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(min(4.0, 0.6 * attempt))
    raise RuntimeError(f"GET {url} failed: {last_err}")


def _justwatch_page(
    *,
    after: str | None,
    page_size: int,
    title_filter: dict,
    offer_filter: dict | None = None,
    timeout_s: float = 15.0,
) -> dict:
    payload = json.dumps({
        "query": JUSTWATCH_QUERY,
        "variables": {
            "after": after,
            "first": page_size,
            "filter": title_filter,
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        JUSTWATCH_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    last_err: Exception | None = None
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read(6 * 1024 * 1024)
            doc = json.loads(body.decode("utf-8"))
            if "errors" in doc and doc["errors"]:
                raise RuntimeError(f"GraphQL error: {doc['errors'][0]}")
            return doc["data"]["popularTitles"]
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, RuntimeError) as exc:
            last_err = exc
            time.sleep(min(5.0, 0.75 * attempt))
    raise RuntimeError(f"JustWatch fetch failed after retries: {last_err}")


# ---------------------------------------------------------------------------
# Provider extractors & fetchers
# ---------------------------------------------------------------------------

def extract_netflix_id(offers: list[dict]) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for offer in offers or ():
        if not isinstance(offer, dict) or offer.get("monetizationType") != "FLATRATE":
            continue
        pkg = offer.get("package") or {}
        tech = pkg.get("technicalName") if isinstance(pkg, dict) else None
        if tech not in ("netflix", "netflixbasicwithads"):
            continue
        url = offer.get("standardWebURL")
        if not isinstance(url, str) or len(url) > 512:
            continue
        match = NETFLIX_ID_URL_RE.fullmatch(url)
        if not match:
            continue
        nid = match.group(1)
        candidates.append((0 if tech == "netflix" else 1, int(nid), nid))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def extract_apple_tv_item(offers: list[dict], media_type: str, title: str) -> tuple[str, str] | None:
    """Extract (umc.cmc.* id, ascii slug) for Apple TV+ (atp FLATRATE only)."""
    for offer in offers or ():
        if not isinstance(offer, dict) or offer.get("monetizationType") != "FLATRATE":
            continue
        pkg = offer.get("package") or {}
        if not isinstance(pkg, dict) or pkg.get("shortName") != "atp":
            continue
        url = offer.get("standardWebURL")
        if not isinstance(url, str) or len(url) > 1024:
            continue
        if media_type == "movie":
            m = APPLE_TV_MOVIE_URL_RE.match(url)
            if m:
                slug, cid = m.group(1), m.group(2)
                if APPLE_TV_ID_STRICT.fullmatch(cid):
                    clean_slug = slug if APPLE_TV_SLUG_STRICT.fullmatch(slug) else make_ascii_slug(title)
                    return cid, clean_slug
        else:
            m_show = APPLE_TV_SHOW_URL_RE.match(url)
            if m_show:
                slug, cid = m_show.group(1), m_show.group(2)
                if APPLE_TV_ID_STRICT.fullmatch(cid):
                    clean_slug = slug if APPLE_TV_SLUG_STRICT.fullmatch(slug) else make_ascii_slug(title)
                    return cid, clean_slug
            m_qs = APPLE_TV_SHOW_ID_QS_RE.search(url)
            if m_qs:
                cid = m_qs.group(1)
                if APPLE_TV_ID_STRICT.fullmatch(cid):
                    return cid, make_ascii_slug(title)
    return None


def extract_tubi_id(offers: list[dict], media_type: str) -> str | None:
    """Extract validated numeric Tubi movie/series ID from ADS/FREE offers."""
    pat = TUBI_MOVIE_URL_RE if media_type == "movie" else TUBI_SHOW_URL_RE
    for offer in offers or ():
        if not isinstance(offer, dict) or offer.get("monetizationType") not in ("ADS", "FREE"):
            continue
        pkg = offer.get("package") or {}
        if not isinstance(pkg, dict) or pkg.get("shortName") != "tbv":
            continue
        url = offer.get("standardWebURL")
        if not isinstance(url, str) or len(url) > 512:
            continue
        m = pat.fullmatch(url)
        if m:
            return m.group(1)
    return None


def fetch_netflix_catalog(*, max_titles: int = 4000, page_size: int = 100, delay_s: float = 0.05) -> tuple[list[dict], int]:
    rows: list[dict] = []
    seen_ids: set[str] = set()
    total_count = 0
    per_bucket = max(100, max_titles // 2)

    for obj_type in ("SHOW", "MOVIE"):
        after: str | None = None
        bucket_rank = 0
        bucket_total = 0
        while bucket_rank < per_bucket and len(rows) < max_titles:
            want = min(page_size, per_bucket - bucket_rank, max_titles - len(rows))
            page = _justwatch_page(
                after=after,
                page_size=want,
                title_filter={"packages": ["nfx"], "monetizationTypes": ["FLATRATE"], "objectTypes": [obj_type]},
                offer_filter={"packages": ["nfx"]},
            )
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
                norm = normalize_title(title)
                if not norm:
                    continue
                seen_ids.add(nid)
                bucket_rank += 1
                year = content.get("originalReleaseYear")
                year_int = int(year) if isinstance(year, int) and 1888 <= year <= 2100 else None
                mtype = "movie" if node.get("objectType") == "MOVIE" else "show"
                rows.append({
                    "provider_id": nid,
                    "title": title,
                    "norm_title": norm,
                    "bare_title": strip_leading_article(norm),
                    "year": year_int,
                    "media_type": mtype,
                    "popularity_rank": bucket_rank,
                    "subtitle": "",
                    "slug": "",
                })
            pi = page.get("pageInfo") or {}
            if not pi.get("hasNextPage") or not pi.get("endCursor"):
                break
            after = str(pi["endCursor"])
            if delay_s > 0:
                time.sleep(delay_s)
        total_count += bucket_total

    rows.sort(key=lambda r: (r["popularity_rank"], 0 if r["media_type"] == "show" else 1, r["provider_id"]))
    for i, r in enumerate(rows, start=1):
        r["popularity_rank"] = i
    return rows, total_count


def fetch_apple_tv_catalog(*, max_titles: int = 2000, page_size: int = 100, delay_s: float = 0.05) -> tuple[list[dict], int]:
    """Fetch US Apple TV+ subscription titles ONLY (package 'atp', FLATRATE)."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    total_count = 0

    for obj_type in ("SHOW", "MOVIE"):
        after: str | None = None
        bucket_rank = 0
        bucket_total = 0
        mtype = "movie" if obj_type == "MOVIE" else "show"
        while len(rows) < max_titles:
            want = min(page_size, max_titles - len(rows))
            page = _justwatch_page(
                after=after,
                page_size=want,
                title_filter={"packages": ["atp"], "monetizationTypes": ["FLATRATE"], "objectTypes": [obj_type]},
                offer_filter={"packages": ["atp"]},
            )
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
                extracted = extract_apple_tv_item(node.get("offers") or [], mtype, title)
                if not extracted:
                    continue
                cid, slug = extracted
                if cid in seen_ids:
                    continue
                norm = normalize_title(title)
                if not norm:
                    continue
                seen_ids.add(cid)
                bucket_rank += 1
                year = content.get("originalReleaseYear")
                year_int = int(year) if isinstance(year, int) and 1888 <= year <= 2100 else None
                rows.append({
                    "provider_id": cid,
                    "title": title,
                    "norm_title": norm,
                    "bare_title": strip_leading_article(norm),
                    "year": year_int,
                    "media_type": mtype,
                    "popularity_rank": bucket_rank,
                    "subtitle": "",
                    "slug": slug,
                })
            pi = page.get("pageInfo") or {}
            if not pi.get("hasNextPage") or not pi.get("endCursor"):
                break
            after = str(pi["endCursor"])
            if delay_s > 0:
                time.sleep(delay_s)
        total_count += bucket_total

    rows.sort(key=lambda r: (r["popularity_rank"], 0 if r["media_type"] == "show" else 1, r["provider_id"]))
    for i, r in enumerate(rows, start=1):
        r["popularity_rank"] = i
    return rows, total_count


def fetch_tubi_catalog(*, max_titles: int = 12000, page_size: int = 100, delay_s: float = 0.04) -> tuple[list[dict], int]:
    """Fetch top US Tubi (ADS/FREE) shows & movies partitioned across release-year buckets."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    total_count = 0

    # Partition across object types + release-year ranges so we smoothly beat the 2,000 cursor cap
    # while prioritizing the most popular Tubi catalog titles across eras.
    buckets: list[tuple[str, dict | None, int]] = [
        ("SHOW", {"min": 2018, "max": 2027}, 1500),
        ("SHOW", {"min": 2005, "max": 2017}, 1200),
        ("SHOW", {"min": 1950, "max": 2004}, 1000),
        ("MOVIE", {"min": 2022, "max": 2027}, 1800),
        ("MOVIE", {"min": 2016, "max": 2021}, 1800),
        ("MOVIE", {"min": 2008, "max": 2015}, 1800),
        ("MOVIE", {"min": 1995, "max": 2007}, 1600),
        ("MOVIE", {"min": 1900, "max": 1994}, 1300),
    ]

    for obj_type, yr_range, cap in buckets:
        if len(rows) >= max_titles:
            break
        mtype = "movie" if obj_type == "MOVIE" else "show"
        after: str | None = None
        bucket_rank = 0
        bucket_total = 0
        tfilter: dict = {
            "packages": ["tbv"],
            "monetizationTypes": ["ADS", "FREE"],
            "objectTypes": [obj_type],
        }
        if yr_range:
            tfilter["releaseYear"] = yr_range
        while bucket_rank < cap and len(rows) < max_titles:
            want = min(page_size, cap - bucket_rank, max_titles - len(rows))
            page = _justwatch_page(
                after=after,
                page_size=want,
                title_filter=tfilter,
                offer_filter={"packages": ["tbv"]},
            )
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
                tid = extract_tubi_id(node.get("offers") or [], mtype)
                if not tid or tid in seen_ids:
                    continue
                norm = normalize_title(title)
                if not norm:
                    continue
                seen_ids.add(tid)
                bucket_rank += 1
                year = content.get("originalReleaseYear")
                year_int = int(year) if isinstance(year, int) and 1888 <= year <= 2100 else None
                rows.append({
                    "provider_id": tid,
                    "title": title,
                    "norm_title": norm,
                    "bare_title": strip_leading_article(norm),
                    "year": year_int,
                    "media_type": mtype,
                    "popularity_rank": bucket_rank,
                    "subtitle": "",
                    "slug": "",
                })
            pi = page.get("pageInfo") or {}
            if not pi.get("hasNextPage") or not pi.get("endCursor"):
                break
            after = str(pi["endCursor"])
            if delay_s > 0:
                time.sleep(delay_s)
        total_count += bucket_total

    rows.sort(key=lambda r: (r["popularity_rank"], 0 if r["media_type"] == "movie" else 1, r["provider_id"]))
    for i, r in enumerate(rows, start=1):
        r["popularity_rank"] = i
    return rows, total_count


def fetch_apple_music_catalog(
    *,
    max_items: int = 12000,
    expand_artists: int = 180,
    delay_s: float = 0.08,
) -> tuple[list[dict], int]:
    """Fetch a rich US Apple Music catalog (songs, albums, artists) from Apple's public APIs."""
    by_id: dict[str, dict] = {}
    artist_rank_order: list[str] = list(CLASSIC_ARTIST_IDS)
    seen_artist_ids: set[str] = set(CLASSIC_ARTIST_IDS)
    rank_counter = 0

    def _add(pid: str, title: str, mtype: str, *, subtitle: str = "", year: int | None = None, rank_hint: int | None = None) -> None:
        nonlocal rank_counter
        if not title or len(title) > 200:
            return
        title = title.strip()
        norm = normalize_title(title)
        if not norm:
            return
        rank_counter += 1
        eff_rank = rank_hint if rank_hint is not None else rank_counter
        cur = by_id.get(pid)
        if cur is not None:
            if eff_rank < cur["popularity_rank"]:
                cur["popularity_rank"] = eff_rank
            return
        by_id[pid] = {
            "provider_id": pid,
            "title": title,
            "norm_title": norm,
            "bare_title": strip_leading_article(norm),
            "year": year,
            "media_type": mtype,
            "popularity_rank": eff_rank,
            "subtitle": subtitle.strip()[:120] if isinstance(subtitle, str) else "",
            "slug": "",
        }

    def _note_artist(aid: str, name: str, rank_hint: int) -> None:
        if not NUMERIC_MUSIC_ID_RE.fullmatch(aid):
            return
        _add(f"artist:{aid}", name, "artist", rank_hint=rank_hint)
        if aid not in seen_artist_ids:
            seen_artist_ids.add(aid)
            artist_rank_order.append(aid)

    # 1. Apple Marketing Tools US Most-Played Top 100 Songs & Albums (best-effort)
    for kind, url in (
        ("song", "https://rss.marketingtools.apple.com/api/v2/us/music/most-played/100/songs.json"),
        ("album", "https://rss.marketingtools.apple.com/api/v2/us/music/most-played/100/albums.json"),
    ):
        try:
            doc = _http_get_json(url, timeout_s=8.0, retries=2)
            for idx, item in enumerate((doc.get("feed") or {}).get("results") or (), start=1):
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or ""
                artist = item.get("artistName") or ""
                aid = str(item.get("artistId") or "")
                item_url = item.get("url") or ""
                year = None
                rdate = item.get("releaseDate")
                if isinstance(rdate, str) and len(rdate) >= 4 and rdate[:4].isdigit():
                    year = int(rdate[:4])
                if aid and artist:
                    _note_artist(aid, artist, idx)
                if kind == "song":
                    m = APPLE_MUSIC_SONG_URL_RE.fullmatch(item_url)
                    if m:
                        _add(f"song:{m.group(1)}:{m.group(2)}", name, "song", subtitle=artist, year=year, rank_hint=idx)
                else:
                    m = APPLE_MUSIC_ALBUM_URL_RE.fullmatch(item_url)
                    if m:
                        _add(f"album:{m.group(1)}", name, "album", subtitle=artist, year=year, rank_hint=idx + 10)
        except Exception:
            pass  # marketing RSS occasionally times out; iTunes RSS below is primary

    # 2. iTunes US RSS Top Songs & Top Albums across 18 genres
    for g_idx, genre in enumerate(ITUNES_GENRES):
        g_seg = f"/genre={genre}" if genre is not None else ""
        base_rank = 100 + g_idx * 40
        for feed_type in ("topsongs", "topalbums"):
            url = f"https://itunes.apple.com/us/rss/{feed_type}/limit=200{g_seg}/json"
            try:
                doc = _http_get_json(url, timeout_s=12.0, retries=2)
            except Exception:
                continue
            entries = (doc.get("feed") or {}).get("entry") or []
            if isinstance(entries, dict):
                entries = [entries]
            for pos, e in enumerate(entries, start=1):
                if not isinstance(e, dict):
                    continue
                name = (e.get("im:name") or {}).get("label") or ""
                artist_obj = e.get("im:artist") or {}
                artist = artist_obj.get("label") or ""
                artist_href = (artist_obj.get("attributes") or {}).get("href") or ""
                m_art = APPLE_MUSIC_ARTIST_URL_RE.fullmatch(artist_href)
                if m_art and artist:
                    _note_artist(m_art.group(1), artist, base_rank + pos)

                rdate = ((e.get("im:releaseDate") or {}).get("label") or "")[:4]
                year = int(rdate) if rdate.isdigit() else None
                item_url = (e.get("id") or {}).get("label") or ""
                if feed_type == "topsongs":
                    m_song = APPLE_MUSIC_SONG_URL_RE.fullmatch(item_url)
                    if m_song:
                        _add(
                            f"song:{m_song.group(1)}:{m_song.group(2)}",
                            name,
                            "song",
                            subtitle=artist,
                            year=year,
                            rank_hint=base_rank + pos,
                        )
                else:
                    m_alb = APPLE_MUSIC_ALBUM_URL_RE.fullmatch(item_url)
                    if m_alb:
                        _add(
                            f"album:{m_alb.group(1)}",
                            name,
                            "album",
                            subtitle=artist,
                            year=year,
                            rank_hint=base_rank + pos + 5,
                        )
            if delay_s > 0:
                time.sleep(delay_s)

    # 3. Expand top classic & chart artists via iTunes Lookup API (top songs & albums per artist)
    # Batch 10 artist IDs per request for fast, polite expansion.
    target_artists = artist_rank_order[:expand_artists]
    batch_size = 10
    for start in range(0, len(target_artists), batch_size):
        if len(by_id) >= max_items:
            break
        batch = target_artists[start : start + batch_size]
        ids_csv = ",".join(batch)
        for entity, lim in (("song", 12), ("album", 6)):
            url = f"https://itunes.apple.com/lookup?id={ids_csv}&entity={entity}&country=US&limit={lim}"
            try:
                doc = _http_get_json(url, timeout_s=15.0, retries=2)
            except Exception:
                continue
            for pos, item in enumerate(doc.get("results") or (), start=1):
                if not isinstance(item, dict):
                    continue
                wtype = item.get("wrapperType")
                aid = str(item.get("artistId") or "")
                aname = item.get("artistName") or ""
                rdate = str(item.get("releaseDate") or "")[:4]
                year = int(rdate) if rdate.isdigit() else None
                if wtype == "artist" and NUMERIC_MUSIC_ID_RE.fullmatch(aid) and aname:
                    _add(f"artist:{aid}", aname, "artist", rank_hint=500 + start + pos)
                elif wtype == "track" and item.get("kind") == "song":
                    cid = str(item.get("collectionId") or "")
                    tid = str(item.get("trackId") or "")
                    tname = item.get("trackName") or ""
                    if NUMERIC_MUSIC_ID_RE.fullmatch(cid) and NUMERIC_MUSIC_ID_RE.fullmatch(tid) and tname:
                        _add(
                            f"song:{cid}:{tid}",
                            tname,
                            "song",
                            subtitle=aname,
                            year=year,
                            rank_hint=1500 + start * 10 + pos,
                        )
                elif wtype == "collection":
                    cid = str(item.get("collectionId") or "")
                    cname = item.get("collectionName") or ""
                    if NUMERIC_MUSIC_ID_RE.fullmatch(cid) and cname:
                        _add(
                            f"album:{cid}",
                            cname,
                            "album",
                            subtitle=aname,
                            year=year,
                            rank_hint=1600 + start * 10 + pos,
                        )
            if delay_s > 0:
                time.sleep(delay_s)

    rows = list(by_id.values())
    rows.sort(key=lambda r: (r["popularity_rank"], r["provider_id"]))
    rows = rows[:max_items]
    for i, r in enumerate(rows, start=1):
        r["popularity_rank"] = i
    return rows, len(by_id)


# ---------------------------------------------------------------------------
# Atomic 0600 SQLite writer
# ---------------------------------------------------------------------------

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
    provider: str,
    total_available: int,
    force: bool = False,
) -> dict:
    """Atomically build and replace a provider's SQLite catalog DB with 0600 creation perms."""
    db_path = Path(db_path)
    if not rows:
        raise ValueError(f"Refusing to write empty {provider} catalog")

    prev_count = _previous_count(db_path)
    if not force and prev_count > 0 and len(rows) < int(prev_count * 0.5):
        raise ValueError(
            f"Sanity check refused {provider} swap: new count {len(rows)} < 50% of previous {prev_count}"
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(db_path.parent, 0o700)

    tmp_path = db_path.with_name(f".{db_path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)

    alias_rows: list[tuple[str, str]] = []
    for r in rows:
        pid = r["provider_id"]
        for alias in generate_aliases(
            r["title"],
            r["norm_title"],
            r["bare_title"],
            subtitle=r.get("subtitle", ""),
            provider=provider,
        ):
            alias_rows.append((alias, pid))

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
                provider_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                norm_title TEXT NOT NULL,
                bare_title TEXT NOT NULL,
                year INTEGER,
                media_type TEXT NOT NULL,
                popularity_rank INTEGER NOT NULL,
                subtitle TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE title_aliases (
                alias TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                PRIMARY KEY (alias, provider_id)
            );
            CREATE INDEX idx_titles_norm ON titles(norm_title, popularity_rank);
            CREATE INDEX idx_titles_bare ON titles(bare_title, popularity_rank);
            CREATE INDEX idx_aliases_alias ON title_aliases(alias);
        """)
        conn.executemany(
            """INSERT INTO titles(provider_id, title, norm_title, bare_title, year, media_type, popularity_rank, subtitle, slug)
               VALUES (:provider_id, :title, :norm_title, :bare_title, :year, :media_type, :popularity_rank, :subtitle, :slug)""",
            rows,
        )
        if alias_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO title_aliases(alias, provider_id) VALUES (?, ?)",
                alias_rows,
            )
        meta = {
            "schema_version": "3",
            "provider": provider,
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
        "provider": provider,
        "db_path": str(db_path),
        "indexed_count": len(rows),
        "alias_count": len(alias_rows),
        "total_available": total_available,
        "bytes": db_path.stat().st_size,
        "sha256": digest,
        "synced_at": synced_at,
    }


def sync_provider(
    provider: str,
    catalog_dir: Path,
    *,
    force: bool = False,
    tubi_max_titles: int = 12000,
    apple_music_max_items: int = 12000,
) -> dict:
    t0 = time.perf_counter()
    if provider == "netflix":
        rows, total = fetch_netflix_catalog()
    elif provider == "apple_tv":
        rows, total = fetch_apple_tv_catalog()
    elif provider == "tubi":
        rows, total = fetch_tubi_catalog(max_titles=tubi_max_titles)
    elif provider == "apple_music":
        rows, total = fetch_apple_music_catalog(max_items=apple_music_max_items)
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    summary = write_catalog_db(
        Path(catalog_dir) / f"{provider}.db",
        rows,
        provider=provider,
        total_available=total,
        force=force,
    )
    summary["elapsed_s"] = round(time.perf_counter() - t0, 2)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync offline R2D2 P5 catalogs into local 0600 SQLite DBs.")
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=ALL_PROVIDERS,
        default=list(ALL_PROVIDERS),
        help="Providers to sync (default: all four)",
    )
    parser.add_argument("--dir", type=Path, default=DEFAULT_CATALOG_DIR, help="Catalog directory (mode 0700)")
    parser.add_argument("--tubi-max-titles", type=int, default=12000, help="Max Tubi titles (default 12000)")
    parser.add_argument("--apple-music-max-items", type=int, default=12000, help="Max Apple Music items (default 12000)")
    parser.add_argument("--force", action="store_true", help="Bypass 50% count-drop sanity check")
    args = parser.parse_args(argv)

    results = []
    failures = []
    for prov in args.providers:
        try:
            res = sync_provider(
                prov,
                args.dir,
                force=args.force,
                tubi_max_titles=args.tubi_max_titles,
                apple_music_max_items=args.apple_music_max_items,
            )
            results.append(res)
        except Exception as exc:
            failures.append({"provider": prov, "error": str(exc)})

    print(json.dumps({"synced": results, "failed": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
