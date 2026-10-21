"""Actual agent/gateway ordering, with only the SSAP transport held inert."""
import unittest

from agent.errors import Outcome
from tests.support import Case, call


class DispatchGate(Case):
    def mute_env(self):
        e = self.env(table={'mute': call('tv.mute', state='on')},
                     surface=lambda: 'r2d2_home')
        e.agent.feedback = None
        return e

    def assert_no_mutation(self, e):
        self.assertFalse(e.transport.mute)
        self.assertEqual(e.transport.launched, [])
        self.assertEqual(e.gateway.used, set())
        self.assertFalse(any(uri.endswith(('/setMute', '/launch', '/volumeUp', '/volumeDown'))
                             for uri, _ in e.transport.requests))

    def test_cancel_during_live_installed_read_blocks_launch(self):
        e = self.env(table={'open netflix': call('tv.open_app', app='netflix')},
                     surface=lambda: 'r2d2_home')
        e.agent.feedback = None
        lease = e.agent.arm()
        original = e.transport.request

        def request(uri, payload, *, timeout_s):
            if uri.endswith('/listApps'):
                self.assertTrue(e.agent.cancel(lease))
            return original(uri, payload, timeout_s=timeout_s)

        e.transport.request = request
        self.assertEqual(e.agent.handle_utterance(lease, 'open netflix'),
                         Outcome('refused', 'REQUEST_EXPIRED'))
        self.assert_no_mutation(e)

    def test_cancel_during_connect_closes_before_register(self):
        e = self.mute_env()
        lease = e.agent.arm()
        original = e.transport.connect

        def connect(**kwargs):
            original(**kwargs)
            self.assertTrue(e.agent.cancel(lease))

        e.transport.connect = connect
        self.assertEqual(e.agent.handle_utterance(lease, 'mute'),
                         Outcome('refused', 'REQUEST_EXPIRED'))
        self.assert_no_mutation(e)
        self.assertEqual((e.transport.registers, e.transport.closed), (0, 1))
        self.assertFalse(e.gateway.connected)

    def test_pause_during_connect_closes_before_register(self):
        e = self.mute_env()
        original = e.transport.connect

        def connect(**kwargs):
            original(**kwargs)
            self.assertTrue(e.kill.pause())

        e.transport.connect = connect
        self.assertEqual(self.say(e, 'mute'), Outcome('refused', 'AGENT_PAUSED'))
        self.assert_no_mutation(e)
        self.assertEqual((e.transport.registers, e.transport.closed), (0, 1))

    def test_cancel_during_register_closes_before_mutation(self):
        e = self.mute_env()
        lease = e.agent.arm()
        original = e.transport.register

        def register(**kwargs):
            reply = original(**kwargs)
            self.assertTrue(e.agent.cancel(lease))
            return reply

        e.transport.register = register
        self.assertEqual(e.agent.handle_utterance(lease, 'mute'),
                         Outcome('refused', 'REQUEST_EXPIRED'))
        self.assert_no_mutation(e)
        self.assertEqual((e.transport.registers, e.transport.closed), (1, 1))
        self.assertFalse(e.gateway.connected)

    def test_expired_execution_deadline_after_register_refuses(self):
        e = self.mute_env()
        original = e.transport.register

        def register(**kwargs):
            reply = original(**kwargs)
            self.clock.wait(15)
            return reply

        e.transport.register = register
        self.assertEqual(self.say(e, 'mute'), Outcome('refused', 'REQUEST_EXPIRED'))
        self.assert_no_mutation(e)
        self.assertEqual(e.transport.closed, 1)

    def test_cancel_during_final_rate_admission_blocks_actual_send(self):
        e = self.mute_env()
        lease = e.agent.arm()
        original = e.gateway.mutations.admit

        def admit(scopes, units=1, *, record=True):
            result = original(scopes, units, record=record)
            if record:
                self.assertTrue(e.agent.cancel(lease))
            return result

        e.gateway.mutations.admit = admit
        self.assertEqual(e.agent.handle_utterance(lease, 'mute'),
                         Outcome('refused', 'REQUEST_EXPIRED'))
        self.assert_no_mutation(e)

    def test_success_still_requires_actual_readback(self):
        e = self.mute_env()
        self.assertEqual(self.say(e, 'mute'), Outcome('verified', 'TV_MUTE_VERIFIED'))
        self.assertTrue(e.transport.mute)
        self.assertTrue(any(uri.endswith('/getStatus') for uri, _ in e.transport.requests))

    def test_unknown_mutation_is_not_retried(self):
        e = self.mute_env()
        e.transport.forced['ssap://audio/setMute'] = OSError('inert lost acknowledgement')
        self.assertEqual(self.say(e, 'mute'), Outcome('unknown', 'TV_UNCONFIRMED'))
        self.assertEqual(sum(uri.endswith('/setMute') for uri, _ in e.transport.requests), 1)


if __name__ == '__main__':
    unittest.main()
