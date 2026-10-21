import json
import re
import unittest

from agent.errors import CODES, Outcome, Refused
from agent.registry import (APP_IDS, MODEL_FACING, PHASES, PROMPT_BUDGET_TOKENS, REGISTRY, SITES, Enum,
                            Int, Text, enabled, estimate_tokens, load_sites, prompt_sha256, render_prompt)
from agent.validate import strict_loads
from tests.support import Case

# [V] Installed Home catalog library (tv/home/catalog.js.in).
CATALOG_LIBRARY = {'netflix', 'com.apple.appletv', 'com.wbd.stream', 'com.tubitv.ott.tubi', 'amazon',
                   'com.apple.applemusic', 'com.disney.disneyplus-prod',
                   'com.movenetworks.app.sling-tv-sling-production', 'hulu', 'youtube.leanback.v4'}


class RegistryShape(Case):
    def test_fourteen_model_facing_tools_and_confirm_is_internal(self):
        self.assertEqual(len(MODEL_FACING), 14)
        self.assertNotIn('meta.confirm', MODEL_FACING)
        self.assertFalse(REGISTRY['meta.confirm'].model_facing)
        self.assertEqual(set(REGISTRY), set(MODEL_FACING) | {'meta.confirm'})

    def test_app_table_is_exactly_the_home_catalog(self):
        self.assertEqual(set(APP_IDS.values()), CATALOG_LIBRARY)
        self.assertEqual(len(APP_IDS), 10)
        with self.assertRaises(TypeError):
            APP_IDS['settings'] = 'com.webos.app.settings'

    def test_first_phase_per_tool(self):
        expect = {'browser.open_site': 'P1a', 'browser.web_search': 'P1a', 'browser.youtube_search': 'P1b',
                  'browser.close': 'P1b', 'control.back': 'P1b', 'control.play_pause': 'P1b',
                  'tv.home': 'P1b', 'tv.open_app': 'P3', 'tv.open_content': 'P5', 'tv.volume': 'P4',
                  'tv.mute': 'P4', 'meta.whats_playing': 'P1b', 'meta.cancel': 'P2',
                  'meta.unsupported': 'P0', 'meta.confirm': 'P2'}
        self.assertEqual({n: t.phase for n, t in REGISTRY.items()}, expect)
        self.assertEqual(enabled('P0'), ('meta.unsupported',))
        self.assertEqual(set(enabled('P1a')), {'browser.open_site', 'browser.web_search', 'meta.unsupported'})

    def test_tv_back_route_is_excluded_and_confirm_rules(self):
        back_tv = [r for r in REGISTRY['control.back'].routes if r.executor == 'tv']
        self.assertEqual([r.phase for r in back_tv], [None])
        self.assertEqual(REGISTRY['tv.open_content'].confirm, 'always')
        self.assertEqual(REGISTRY['tv.volume'].confirm, 'policy')

    def test_outcome_vocabulary_is_closed(self):
        with self.assertRaises(ValueError):
            Outcome('done', 'TOOL_SCHEMA')
        with self.assertRaises(ValueError):
            Outcome('refused', 'something the model said')
        with self.assertRaises(ValueError):
            Refused('free text')
        self.assertIn('TOOL_SCHEMA', CODES)


class ArgTypes(Case):
    def test_enum_is_exact(self):
        spec = Enum(('youtube', 'duckduckgo'))
        self.assertEqual(spec.check('youtube'), 'youtube')
        for bad in ('YouTube', 'youtube ', ' youtube', 'https://www.youtube.com', 1, None, ['youtube'], True):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                spec.check(bad)

    def test_int_rejects_bool_float_and_range(self):
        spec = Int(1, 5)
        self.assertEqual(spec.check(5), 5)
        for bad in (True, False, 0, 6, 2.0, '2', None, -1, 10 ** 30):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                spec.check(bad)

    def test_text_nfkc_cf_and_whitespace(self):
        spec = Text(256, 1024)
        self.assertEqual(spec.check('ｌｏｆｉ  beats'), 'lofi beats')  # full-width -> ascii
        self.assertEqual(spec.check('ca​ts‮'), 'cats')                         # zero-width, bidi
        self.assertEqual(spec.check('  jazz 　piano '), 'jazz piano')
        self.assertEqual(spec.check('café'), 'café')

    def test_text_refuses_controls_instead_of_rewriting(self):
        spec = Text(256, 1024)
        for bad in ('x\nexec', 'x\texec', 'a\x00b', 'a\x85b', 'a b', 'a b', '\ud800x', 'ab',
                    '', '   ', '​', 1, None, b'cats'):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                spec.check(bad)

    def test_text_bounds(self):
        spec = Text(256, 1024)
        self.assertEqual(len(spec.check('a' * 256)), 256)
        with self.assertRaises(Refused):
            spec.check('a' * 257)
        self.assertEqual(spec.check('\U0001F600' * 256), '\U0001F600' * 256)  # 1024 bytes exactly
        with self.assertRaises(Refused):
            Text(10, 15).check('é' * 8)   # 8 chars but 16 UTF-8 bytes
        self.assertEqual(Text(10, 16).check('é' * 8), 'é' * 8)

    def test_web_search_strips_bangs_and_backslashes_after_nfkc(self):
        spec = REGISTRY['browser.web_search'].spec['query']
        self.assertEqual(spec.check('!g cats'), 'g cats')
        self.assertEqual(spec.check('！g dogs'), 'g dogs')      # full-width bang
        self.assertEqual(spec.check('﹗w python'), 'w python')  # small bang
        self.assertEqual(spec.check('\\evil'), 'evil')
        self.assertEqual(spec.check('＼evil'), 'evil')          # full-width backslash
        for bad in ('!!!', '\\', '! \\ !'):
            with self.subTest(bad=bad), self.assertRaises(Refused):
                spec.check(bad)
        # Only web_search strips; YouTube search keeps text and percent-encodes it.
        self.assertEqual(REGISTRY['browser.youtube_search'].spec['query'].check('!g'), '!g')


