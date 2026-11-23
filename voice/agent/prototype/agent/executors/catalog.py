"""Offline (<5 ms p99, <=75 ms SLA) multi-provider catalog index & TVGateway deep-link builders.

Supports Netflix, Apple TV+ (subscription), Tubi, and Apple Music with identical
security and latency invariants:
- 100% offline hot path (zero network I/O, zero query logging, no raw query text in errors).
- Lock-free immutable _Snapshot swap: concurrent resolve() calls never block on DB reload;
  stat checks run at most once per reload_interval_s and rebuilds run on a background thread
  with periodic GIL yields. Last good snapshot stays live if a rebuild fails, and reload
  errors are recorded in health() (never silently dropped).
- Private-file checks ON by default: absolute path, no symlinks, parent dir 0700 owned by
  current UID, DB regular file 0600 owned by current UID with nlink == 1.
- Strict TVGateway builder contract:
    qualified: bool (False by default; set True only after live TV qualification)
    build(kind, text) -> dict (refuses kind != 'title' with DEEPLINK_UNQUALIFIED;
                               refuses unmatched titles with TITLE_NOT_FOUND)
    validate(params) -> bool (exact key set + strict per-provider regex + _flat_params)
    classify_match(kind, text) -> ('direct'|'confirm', sanitized_title)
    classify_match_pinned(kind, text) -> ('direct'|'confirm', sanitized_title, content_target)
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import unicodedata

from ..errors import Refused
from .tv_gateway import _flat_params

DEFAULT_CATALOG_DIR = Path.home() / '.local' / 'share' / 'r2d2-catalog'
DEFAULT_NETFLIX_DB = DEFAULT_CATALOG_DIR / 'netflix.db'
DEFAULT_APPLE_TV_DB = DEFAULT_CATALOG_DIR / 'apple_tv.db'
DEFAULT_TUBI_DB = DEFAULT_CATALOG_DIR / 'tubi.db'
DEFAULT_APPLE_MUSIC_DB = DEFAULT_CATALOG_DIR / 'apple_music.db'

NETFLIX_ID_STRICT = re.compile(r'^[0-9]{6,10}$')
NETFLIX_TARGET_STRICT = re.compile(
    r'^m=http://api\.netflix\.com/catalog/titles/movies/[0-9]{6,10}&source_type=4$'
)

APPLE_TV_ID_STRICT = re.compile(r'^umc\.cmc\.[a-z0-9]{16,32}$')
APPLE_TV_SLUG_STRICT = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$')
APPLE_TV_TARGET_STRICT = re.compile(
    r'^https://tv\.apple\.com/us/(?:show|movie)/[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?/umc\.cmc\.[a-z0-9]{16,32}$'
)

TUBI_ID_STRICT = re.compile(r'^[0-9]{4,12}$')
TUBI_TARGET_STRICT = re.compile(r'^[0-9]{4,12}$')

APPLE_MUSIC_ID_STRICT = re.compile(
    r'^(?:artist:[0-9]{4,14}|album:[0-9]{4,14}|song:[0-9]{4,14}:[0-9]{4,14})$'
)
APPLE_MUSIC_TARGET_STRICT = re.compile(
    r'^https://music\.apple\.com/us/(?:artist|album|song)/[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?/[0-9]{4,14}$'
)

MAX_QUERY_CHARS = 160
MAX_QUERY_BYTES = 640
LATENCY_BUDGET_MS = 75.0
AMBIGUITY_MARGIN = 0.04
STALE_AFTER_HOURS = 48.0

_NON_ALNUM_RE = re.compile(r'[^a-z0-9\s]+')
_SPACES_RE = re.compile(r'\s+')
_LEADING_ARTICLE_RE = re.compile(r'^(?:the|a|an)\s+')
_BIDI_AND_CTRL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]')

_STOPWORDS = frozenset({'the', 'a', 'an', 'of', 'in', 'on', 'to', 'for', 'and', 'or', 'at', 'by', 'with'})

_SPOKEN_PREFIX_RE = re.compile(
    r'^(?:please\s+)?(?:play|watch|open|put\s+on|start|resume|listen\s+to|find|search(?:\s+for)?)\s+',
    re.IGNORECASE,
)
_VIDEO_MEDIA_PREFIX_RE = re.compile(r'^(?:the\s+)?(movie|show|series|film)\s+', re.IGNORECASE)
_MUSIC_MEDIA_PREFIX_RE = re.compile(r'^(?:the\s+)?(song|track|album|artist|music\s+by)\s+', re.IGNORECASE)
_SPOKEN_SUFFIX_RE = re.compile(
    r'(?:\s+(?:season|episode)\s+\d+'
    r'|\s+(?:the\s+)?(?:movie|show|series|film|song|track|album)'
    r'|\s+(?:on|in|from)\s+(?:netflix|apple\s*tv(?:\s*plus|\+)?|tubi|apple\s*music))+$',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TitleEntry:
    provider_id: str
    title: str
    norm_title: str
    bare_title: str
    year: int | None
    media_type: str
    popularity_rank: int
    tokens: frozenset[str]
    subtitle: str = ''
    slug: str = ''

    @property
    def netflix_id(self) -> str:
        """Backwards-compatible alias for Netflix callers."""
        return self.provider_id


@dataclass(frozen=True)
class _Snapshot:
    sig: tuple[int, int, int, int]
    entries: tuple[TitleEntry, ...]
    by_norm: dict[str, tuple[int, ...]]
    by_bare: dict[str, tuple[int, ...]]
    by_alias: dict[str, tuple[int, ...]]
    by_token: dict[str, tuple[int, ...]]
    by_trigram: dict[str, tuple[int, ...]]
    meta: dict[str, str]


@dataclass(frozen=True)
class ResolveResult:
    provider_id: str
    title: str
    year: int | None
    media_type: str
    popularity_rank: int
    confidence: float
    match_tier: str
    ambiguous: bool
    latency_ms: float
    content_target: str
    subtitle: str = ''
    slug: str = ''

    @property
    def netflix_id(self) -> str:
        """Backwards-compatible alias for Netflix callers."""
        return self.provider_id


def normalize_title(text: str) -> str:
    """Case-fold, strip diacritics and punctuation, collapse whitespace."""
    if type(text) is not str:
        return ''
    decomposed = unicodedata.normalize('NFKD', text)
    ascii_only = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _NON_ALNUM_RE.sub(' ', ascii_only.casefold().replace('&', ' and '))
    return _SPACES_RE.sub(' ', cleaned).strip()


def strip_leading_article(norm: str) -> str:
    return _LEADING_ARTICLE_RE.sub('', norm, count=1).strip()


def make_ascii_slug(text: str, *, fallback: str = 'title') -> str:
    norm = normalize_title(text)
    slug = norm.replace(' ', '-')[:80].strip('-')
    if slug and APPLE_TV_SLUG_STRICT.fullmatch(slug):
        return slug
    return fallback


def sanitize_display_title(text: str, *, max_chars: int = 60) -> str:
    """Sanitize a title for TV overlay cards: strip control/bidi chars, ASCII printable, capped."""
    if type(text) is not str:
        return ''
    cleaned = _BIDI_AND_CTRL_RE.sub('', text)
    decomposed = unicodedata.normalize('NFKD', cleaned)
    ascii_only = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    printable = ''.join(ch if (' ' <= ch <= '~') else ' ' for ch in ascii_only)
    collapsed = _SPACES_RE.sub(' ', printable).strip()
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 3].rstrip() + '...'


def parse_spoken_query(raw: str, *, provider: str = 'netflix') -> tuple[str, str | None]:
    """Strip common voice framing and return (cleaned_query, optional preferred_media_type)."""
    q = raw.strip()
    preferred: str | None = None
    for _ in range(3):
        nxt = _SPOKEN_PREFIX_RE.sub('', q).strip()
        if nxt == q:
            break
        q = nxt

    if provider == 'apple_music':
        m = _MUSIC_MEDIA_PREFIX_RE.match(q)
        if m:
            hint = m.group(1).casefold()
            if hint in ('song', 'track'):
                preferred = 'song'
            elif hint == 'album':
                preferred = 'album'
            else:
                preferred = 'artist'
            q = q[m.end():].strip()
    else:
        m = _VIDEO_MEDIA_PREFIX_RE.match(q)
        if m:
            hint = m.group(1).casefold()
            preferred = 'movie' if hint in ('movie', 'film') else 'show'
            q = q[m.end():].strip()

    q = _SPOKEN_SUFFIX_RE.sub('', q).strip()
    return q or raw.strip(), preferred


def clean_spoken_query(raw: str) -> str:
    cleaned, _ = parse_spoken_query(raw, provider='netflix')
    return cleaned


def _trigrams(norm: str) -> set[str]:
    if not norm:
        return set()
    padded = f'  {norm} '
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _damerau_within(a: str, b: str, max_dist: int) -> int:
    """Exact optimal string alignment distance capped at max_dist + 1."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    prev2 = list(range(lb + 1))
    prev1 = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        row_min = i
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            val = min(prev1[j] + 1, curr[j - 1] + 1, prev1[j - 1] + cost)
            if i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == b[j - 1]:
                val = min(val, prev2[j - 2] + 1)
            curr[j] = val
            if val < row_min:
                row_min = val
        if row_min > max_dist:
            return max_dist + 1
        prev2, prev1 = prev1, curr
    return prev1[lb]


