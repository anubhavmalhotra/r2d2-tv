"""Lease and handoff races with real threads."""
import os
import threading
import unittest

from agent.agent import Lease
from agent.audit import AuditUnavailable
from agent.bridge import ARM_WINDOW_S, HandoffWindow
from agent.errors import Outcome, Refused
from agent.golden import golden_model
from agent.validate import canonical
from tests_adversarial.adv_support import TOKEN, Case, call, leak_forms, mutations, scripted, tree_bytes

UID = os.getuid()


class Gate:
    """A model that blocks inside interpret() until released."""
    def __init__(self, raw, phase='P2'):
        self.raw = raw
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0
        self.prompt_sha256 = scripted('x', phase).prompt_sha256

    def interpret(self, transcript, *, timeout_s):
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        return self.raw


class LeaseRaces(Case):
    def test_same_lease_from_many_threads_runs_once(self):
        model = Gate(call('control.back'))
        e = self.env('P2', model=model)
        e.channel.open_session(surface='browser_other')
        lease = e.agent.arm()
        barrier = threading.Barrier(8)
        results = []

        def go():
            barrier.wait()
            results.append(e.agent.handle_utterance(lease, 'go back'))
        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        self.assertTrue(model.entered.wait(5))
        model.release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(model.calls, 1)
        self.assertEqual(sorted(r.code for r in results).count('REQUEST_NOT_ARMED'), 7)
        self.assertEqual(len(mutations(e)), 1)

    def test_cancel_during_model_call_blocks_dispatch(self):
        holder = {}

        class Cancelling:
            prompt_sha256 = scripted('x', 'P2').prompt_sha256

            def interpret(self, transcript, *, timeout_s):
                holder['agent'].cancel(holder['lease'])
                return call('control.back')
        e = self.env('P2', model=Cancelling())
        e.channel.open_session(surface='browser_other')
        holder['agent'], holder['lease'] = e.agent, e.agent.arm()
        self.assertEqual(e.agent.handle_utterance(holder['lease'], 'go back'), Outcome('refused', 'REQUEST_EXPIRED'))
        self.assertEqual(mutations(e), [])

    def test_pause_during_owner_open_wait_blocks_dispatch(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.tile_at = self.clock() + 30
        real_wait = self.clock.wait

        def wait(seconds):
            real_wait(seconds)
            if self.clock() > 1010 and not e.kill.paused():
                e.kill.pause('OWNER')
        e.browser.wait = wait
        self.assertEqual(self.say(e, 'open duckduckgo'), Outcome('refused', 'AGENT_PAUSED'))
        self.assertEqual(mutations(e), [])

    def test_leases_do_not_cross_agents_and_tampering_does_not_revive(self):
        a = self.env('P2', model=golden_model('P2'))
        b = self.env('P2', model=golden_model('P2'))
        for e in (a, b):
            e.channel.open_session(surface='browser_other')
        lease = a.agent.arm()
        b.agent.arm()
        self.assertEqual(b.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))
        self.assertEqual(a.agent.handle_utterance(lease, 'go back').state, 'accepted')
        lease.consumed = False
        lease.cancelled = False
        lease.expires += 1000
        self.assertEqual(a.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'REQUEST_NOT_ARMED'))
        self.assertEqual(len(mutations(a)), 1)
        self.assertEqual(mutations(b), [])

    def test_attempt_value_is_validated_before_it_can_reach_the_audit(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session(surface='browser_other')
        for attempt in (TOKEN, 'AB' * 24, 'ab' * 23, 7, ''):
            out = e.agent.handle_utterance(e.agent.arm(), 'go back', attempt=attempt)
            self.assertEqual(out, Outcome('refused', 'HANDOFF_ATTEMPT'))
        self.assertEqual(mutations(e), [])
        blob = tree_bytes(e.base)
        for form in leak_forms(TOKEN):
            self.assertNotIn(form, blob)
        self.assertEqual(e.agent.handle_utterance(e.agent.arm(), 'go back', attempt='ab' * 24).state, 'accepted')


class _FailingAppend:
    def __init__(self, inner):
        self.inner = inner

    def writable(self):
        return True

    def append(self, row):
        raise AuditUnavailable('AUDIT_WRITE')


def frame(clock, attempt, tool='browser.web_search', args=None):
    return canonical({'schema': 1, 'kind': 'call', 'attempt': attempt, 'parsedAt': clock(),
                      'tool': tool, 'args': {'query': 'space news'} if args is None else args})


class HandoffRaces(Case):
    def setUp(self):
        super().setUp()
        self.e = self.env('P1b', model=None)
        self.e.channel.open_session(surface='browser_other')

    def test_concurrent_frames_for_one_arm_admit_exactly_one(self):
        window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock, audit=self.e.audit)
        attempt = 'a1' * 24
        window.arm(attempt)
        barrier = threading.Barrier(12)
        results = []

        def go():
            barrier.wait()
            results.append(window.deliver(frame(self.clock, attempt), peer_uid=UID))
        threads = [threading.Thread(target=go) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        codes = [r.code for r in results]
        self.assertEqual(codes.count('HANDOFF_NOT_ARMED'), 11, codes)
        self.assertEqual(len(mutations(self.e)), 1)

    def test_wrong_peer_cannot_consume_or_close_the_window(self):
        window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock, audit=self.e.audit)
        attempt = 'a2' * 24
        window.arm(attempt)
        self.assertEqual(window.deliver(frame(self.clock, attempt), peer_uid=UID + 1),
                         Outcome('refused', 'HANDOFF_PEER'))
        self.assertEqual(window.deliver(frame(self.clock, attempt), peer_uid=True), Outcome('refused', 'HANDOFF_PEER'))
        self.assertTrue(window.is_open())
        self.assertEqual(window.deliver(frame(self.clock, attempt), peer_uid=UID).state, 'accepted')

    def test_arm_is_closed_when_its_audit_row_cannot_be_written(self):
        """The arm is an audited event. If that row cannot be written the window
        must stay closed (and the agent pauses), never armed-but-unaudited."""
        window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock, audit=_FailingAppend(self.e.audit))
        attempt = 'a3' * 24
        with self.assertRaises((Refused, AuditUnavailable)):
            window.arm(attempt)
        self.assertFalse(window.is_open())
        self.assertEqual(window.deliver(frame(self.clock, attempt), peer_uid=UID),
                         Outcome('refused', 'HANDOFF_NOT_ARMED'))
        self.assertEqual(mutations(self.e), [])
        self.assertTrue(self.e.kill.paused())

    def test_an_attempt_id_can_be_armed_only_once(self):
        window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock, audit=self.e.audit)
        attempt = 'a4' * 24
        window.arm(attempt)
        self.clock.wait(ARM_WINDOW_S + 1)                 # expires without a delivery
        self.assertFalse(window.is_open())
        with self.assertRaises(Refused) as caught:
            window.arm(attempt)
        self.assertEqual(caught.exception.code, 'HANDOFF_ATTEMPT')
        self.assertFalse(window.is_open())

    def test_meta_confirm_over_handoff_is_disabled_without_tripping(self):
        window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock, audit=self.e.audit)
        attempt = 'a5' * 24
        window.arm(attempt)
        self.assertEqual(window.deliver(frame(self.clock, attempt, 'meta.confirm', {'answer': 'yes'}), peer_uid=UID),
                         Outcome('refused', 'TOOL_DISABLED'))
        self.assertFalse(self.e.kill.paused())

    def test_benign_unicode_query_does_not_trip_parse_mismatch(self):
        """The reference v5-side builder and the agent's re-parse must agree on every
        query the builder itself accepted (normalize_text idempotency)."""
        from agent.bridge import build_call_frame
        window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock, audit=self.e.audit)
        attempt = 'a6' * 24
        window.arm(attempt)
        raw = '{"action":"web_search","query":"cafe‍́ menu"}'
        out = window.deliver(build_call_frame(attempt, self.clock(), raw), peer_uid=UID)
        self.assertEqual(out, Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.assertFalse(self.e.kill.paused())


if __name__ == '__main__':
    unittest.main()
