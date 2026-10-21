import json
import unittest
from urllib.parse import urlsplit

from agent.errors import Refused
from agent.parse import check_call, legacy_to_call, parse_legacy_intent, parse_tool_call
from agent.registry import APP_IDS, HOME_ID
from agent.router import GOOGLE_PATTERN, YT_PATTERN, allowed_url, plan
from tests.support import Case, call

NONCE = 'ab' * 16
VALID = {
    'browser.open_site': {'site': 'youtube'},
    'browser.web_search': {'query': 'space news'},
    'browser.youtube_search': {'query': 'lo-fi music'},
    'browser.close': {},
    'control.back': {},
    'control.play_pause': {'action': 'toggle'},
    'tv.home': {},
    'tv.open_app': {'app': 'netflix'},
    'tv.open_content': {'app': 'apple_tv', 'kind': 'title', 'text': 'Severance'},
    'tv.volume': {'direction': 'up', 'steps': 2},
    'tv.mute': {'state': 'on'},
    'meta.whats_playing': {},
    'meta.cancel': {},
    'meta.unsupported': {},
}


class Parse(Case):
    def test_every_model_facing_tool_parses(self):
        for tool, args in VALID.items():
            with self.subTest(tool=tool):
                parsed = parse_tool_call(json.dumps({'tool': tool, 'args': args}))
                self.assertEqual((parsed.tool, dict(parsed.args)), (tool, args))

    def test_closed_shape_refusals(self):
        bad = [
            {'tool': 'browser.open_site', 'args': {'site': 'youtube'}, 'url': 'https://evil.example'},
            {'tool': 'browser.open_site', 'args': {'site': 'youtube', 'url': 'https://evil.example'}},
            {'tool': 'browser.open_site', 'args': {}},
            {'tool': 'browser.open_site'},
            {'args': {}},
            {'tool': 'browser.open_site', 'args': {'site': 'https://evil.example'}},
            {'tool': 'browser.open_site', 'args': {'site': 'YouTube'}},
            {'tool': 'Browser.open_site', 'args': {'site': 'youtube'}},
            {'tool': 'shell.exec', 'args': {'cmd': 'rm -rf /'}},
            {'tool': 'system.run', 'args': {}},
            {'tool': 'meta.confirm', 'args': {'answer': 'yes'}},   # never model-facing
            {'tool': 'tv.open_app', 'args': {'app': 'com.webos.app.settings'}},
            {'tool': 'tv.open_app', 'args': {'app': 'netflix '}},
            {'tool': 'tv.open_app', 'args': {'app': 'local.owner.applibrary'}},
            {'tool': 'tv.open_app', 'args': {'app': 'com.apple.appletv'}},    # raw ID, not key
            {'tool': 'tv.open_app', 'args': {'app': {'id': 'netflix'}}},
            {'tool': 'tv.volume', 'args': {'direction': 'up', 'steps': True}},
            {'tool': 'tv.volume', 'args': {'direction': 'up', 'steps': 2.0}},
            {'tool': 'tv.volume', 'args': {'direction': 'up', 'steps': 6}},
            {'tool': 'tv.volume', 'args': {'direction': 'up', 'steps': 0}},
            {'tool': 'tv.volume', 'args': {'direction': 'sideways', 'steps': 1}},
            {'tool': 'tv.mute', 'args': {'state': True}},
            {'tool': 'browser.web_search', 'args': {'query': 'x' * 257}},
            {'tool': 'browser.web_search', 'args': {'query': ['a', 'b']}},
            {'tool': 'browser.web_search', 'args': {'query': 'line\nbreak'}},
            {'tool': 'control.back', 'args': []},
            {'tool': ['browser.close'], 'args': {}},
            [{'tool': 'browser.close', 'args': {}}],
            None, 'browser.close', 7,
        ]
        for value in bad:
            with self.subTest(value=str(value)[:70]), self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
                parse_tool_call(json.dumps(value))

    def test_raw_text_refusals(self):
        ok = call('browser.close')
        for raw in ('```json\n' + ok + '\n```', 'Sure! ' + ok, ok + ok, ok + '\n' + ok,
                    '{"tool":"browser.close","tool":"tv.home","args":{}}',
                    '{"tool":"tv.volume","args":{"direction":"up","steps":NaN}}',
                    '{"tool":"tv.volume","args":{"direction":"up","steps":Infinity}}',
                    json.dumps({'tool': 'browser.web_search', 'args': {'query': 'a' * 200}}) + ' ' * 1900,
                    '', '   ', None, b'{"tool":"browser.close","args":{}}', {'tool': 'browser.close', 'args': {}}):
            with self.subTest(raw=str(raw)[:40]), self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
                parse_tool_call(raw)

    def test_legacy_intents_map_per_design_5_4(self):
        cases = [({'action': 'browser_open', 'destination': 'youtube'}, ('browser.open_site', {'site': 'youtube'})),
                 ({'action': 'web_search', 'query': 'space news'}, ('browser.web_search', {'query': 'space news'})),
                 ({'action': 'tv_content_open', 'provider': 'apple_tv', 'title': 'Ted Lasso'},
                  ('tv.open_content', {'app': 'apple_tv', 'kind': 'title', 'text': 'Ted Lasso'})),
                 ({'action': 'unsupported'}, ('meta.unsupported', {}))]
        for intent, (tool, args) in cases:
            result = legacy_to_call(parse_legacy_intent(json.dumps(intent)))
            self.assertEqual((result.tool, dict(result.args)), (tool, args))
        for bad in ({'action': 'browser_open', 'destination': 'netflix'}, {'action': 'shell', 'command': 'x'},
                    {'action': 'unsupported', 'reason': 'x'}, {'action': 'web_search', 'query': 'x\nexec'}):
            with self.assertRaises(Refused):
                parse_legacy_intent(json.dumps(bad))


