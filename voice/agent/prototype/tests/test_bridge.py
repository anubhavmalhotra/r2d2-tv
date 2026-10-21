"""v5 recognition -> agent bridge stub: in-memory results, receipts and the handoff window."""
import json
import os
import unittest

from agent.bridge import (HandoffWindow, P2Bridge, RecognitionResult, build_call_frame, refuse_receipt)
from agent.errors import Outcome, Refused
from agent.golden import golden_model
from agent.validate import canonical
from tests.support import Case
from tests.test_agent import mutations

ATTEMPT = 'ab' * 24          # v4/v5 attempt ids are 48 hex
UID = os.getuid()


class Handoff(Case):
    def setUp(self):
        super().setUp()
        self.e = self.env('P1b', model=None)
        self.e.channel.open_session(surface='browser_other')
        self.window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock, audit=self.e.audit)

    def frame(self, tool='browser.web_search', args=None, attempt=ATTEMPT, parsed_at=None, **extra):
        value = {'schema': 1, 'kind': 'call', 'attempt': attempt,
                 'parsedAt': self.clock() if parsed_at is None else parsed_at,
                 'tool': tool, 'args': {'query': 'space news'} if args is None else args, **extra}
        return canonical(value)

    def test_closed_unless_armed(self):
        self.assertFalse(self.window.is_open())
        self.assertEqual(self.window.deliver(self.frame(), peer_uid=UID), Outcome('refused', 'HANDOFF_NOT_ARMED'))
        self.assertEqual(mutations(self.e), [])

    def test_one_call_per_armed_attempt(self):
        self.window.arm(ATTEMPT)
        self.assertEqual(self.window.deliver(self.frame(), peer_uid=UID),
                         Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.assertFalse(self.window.is_open())
        self.assertEqual(self.window.deliver(self.frame(), peer_uid=UID), Outcome('refused', 'HANDOFF_NOT_ARMED'))
        with self.assertRaisesRegex(Refused, '^HANDOFF_ATTEMPT$'):
            self.window.arm(ATTEMPT)                     # attempt ids are one-use
        self.assertEqual(len(mutations(self.e)), 1)
        rows = self.audit_rows(self.e)
        self.assertEqual(rows[0]['event'], 'handoff_arm')
        self.assertEqual((rows[1]['origin'], rows[1]['attempt'], rows[1]['selection']['path']),
                         ('handoff', ATTEMPT, 'handoff'))

    def test_window_expires_after_five_minutes(self):
        self.window.arm(ATTEMPT)
        self.clock.wait(300)
        self.assertEqual(self.window.deliver(self.frame(), peer_uid=UID), Outcome('refused', 'HANDOFF_NOT_ARMED'))

    def test_peer_attempt_freshness_and_schema(self):
        cases = [
            (dict(), {'peer_uid': UID + 1}, 'HANDOFF_PEER'),
            (dict(attempt='cd' * 24), {}, 'HANDOFF_ATTEMPT'),
            (dict(parsed_at=-11.0), {}, 'HANDOFF_STALE'),
            (dict(parsed_at=+5.0), {}, 'HANDOFF_STALE'),
            (dict(parsed_at=True), {}, 'HANDOFF_STALE'),
            (dict(extra_field=1), {}, 'HANDOFF_SCHEMA'),
        ]
        for index, (frame_kw, deliver_kw, code) in enumerate(cases):
            attempt = '%048x' % (index + 1)
            self.window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock)
            self.window.arm(attempt)
            kw = dict(frame_kw)
            if 'parsed_at' in kw and type(kw['parsed_at']) is float:
                kw['parsed_at'] = self.clock() + kw['parsed_at']
            kw.setdefault('attempt', attempt)
            with self.subTest(code=code):
                result = self.window.deliver(self.frame(**kw), **{'peer_uid': UID, **deliver_kw})
                self.assertEqual(result, Outcome('refused', code))
        for raw in (b'x' * 2049, b'{"schema":1,"schema":1}', b'not json', b'[]',
                    canonical({'schema': 2, 'kind': 'call', 'attempt': ATTEMPT, 'parsedAt': 0, 'tool': 'x', 'args': {}})):
            window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock)
            window.arm('%048x' % (len(raw) + 100))
            self.assertEqual(window.deliver(raw, peer_uid=UID), Outcome('refused', 'HANDOFF_SCHEMA'))
        self.assertEqual(mutations(self.e), [])

    def test_tv_tools_never_cross_the_handoff(self):
        for tool, args in (('tv.open_app', {'app': 'netflix'}), ('tv.volume', {'direction': 'up', 'steps': 1}),
                           ('tv.open_content', {'app': 'apple_tv', 'kind': 'title', 'text': 'Silo'}),
                           ('meta.confirm', {'answer': 'yes'})):
            window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock)
            window.arm('%048x' % (len(tool) + 7))
            with self.subTest(tool=tool):
                frame = self.frame(tool=tool, args=args, attempt=window.armed[0])
                self.assertEqual(window.deliver(frame, peer_uid=UID), Outcome('refused', 'TOOL_DISABLED'))

    def test_tv_routes_blocked_even_in_a_later_phase_agent(self):
        e = self.env('P3', model=None)                    # browser idle -> tv.home would route to TV
        window = HandoffWindow(e.agent, uid=UID, clock=self.clock)
        window.arm(ATTEMPT)
        self.assertEqual(window.deliver(self.frame(tool='tv.home', args={}), peer_uid=UID),
                         Outcome('refused', 'TOOL_DISABLED'))
        self.assertEqual(e.transport.launched, [])

    def test_reparse_mismatch_trips_pause(self):
        for tool, args in (('browser.web_search', {'query': 'space​ news'}),   # agent normalizes -> differs
                           ('shell.exec', {'cmd': 'ls'}),                            # sender accepted, agent refuses
                           ('browser.open_site', {'site': 'https://evil.example'})):
            e = self.env('P1b', model=None)
            e.channel.open_session()
            window = HandoffWindow(e.agent, uid=UID, clock=self.clock)
            window.arm(ATTEMPT)
            with self.subTest(tool=tool):
                self.assertEqual(window.deliver(self.frame(tool=tool, args=args), peer_uid=UID),
                                 Outcome('refused', 'PARSE_MISMATCH'))
                self.assertTrue(e.kill.paused())
                self.assertEqual(mutations(e), [])

    def test_confirmation_needing_call_refused_in_p1(self):
        for index in range(4):
            window = HandoffWindow(self.e.agent, uid=UID, clock=self.clock)
            window.arm('%048x' % (index + 1))
            result = window.deliver(self.frame(tool='control.back', args={}, attempt='%048x' % (index + 1)),
                                    peer_uid=UID)
        self.assertEqual(result, Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))
        self.assertEqual(len(mutations(self.e)), 3)

    def test_legacy_frame_builder_round_trip(self):
        self.window.arm(ATTEMPT)
        frame = build_call_frame(ATTEMPT, self.clock(), json.dumps({'action': 'browser_open', 'destination': 'youtube'}))
        self.assertEqual(self.window.deliver(frame, peer_uid=UID).code, 'BROWSER_NAVIGATION_ACCEPTED')
        with self.assertRaises(Refused):
            build_call_frame(ATTEMPT, 0, json.dumps({'action': 'browser_open', 'destination': 'evil'}))

    def test_paused_agent_refuses_handoff(self):
        self.e.kill.pause()
        self.window.arm(ATTEMPT)
        self.assertEqual(self.window.deliver(self.frame(), peer_uid=UID), Outcome('refused', 'AGENT_PAUSED'))


