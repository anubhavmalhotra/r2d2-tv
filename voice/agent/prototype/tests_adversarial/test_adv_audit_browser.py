"""Audit / feedback leakage with an identifier-shaped sentinel, and browser-executor
false-success attempts (never report success from an ACK)."""
import json
import os
import unittest

from agent.audit import AuditLog
from agent.bridge import HandoffWindow
from agent.errors import Outcome
from agent.executors.feedback import TEMPLATES
from agent.golden import golden_model
from agent.registry import APP_IDS
from agent.validate import canonical
from tests_adversarial.adv_support import (TOKEN, AppleBuilder, Case, call, leak_forms, mutations, scripted,
                                           tree_bytes)


class AuditLeakage(Case):
    def assert_never_on_disk(self, e):
        blob = tree_bytes(e.base)
        for form in leak_forms(TOKEN):
            self.assertNotIn(form, blob)
        self.assertGreater(AuditLog.verify(e.audit_dir), 0)

    def test_identifier_shaped_free_text_never_reaches_disk(self):
        """TOKEN passes the audit's structural guard (_SAFE_STR), so this exercises the
        real control: free text recorded as a length only."""
        e = self.env('P5', model=None, deeplinks={'apple_tv': AppleBuilder()})
        e.channel.open_session(surface='browser_other')
        outputs = [
            call('browser.web_search', query=TOKEN),
            call('browser.youtube_search', query=TOKEN),
            call('tv.open_content', app='apple_tv', kind='title', text=TOKEN),
            call('tv.open_app', app=TOKEN),
            '{"tool":"%s","args":{}}' % TOKEN,
            'not json ' + TOKEN,
            call('meta.unsupported'),
        ]
        for raw in outputs:
            e.agent.model = scripted(raw)
            self.say(e, TOKEN)
            self.clock.wait(7)
        self.say(e, 'yes')

        class Boom:
            prompt_sha256 = scripted('x').prompt_sha256

            def interpret(self, transcript, *, timeout_s):
                raise RuntimeError(TOKEN + transcript)
        e.agent.model = Boom()
        self.assertEqual(self.say(e, TOKEN), Outcome('refused', 'MODEL_UNAVAILABLE'))
        self.assertTrue(any(TOKEN in text for _, text in e.overlay.shown))   # TV overlay only
        self.assert_never_on_disk(e)

    def test_handoff_frames_carrying_the_sentinel(self):
        e = self.env('P1b', model=None)
        e.channel.open_session(surface='browser_other')
        window = HandoffWindow(e.agent, uid=os.getuid(), clock=self.clock, audit=e.audit)
        frames = [
            {'tool': 'browser.web_search', 'args': {'query': TOKEN}},
            {'tool': TOKEN, 'args': {}},
            {'tool': 'browser.web_search', 'args': {'query': TOKEN + '  x'}},     # mismatch -> trip
        ]
        for index, body in enumerate(frames):
            attempt = ('%02x' % index) * 24
            e.kill.resume()
            window.arm(attempt)
            window.deliver(canonical({'schema': 1, 'kind': 'call', 'attempt': attempt,
                                      'parsedAt': self.clock(), **body}), peer_uid=os.getuid())
            self.clock.wait(7)
        self.assert_never_on_disk(e)

    def test_toasts_never_carry_free_text(self):
        messages = []
        e = self.env('P4', model=None)
        real = e.transport.request

        def spy(uri, payload, *, timeout_s):
            if uri.endswith('createToast'):
                messages.append(payload['message'])
            return real(uri, payload, timeout_s=timeout_s)
        e.transport.request = spy
        e.transport.foreground_id = APP_IDS['netflix']          # no overlay: toasts are the surface
        for raw in (call('browser.web_search', query=TOKEN), call('tv.volume', direction='up', steps=5),
                    call('meta.unsupported'), call('tv.mute', state='on')):
            e.agent.model = scripted(raw, 'P4')
            self.say(e, TOKEN)
            self.clock.wait(61)
        self.assertTrue(messages)
        self.assertTrue(all(m in TEMPLATES.values() for m in messages), messages)
        self.assert_never_on_disk(e)

    def test_structural_guard_is_shape_only(self):
        """[Residual, documented] _SAFE_STR admits any identifier-shaped string, so the
        guard alone would not stop a one-word query; audit_args is the real control."""
        e = self.env('P2', model=None)
        e.audit.append({'note': TOKEN})
        self.assertIn(TOKEN.encode(), tree_bytes(e.audit_dir))


class BrowserFalseSuccess(Case):
    def test_unknown_navigate_with_stale_match_is_not_success(self):
        """The channel reports `matched` against the last *accepted* target
        (browser_channel.py bumps generation only on accept). A second
        navigate whose ACK is unknown and which never lands must not be reported
        as accepted just because the page still matches the previous target."""
        e = self.env('P1b', model=golden_model('P1b'))
        e.channel.open_session(surface='browser_other')
        self.assertEqual(self.say(e, 'open youtube in the browser'),
                         Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.clock.wait(20)
        e.channel.results['navigate'] = 'unknown'
        e.channel.effects['navigate'] = False
        self.assertEqual(self.say(e, 'search the web for weather in springfield'),
                         Outcome('unknown', 'NAVIGATION_UNCONFIRMED'))
        self.assertEqual(e.channel.page, 'https://www.youtube.com')

    def test_accepted_ack_without_new_generation_is_not_success(self):
        e = self.env('P1b', model=golden_model('P1b'))
        e.channel.open_session(surface='browser_other')
        self.say(e, 'open youtube in the browser')
        self.clock.wait(20)
        e.channel.effects['navigate'] = False                # ACK 'accepted', nothing happens
        self.assertEqual(self.say(e, 'open youtube in the browser'),
                         Outcome('unknown', 'NAVIGATION_UNCONFIRMED'))

    def test_navigation_that_lands_elsewhere_is_not_success(self):
        """A redirect (for example an off-site bang) bumps the generation but the
        owned page no longer matches the target: unknown, never accepted."""
        e = self.env('P1b', model=golden_model('P1b'))
        e.channel.open_session(surface='browser_other')
        real = e.channel.navigate

        def redirected(session, op, url, *, timeout_s):
            result = real(session, op, url, timeout_s=timeout_s)
            e.channel.page = 'https://elsewhere.example/'
            return result
        e.channel.navigate = redirected
        self.assertEqual(self.say(e, 'search the web for weather in springfield'),
                         Outcome('unknown', 'NAVIGATION_UNCONFIRMED'))

    def test_same_target_twice_with_real_effect_is_still_accepted(self):
        e = self.env('P1b', model=golden_model('P1b'))
        e.channel.open_session(surface='browser_other')
        for _ in range(2):
            self.assertEqual(self.say(e, 'open youtube in the browser'),
                             Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
            self.clock.wait(20)
        self.assertEqual(len(mutations(e)), 2)


if __name__ == '__main__':
    unittest.main()