def _is_plural_pair(a: str, b: str) -> bool:
    return a + 's' == b or b + 's' == a or a + 'es' == b or b + 'es' == a


def _token_fuzzy_match(qt: str, et: str) -> bool:
    """Strict single-token fuzzy agreement guard against false-positive near-misses."""
    if qt == et or _is_plural_pair(qt, et):
        return True
    min_len = min(len(qt), len(et))
    if min_len < 7:
        return False
    if qt[:2] != et[:2]:
        return False
    max_d = 1 if min_len < 9 else 2
    return _damerau_within(qt, et, max_d) <= max_d


def _tokens_compatible(q_tokens: list[str], e_tokens: list[str]) -> bool:
    """Verify every salient query token matches a candidate token (and vice versa for short titles)."""
    if not q_tokens or not e_tokens:
        return False
    q_salient = [t for t in q_tokens if t not in _STOPWORDS] or q_tokens
    e_salient = [t for t in e_tokens if t not in _STOPWORDS] or e_tokens
    for qt in q_salient:
        if not any(_token_fuzzy_match(qt, et) for et in e_salient):
            return False
    matched_e = sum(1 for et in e_salient if any(_token_fuzzy_match(qt, et) for qt in q_salient))
    if len(q_salient) == 1 and len(e_salient) > 1:
        return False
    return (matched_e / len(e_salient)) >= 0.65


