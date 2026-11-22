"""End-to-end tests for P5 Netflix catalog integration inside Agent -> TVExecutor -> TVGateway."""
import hashlib
import os
from pathlib import Path
import sys
import time
import unittest

from agent.executors.netflix_catalog import (
    NetflixCatalogIndex,
    NetflixDeeplinkBuilder,
    sanitize_display_title,
)
from agent.registry import prompt_sha256
from tests.support import Case, call

_TOOLS_DIR = Path(__file__).resolve().parents[4] / 'tools'
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from netflix_catalog_sync import write_catalog_db  # noqa: E402

SAMPLE_ROWS = [
    {'netflix_id': '80057281', 'title': 'Stranger Things', 'norm_title': 'stranger things', 'bare_title': 'stranger things', 'year': 2016, 'media_type': 'SHOW', 'popularity_rank': 1},
    {'netflix_id': '81435684', 'title': 'Arcane: League of Legends', 'norm_title': 'arcane league of legends', 'bare_title': 'arcane league of legends', 'year': 2021, 'media_type': 'SHOW', 'popularity_rank': 2},
    {'netflix_id': '81040344', 'title': 'Squid Game', 'norm_title': 'squid game', 'bare_title': 'squid game', 'year': 2021, 'media_type': 'SHOW', 'popularity_rank': 3},
    {'netflix_id': '81288983', 'title': 'The Diplomat', 'norm_title': 'the diplomat', 'bare_title': 'diplomat', 'year': 2023, 'media_type': 'SHOW', 'popularity_rank': 4},
    {'netflix_id': '81288999', 'title': 'The Diplomat', 'norm_title': 'the diplomat', 'bare_title': 'diplomat', 'year': 2023, 'media_type': 'SHOW', 'popularity_rank': 5},
    {'netflix_id': '70001001', 'title': 'Deliverance', 'norm_title': 'deliverance', 'bare_title': 'deliverance', 'year': 1972, 'media_type': 'MOVIE', 'popularity_rank': 6},
    {'netflix_id': '70001002', 'title': 'Officer', 'norm_title': 'officer', 'bare_title': 'officer', 'year': 2018, 'media_type': 'MOVIE', 'popularity_rank': 7},
    {'netflix_id': '70178217', 'title': 'House of Cards', 'norm_title': 'house of cards', 'bare_title': 'house of cards', 'year': 2013, 'media_type': 'SHOW', 'popularity_rank': 8},
]


