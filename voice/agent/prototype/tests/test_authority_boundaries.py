"""Revoke at the last mutation boundary; publish consent only after its proof."""
import os
import threading
import unittest

from agent.audit import AuditUnavailable
from agent.bridge import ARM_WINDOW_S, HandoffWindow
from agent.errors import Outcome, Refused
from agent.validate import canonical
from tests.support import Case, call


class _Builder:
    qualified = True

    def build(self, kind, text):
        return {'contentTarget': 'example'}

    def validate(self, params):
        return params == {'contentTarget': 'example'}


class AuthorityBoundaries(Case):
    def close_env(self):
        e = self.env(table={'close': call('browser.close')}, surface=lambda: 'browser_other')
        e.channel.open_session()
        e.agent.feedback = None
        return e

    def close_during_read(self, revoke):
        e = self.close_env()
        lease = e.agent.arm()
        original = e.channel.manager_state
        calls = []

        def state(*, timeout_s):
            calls.append(None)
            if len(calls) == 2:  # the read after the first executor gate
                revoke(e, lease)
            return original(timeout_s=timeout_s)

        e.channel.manager_state = state
        result = e.agent.handle_utterance(lease, 'close')
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(e.channel.calls, [])
        self.assertEqual(e.channel.state, 'ready')
        return result

    def test_browser_end_cancelled_during_final_read(self):
        self.assertEqual(self.close_during_read(lambda e, lease: e.agent.cancel(lease)),
                         Outcome('refused', 'REQUEST_EXPIRED'))

    def test_browser_end_paused_during_final_read(self):
        self.assertEqual(self.close_during_read(lambda e, lease: e.kill.pause()),
                         Outcome('refused', 'AGENT_PAUSED'))

    def test_browser_end_deadline_expires_during_final_read(self):
        self.assertEqual(self.close_during_read(lambda e, lease: self.clock.wait(15)),
                         Outcome('refused', 'REQUEST_EXPIRED'))

    def card_env(self):
        return self.env(table={'open title': call('tv.open_content', app='apple_tv',
                                                 kind='title', text='Example')},
                        surface=lambda: 'browser_other', deeplinks={'apple_tv': _Builder()})

    def test_failed_confirmation_display_drops_pending_plan(self):
        e = self.card_env()
        e.feedback.overlay = lambda code, text: False
        self.assertEqual(self.say(e, 'open title'),
                         Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))
        self.assertFalse(e.agent.confirm.occupied())
        self.assertFalse(self.audit_rows(e)[0]['feedback']['shown'])
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [])

    def test_cancelled_while_showing_confirmation_drops_pending_plan(self):
        e = self.card_env()
        lease = e.agent.arm()

        def overlay(code, text):
            e.agent.cancel(lease)
            return True

        e.feedback.overlay = overlay
        self.assertEqual(e.agent.handle_utterance(lease, 'open title'),
                         Outcome('refused', 'REQUEST_EXPIRED'))
        self.assertFalse(e.agent.confirm.occupied())
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [])

    def test_visible_confirmation_still_executes_once(self):
        e = self.card_env()
        self.assertEqual(self.say(e, 'open title'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertTrue(self.audit_rows(e)[0]['feedback']['shown'])
        self.assertEqual(self.say(e, 'yes'), Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(len(e.transport.launched), 1)

    def arm_during_audit(self, fail):
        e = self.env('P1b', surface=lambda: 'browser_other')
        e.channel.open_session()
        entered, release = threading.Event(), threading.Event()
        errors = []

        class Audit:
            def append(inner, row):
                entered.set()
                if not release.wait(3):
                    raise AuditUnavailable('AUDIT_WRITE')
                if fail:
                    raise AuditUnavailable('AUDIT_WRITE')
                e.audit.append(row)

        window = HandoffWindow(e.agent, uid=os.getuid(), clock=self.clock, audit=Audit())
        attempt = 'a1' * 24
        frame = canonical({'schema': 1, 'kind': 'call', 'attempt': attempt,
                           'parsedAt': self.clock(), 'tool': 'browser.close', 'args': {}})

        def arm():
            try:
                window.arm(attempt)
            except Refused as error:
                errors.append(error.code)

        worker = threading.Thread(target=arm)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertFalse(window.is_open())
            self.assertEqual(window.deliver(frame, peer_uid=os.getuid()),
                             Outcome('refused', 'HANDOFF_NOT_ARMED'))
            with self.assertRaises(Refused):
                window.arm('a2' * 24)
            self.assertEqual(e.channel.calls, [])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        return e, window, frame, errors

    def test_failed_arm_audit_never_publishes_delivery_authority(self):
        e, window, frame, errors = self.arm_during_audit(True)
        self.assertEqual(errors, ['AUDIT_UNAVAILABLE'])
        self.assertTrue(e.kill.paused())
        self.assertEqual(window.deliver(frame, peer_uid=os.getuid()),
                         Outcome('refused', 'HANDOFF_NOT_ARMED'))
        self.assertEqual(e.channel.calls, [])

    def test_successful_arm_audit_publishes_only_after_commit(self):
        e, window, frame, errors = self.arm_during_audit(False)
        self.assertEqual(errors, [])
        self.assertEqual(window.deliver(frame, peer_uid=os.getuid()),
                         Outcome('verified', 'BROWSER_SESSION_ENDED'))
        self.assertEqual(len(e.channel.calls), 1)

    def test_slow_arm_audit_does_not_restart_window(self):
        e = self.env('P1b')
        clock = self.clock

        class Audit:
            def append(inner, row):
                clock.wait(ARM_WINDOW_S)

        window = HandoffWindow(e.agent, uid=os.getuid(), clock=clock, audit=Audit())
        with self.assertRaisesRegex(Refused, '^HANDOFF_NOT_ARMED$'):
            window.arm('a1' * 24)
        self.assertFalse(window.is_open())
        self.assertFalse(window.arming)


if __name__ == '__main__':
    unittest.main()