def format_netflix_content_target(netflix_id: str) -> str:
    if type(netflix_id) is not str or NETFLIX_ID_STRICT.fullmatch(netflix_id) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    target = f'm=http://api.netflix.com/catalog/titles/movies/{netflix_id}&source_type=4'
    if NETFLIX_TARGET_STRICT.fullmatch(target) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    return target


def format_apple_tv_content_target(provider_id: str, media_type: str, slug: str = '', title: str = '') -> str:
    if type(provider_id) is not str or APPLE_TV_ID_STRICT.fullmatch(provider_id) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    kind_seg = 'movie' if media_type == 'movie' else ('show' if media_type == 'show' else '')
    if not kind_seg:
        raise Refused('DEEPLINK_UNQUALIFIED')
    clean_slug = slug if (type(slug) is str and APPLE_TV_SLUG_STRICT.fullmatch(slug)) else make_ascii_slug(title)
    target = f'https://tv.apple.com/us/{kind_seg}/{clean_slug}/{provider_id}'
    if APPLE_TV_TARGET_STRICT.fullmatch(target) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    return target


def format_tubi_content_target(provider_id: str, media_type: str = 'movie') -> str:
    """LG webOS com.tubitv.ott.tubi takes the raw numeric content ID in contentTarget."""
    if type(provider_id) is not str or TUBI_ID_STRICT.fullmatch(provider_id) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    if media_type not in ('movie', 'show'):
        raise Refused('DEEPLINK_UNQUALIFIED')
    if TUBI_TARGET_STRICT.fullmatch(provider_id) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    return provider_id


def format_apple_music_content_target(provider_id: str, title: str = '', slug: str = '') -> str:
    """LG webOS com.apple.applemusic requires canonical /us/{artist|album|song}/{slug}/{id} (no ?i=)."""
    if type(provider_id) is not str or APPLE_MUSIC_ID_STRICT.fullmatch(provider_id) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    clean_slug = slug if (type(slug) is str and APPLE_TV_SLUG_STRICT.fullmatch(slug)) else make_ascii_slug(title)
    parts = provider_id.split(':')
    if parts[0] == 'artist' and len(parts) == 2:
        target = f'https://music.apple.com/us/artist/{clean_slug}/{parts[1]}'
    elif parts[0] == 'album' and len(parts) == 2:
        target = f'https://music.apple.com/us/album/{clean_slug}/{parts[1]}'
    elif parts[0] == 'song' and len(parts) == 3:
        target = f'https://music.apple.com/us/song/{clean_slug}/{parts[2]}'
    else:
        raise Refused('DEEPLINK_UNQUALIFIED')
    if APPLE_MUSIC_TARGET_STRICT.fullmatch(target) is None:
        raise Refused('DEEPLINK_UNQUALIFIED')
    return target


