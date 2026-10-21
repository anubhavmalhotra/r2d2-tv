"""Fail-closed limits that an always-ready service (P2+) will reach in normal use.
These pin current behaviour: safe (refused, never unknown or success) but they stop
voice control until a restart or the hour rolls over. See the verification notes."""
import unittest

from agent.errors import Outcome
from agent.golden import golden_model
from agent.registry import APP_IDS
from agent.router import plan
from agent.parse import check_call
from tests_adversarial.adv_support import Case, mutations, writes


class Availability(Case):
    def test_browser_op_set_cap_fails_closed_after_4096_ops(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session(surface='browser_other')
        e.browser.operations.update('%032x:history' % i for i in range(4096))
        self.assertEqual(self.say(e, 'go back'), Outcome('refused', 'TOOL_SCHEMA'))
        self.assertEqual(mutations(e), [])

    def test_gateway_op_set_cap_fails_closed_after_4096_writes(self):
        e = self.env('P4', model=golden_model('P4'))
        e.transport.foreground_id = APP_IDS['netflix']
        e.gateway.used.update('%032x' % i for i in range(4096))
        self.assertEqual(self.say(e, 'mute'), Outcome('refused', 'TOOL_SCHEMA'))
        self.assertEqual([u for u in writes(e) if u.endswith('setMute')], [])

    def test_hourly_gateway_mutation_budget_is_reachable_by_normal_volume_use(self):
        """The gateway is capped at 120 requests/h; the agent allows 30 volume
        steps/min and every P4 outcome also sends a toast through the same budget."""
        e = self.env('P4', model=golden_model('P4'), volume_ceiling=100)
        e.transport.foreground_id = APP_IDS['netflix']
        outcomes = []
        for index in range(40):
            e.transport.volume = 50
            outcomes.append(self.say(e, 'volume up 5' if index % 2 else 'lower the volume by 4'))
            self.clock.wait(61)                          # stay clear of the repeat rule
        first = outcomes.index(Outcome('refused', 'RATE_LIMITED'))
        self.assertLess(first * 61, 3600)                 # budget gone well inside the hour
        # The budget runs out in the middle of a 4-5 step plan: that plan is cut
        # short (unknown, never retried) and every later TV write is refused.
        self.assertEqual(outcomes[first - 1], Outcome('unknown', 'TV_UNCONFIRMED'))
        self.assertTrue(all(o.state == 'verified' for o in outcomes[:first - 1]))
        self.assertTrue(all(o == Outcome('refused', 'RATE_LIMITED') for o in outcomes[first:]))


if __name__ == '__main__':
    unittest.main()