class TestNetflixCatalogP5(Case):
    def setUp(self):
        super().setUp()
        self.db_path = self.tmp / 'netflix.db'
        write_catalog_db(self.db_path, SAMPLE_ROWS, total_available=len(SAMPLE_ROWS))

    def _make_env(self, table, *, qualified=True, surface='browser_youtube'):
        idx = NetflixCatalogIndex(self.db_path, require_private=True)
        builder = NetflixDeeplinkBuilder(idx, qualified=qualified, preload=True)
        self.assertGreater(idx.size, 0)  # preloaded at construction
        env = self.env(
            phase='P5',
            table=table,
            with_tv=True,
            overlay=True,
            surface=lambda: surface,
            deeplinks={'netflix': builder},
        )
        return env, builder

    def test_unambiguous_exact_and_alias_launch_directly(self):
        env, _ = self._make_env({
            'play stranger things on netflix': call('tv.open_content', app='netflix', kind='title', text='Stranger Things'),
            'play arcane on netflix': call('tv.open_content', app='netflix', kind='title', text='Arcane'),
        })
        captured = []
        orig_req = env.transport.request

        def spy(uri, payload, *, timeout_s):
            if uri == 'ssap://system.launcher/launch':
                captured.append(dict(payload))
            return orig_req(uri, payload, timeout_s=timeout_s)

        env.transport.request = spy

        out1 = self.say(env, 'play stranger things on netflix')
        self.assertEqual((out1.state, out1.code), ('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0],
            {'id': 'netflix', 'params': {'contentTarget': 'm=http://api.netflix.com/catalog/titles/movies/80057281&source_type=4'}},
        )

        out2 = self.say(env, 'play arcane on netflix')
        self.assertEqual((out2.state, out2.code), ('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(len(captured), 2)
        self.assertEqual(
            captured[1]['params']['contentTarget'],
            'm=http://api.netflix.com/catalog/titles/movies/81435684&source_type=4',
        )

    def test_ambiguous_and_fuzzy_matches_require_confirmation_card(self):
        env, _ = self._make_env({
            'play the diplomat on netflix': call('tv.open_content', app='netflix', kind='title', text='The Diplomat'),
            'play squid games on netflix': call('tv.open_content', app='netflix', kind='title', text='squid games'),
        })
        # Ambiguous title -> must ask for confirmation, nothing launched yet
        out = self.say(env, 'play the diplomat on netflix')
        self.assertEqual((out.state, out.code), ('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual([p for uri, p in env.transport.requests if uri == 'ssap://system.launcher/launch'], [])

        # Confirming 'yes' executes the frozen plan
        out_yes = self.say(env, 'yes')
        self.assertEqual((out_yes.state, out_yes.code), ('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(len([p for uri, p in env.transport.requests if uri == 'ssap://system.launcher/launch']), 1)

        # Fuzzy match ('squid games' -> 'Squid Game') -> must ask for confirmation
        out_f = self.say(env, 'play squid games on netflix')
        self.assertEqual((out_f.state, out_f.code), ('refused', 'CONFIRMATION_PENDING'))
        self.assertTrue(any('Squid Game' in text for _, text in env.overlay.shown))
        out_no = self.say(env, 'no')
        self.assertEqual((out_no.state, out_no.code), ('refused', 'CONFIRMATION_DECLINED'))
        self.assertEqual(len([p for uri, p in env.transport.requests if uri == 'ssap://system.launcher/launch']), 1)

    def test_non_netflix_titles_refuse_title_not_found_fast(self):
        table = {
            f'play {t} on netflix': call('tv.open_content', app='netflix', kind='title', text=t)
            for t in ('Severance', 'The Office', 'House of the Dragon')
        }
        env, _ = self._make_env(table)
        for title in ('Severance', 'The Office', 'House of the Dragon'):
            out = self.say(env, f'play {title} on netflix')
            self.assertEqual((out.state, out.code), ('refused', 'TITLE_NOT_FOUND'))
        self.assertEqual([p for uri, p in env.transport.requests if uri == 'ssap://system.launcher/launch'], [])

    def test_kind_search_and_unqualified_refuse_deeplink_unqualified(self):
        env, _ = self._make_env({
            'search stranger things on netflix': call('tv.open_content', app='netflix', kind='search', text='Stranger Things'),
        })
        out1 = self.say(env, 'search stranger things on netflix')
        self.assertEqual((out1.state, out1.code), ('refused', 'DEEPLINK_UNQUALIFIED'))
        self.assertEqual([p for uri, p in env.transport.requests if uri == 'ssap://system.launcher/launch'], [])

    def test_health_staleness_and_reload_failure_tracking(self):
        now = [time.time()]
        idx = NetflixCatalogIndex(
            self.db_path,
            require_private=True,
            reload_interval_s=0.0,
            wall_clock=lambda: now[0],
        )
        idx.load(force=True)
        h0 = idx.health()
        self.assertTrue(h0['loaded'])
        self.assertFalse(h0['stale'])
        self.assertEqual(h0['reload_failures'], 0)

        now[0] += 49 * 3600
        self.assertTrue(idx.health()['stale'])

        os.chmod(self.db_path, 0o644)
        idx.maybe_reload_async()
        h_err = idx.health()
        self.assertGreaterEqual(h_err['reload_failures'], 1)
        self.assertIsNotNone(h_err['last_reload_error'])
        self.assertEqual(idx.resolve('Stranger Things', check_reload=False).netflix_id, '80057281')
        os.chmod(self.db_path, 0o600)

    def test_sanitize_display_title_strips_bidi_and_control(self):
        dirty = 'Stranger\u202e\x07 Things\u200f: ' + 'A' * 80
        clean = sanitize_display_title(dirty)
        self.assertNotIn('\u202e', clean)
        self.assertNotIn('\x07', clean)
        self.assertLessEqual(len(clean), 60)
        self.assertTrue(clean.isascii() and clean.isprintable())

    def test_local_qwen_prompt_matches_registry_p5_sha(self):
        backend_dir = Path(__file__).resolve().parents[3] / 'backend'
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        import local_qwen  # noqa: E402
        got_sha = hashlib.sha256(local_qwen.prompt_for_phase('P5').encode('utf-8')).hexdigest()
        self.assertEqual(got_sha, prompt_sha256('P5'))


if __name__ == '__main__':
    unittest.main()