def _verify_private_db(path: Path) -> tuple[int, int, int, int]:
    """Enforce R2D2 private-file rules on DB path + parent dir. Returns (ino, dev, mtime_ns, size)."""
    if not path.is_absolute():
        raise Refused('DEEPLINK_UNQUALIFIED')
    if path.resolve() != path.absolute():
        raise Refused('DEEPLINK_UNQUALIFIED')
    parent = path.parent
    try:
        pst = os.lstat(parent)
        st = os.lstat(path)
    except OSError:
        raise Refused('DEEPLINK_UNQUALIFIED') from None
    uid = os.getuid()
    if not (stat.S_ISDIR(pst.st_mode) and pst.st_uid == uid and (pst.st_mode & 0o077) == 0):
        raise Refused('DEEPLINK_UNQUALIFIED')
    if not (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == uid
        and st.st_nlink == 1
        and (st.st_mode & 0o077) == 0
        and 0 < st.st_size <= 64 * 1024 * 1024
    ):
        raise Refused('DEEPLINK_UNQUALIFIED')
    return (st.st_ino, st.st_dev, st.st_mtime_ns, st.st_size)


def _format_entry_target(provider: str, entry: TitleEntry) -> str:
    if provider == 'netflix':
        return format_netflix_content_target(entry.provider_id)
    if provider == 'apple_tv':
        return format_apple_tv_content_target(entry.provider_id, entry.media_type, entry.slug, entry.title)
    if provider == 'tubi':
        return format_tubi_content_target(entry.provider_id, entry.media_type)
    if provider == 'apple_music':
        return format_apple_music_content_target(entry.provider_id, entry.title, entry.slug)
    raise Refused('DEEPLINK_UNQUALIFIED')


def _id_regex_for(provider: str) -> re.Pattern[str]:
    if provider == 'netflix':
        return NETFLIX_ID_STRICT
    if provider == 'apple_tv':
        return APPLE_TV_ID_STRICT
    if provider == 'tubi':
        return TUBI_ID_STRICT
    if provider == 'apple_music':
        return APPLE_MUSIC_ID_STRICT
    raise ValueError(f'unsupported provider: {provider}')


