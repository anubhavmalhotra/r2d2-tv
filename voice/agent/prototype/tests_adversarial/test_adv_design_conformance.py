"""Independent transcription of the v1 design tables, compared with the prototype.

Tables: the tool registry, argument types, fast path grammar, outcome codes, rate
limits, the gateway table and the P4 manifest rule. Known deviations are asserted
explicitly so a silent change shows up."""
import unittest

from agent import fastpath
from agent.errors import CODES
from agent.executors.tv_gateway import FORBIDDEN_PERMISSIONS, P4_MANIFEST, SSAP
from agent.policy import CONFIRM_TTL_S, LIMITS, REPEAT_LIMIT, REPEAT_WINDOW_S
from agent.registry import APP_IDS, REGISTRY, Enum, Int, Text
from tests_adversarial.adv_support import Case

# name: (args {name: (type, detail)}, confirm, {route-when: (executor, first phase)})
EXPECTED_TOOLS = {
    'browser.open_site': ({'site': ('enum', ('youtube', 'duckduckgo'))}, 'never', {'always': ('browser', 'P1a')}),
    'browser.web_search': ({'query': ('text', (256, '!\\'))}, 'never', {'always': ('browser', 'P1a')}),
    'browser.youtube_search': ({'query': ('text', (256, ''))}, 'never', {'always': ('browser', 'P1b')}),
    'browser.close': ({}, 'never', {'always': ('browser', 'P1b')}),
    'control.back': ({}, 'never', {'browser': ('browser', 'P1b'), 'other': ('tv', None)}),
    'control.play_pause': ({'action': ('enum', ('play', 'pause', 'toggle'))}, 'never',
                           {'browser': ('browser', 'P1b'), 'other': ('tv', 'P4')}),
    'tv.home': ({}, 'never', {'browser': ('browser', 'P1b'), 'other': ('tv', 'P3')}),
    'tv.open_app': ({'app': ('enum', ('netflix', 'apple_tv', 'youtube_app', 'prime_video', 'disney_plus',
                                      'hbo_max', 'hulu', 'sling', 'tubi', 'apple_music'))}, 'never',
                    {'always': ('tv', 'P3')}),
    'tv.open_content': ({'app': ('enum', ('apple_tv',)), 'kind': ('enum', ('search', 'title')),
                         'text': ('text', (160, ''))}, 'always', {'always': ('tv', 'P5')}),
    'tv.volume': ({'direction': ('enum', ('up', 'down')), 'steps': ('int', (1, 5))}, 'policy',
                  {'always': ('tv', 'P4')}),
    'tv.mute': ({'state': ('enum', ('on', 'off'))}, 'never', {'always': ('tv', 'P4')}),
    'meta.whats_playing': ({}, 'never', {'browser': ('browser', 'P1b'), 'other': ('tv', 'P4')}),
    'meta.cancel': ({}, 'never', {'always': ('agent', 'P2')}),
    'meta.unsupported': ({}, 'never', {'always': ('none', 'P0')}),
    'meta.confirm': ({'answer': ('enum', ('yes', 'no'))}, 'never', {'always': ('agent', 'P2')}),
}

EXPECTED_CODES = ('AGENT_PAUSED AUDIT_UNAVAILABLE TOOL_SCHEMA TOOL_DISABLED RATE_LIMITED CONFIRMATION_PENDING '
                  'CONFIRMATION_DECLINED CONFIRMATION_EXPIRED CONFIRMATION_SURFACE_UNAVAILABLE BROWSER_NOT_OPENED '
                  'BROWSER_HISTORY_ACCEPTED BROWSER_MEDIA_VERIFIED BROWSER_MEDIA_NO_VIDEO BROWSER_SESSION_ENDED '
                  'TV_CHANNEL_UNAVAILABLE TV_TOOL_UNAVAILABLE TV_PERMISSION_DENIED TV_LAUNCH_ACCEPTED '
                  'TV_FOREGROUND_VERIFIED TV_VOLUME_ACCEPTED TV_MUTE_VERIFIED TV_MEDIA_ACCEPTED DEEPLINK_UNQUALIFIED').split()