class Receipts(Case):
    def test_saved_receipts_never_authorize(self):
        receipt = {'schema': 1, 'kind': 'physical-speech-tls-server', 'expectedIntentMatched': True,
                   'commandDispatched': False}
        self.assertEqual(refuse_receipt(receipt), Outcome('refused', 'RECEIPT_NOT_AUTHORITY'))

    def test_p2_recognition_result_path(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session(surface='browser_other')
        bridge = P2Bridge(e.agent)
        result = RecognitionResult(ATTEMPT, 'ptt', 'search the web for news today', 412)
        self.assertNotIn('news', repr(result))
        self.assertEqual(bridge.on_recognition(e.agent.arm(), result).code, 'BROWSER_NAVIGATION_ACCEPTED')
        self.assertEqual(bridge.on_recognition(e.agent.arm(), {'transcript': 'go back'}),
                         Outcome('refused', 'TRANSCRIPT_SCOPE'))
        for bad in (('ab' * 16, 'ptt', 'go back', 1), (ATTEMPT, 'wake_word', 'go back', 1),
                    (ATTEMPT, 'ptt', ' go back', 1), (ATTEMPT, 'ptt', 'go back', 9000)):
            with self.assertRaises(ValueError):
                RecognitionResult(*bad)


if __name__ == '__main__':
    unittest.main()