class Router(Case):
    def plan(self, tool, args=None, surface='browser_youtube_watch', phase='P5', **kw):
        return plan(check_call({'tool': tool, 'args': VALID[tool] if args is None else args}),
                    surface, NONCE, phase, **kw)

    def test_urls_come_only_from_templates(self):
        self.assertEqual(self.plan('browser.open_site').param('url'), 'https://www.youtube.com')
        self.assertEqual(self.plan('browser.open_site', {'site': 'duckduckgo'}).param('url'), 'https://duckduckgo.com')
        url = self.plan('browser.web_search', {'query': 'https://evil.example/x?y=1&z=2#f'}).param('url')
        self.assertEqual(urlsplit(url).netloc, 'www.google.com')
        self.assertEqual(urlsplit(url).path, '/search')
        self.assertTrue(GOOGLE_PATTERN.fullmatch(url))
        tail = url[len('https://www.google.com/search?q='):]
        for char in '&#?/=:':
            self.assertNotIn(char, tail)
        yt = self.plan('browser.youtube_search', {'query': 'jazz & blues #1'}).param('url')
        self.assertEqual(yt, 'https://www.youtube.com/results?search_query=jazz+%26+blues+%231')
        self.assertTrue(YT_PATTERN.fullmatch(yt))

    def test_default_search_preserves_query_validation(self):
        for query in ('!g cats', '！g cats', '\\cats', 'cats !w'):
            url = self.plan('browser.web_search', {'query': query}).param('url')
            self.assertNotIn('!', url)
            self.assertNotIn('%21', url.upper())
            self.assertNotIn('%5C', url.upper())

    def test_encoded_query_longer_than_channel_limit_is_refused(self):
        with self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
            self.plan('browser.web_search', {'query': '中' * 200})   # 200 chars -> 1800 encoded
        self.assertTrue(allowed_url(self.plan('browser.web_search', {'query': 'a' * 256}).param('url')))

    def test_allowlist(self):
        for url in ('https://www.youtube.com', 'https://duckduckgo.com', 'https://duckduckgo.com/?q=cats'):
            self.assertTrue(allowed_url(url), url)
        for url in ('https://www.youtube.com/', 'https://www.youtube.com.evil.example',
                    'https://duckduckgo.com/?q=a&kp=1', 'https://duckduckgo.com/?q=a#b', 'http://duckduckgo.com/?q=a',
                    'https://duckduckgo.com/?q=', 'https://www.youtube.com/watch?v=abc', 'javascript:alert(1)',
                    'https://duckduckgo.com/?q=%21g', 'https://duckduckgo.com/?q=cats%20%21w',
                    'https://duckduckgo.com/?q=%5Cevil', 'https://duckduckgo.com/?q=%EF%BC%81g', None):
            self.assertFalse(allowed_url(url), url)

    def test_google_query_only_and_explicit_duckduckgo_site(self):
        prefix = 'https://www.google.com/search?q='
        self.assertEqual(self.plan('browser.web_search',{'query':'café & cats'}).param('url'),prefix+'caf%C3%A9%20%26%20cats')
        self.assertEqual(self.plan('browser.open_site',{'site':'duckduckgo'}).param('url'),'https://duckduckgo.com')
        self.assertTrue(allowed_url(prefix+'a'*1500))
        for tail in ('', 'a'*1501, 'cats&source=voice', 'cats#top', 'cats+dogs', '%', '%2f', '%FF', '%0A', '%2521g'):
            self.assertFalse(allowed_url(prefix+tail),tail[:40])
        for url in ('https://google.com/search?q=cats', 'https://www.google.com:443/search?q=cats',
                    'https://www.google.com/search?source=voice&q=cats', 'https://user@www.google.com/search?q=cats'):
            self.assertFalse(allowed_url(url),url)

    def test_app_ids_only_from_table(self):
        p = self.plan('tv.open_app', surface='r2d2_home')
        self.assertEqual(dict(p.params), {'key': 'netflix', 'app_id': 'netflix'})
        for key, app_id in APP_IDS.items():
            self.assertEqual(self.plan('tv.open_app', {'app': key}, surface='r2d2_home').param('app_id'), app_id)
        self.assertEqual(self.plan('tv.home', surface='r2d2_home').param('app_id'), HOME_ID)

    def test_surface_routing(self):
        self.assertEqual(self.plan('control.back').action, 'history')
        with self.assertRaisesRegex(Refused, '^TV_TOOL_UNAVAILABLE$'):
            self.plan('control.back', surface='app_netflix')
        self.assertEqual(self.plan('control.play_pause').params, (('want', 'toggle'),))
        with self.assertRaisesRegex(Refused, '^TV_TOOL_UNAVAILABLE$'):
            self.plan('control.play_pause', surface='app_netflix')     # toggle has no TV read
        self.assertEqual(self.plan('control.play_pause', {'action': 'pause'}, surface='app_netflix').executor, 'tv')
        with self.assertRaisesRegex(Refused, '^TOOL_DISABLED$'):
            self.plan('control.play_pause', {'action': 'pause'}, surface='app_netflix', phase='P3')
        home_browser = self.plan('tv.home')
        self.assertEqual((home_browser.executor, home_browser.action, home_browser.params),
                         ('browser', 'end', (('reason', 'home'),)))
        self.assertEqual(self.plan('tv.home', surface='app_hulu', phase='P3').executor, 'tv')
        with self.assertRaisesRegex(Refused, '^TOOL_DISABLED$'):
            self.plan('tv.home', surface='app_hulu', phase='P2')
        with self.assertRaisesRegex(Refused, '^TOOL_DISABLED$'):
            self.plan('tv.open_app', surface='r2d2_home', tv_routes=False)
        self.assertEqual(self.plan('meta.whats_playing', surface='app_tubi').action, 'foreground')
        with self.assertRaisesRegex(Refused, '^UNSUPPORTED_COMMAND$'):
            self.plan('meta.unsupported')

    def test_plan_identity(self):
        p = self.plan('tv.volume', surface='r2d2_home')
        self.assertEqual((p.op, p.units, p.scopes), (NONCE + ':volume', 2, ('dispatch', 'volume_steps')))
        self.assertEqual(p.digest(), self.plan('tv.volume', surface='r2d2_home').digest())
        self.assertNotEqual(p.digest(), self.plan('tv.volume', {'direction': 'up', 'steps': 3},
                                                  surface='r2d2_home').digest())
        for surface, nonce in (('desktop', NONCE), ('r2d2_home', 'x' * 32), ('r2d2_home', NONCE.upper())):
            with self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
                plan(check_call({'tool': 'browser.close', 'args': {}}), surface, nonce, 'P5')


if __name__ == '__main__':
    unittest.main()
