"""Deep-link parameter smuggling and TV gateway payload confusion. P5 builders do
not exist yet; these tests pin what the gateway itself enforces when a future
builder is sloppy."""
import os
import secrets
import unittest

from agent.errors import Outcome, Refused
from agent.parse import check_call
from agent.registry import APP_IDS, BROWSER_ID, HOME_ID
from agent.router import Plan, plan
from agent.executors.tv_gateway import OPEN_URL, TVExecutor
from tests_adversarial.adv_support import AppleBuilder, Case, writes


class SloppyBuilder:
    """A builder that trusts its input: the gateway must still bound what it sends."""
    qualified = True

    def __init__(self, params):
        self.params = params
        self.calls = []

    def build(self, kind, text):
        self.calls.append(kind)
        return self.params(kind, text) if callable(self.params) else self.params

    def validate(self, params):
        return True


class TruthyValidator(AppleBuilder):
    def validate(self, params):
        return 1  # truthy but not True


class Unqualified(AppleBuilder):
    qualified = 'yes'


class DeepLinkSmuggling(Case):
    def gw(self, builder):
        e = self.env('P5', deeplinks={'apple_tv': builder})
        e.transport.foreground_id = HOME_ID
        return e

    def test_gateway_bounds_params_from_a_sloppy_builder(self):
        hostile = [
            {'contentTarget': {'nested': 'x'}},
            {'contentTarget': ['a', 'b']},
            {'contentTarget': 1},
            {'contentTarget': 'line\nbreak'},
            {'contentTarget': 'café'},
            {'contentTarget': 'x' * 5000},
            {'a': 'x', 'b': 'x', 'c': 'x', 'd': 'x', 'e': 'x'},
            {'bad key': 'x'},
            {},
            {'contentTarget': lambda: None},
        ]
        for params in hostile:
            with self.subTest(params=repr(params)[:50]):
                e = self.gw(SloppyBuilder(params))
                with self.assertRaises(Refused):
                    e.gateway.launch_content('apple_tv', 'title', 'Severance', secrets.token_hex(16))
                self.assertEqual(writes(e), [])

    def test_free_text_reaches_only_a_qualified_builder(self):
        e = self.gw(SloppyBuilder(lambda kind, text: {'contentTarget': 'q=' + text}))
        # A flat, printable ASCII param is the builder's responsibility.
        status, _ = e.gateway.launch_content('apple_tv', 'search', 'Severance', secrets.token_hex(16))
        self.assertEqual(status, 'accepted')
        for builder in (TruthyValidator(), Unqualified()):
            e = self.gw(builder)
            with self.assertRaises(Refused):
                e.gateway.launch_content('apple_tv', 'title', 'Severance', secrets.token_hex(16))
            self.assertEqual(writes(e), [])

    def test_builder_cannot_override_the_app_id(self):
        e = self.gw(SloppyBuilder({'id': 'com.webos.app.settings'}))
        try:
            e.gateway.launch_content('apple_tv', 'title', 'x', secrets.token_hex(16))
        except Refused:
            pass
        self.assertTrue(all(i == APP_IDS['apple_tv'] for i in e.transport.launched))

    def test_executor_revalidates_open_content_params(self):
        e = self.gw(AppleBuilder())
        base = plan(check_call({'tool': 'tv.open_content',
                                'args': {'app': 'apple_tv', 'kind': 'title', 'text': 'Severance'}}),
                    'r2d2_home', 'ab' * 16, 'P5')
        bad_params = [
            {'key': 'apple_tv', 'app_id': APP_IDS['apple_tv'], 'kind': 'url', 'text': 'x'},
            {'key': 'apple_tv', 'app_id': APP_IDS['apple_tv'], 'kind': 'title', 'text': 'x\ny'},
            {'key': 'apple_tv', 'app_id': APP_IDS['apple_tv'], 'kind': 'title', 'text': 'x' * 161},
            {'key': 'apple_tv', 'app_id': APP_IDS['apple_tv'], 'kind': 'title', 'text': 7},
            {'key': 'apple_tv', 'app_id': APP_IDS['netflix'], 'kind': 'title', 'text': 'x'},
            {'key': 'netflix', 'app_id': APP_IDS['netflix'], 'kind': 'title', 'text': 'x'},
            {'key': 'apple_tv', 'app_id': BROWSER_ID, 'kind': 'title', 'text': 'x'},
        ]
        for params in bad_params:
            with self.subTest(params=params):
                forged = Plan(base.tool, 'tv', 'open_content', base.op, tuple(sorted(params.items())), base.scopes)
                result = TVExecutor(e.gateway, clock=self.clock, wait=self.clock.wait).run(
                    forged, gate=lambda: None, deadline=self.clock() + 10)
                self.assertEqual((result.state, result.code, result.dispatched), ('refused', 'TOOL_SCHEMA', False))
        self.assertEqual(writes(e), [])

    def test_launch_payload_table_rejects_shape_confusion(self):
        e = self.gw(AppleBuilder())
        g = e.gateway
        url = 'http://192.0.2.10:4181/' + 'a' * 64 + '/open'
        session = 'b' * 32
        bad = [
            {'id': BROWSER_ID},
            {'id': HOME_ID, 'params': {'contentTarget': url + '#voice-session=' + session}},
            {'id': BROWSER_ID, 'params': {'contentTarget': url + '#voice-session=' + session + '&x=1'}},
            {'id': BROWSER_ID, 'params': {'contentTarget': url + '#voice-session=' + session + '#x'}},
            {'id': BROWSER_ID, 'params': {'contentTarget': url + '?x=1#voice-session=' + session}},
            {'id': BROWSER_ID, 'params': {'contentTarget': url + '#voice-session=' + session, 'x': 1}},
            {'id': BROWSER_ID, 'params': {'contentTarget': url.replace('http:', 'https:') + '#voice-session=' + session}},
            {'id': BROWSER_ID, 'params': {'contentTarget': url.replace('4181', '4177') + '#voice-session=' + session}},
            {'id': BROWSER_ID, 'params': {'contentTarget': url + '#voice-session=' + session + '\n'}},
            {'id': 'com.webos.app.settings'},
            {'id': APP_IDS['netflix'], 'params': {}},
            {'id': APP_IDS['netflix'], 'extra': 1},
            {'id': [APP_IDS['netflix']]},
        ]
        bad.append({'id': [APP_IDS['netflix']], 'params': {'contentTarget': 'x'}})
        for payload in bad:
            with self.subTest(payload=repr(payload)[:70]):
                self.assertFalse(g._payload_ok('launch', payload))
        self.assertTrue(g._payload_ok('launch', {'id': BROWSER_ID,
                                                 'params': {'contentTarget': url + '#voice-session=' + session}}))

    def test_open_browser_session_and_constant_file_tricks(self):
        e = self.gw(AppleBuilder())
        for session in ('B' * 32, 'b' * 31, 'b' * 33, 'b' * 32 + '\n', 'g' * 32, None, 32):
            with self.assertRaises(Refused):
                e.gateway.open_browser(session, secrets.token_hex(16))
        good = 'http://192.0.2.10:4181/%s/open' % ('c' * 64)
        variants = [
            ({'url': good + '/'}, 0o600), ({'url': good + '?a=1'}, 0o600), ({'url': good, 'x': 1}, 0o600),
            ({'url': good.replace('.10:', '.11:')}, 0o600), ({'url': good + '\n'}, 0o600), ({'url': good}, 0o644),
        ]
        for doc, mode in variants:
            with self.subTest(doc=doc, mode=oct(mode)):
                path = self.private_file('open-%s.json' % secrets.token_hex(4), doc, mode=mode, base=e.base)
                e.gateway.open_url_path = path
                with self.assertRaises(Refused):
                    e.gateway.open_browser('d' * 32, secrets.token_hex(16))
        target = self.private_file('open-real.json', {'url': good}, base=e.base)
        link = e.base / 'open-link.json'
        os.symlink(target, link)
        e.gateway.open_url_path = link
        with self.assertRaises(Refused):
            e.gateway.open_browser('d' * 32, secrets.token_hex(16))
        self.assertEqual(e.transport.launched, [])
        self.assertIsNotNone(OPEN_URL.fullmatch(good))


if __name__ == '__main__':
    unittest.main()