class CatalogIndex:
    """In-memory, lock-free-read offline catalog resolver with <=75 ms SLA."""

    def __init__(
        self,
        db_path: Path,
        *,
        provider: str,
        require_private: bool = True,
        reload_interval_s: float = 5.0,
        wall_clock=time.time,
    ):
        self.db_path = Path(db_path)
        self.provider = provider
        self._id_re = _id_regex_for(provider)
        self.require_private = require_private
        self.reload_interval_s = max(0.0, float(reload_interval_s))
        self._wall_clock = wall_clock
        self._snapshot: _Snapshot | None = None
        self._reload_lock = threading.Lock()
        self._reload_in_flight = False
        self._last_stat_check: float = 0.0
        self._reload_failures: int = 0
        self._last_reload_error: str | None = None
        self._last_reload_error_at: float | None = None

    def _stat_sig(self) -> tuple[int, int, int, int]:
        if self.require_private:
            return _verify_private_db(self.db_path)
        try:
            st = os.lstat(self.db_path)
        except OSError:
            raise Refused('DEEPLINK_UNQUALIFIED') from None
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            raise Refused('DEEPLINK_UNQUALIFIED')
        return (st.st_ino, st.st_dev, st.st_mtime_ns, st.st_size)

    def _build_snapshot(self, sig: tuple[int, int, int, int], *, yield_gil: bool = False) -> _Snapshot:
        uri = f'file:{self.db_path}?mode=ro&immutable=1'
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            if self.require_private:
                st_after = _verify_private_db(self.db_path)
                if st_after != sig:
                    raise Refused('DEEPLINK_UNQUALIFIED')

            cols = {row[1] for row in conn.execute('PRAGMA table_info(titles)')}
            id_col = 'provider_id' if 'provider_id' in cols else 'netflix_id'
            sub_expr = 'subtitle' if 'subtitle' in cols else "''"
            slug_expr = 'slug' if 'slug' in cols else "''"

            rows = conn.execute(
                f"""
                SELECT {id_col}, title, norm_title, bare_title, year, media_type,
                       popularity_rank, {sub_expr}, {slug_expr}
                FROM titles
                ORDER BY popularity_rank ASC
                """
            ).fetchall()

            alias_rows: list[tuple[str, str]] = []
            has_aliases = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='title_aliases'"
            ).fetchone()
            if has_aliases:
                acols = {row[1] for row in conn.execute('PRAGMA table_info(title_aliases)')}
                acol = 'alias' if 'alias' in acols else 'alias_norm'
                aid_col = 'provider_id' if 'provider_id' in acols else 'netflix_id'
                alias_rows = conn.execute(
                    f'SELECT {acol}, {aid_col} FROM title_aliases'
                ).fetchall()

            meta: dict[str, str] = {}
            for mtable in ('meta', 'catalog_meta'):
                has_meta = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (mtable,),
                ).fetchone()
                if has_meta:
                    meta = dict(conn.execute(f'SELECT key, value FROM {mtable}').fetchall())
                    break
        except Refused:
            raise
        except sqlite3.Error:
            raise Refused('DEEPLINK_UNQUALIFIED') from None
        finally:
            conn.close()

        entries: list[TitleEntry] = []
        id_to_idx: dict[str, int] = {}
        by_norm: dict[str, list[int]] = defaultdict(list)
        by_bare: dict[str, list[int]] = defaultdict(list)
        by_alias: dict[str, list[int]] = defaultdict(list)
        by_token: dict[str, list[int]] = defaultdict(list)
        by_trigram: dict[str, list[int]] = defaultdict(list)

        for row_num, (pid, title, norm, bare, year, mtype, rank, subtitle, slug) in enumerate(rows):
            if yield_gil and (row_num & 0xFF) == 0:
                time.sleep(0)
            if type(pid) is not str or self._id_re.fullmatch(pid) is None:
                continue
            if type(title) is not str or not title:
                continue
            norm = norm if type(norm) is str and norm else normalize_title(title)
            if not norm:
                continue
            bare = bare if type(bare) is str else strip_leading_article(norm)
            sub_s = subtitle if type(subtitle) is str else ''
            slug_s = slug if (type(slug) is str and APPLE_TV_SLUG_STRICT.fullmatch(slug)) else ''
            idx = len(entries)
            toks = frozenset(t for t in (bare or norm).split() if t)
            entries.append(
                TitleEntry(
                    provider_id=pid,
                    title=title,
                    norm_title=norm,
                    bare_title=bare,
                    year=year if type(year) is int else None,
                    media_type=mtype.casefold() if type(mtype) is str and mtype else 'unknown',
                    popularity_rank=int(rank) if type(rank) is int else idx + 1,
                    tokens=toks,
                    subtitle=sub_s,
                    slug=slug_s,
                )
            )
            id_to_idx[pid] = idx
            by_norm[norm].append(idx)
            if bare:
                by_bare[bare].append(idx)
            for t in toks:
                by_token[t].append(idx)
            for tg in _trigrams(bare or norm):
                by_trigram[tg].append(idx)

        for alias_norm, pid in alias_rows:
            if type(alias_norm) is not str or not alias_norm:
                continue
            idx = id_to_idx.get(pid)
            if idx is not None and idx not in by_alias[alias_norm]:
                by_alias[alias_norm].append(idx)

        for idxs in by_alias.values():
            idxs.sort()

        return _Snapshot(
            sig=sig,
            entries=tuple(entries),
            by_norm={k: tuple(v) for k, v in by_norm.items()},
            by_bare={k: tuple(v) for k, v in by_bare.items()},
            by_alias={k: tuple(v) for k, v in by_alias.items()},
            by_token={k: tuple(v) for k, v in by_token.items()},
            by_trigram={k: tuple(v) for k, v in by_trigram.items()},
            meta=meta,
        )

    def load(self, *, force: bool = False) -> bool:
        """Synchronously load or reload snapshot. Never holds a lock across resolve()."""
        sig = self._stat_sig()
        cur = self._snapshot
        if not force and cur is not None and cur.sig == sig:
            self._last_stat_check = time.monotonic()
            return False
        new_snap = self._build_snapshot(sig)
        with self._reload_lock:
            self._snapshot = new_snap
            self._last_stat_check = time.monotonic()
            self._last_reload_error = None
            self._last_reload_error_at = None
        return True

    def maybe_reload_async(self) -> None:
        """Rate-limited stat check; spawns a background rebuild if DB file changed."""
        now = time.monotonic()
        if now - self._last_stat_check < self.reload_interval_s:
            return
        with self._reload_lock:
            if self._reload_in_flight or (now - self._last_stat_check < self.reload_interval_s):
                return
            self._last_stat_check = now
            try:
                sig = self._stat_sig()
            except Refused:
                self._reload_failures += 1
                self._last_reload_error = 'private_check_or_stat_failed'
                self._last_reload_error_at = self._wall_clock()
                return
            cur = self._snapshot
            if cur is not None and cur.sig == sig:
                return
            self._reload_in_flight = True

        def _worker() -> None:
            try:
                snap = self._build_snapshot(sig, yield_gil=True)
                with self._reload_lock:
                    self._snapshot = snap
                    self._last_reload_error = None
                    self._last_reload_error_at = None
            except Refused as err:
                with self._reload_lock:
                    self._reload_failures += 1
                    self._last_reload_error = f'reload_refused:{err.code}'
                    self._last_reload_error_at = self._wall_clock()
            except Exception as exc:
                with self._reload_lock:
                    self._reload_failures += 1
                    self._last_reload_error = f'reload_error:{type(exc).__name__}'
                    self._last_reload_error_at = self._wall_clock()
            finally:
                with self._reload_lock:
                    self._reload_in_flight = False

        threading.Thread(target=_worker, name=f'r2d2-{self.provider}-reload', daemon=True).start()

    @property
    def size(self) -> int:
        snap = self._snapshot
        return len(snap.entries) if snap is not None else 0

    def health(self) -> dict:
        snap = self._snapshot
        meta = snap.meta if snap is not None else {}
        synced_at = float(meta['synced_at']) if 'synced_at' in meta else None
        age_hours = round((self._wall_clock() - synced_at) / 3600.0, 2) if synced_at else None
        return {
            'provider': self.provider,
            'db_path': str(self.db_path),
            'loaded': snap is not None and len(snap.entries) > 0,
            'titles': len(snap.entries) if snap is not None else 0,
            'aliases': len(snap.by_alias) if snap is not None else 0,
            'synced_at': synced_at,
            'age_hours': age_hours,
            'stale': (age_hours is None) or (age_hours > STALE_AFTER_HOURS),
            'reload_in_flight': self._reload_in_flight,
            'reload_failures': self._reload_failures,
            'last_reload_error': self._last_reload_error,
            'last_reload_error_at': self._last_reload_error_at,
        }

    def _pick_from_indices(
        self,
        snap: _Snapshot,
        idxs: tuple[int, ...],
        preferred_media_type: str | None,
    ) -> tuple[TitleEntry, bool]:
        if preferred_media_type:
            matching = [i for i in idxs if snap.entries[i].media_type == preferred_media_type]
            if matching:
                return snap.entries[matching[0]], len(matching) > 1
        return snap.entries[idxs[0]], len(idxs) > 1

    def resolve(
        self,
        query: str,
        *,
        min_confidence: float = 0.78,
        deadline_ms: float = LATENCY_BUDGET_MS,
        check_reload: bool = True,
    ) -> ResolveResult:
        t0 = time.perf_counter()
        if type(query) is not str or not query or len(query) > MAX_QUERY_CHARS or len(query.encode('utf-8')) > MAX_QUERY_BYTES:
            raise Refused('TOOL_SCHEMA')

        cleaned, preferred = parse_spoken_query(query, provider=self.provider)
        norm = normalize_title(cleaned)
        if not norm:
            raise Refused('TITLE_NOT_FOUND')
        bare = strip_leading_article(norm)

        snap = self._snapshot
        if snap is None:
            self.load(force=True)
            snap = self._snapshot
        elif check_reload:
            self.maybe_reload_async()

        if snap is None or not snap.entries:
            raise Refused('DEEPLINK_UNQUALIFIED')

        # Tier 1a: O(1) exact normalized match
        if norm in snap.by_norm:
            entry, ambig = self._pick_from_indices(snap, snap.by_norm[norm], preferred)
            return self._finish(entry, 1.0, 'exact', ambig, t0, deadline_ms)

        # Tier 1b: O(1) article-stripped exact match
        if bare and bare in snap.by_bare:
            entry, ambig = self._pick_from_indices(snap, snap.by_bare[bare], preferred)
            return self._finish(entry, 0.97, 'exact_bare', ambig, t0, deadline_ms)

        # Tier 1c: O(1) precomputed alias match (subtitles, hyphen-joined, numerals, "song by artist")
        for key in (norm, bare):
            if key and key in snap.by_alias:
                entry, ambig = self._pick_from_indices(snap, snap.by_alias[key], preferred)
                return self._finish(entry, 0.94, 'alias', ambig, t0, deadline_ms)

        # Tier 2/3: Narrow candidates via token inverted index + 3-gram index
        q_target = bare or norm
        q_tokens = [t for t in q_target.split() if t]
        candidate_scores: Counter[int] = Counter()

        for tok in q_tokens:
            hits = snap.by_token.get(tok)
            if hits:
                w = 6 if len(hits) < 40 else (3 if len(hits) < 200 else 1)
                for idx in hits:
                    candidate_scores[idx] += w

        for tg in _trigrams(q_target):
            hits = snap.by_trigram.get(tg)
            if hits and len(hits) <= 800:
                for idx in hits:
                    candidate_scores[idx] += 1

        if not candidate_scores:
            raise Refused('TITLE_NOT_FOUND')

        top_ids = [idx for idx, _ in candidate_scores.most_common(40)]
        q_tok_set = frozenset(q_tokens)
        eff_threshold = max(min_confidence, 0.86) if len(q_tokens) <= 2 else min_confidence

        scored: list[tuple[float, str, TitleEntry]] = []
        for idx in top_ids:
            e = snap.entries[idx]
            e_target = e.bare_title or e.norm_title
            e_tokens = e_target.split()
            if not _tokens_compatible(q_tokens, e_tokens):
                continue
            seq = SequenceMatcher(None, q_target, e_target).ratio()
            e_tok_set = frozenset(e_tokens)
            inter = len(q_tok_set & e_tok_set)
            tok_f1 = (2.0 * inter) / (len(q_tok_set) + len(e_tok_set)) if (q_tok_set and e_tok_set) else 0.0
            pop_bonus = max(0.0, 0.025 * (1.0 - min(e.popularity_rank, 2000) / 2000.0))
            type_bonus = 0.015 if (preferred and e.media_type == preferred) else 0.0
            score = min(0.95, max(seq, 0.65 * seq + 0.35 * tok_f1) + pop_bonus + type_bonus)
            tier = 'token' if tok_f1 >= 0.85 else 'fuzzy'
            scored.append((score, tier, e))

        if not scored:
            raise Refused('TITLE_NOT_FOUND')

        scored.sort(key=lambda item: (-item[0], item[2].popularity_rank))
        best_score, best_tier, best_entry = scored[0]
        if best_score < eff_threshold:
            raise Refused('TITLE_NOT_FOUND')

        runner_up_close = (
            len(scored) > 1
            and scored[1][2].provider_id != best_entry.provider_id
            and (best_score - scored[1][0]) < AMBIGUITY_MARGIN
        )
        return self._finish(best_entry, round(best_score, 4), best_tier, runner_up_close, t0, deadline_ms)

    def _finish(
        self,
        entry: TitleEntry,
        confidence: float,
        tier: str,
        ambiguous: bool,
        t0: float,
        deadline_ms: float,
    ) -> ResolveResult:
        target = _format_entry_target(self.provider, entry)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if elapsed_ms > deadline_ms:
            raise Refused('DEEPLINK_UNQUALIFIED')
        return ResolveResult(
            provider_id=entry.provider_id,
            title=entry.title,
            year=entry.year,
            media_type=entry.media_type,
            popularity_rank=entry.popularity_rank,
            confidence=confidence,
            match_tier=tier,
            ambiguous=ambiguous,
            latency_ms=round(elapsed_ms, 3),
            content_target=target,
            subtitle=entry.subtitle,
            slug=entry.slug,
        )