class Sites(Case):
    def base(self):
        return {'schema': 1, 'sites': [{'key': 'youtube', 'name': 'YouTube', 'url': 'https://www.youtube.com',
                                        'aliases': ['youtube']}]}

    def test_shipped_sites(self):
        self.assertEqual({k: s.url for k, s in SITES.items()},
                         {'youtube': 'https://www.youtube.com', 'duckduckgo': 'https://duckduckgo.com'})

    def test_bad_sites_files_refused(self):
        def variant(**row):
            v = self.base()
            v['sites'][0].update(row)
            return json.dumps(v)
        bad = [variant(url='http://www.youtube.com'), variant(url='https://www.youtube.com/'),
               variant(url='https://www.youtube.com/?q=1'), variant(url='https://user@evil.example'),
               variant(url='https://www.youtube.com:8443'), variant(key='YouTube'),
               variant(aliases=['YouTube']), variant(aliases=[]), variant(extra=True),
               variant(name='x' * 33), json.dumps({'schema': 2, 'sites': []}),
               '{"schema":1,"schema":1,"sites":[]}', json.dumps({'schema': 1, 'sites': [self.base()['sites'][0]] * 2}),
               json.dumps({'schema': 1, 'sites': [dict(self.base()['sites'][0], key='k%d' % i) for i in range(17)]})]
        for raw in bad:
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                load_sites(raw.encode())
        self.assertEqual(list(load_sites(json.dumps(self.base()).encode())), ['youtube'])


class Prompt(Case):
    def test_prompt_lists_only_enabled_tools(self):
        for phase in PHASES:
            text = render_prompt(phase)
            listed = set(re.findall(r'^- ([a-z_.]+) ', text, re.M))
            self.assertEqual(listed, set(enabled(phase)), phase)
            self.assertNotIn('meta.confirm', text)

    def test_prompt_carries_no_ids_urls_or_surface(self):
        for phase in PHASES:
            text = render_prompt(phase)
            self.assertNotIn('http', text)
            self.assertNotIn('surface', text)
            for app_id in set(APP_IDS.values()) - set(APP_IDS):
                self.assertNotIn(app_id, text)
            self.assertNotIn('local.owner.applibrary', text)

    def test_budget_and_pinning(self):
        for phase in PHASES:
            self.assertLessEqual(estimate_tokens(render_prompt(phase)), PROMPT_BUDGET_TOKENS)
            self.assertEqual(prompt_sha256(phase), prompt_sha256(phase))
        self.assertNotEqual(prompt_sha256('P1b'), prompt_sha256('P3'))
        self.assertIn('use steps 2', render_prompt('P4'))
        self.assertNotIn('use steps 2', render_prompt('P3'))


class StrictJson(Case):
    def test_strict_rules(self):
        for raw in ('{"a":1,"a":2}', 'NaN', '{"a":Infinity}', '{"a":-Infinity}', '{"a":1e400}',
                    '[' * 3000, '{"a":1} {"b":2}', '﻿{}', b'\xff\xfe', 3):
            with self.subTest(raw=str(raw)[:20]), self.assertRaises(Refused):
                strict_loads(raw, 8192)
        with self.assertRaises(Refused):
            strict_loads('"' + 'a' * 2050 + '"', 2048)
        self.assertEqual(strict_loads(' {"a":[1,2]} ', 64), {'a': [1, 2]})


if __name__ == '__main__':
    unittest.main()
