"""Lease expiry / replay / concurrency, and the confirmation policy."""
import threading
import unittest

from agent.agent import Lease
from agent.confirm import Pending
from agent.errors import Outcome, Refused
from agent.executors.browser import BrowserExecutor
from agent.golden import golden_model
from agent.registry import APP_IDS
from agent.router import plan
from agent.parse import check_call
from tests.support import Case, call
from tests.test_agent import AppleBuilder, mutations, writes


class Leases(Case):
    def setUp(self):
        super().setUp()
        self.e = self.env('P2', model=golden_model('P2'))
        self.e.channel.open_session(surface='browser_other')

    def test_expired_lease_never_reaches_model(self):
        lease = self.e.agent.arm()
        self.clock.wait(31)
        self.assertEqual(self.e.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'REQUEST_EXPIRED'))
        self.assertEqual(self.e.model.calls, 0)

    def test_replay_and_forged_leases(self):
        lease = self.e.agent.arm()
        self.assertEqual(self.e.agent.handle_utterance(lease, 'go back').state, 'accepted')
        self.assertEqual(self.e.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))
        forged = Lease(lease.nonce, lease.expires + 100)
        self.assertEqual(self.e.agent.handle_utterance(forged, 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))
        self.assertEqual(self.e.agent.handle_utterance(object(), 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))
        self.assertEqual(len(mutations(self.e)), 1)
        self.assertEqual(self.e.model.calls, 1)

    def test_new_arm_invalidates_prior_and_cancel_is_identity_bound(self):
        old = self.e.agent.arm()
        new = self.e.agent.arm()
        self.assertEqual(self.e.agent.handle_utterance(old, 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))
        self.assertFalse(self.e.agent.cancel(old))          # late, old ingress cannot cancel the new lease
        self.assertEqual(self.e.agent.handle_utterance(new, 'go back').state, 'accepted')
        lease = self.e.agent.arm()
        self.assertTrue(self.e.agent.cancel(lease))
        self.assertEqual(self.e.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))

    def test_lease_expiring_during_model_call_blocks_dispatch(self):
        clock = self.clock

        class Slow:
            prompt_sha256 = self.e.model.prompt_sha256

            def interpret(self, transcript, *, timeout_s):
                clock.wait(31)
                return call('control.back')
        e = self.env('P2', model=Slow())
        e.channel.open_session()
        self.assertEqual(self.say(e, 'go back'), Outcome('refused', 'REQUEST_EXPIRED'))
        self.assertEqual(mutations(e), [])

    def test_one_in_flight_and_cancel_during_owner_open_wait(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session(surface='browser_other')
        entered, release = threading.Event(), threading.Event()
        # Hold the executor inside the owner_open wait (the 140 s window).
        e.browser._owner_open = lambda deadline: (entered.set(), release.wait(5), e.channel.session)[2]
        first = e.agent.arm()
        results = []
        worker = threading.Thread(target=lambda: results.append(e.agent.handle_utterance(first, 'open duckduckgo')))
        worker.start()
        self.assertTrue(entered.wait(5))
        with self.assertRaisesRegex(Refused, '^AGENT_BUSY$'):
            e.agent.arm()                                     # one utterance in flight
        stale = Lease('0' * 32, self.clock() + 30)
        self.assertEqual(e.agent.handle_utterance(stale, 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))
        self.assertTrue(e.agent.cancel(first))               # ingress cancel while waiting
        release.set()
        worker.join(5)
        self.assertEqual(results, [Outcome('refused', 'REQUEST_EXPIRED')])   # gate refused the dispatch
        self.assertEqual(mutations(e), [])
        self.assertIsNotNone(e.agent.arm())                  # free again

    def test_executor_op_is_never_replayed(self):
        browser = BrowserExecutor(self.e.channel, clock=self.clock, wait=self.clock.wait)
        p = plan(check_call({'tool': 'control.back', 'args': {}}), 'browser_other', 'cd' * 16, 'P2')
        first = browser.run(p, gate=lambda: None, deadline=self.clock() + 15)
        second = browser.run(p, gate=lambda: None, deadline=self.clock() + 15)
        self.assertEqual((first.state, second.state, second.code, second.dispatched),
                         ('accepted', 'refused', 'TOOL_SCHEMA', False))
        self.assertEqual(len(mutations(self.e)), 1)


class Confirmation(Case):
    def p5(self, **kw):
        e = self.env('P5', model=golden_model('P5'), deeplinks={'apple_tv': AppleBuilder()}, **kw)
        e.channel.open_session(surface='browser_other')   # overlay is the confirmation surface
        return e

    def test_always_confirm_then_yes_executes_frozen_plan(self):
        e = self.p5()
        self.assertEqual(self.say(e, 'open severance on apple tv'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(e.transport.launched, [])
        self.assertEqual(self.say(e, 'yes please'), Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(e.transport.launched, ['com.apple.appletv'])
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, ['com.apple.appletv'])

    def test_no_dont_cancel_and_expiry_execute_nothing(self):
        for answer, code in (('no', 'CONFIRMATION_DECLINED'), ("don't", 'CONFIRMATION_DECLINED'),
                             ('no thanks', 'CONFIRMATION_DECLINED')):
            e = self.p5()
            self.say(e, 'open slow horses in apple tv')
            self.assertEqual(self.say(e, answer), Outcome('refused', code))
            self.assertEqual(e.transport.launched, [])
        e = self.p5()
        self.say(e, 'open slow horses in apple tv')
        self.assertEqual(self.say(e, 'stop'), Outcome('verified', 'PENDING_CANCELLED'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        e = self.p5()
        self.say(e, 'open slow horses in apple tv')
        self.clock.wait(15)
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'CONFIRMATION_EXPIRED'))
        self.assertEqual(e.transport.launched, [])

    def test_model_cancel_and_other_utterance_decline(self):
        e = self.p5()
        self.say(e, 'open slow horses in apple tv')
        self.assertEqual(self.say(e, 'scratch that'), Outcome('verified', 'PENDING_CANCELLED'))
        self.say(e, 'open slow horses in apple tv')
        self.assertEqual(self.say(e, 'open netflix'), Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [APP_IDS['netflix']])
        rows = self.audit_rows(e)
        self.assertIn('declined', [r['policy']['pending'] for r in rows])

    def test_yes_without_pending_never_reaches_model(self):
        e = self.p5()
        for word in ('yes', 'ok', 'okay', 'confirm', 'do it', 'no'):
            self.assertEqual(self.say(e, word), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.model.calls, 0)

    def test_tampered_pending_plan_is_refused(self):
        e = self.p5()
        self.say(e, 'open slow horses in apple tv')
        held = e.agent.confirm.pending
        other = plan(check_call({'tool': 'tv.open_content',
                                 'args': {'app': 'apple_tv', 'kind': 'title', 'text': 'Other'}}),
                     'browser_other', 'ef' * 16, 'P5')
        e.agent.confirm.pending = Pending(other, held.digest, held.expires, held.surface, held.rule, held.audit_args)
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'TOOL_SCHEMA'))
        self.assertEqual(e.transport.launched, [])

    def test_deeplink_unqualified_by_default(self):
        e = self.env('P5', model=golden_model('P5'))
        e.channel.open_session(surface='browser_other')
        self.say(e, 'open severance on apple tv')
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'DEEPLINK_UNQUALIFIED'))
        self.assertEqual(e.transport.launched, [])

    def test_volume_ceiling_confirms_on_toast_surface(self):
        e = self.env('P4', model=golden_model('P4'))
        e.transport.foreground_id, e.transport.volume = APP_IDS['netflix'], 34
        self.assertEqual(self.say(e, 'turn the volume up by 3'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(e.transport.volume, 34)
        self.assertEqual(self.say(e, 'no'), Outcome('refused', 'CONFIRMATION_DECLINED'))
        self.assertEqual(e.transport.volume, 34)
        self.clock.wait(20)
        self.say(e, 'turn the volume up by 3')
        self.clock.wait(2)                                   # the owner presses and says "yes"
        self.assertEqual(self.say(e, 'yes'), Outcome('verified', 'TV_VOLUME_VERIFIED'))
        self.assertEqual(e.transport.volume, 37)
        self.clock.wait(2)
        self.assertEqual(self.say(e, 'volume up 1'), Outcome('refused', 'CONFIRMATION_PENDING'))
        # A 4th tv.volume call within 60 s hits the repeat rule even downwards ...
        self.assertEqual(self.say(e, 'volume down'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.clock.wait(61)
        # ... but on its own, down never needs the ceiling rule.
        self.assertEqual(self.say(e, 'volume down'), Outcome('verified', 'TV_VOLUME_VERIFIED'))

    def test_unreadable_volume_fails_safe_to_confirmation(self):
        e = self.env('P4', model=golden_model('P4'))
        e.transport.forced['ssap://audio/getVolume'] = {'returnValue': False}
        self.assertEqual(self.say(e, 'volume up 1'), Outcome('refused', 'CONFIRMATION_PENDING'))

    def test_repeat_rule_fourth_call_in_60s(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session(surface='browser_other')
        for _ in range(3):
            self.assertEqual(self.say(e, 'go back').state, 'accepted')
        self.assertEqual(self.say(e, 'go back'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(self.say(e, 'yes').state, 'accepted')
        self.assertEqual(len(mutations(e)), 4)

    def test_confirmation_needs_fast_path_and_a_surface(self):
        # P1b: no fast path -> confirmation-requiring calls are refused outright.
        e = self.env('P1b', model=golden_model('P1b'))
        e.channel.open_session(surface='browser_other')
        for _ in range(3):
            self.say(e, 'go back')
        self.assertEqual(self.say(e, 'go back'), Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))
        # P5, app in front: a free-text card cannot go on a toast.
        e = self.env('P5', model=golden_model('P5'), deeplinks={'apple_tv': AppleBuilder()})
        e.transport.foreground_id = APP_IDS['hulu']
        self.assertEqual(self.say(e, 'open severance on apple tv'),
                         Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        # No feedback surface at all.
        e = self.env('P5', model=golden_model('P5'), overlay=False, with_tv=False)
        self.assertEqual(self.say(e, 'open severance on apple tv'),
                         Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))
        self.assertEqual(writes(e), [])


if __name__ == '__main__':
    unittest.main()