class CatalogDeeplinkBuilder:
    """Base thread-safe TVGateway.deeplinks[provider] builder with P5 confirmation policy."""

    PROVIDER: str = ''
    TARGET_REGEX: re.Pattern[str] = re.compile(r'^(?!)$')
    _DIRECT_TIERS = frozenset(('exact', 'exact_bare', 'alias'))
    _CONFIRM_SINGLE_TOKEN_PROVIDERS = frozenset(('tubi', 'apple_music'))

    def __init__(
        self,
        index: CatalogIndex,
        *,
        qualified: bool = False,
        min_confidence: float = 0.78,
        preload: bool = True,
    ):
        if self.PROVIDER and index.provider != self.PROVIDER:
            raise ValueError(f'expected {self.PROVIDER} index, got {index.provider}')
        self.index = index
        self.qualified = bool(qualified)
        self.min_confidence = float(min_confidence)
        if preload and index.size == 0:
            try:
                index.load(force=False)
            except Refused:
                pass  # bridge stays up; build() will refuse fail-closed until DB is synced

    def health(self) -> dict:
        info = self.index.health()
        info['qualified'] = self.qualified
        return info

    def _format_display(self, match: ResolveResult) -> str:
        if match.subtitle and normalize_title(match.subtitle) != normalize_title(match.title):
            return sanitize_display_title(f'{match.title} - {match.subtitle}')
        return sanitize_display_title(match.title)

    def classify_match_pinned(self, kind: str, text: str) -> tuple[str, str, str]:
        """Classify match and return ('direct'|'confirm', sanitized_title, pinned_content_target).

        - 'direct' ONLY when qualified, kind == 'title', unambiguous, and tier in exact/exact_bare/alias.
        - 'confirm' for ambiguous matches, fuzzy/token tiers, or anything uncertain.
        - Raises Refused('TITLE_NOT_FOUND') or Refused('DEEPLINK_UNQUALIFIED') if not launchable.
        """
        if not self.qualified or kind != 'title':
            raise Refused('DEEPLINK_UNQUALIFIED')
        match = self.index.resolve(text, min_confidence=self.min_confidence)
        params = {'contentTarget': match.content_target}
        if not self.validate(params):
            raise Refused('DEEPLINK_UNQUALIFIED')
        display = self._format_display(match)
        cleaned, preferred = parse_spoken_query(text, provider=self.PROVIDER)
        q_tokens = [t for t in normalize_title(cleaned).split() if t]
        single_tok_guard = (
            self.PROVIDER in self._CONFIRM_SINGLE_TOKEN_PROVIDERS
            and len(q_tokens) <= 1
            and preferred is None
        )
        mode = (
            'direct'
            if (not match.ambiguous and not single_tok_guard and match.match_tier in self._DIRECT_TIERS)
            else 'confirm'
        )
        return mode, display, match.content_target

    def classify_match(self, kind: str, text: str) -> tuple[str, str]:
        mode, display, _ = self.classify_match_pinned(kind, text)
        return mode, display

    def validate(self, params: dict) -> bool:
        """Called by TVGateway._payload_ok() right before SSAP send."""
        if not self.qualified or type(params) is not dict or set(params) != {'contentTarget'}:
            return False
        target = params.get('contentTarget')
        return (
            type(target) is str
            and self.TARGET_REGEX.fullmatch(target) is not None
            and _flat_params(params)
        )

    def build(self, kind: str, text: str) -> dict[str, str]:
        if not self.qualified or kind != 'title':
            raise Refused('DEEPLINK_UNQUALIFIED')
        match = self.index.resolve(text, min_confidence=self.min_confidence)
        params = {'contentTarget': match.content_target}
        if not self.validate(params):
            raise Refused('DEEPLINK_UNQUALIFIED')
        return params