class RegistryMatchesDesign(Case):
    def test_tool_table(self):
        self.assertEqual(set(REGISTRY), set(EXPECTED_TOOLS))
        for name, (args, confirm, routes) in EXPECTED_TOOLS.items():
            tool = REGISTRY[name]
            with self.subTest(tool=name):
                self.assertEqual(tool.confirm, confirm)
                self.assertEqual(tool.model_facing, name != 'meta.confirm')
                self.assertEqual({r.when: (r.executor, r.phase) for r in tool.routes}, routes)
                self.assertEqual(set(tool.spec), set(args))
                for arg, (kind, detail) in args.items():
                    spec = tool.spec[arg]
                    if kind == 'enum':
                        self.assertIsInstance(spec, Enum)
                        self.assertEqual(set(spec.values), set(detail))
                    elif kind == 'int':
                        self.assertIsInstance(spec, Int)
                        self.assertEqual((spec.lo, spec.hi), detail)
                    else:
                        self.assertIsInstance(spec, Text)
                        self.assertEqual((spec.max_chars, spec.strip), detail)

    def test_app_keys(self):
        self.assertEqual(dict(APP_IDS), {
            'netflix': 'netflix', 'apple_tv': 'com.apple.appletv', 'youtube_app': 'youtube.leanback.v4',
            'prime_video': 'amazon', 'disney_plus': 'com.disney.disneyplus-prod', 'hbo_max': 'com.wbd.stream',
            'hulu': 'hulu', 'sling': 'com.movenetworks.app.sling-tv-sling-production',
            'tubi': 'com.tubitv.ott.tubi', 'apple_music': 'com.apple.applemusic'})

    def test_fast_path_grammar(self):
        for phrase in ('cancel', 'stop', 'never mind', 'nevermind'):
            self.assertEqual(fastpath.match(phrase), ('cancel', None))
        for phrase in ('yes', 'yes please', 'confirm', 'do it', 'ok', 'okay'):
            self.assertEqual(fastpath.match(phrase), ('confirm', 'yes'))
        for phrase in ('no', 'no thanks', "don't"):
            self.assertEqual(fastpath.match(phrase), ('confirm', 'no'))

    def test_confirmation_constants(self):
        self.assertEqual((CONFIRM_TTL_S, REPEAT_LIMIT, REPEAT_WINDOW_S), (15, 3, 60))

    def test_rate_limits(self):
        self.assertEqual(LIMITS['utterance'], ((12, 60), (240, 3600)))
        self.assertEqual(LIMITS['dispatch'], ((10, 60),))
        self.assertEqual(LIMITS['browser'], ((6, 60),))
        self.assertEqual(LIMITS['launch'], ((4, 60),))
        self.assertEqual(LIMITS['volume_steps'], ((30, 60),))
        for scope in ('mute', 'play_pause', 'back'):
            self.assertEqual(LIMITS[scope], ((10, 60),))
        self.assertIn('launch', [s for r in REGISTRY['tv.open_app'].routes for s in r.scopes])
        self.assertIn('launch', [s for r in REGISTRY['tv.home'].routes if r.executor == 'tv' for s in r.scopes])

    def test_outcome_codes(self):
        missing = [code for code in EXPECTED_CODES if code not in CODES]
        # [Deviation] The prototype never reports TV_LAUNCH_ACCEPTED: a
        # launch whose foreground read does not match is unknown/TV_UNCONFIRMED.
        self.assertEqual(missing, ['TV_LAUNCH_ACCEPTED'])

    def test_gateway_table_and_manifest(self):
        uris = {uri for uri, _, _ in SSAP.values()}
        self.assertFalse(any('networkinput' in u or 'Pointer' in u or '/close' in u or 'settings' in u.lower()
                             or 'power' in u.lower() or 'turnOff' in u for u in uris))
        self.assertFalse(set(P4_MANIFEST) & set(FORBIDDEN_PERMISSIONS))
        self.assertEqual({name: phase for name, (_, phase, _) in SSAP.items() if phase == 'P3'},
                         {'foreground': 'P3', 'installed': 'P3', 'launch': 'P3'})


if __name__ == '__main__':
    unittest.main()
