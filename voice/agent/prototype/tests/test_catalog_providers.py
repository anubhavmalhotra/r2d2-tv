"""Multi-provider offline catalog tests: Apple TV+, Tubi, Apple Music, Netflix, and pinned confirmation."""
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from agent.errors import Outcome, Refused
from agent.executors.catalog import (
    AppleMusicDeeplinkBuilder,
    AppleTVDeeplinkBuilder,
    CatalogIndex,
    NetflixDeeplinkBuilder,
    TubiDeeplinkBuilder,
    build_catalog_deeplinks,
    normalize_title,
    strip_leading_article,
)
from agent.registry import APP_IDS, HOME_ID
from tests.support import Case, call, scripted


def _write_provider_db(
    path: Path,
    provider: str,
    rows: list[tuple[str, str, int | None, str, int, str, str]],
    aliases: list[tuple[str, str]] = (),
) -> None:
    """Create a strictly 0600 SQLite catalog DB inside a 0700 parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f'.{path.name}.tmp.{os.getpid()}.{time.time_ns()}')
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
        """)
        for pid, title, year, mtype, rank, subtitle, slug in rows:
            norm = normalize_title(title)
            bare = strip_leading_article(norm)
            conn.execute(
                'INSERT INTO titles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (pid, title, norm, bare, year, mtype, rank, subtitle, slug),
            )
        if aliases:
            conn.executemany('INSERT OR IGNORE INTO title_aliases VALUES (?, ?)', aliases)
        conn.executemany(
            'INSERT INTO meta VALUES (?, ?)',
            [
                ('schema_version', '3'),
                ('provider', provider),
                ('synced_at', str(int(time.time()))),
                ('indexed_count', str(len(rows))),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class MultiProviderCatalogTests(Case):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.cat_dir = Path(self._tmp.name).resolve() / 'catalog'
        self.cat_dir.mkdir(mode=0o700)
        os.chmod(self.cat_dir, 0o700)

        _write_provider_db(
            self.cat_dir / 'netflix.db',
            'netflix',
            [
                ('80057281', 'Stranger Things', 2016, 'show', 1, '', ''),
                ('81288983', 'The Diplomat', 2023, 'show', 2, '', ''),
                ('81600001', 'The Diplomat', 2024, 'movie', 3, '', ''),
            ],
        )
        _write_provider_db(
            self.cat_dir / 'apple_tv.db',
            'apple_tv',
            [
                ('umc.cmc.1srk2goyh2q2zdxcx605w8vtx', 'Severance', 2022, 'show', 1, '', 'severance'),
                ('umc.cmc.2szz3fdt71tl1ulnbp8utgq5o', 'Slow Horses', 2022, 'show', 2, '', 'slow-horses'),
                ('umc.cmc.6lic0hhuxkrbem0n29vzjopii', 'Tenzing', 2026, 'movie', 3, '', 'tenzing'),
            ],
        )
        _write_provider_db(
            self.cat_dir / 'tubi.db',
            'tubi',
            [
                ('550824', 'The Hunger Games', 2012, 'movie', 1, '', ''),
                ('300000737', 'Taboo', 2017, 'show', 2, '', ''),
                ('688825', 'Green Room', 2016, 'movie', 3, '', ''),
            ],
        )
        _write_provider_db(
            self.cat_dir / 'apple_music.db',
            'apple_music',
            [
                ('artist:159260351', 'Taylor Swift', None, 'artist', 1, '', ''),
                ('song:1499378108:1499378607', 'Blinding Lights', 2020, 'song', 2, 'The Weeknd', ''),
                ('song:269572838:269573364', 'Thriller', 1982, 'song', 3, 'Michael Jackson', ''),
                ('album:269572838', 'Thriller', 1982, 'album', 4, 'Michael Jackson', ''),
            ],
            aliases=[
                ('blinding lights by the weeknd', 'song:1499378108:1499378607'),
                ('thriller by michael jackson', 'song:269572838:269573364'),
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def test_build_catalog_deeplinks_fails_closed_unless_explicitly_qualified(self):
        builders = build_catalog_deeplinks(self.cat_dir, qualified_apps=('netflix',))
        self.assertTrue(builders['netflix'].qualified)
        self.assertFalse(builders['apple_tv'].qualified)
        self.assertFalse(builders['tubi'].qualified)
        self.assertFalse(builders['apple_music'].qualified)
        for prov in ('apple_tv', 'tubi', 'apple_music'):
            with self.assertRaises(Refused) as ctx:
                builders[prov].build('title', 'Severance')
            self.assertEqual(ctx.exception.code, 'DEEPLINK_UNQUALIFIED')

    def test_apple_tv_tubi_and_apple_music_targets_and_validation(self):
        builders = build_catalog_deeplinks(
            self.cat_dir,
            qualified_apps=('netflix', 'apple_tv', 'tubi', 'apple_music'),
        )
        # Apple TV+ show & movie
        p_show = builders['apple_tv'].build('title', 'play Severance on Apple TV')
        self.assertEqual(p_show, {'contentTarget': 'https://tv.apple.com/us/show/severance/umc.cmc.1srk2goyh2q2zdxcx605w8vtx'})
        p_movie = builders['apple_tv'].build('title', 'Tenzing')
        self.assertEqual(p_movie, {'contentTarget': 'https://tv.apple.com/us/movie/tenzing/umc.cmc.6lic0hhuxkrbem0n29vzjopii'})
        self.assertFalse(builders['apple_tv'].validate({'contentTarget': 'https://tv.apple.com/us/show/x/umc.cmc.1srk2goyh2q2zdxcx605w8vtx?evil=1'}))
        self.assertFalse(builders['apple_tv'].validate({'contentTarget': 'https://evil.example/us/show/severance/umc.cmc.1srk2goyh2q2zdxcx605w8vtx'}))

        # Tubi movie & show
        self.assertEqual(
            builders['tubi'].build('title', 'watch The Hunger Games on Tubi'),
            {'contentTarget': 'https://tubitv.com/movies/550824'},
        )
        self.assertEqual(
            builders['tubi'].build('title', 'Taboo'),
            {'contentTarget': 'https://tubitv.com/series/300000737'},
        )
        self.assertFalse(builders['tubi'].validate({'contentTarget': 'https://tubitv.com/movies/550824?link-action=play'}))

        # Apple Music artist, song by artist alias, and media-type hint (album vs song)
        self.assertEqual(
            builders['apple_music'].build('title', 'play Taylor Swift on Apple Music'),
            {'contentTarget': 'https://music.apple.com/us/artist/159260351'},
        )
        self.assertEqual(
            builders['apple_music'].build('title', 'play Blinding Lights by The Weeknd on Apple Music'),
            {'contentTarget': 'https://music.apple.com/us/album/1499378108?i=1499378607'},
        )
        self.assertEqual(
            builders['apple_music'].build('title', 'play album Thriller on Apple Music'),
            {'contentTarget': 'https://music.apple.com/us/album/269572838'},
        )
        self.assertEqual(
            builders['apple_music'].build('title', 'play song Thriller on Apple Music'),
            {'contentTarget': 'https://music.apple.com/us/album/269572838?i=269573364'},
        )
        self.assertFalse(builders['apple_music'].validate({'contentTarget': 'https://music.apple.com/us/album/269572838?i=1&evil=2'}))

    def test_apple_music_ambiguous_card_includes_artist_subtitle(self):
        builders = build_catalog_deeplinks(self.cat_dir, qualified_apps=('apple_music',))
        # 'Thriller' exists as both song & album -> ambiguous -> confirm with sanitized subtitle
        mode, display, pinned = builders['apple_music'].classify_match_pinned('title', 'Thriller')
        self.assertEqual(mode, 'confirm')
        self.assertEqual(display, 'Thriller - Michael Jackson')
        self.assertEqual(pinned, 'https://music.apple.com/us/album/269572838?i=269573364')

        # Disambiguated with media hint 'album Thriller' -> direct launch
        mode_a, display_a, pinned_a = builders['apple_music'].classify_match_pinned('title', 'album Thriller')
        self.assertEqual(mode_a, 'direct')
        self.assertEqual(display_a, 'Thriller - Michael Jackson')
        self.assertEqual(pinned_a, 'https://music.apple.com/us/album/269572838')

    def test_pinned_target_prevents_swap_across_db_reload_between_confirm_and_yes(self):
        builders = build_catalog_deeplinks(self.cat_dir, qualified_apps=('netflix',))
        # Track actual launch payloads sent over SSAP
        sent_payloads = []
        table = {
            'play the diplomat on netflix': call('tv.open_content', app='netflix', kind='title', text='The Diplomat'),
        }
        env = self.env(
            phase='P5',
            table=table,
            with_tv=True,
            overlay=True,
            surface=lambda: 'browser_youtube',
            deeplinks=builders,
        )
        env.transport.foreground_id = HOME_ID
        orig_req = env.transport.request

        def capture(uri, payload, *, timeout_s):
            if uri == 'ssap://system.launcher/launch' and 'params' in payload:
                sent_payloads.append(payload)
            return orig_req(uri, payload, timeout_s=timeout_s)

        env.transport.request = capture

        # Step 1: Ambiguous 'The Diplomat' -> confirmation card pins rank-1 (81288983)
        out1 = self.say(env, 'play the diplomat on netflix')
        self.assertEqual((out1.state, out1.code), ('refused', 'CONFIRMATION_PENDING'))

        # Step 2: Before user says 'yes', swap DB on disk so 81600001 ranks #1 and reload
        _write_provider_db(
            self.cat_dir / 'netflix.db',
            'netflix',
            [
                ('81600001', 'The Diplomat', 2024, 'movie', 1, '', ''),
                ('81288983', 'The Diplomat', 2023, 'show', 2, '', ''),
            ],
        )
        builders['netflix'].index.load(force=True)
        self.assertEqual(builders['netflix'].index.resolve('The Diplomat', check_reload=False).provider_id, '81600001')

        # Step 3: User confirms -> MUST launch the pinned 81288983, NOT swapped 81600001!
        out2 = self.say(env, 'yes')
        self.assertEqual((out2.state, out2.code), ('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(len(sent_payloads), 1)
        self.assertEqual(
            sent_payloads[0],
            {
                'id': APP_IDS['netflix'],
                'params': {'contentTarget': 'm=http://api.netflix.com/catalog/titles/movies/81288983&source_type=4'},
            },
        )

    def test_end_to_end_p5_launches_all_four_providers_when_qualified(self):
        builders = build_catalog_deeplinks(
            self.cat_dir,
            qualified_apps=('netflix', 'apple_tv', 'tubi', 'apple_music'),
        )
        table = {
            'play stranger things on netflix': call('tv.open_content', app='netflix', kind='title', text='Stranger Things'),
            'play slow horses on apple tv': call('tv.open_content', app='apple_tv', kind='title', text='Slow Horses'),
            'play green room on tubi': call('tv.open_content', app='tubi', kind='title', text='Green Room'),
            'play taylor swift on apple music': call('tv.open_content', app='apple_music', kind='title', text='Taylor Swift'),
            'play missing on apple tv': call('tv.open_content', app='apple_tv', kind='title', text='Stranger Things'),
        }
        env = self.env(
            phase='P5',
            table=table,
            with_tv=True,
            overlay=True,
            surface=lambda: 'browser_youtube',
            deeplinks=builders,
        )
        env.transport.foreground_id = HOME_ID

        for phrase in (
            'play stranger things on netflix',
            'play slow horses on apple tv',
            'play green room on tubi',
            'play taylor swift on apple music',
        ):
            self.clock.wait(65)
            out = self.say(env, phrase)
            self.assertEqual((out.state, out.code), ('verified', 'TV_FOREGROUND_VERIFIED'), phrase)

        # Cross-provider miss fails fast with TITLE_NOT_FOUND
        out_miss = self.say(env, 'play missing on apple tv')
        self.assertEqual((out_miss.state, out_miss.code), ('refused', 'TITLE_NOT_FOUND'))


if __name__ == '__main__':
    unittest.main()