class NetflixCatalogIndex(CatalogIndex):
    def __init__(
        self,
        db_path: Path = DEFAULT_NETFLIX_DB,
        *,
        require_private: bool = True,
        reload_interval_s: float = 5.0,
        wall_clock=time.time,
    ):
        super().__init__(
            db_path,
            provider='netflix',
            require_private=require_private,
            reload_interval_s=reload_interval_s,
            wall_clock=wall_clock,
        )


class NetflixDeeplinkBuilder(CatalogDeeplinkBuilder):
    PROVIDER = 'netflix'
    TARGET_REGEX = NETFLIX_TARGET_STRICT


class AppleTVDeeplinkBuilder(CatalogDeeplinkBuilder):
    PROVIDER = 'apple_tv'
    TARGET_REGEX = APPLE_TV_TARGET_STRICT


class TubiDeeplinkBuilder(CatalogDeeplinkBuilder):
    PROVIDER = 'tubi'
    TARGET_REGEX = TUBI_TARGET_STRICT


class AppleMusicDeeplinkBuilder(CatalogDeeplinkBuilder):
    PROVIDER = 'apple_music'
    TARGET_REGEX = APPLE_MUSIC_TARGET_STRICT


PROVIDER_BUILDERS: dict[str, type[CatalogDeeplinkBuilder]] = {
    'netflix': NetflixDeeplinkBuilder,
    'apple_tv': AppleTVDeeplinkBuilder,
    'tubi': TubiDeeplinkBuilder,
    'apple_music': AppleMusicDeeplinkBuilder,
}


def build_catalog_deeplinks(
    catalog_dir: Path = DEFAULT_CATALOG_DIR,
    *,
    qualified_apps: tuple[str, ...] | frozenset[str] | set[str] = (),
    providers: tuple[str, ...] = ('netflix', 'apple_tv', 'tubi', 'apple_music'),
    require_private: bool = True,
    preload: bool = True,
) -> dict[str, CatalogDeeplinkBuilder]:
    """Build per-provider offline deep-link builders.

    Only apps explicitly listed in `qualified_apps` receive `qualified=True`.
    All others remain `qualified=False` (fail-closed) until live TV qualification passes.
    """
    base = Path(catalog_dir)
    qset = frozenset(qualified_apps)
    builders: dict[str, CatalogDeeplinkBuilder] = {}
    for prov in providers:
        cls = PROVIDER_BUILDERS.get(prov)
        if cls is None:
            raise ValueError(f'unknown provider: {prov}')
        idx = CatalogIndex(base / f'{prov}.db', provider=prov, require_private=require_private)
        builders[prov] = cls(idx, qualified=(prov in qset), preload=preload)
    return builders
