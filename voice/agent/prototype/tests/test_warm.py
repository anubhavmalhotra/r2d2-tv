"""TV-aware warm policy: power-state driven warm/unload, the 5 min grace, the 30 min
unknown hold, warm backoff, in-flight protection, the press path's feedback codes,
the kill switch, and a monotonic clock that refuses to go backwards. Fakes only; no
socket, no model."""
import os
import unittest

from agent.agent import EXEC_SLACK_S, LEASE_S, MODEL_TIMEOUT_S, Agent, Lease
from agent.audit import AuditLog
from agent.errors import Outcome, Refused
from agent.executors.browser import OWNER_OPEN_WINDOW_S
from agent.executors.feedback import TEMPLATES
from agent.golden import golden_model
from agent.registry import APP_IDS, prompt_sha256
from agent.warm import (BACKOFF_S, GRACE_S, INFLIGHT_MAX_S, POWER_STATES, SSAP_POWER, STALE_S,
                        UNKNOWN_HOLD_S, WARM_TIMEOUT_S, FakePool, FakePowerSource, WarmController,
                        power_from_reply)
from tests.support import SENTINEL, Case, call

ACCEPTED_BACK = Outcome('accepted', 'BROWSER_HISTORY_ACCEPTED')
ROW_KEYS = {'schema', 'ts', 'seq', 'prev', 'event', 'from', 'to', 'reason', 'power', 'state',
            'failures', 'retry_s', 'count'}


class WarmCase(Case):
    def warm_env(self, phase='P2', *, model=None, warm_s=84, **kw):
        e = self.env(phase, model=model or golden_model(phase), **kw)
        e.pool, e.power = FakePool(self.clock, warm_s=warm_s), FakePowerSource(self.clock)
        e.warm = WarmController(e.pool, e.power, clock=self.clock, audit=e.audit, killswitch=e.kill)
        e.agent = Agent(phase=phase, audit=e.audit, killswitch=e.kill, model=e.model, browser=e.browser,
                        tv=e.tv, feedback=e.feedback, clock=self.clock, warm=e.warm)
        return e

    def run_for(self, e, seconds, step=1.0):
        """The service loop: tick every `step` seconds of fake time."""
        end = self.clock() + seconds
        while self.clock() < end:
            self.clock.wait(min(step, end - self.clock()))
            e.warm.tick()
        return e.warm.state

    def warm_up(self, e):
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'warming')
        self.assertEqual(self.run_for(e, 84), 'ready')

    def press(self, e):
        """-> the Lease, or the refusal code the ingress would relay."""
        try:
            return e.agent.arm()
        except Refused as error:
            return error.code

    def transitions(self, e):
        return [(r['from'], r['to'], r['reason']) for r in self.audit_rows(e) if r.get('event') == 'warm']

    def refusals(self, e):
        return [(r['code'], r['warm'], r['feedback']['shown'])
                for r in self.audit_rows(e) if r.get('event') == 'press_refused']


class PowerDriven(WarmCase):
    def test_power_on_warms_then_ready_and_press_while_warming_is_visible(self):
        e = self.warm_env()
        e.channel.open_session(surface='browser_other')
        self.assertEqual(e.warm.tick(), 'unloaded')              # no reading yet: unknown never warms
        self.assertEqual(self.press(e), 'VOICE_UNAVAILABLE')
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'warming')               # at once, no waiting for a press
        self.run_for(e, 30)
        self.assertEqual(self.press(e), 'VOICE_WARMING_UP')
        self.assertEqual(e.overlay.shown[-1], ('VOICE_WARMING_UP', TEMPLATES['VOICE_WARMING_UP']))
        self.assertEqual(self.run_for(e, 53), 'warming')
        self.assertEqual(self.run_for(e, 1), 'ready')            # 84 s after power-on
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)
        self.assertEqual(e.pool.calls, ['begin_warm'])
        self.assertEqual(e.warm.inflight, {})                    # released when the utterance ended
        self.assertEqual(self.transitions(e), [('unloaded', 'warming', 'POWER_ACTIVE'),
                                               ('warming', 'ready', 'WARM_DONE')])
        self.assertEqual(self.refusals(e), [('VOICE_UNAVAILABLE', 'unloaded', True),
                                            ('VOICE_WARMING_UP', 'warming', True)])
        self.assertEqual(AuditLog.verify(e.audit_dir), len(self.audit_rows(e)))

    def test_standby_starts_grace_then_unloads_after_5_min(self):
        for state in ('screen_off', 'active_standby', 'suspend', 'off'):
            with self.subTest(state=state):
                e = self.warm_env()
                self.warm_up(e)
                e.power.set(state)
                self.assertEqual(e.warm.tick(), 'grace')
                self.assertEqual(self.run_for(e, GRACE_S - 1), 'grace')
                self.assertEqual(e.pool.calls, ['begin_warm'])
                self.assertEqual(self.run_for(e, 1), 'unloaded')
                self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])
                self.assertEqual(self.press(e), 'VOICE_UNAVAILABLE')
                self.assertEqual(self.transitions(e)[2:], [('ready', 'grace', 'POWER_INACTIVE'),
                                                           ('grace', 'unloading', 'GRACE_EXPIRED'),
                                                           ('unloading', 'unloaded', 'UNLOAD_VERIFIED')])

    def test_back_on_within_grace_stays_warm(self):
        e = self.warm_env()
        self.warm_up(e)
        e.power.set('active_standby')
        self.assertEqual(e.warm.tick(), 'grace')
        self.run_for(e, GRACE_S - 10)
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'ready')
        self.assertEqual(self.run_for(e, 4 * 3600, step=60), 'ready')   # no idle timer
        self.assertEqual(e.pool.calls, ['begin_warm'])
        self.assertEqual(self.transitions(e)[-1], ('grace', 'ready', 'POWER_ACTIVE'))

    def test_press_during_grace_proceeds_without_extending_it(self):
        e = self.warm_env()
        e.channel.open_session()
        self.warm_up(e)
        e.power.set('screen_off')
        e.warm.tick()
        self.run_for(e, 100)
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)
        self.assertEqual(self.run_for(e, GRACE_S - 101), 'grace')
        self.assertEqual(self.run_for(e, 1), 'unloaded')

    def test_flapping_never_double_warms_or_unloads_early(self):
        e = self.warm_env()
        for state in ('active', 'off', 'active', 'screen_off', 'active'):
            e.power.set(state)
            e.warm.tick()
            self.clock.wait(5)
        self.assertEqual((e.warm.state, e.pool.calls), ('warming', ['begin_warm']))
        self.assertEqual(self.run_for(e, 84), 'ready')
        for state in ('active_standby', 'active', 'off', 'active', 'off'):
            e.power.set(state)
            e.warm.tick()
            self.clock.wait(10)
        self.assertEqual(e.warm.tick(), 'grace')
        # The grace counts from the last non-Active report (10 s ago).
        self.assertEqual(self.run_for(e, GRACE_S - 11), 'grace')
        self.assertEqual(self.run_for(e, 1), 'unloaded')
        self.assertEqual((e.pool.calls, e.pool.overlaps), (['begin_warm', 'unload'], 0))

    def test_off_during_warming_counts_grace_from_the_off_report(self):
        e = self.warm_env()
        e.power.set('active')
        e.warm.tick()
        self.run_for(e, 10)
        e.power.set('off')
        e.warm.tick()
        self.assertEqual(self.run_for(e, 74), 'grace')           # the warm finishes; never aborted
        self.assertEqual(self.transitions(e)[-1], ('warming', 'grace', 'WARM_DONE'))
        self.assertEqual(self.run_for(e, GRACE_S - 75), 'grace')
        self.assertEqual(self.run_for(e, 1), 'unloaded')


class UnknownPower(WarmCase):
    def test_unknown_source_holds_30_min_then_unloads_and_never_warms(self):
        e = self.warm_env()
        self.warm_up(e)
        e.power.broken = True
        self.assertEqual(e.warm.tick(), 'ready')
        self.assertEqual(self.run_for(e, UNKNOWN_HOLD_S - 1, step=10), 'ready')
        lease = self.press(e)
        self.assertIsInstance(lease, Lease)                      # still warm: presses proceed
        e.agent.cancel(lease)
        self.assertEqual(self.run_for(e, 1), 'unloaded')
        self.assertEqual(self.run_for(e, 3600, step=60), 'unloaded')
        self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])  # 'unknown' never starts a warm
        self.assertEqual(self.press(e), 'VOICE_UNAVAILABLE')
        self.assertIn(('ready', 'unloading', 'UNKNOWN_HOLD_EXPIRED'), self.transitions(e))
        powers = [r['power'] for r in self.audit_rows(e) if r.get('event') == 'warm_power']
        self.assertEqual(powers, ['active', 'unknown'])

    def test_stale_reading_is_unknown_after_30_s(self):
        e = self.warm_env()
        self.warm_up(e)
        e.power.live = False                                      # the poll stalls; last reading kept
        self.run_for(e, STALE_S)
        self.assertEqual(e.warm.status()['power'], 'active')
        self.run_for(e, 1)
        self.assertEqual(e.warm.status()['power'], 'unknown')
        self.assertEqual(self.run_for(e, UNKNOWN_HOLD_S - 1, step=10), 'ready')
        self.assertEqual(self.run_for(e, 1), 'unloaded')

    def test_unknown_then_active_resumes_and_resets_the_hold(self):
        e = self.warm_env()
        self.warm_up(e)
        e.power.broken = True
        self.run_for(e, 1200, step=10)
        e.power.broken = False
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'ready')
        e.power.broken = True
        self.assertEqual(self.run_for(e, 1200, step=10), 'ready')   # a fresh hold, not 2400 s
        self.assertEqual(e.pool.calls, ['begin_warm'])
        self.assertEqual(self.run_for(e, 620, step=10), 'unloaded')
        e.power.broken = False
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'warming')                  # back on: warm at once
        self.assertEqual(e.pool.calls, ['begin_warm', 'unload', 'begin_warm'])

    def test_unknown_during_grace_keeps_the_grace(self):
        e = self.warm_env()
        self.warm_up(e)
        e.power.set('off')
        e.warm.tick()
        e.power.broken = True
        self.assertEqual(self.run_for(e, GRACE_S - 1), 'grace')
        self.assertEqual(self.run_for(e, 1), 'unloaded')

    def test_malformed_readings_are_unknown(self):
        clock = self.clock

        class Scripted:
            value = None

            def current(self):
                return self.value
        for value in (('Active', clock()), ('active', clock() + 1), ('active', True), ('active', float('nan')),
                      'active', ('active', clock(), 1), ['active', clock()], ('ACTIVE', clock()), ({}, clock())):
            source, pool = Scripted(), FakePool(clock)
            source.value = value
            e = self.env('P2')
            warm = WarmController(pool, source, clock=clock, audit=e.audit, killswitch=e.kill)
            with self.subTest(value=repr(value)):
                self.assertEqual(warm.tick(), 'unloaded')
                self.assertEqual(warm.status()['power'], 'unknown')
                self.assertEqual(pool.calls, [])

    def test_power_reply_mapping_is_closed(self):
        self.assertEqual(set(SSAP_POWER.values()), set(POWER_STATES))
        for raw, want in SSAP_POWER.items():
            self.assertEqual(power_from_reply({'returnValue': True, 'state': raw}), want)
        for bad in (None, [], {'state': 'Active'}, {'returnValue': False, 'state': 'Active'},
                    {'returnValue': 1, 'state': 'Active'}, {'returnValue': True, 'state': 'active'},
                    {'returnValue': True, 'state': 'Screen Saver'}, {'returnValue': True, 'state': ['Active']},
                    {'returnValue': True, 'state': 'Active', 'processing': 'Request Power Off'},
                    {'returnValue': True, 'state': 'Active', 'processing': ['x']}):
            with self.subTest(bad=bad):
                self.assertEqual(power_from_reply(bad), 'unknown')


class InFlight(WarmCase):
    def slow_model(self, during):
        clock, holder = self.clock, {}

        class Slow:
            prompt_sha256 = prompt_sha256('P2')

            def interpret(self, transcript, *, timeout_s):
                e = holder['e']
                clock.wait(2)                                        # the grace runs out mid-call
                during(e)
                return call('control.back')
        return Slow(), holder

    def test_utterance_in_flight_when_grace_expires_finishes_first(self):
        seen = []
        model, holder = self.slow_model(lambda e: seen.append((e.warm.tick(), list(e.pool.calls), e.pool.loaded)))
        e = holder['e'] = self.warm_env(model=model)
        e.channel.open_session()
        self.warm_up(e)
        unload_inflight = []
        e.pool.on_unload = lambda: unload_inflight.append(len(e.warm.inflight))
        e.power.set('screen_off')
        e.warm.tick()
        self.run_for(e, GRACE_S - 1)
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)       # pressed in the last second
        self.assertEqual(seen, [('unloading', ['begin_warm'], 'ready')])
        self.assertEqual(self.press(e), 'VOICE_UNAVAILABLE')          # decided: no new utterance
        self.assertEqual(e.warm.tick(), 'unloaded')
        self.assertEqual((e.pool.calls, unload_inflight), (['begin_warm', 'unload'], [0]))

    def test_tv_back_while_draining_cancels_the_unload(self):
        def during(e):
            self.assertEqual(e.warm.tick(), 'unloading')
            e.power.set('active')
            self.assertEqual(e.warm.tick(), 'ready')
        model, holder = self.slow_model(during)
        e = holder['e'] = self.warm_env(model=model)
        e.channel.open_session()
        self.warm_up(e)
        e.power.set('off')
        e.warm.tick()
        self.run_for(e, GRACE_S - 1)
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)
        self.assertEqual(self.run_for(e, 600, step=10), 'ready')
        self.assertEqual(e.pool.calls, ['begin_warm'])

    def test_tickets_follow_the_lease(self):
        e = self.warm_env()
        e.channel.open_session()
        self.warm_up(e)
        lease = e.agent.arm()
        self.assertEqual(len(e.warm.inflight), 1)
        self.assertTrue(e.agent.cancel(lease))
        self.assertEqual(e.warm.inflight, {})
        e.agent.arm()
        e.agent.arm()                                                 # replaces a lease that never ran
        self.assertEqual(len(e.warm.inflight), 1)
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)
        self.assertEqual(e.warm.inflight, {})
        # An ingress cancel while the model runs must not release early.
        counts, holder = [], {}

        class Cancelling:
            prompt_sha256 = prompt_sha256('P2')

            def interpret(self, transcript, *, timeout_s):
                counts.append(len(e.warm.inflight))
                e.agent.cancel(holder['lease'])
                counts.append(len(e.warm.inflight))
                return call('control.back')
        e.agent.model = Cancelling()
        holder['lease'] = e.agent.arm()
        self.assertEqual(e.agent.handle_utterance(holder['lease'], 'go back'), Outcome('refused', 'REQUEST_EXPIRED'))
        self.assertEqual((counts, e.warm.inflight), ([1, 1], {}))

    def test_abandoned_lease_cannot_pin_the_models(self):
        self.assertGreaterEqual(INFLIGHT_MAX_S, LEASE_S + 5 + MODEL_TIMEOUT_S + OWNER_OPEN_WINDOW_S + EXEC_SLACK_S)
        e = self.warm_env()
        self.warm_up(e)
        e.power.set('off')
        e.warm.tick()
        self.run_for(e, 200)
        e.agent.arm()                                     # the ingress never sends PCM nor cancels
        self.assertEqual(self.run_for(e, GRACE_S - 200), 'unloading')
        self.assertEqual(self.run_for(e, INFLIGHT_MAX_S - (GRACE_S - 200) - 1), 'unloading')
        self.assertEqual(e.pool.calls, ['begin_warm'])
        self.assertEqual(self.run_for(e, 1), 'unloaded')
        self.assertEqual([r.get('count') for r in self.audit_rows(e) if r.get('reason') == 'INFLIGHT_EXPIRED'], [1])


class Failures(WarmCase):
    def test_warm_failure_backs_off_60_then_300_seconds(self):
        self.assertEqual(BACKOFF_S, (60, 300))
        e = self.warm_env()
        e.pool.fail_warm = True
        e.power.set('active')
        e.warm.tick()
        self.assertEqual(self.run_for(e, 84), 'degraded')
        self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])    # cleanup verified first
        self.assertEqual(self.press(e), 'VOICE_UNAVAILABLE')
        self.run_for(e, 59)
        self.assertEqual(e.pool.calls.count('begin_warm'), 1)
        self.assertEqual(self.run_for(e, 1), 'warming')
        self.assertEqual(self.run_for(e, 84), 'degraded')
        self.run_for(e, 299)
        self.assertEqual(e.pool.calls.count('begin_warm'), 2)
        self.run_for(e, 1)
        self.assertEqual(e.pool.calls.count('begin_warm'), 3)
        self.assertEqual(self.run_for(e, 84), 'degraded')
        self.assertEqual(self.run_for(e, 299), 'degraded')          # 300 s for every further failure
        e.pool.fail_warm = False
        self.assertEqual(self.run_for(e, 1), 'warming')
        self.assertEqual(self.run_for(e, 84), 'ready')
        degraded = [(r['reason'], r['failures'], r['retry_s']) for r in self.audit_rows(e) if r.get('to') == 'degraded']
        self.assertEqual(degraded, [('WARM_FAILED', 1, 60), ('WARM_FAILED', 2, 300), ('WARM_FAILED', 3, 300)])
        self.assertEqual((e.warm.failures, e.pool.overlaps), (0, 0))

    def test_raise_timeout_and_flapping_do_not_bypass_backoff(self):
        e = self.warm_env()
        e.pool.raise_warm = True
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'degraded')
        for state in ('off', 'active', 'off', 'active'):
            e.power.set(state)
            e.warm.tick()
            self.clock.wait(10)
        self.assertEqual(e.pool.calls.count('begin_warm'), 1)
        self.run_for(e, 20)
        self.assertEqual(e.pool.calls.count('begin_warm'), 2)        # exactly at +60 s
        e = self.warm_env()
        e.pool.hang = True
        e.power.set('active')
        e.warm.tick()
        self.assertEqual(self.run_for(e, WARM_TIMEOUT_S - 1), 'warming')
        self.assertEqual(self.run_for(e, 1), 'degraded')
        self.assertIn(('warming', 'unloading', 'WARM_TIMEOUT'), self.transitions(e))

    def test_unverified_unload_blocks_every_new_warm(self):
        e = self.warm_env()
        e.pool.fail_warm, e.pool.unload_ok = True, False
        e.power.set('active')
        e.warm.tick()
        self.assertEqual(self.run_for(e, 84), 'degraded')
        self.run_for(e, 60)
        self.assertEqual(e.pool.calls, ['begin_warm', 'unload', 'unload'])   # retried cleanup, no warm
        e.pool.fail_warm, e.pool.unload_ok = False, True
        self.run_for(e, 299)
        self.assertEqual(e.pool.calls.count('begin_warm'), 1)
        self.assertEqual(self.run_for(e, 1), 'warming')
        self.assertEqual(e.pool.overlaps, 0)
        reasons = [r['reason'] for r in self.audit_rows(e) if r.get('to') == 'degraded']
        self.assertEqual(reasons, ['UNLOAD_UNVERIFIED', 'UNLOAD_UNVERIFIED'])

    def test_pool_failure_while_ready_waits_for_the_utterance(self):
        e = self.warm_env()
        self.warm_up(e)
        lease = e.agent.arm()
        e.pool.loaded = 'failed'
        self.assertEqual(e.warm.tick(), 'unloading')
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'unloading')                # a failure is never resumed
        self.assertEqual(e.pool.calls, ['begin_warm'])
        e.agent.cancel(lease)
        self.assertEqual(e.warm.tick(), 'degraded')
        self.assertEqual(self.run_for(e, 60), 'warming')

    def test_constructor_refuses_incomplete_pool_or_source(self):
        e = self.env('P2')
        with self.assertRaisesRegex(ValueError, 'WARM_POOL'):
            WarmController(object(), FakePowerSource(self.clock), clock=self.clock, audit=e.audit, killswitch=e.kill)
        with self.assertRaisesRegex(ValueError, 'WARM_SOURCE'):
            WarmController(FakePool(self.clock), object(), clock=self.clock, audit=e.audit, killswitch=e.kill)


class PressPathAndSafety(WarmCase):
    def test_press_while_warming_reaches_the_tv_as_a_fixed_toast_in_p4(self):
        e = self.warm_env('P4')
        e.transport.foreground_id = APP_IDS['netflix']
        e.power.set('active')
        e.warm.tick()
        with self.assertRaisesRegex(Refused, '^VOICE_WARMING_UP$'):
            e.agent.arm()
        self.assertIn('ssap://system.notifications/createToast', [u for u, _ in e.transport.requests])
        self.assertEqual(self.refusals(e), [('VOICE_WARMING_UP', 'warming', True)])
        self.assertTrue(all(len(TEMPLATES[c]) <= 60 for c in ('VOICE_WARMING_UP', 'VOICE_UNAVAILABLE')))

    def test_without_a_tv_surface_the_code_still_reaches_the_ingress(self):
        # P2, Home in front: no overlay and no toast. Only the ingress reply to the TV
        # client can carry it (rendering it on the mic track is an open item).
        e = self.warm_env('P2')
        e.power.set('active')
        e.warm.tick()
        self.assertEqual(self.press(e), 'VOICE_WARMING_UP')
        self.assertEqual(self.refusals(e), [('VOICE_WARMING_UP', 'warming', False)])

    def test_pause_wins_at_the_press_and_the_power_policy_keeps_running(self):
        e = self.warm_env()
        e.channel.open_session()
        e.kill.pause()
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'warming')            # Paused keeps the models warm
        self.assertEqual(self.press(e), 'AGENT_PAUSED')       # not VOICE_WARMING_UP
        self.assertEqual(self.run_for(e, 84), 'ready')
        self.assertEqual(self.press(e), 'AGENT_PAUSED')
        e.kill.resume()
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)   # instant: no re-warm on resume
        e.kill.pause()
        e.power.set('off')
        e.warm.tick()
        self.assertEqual(self.run_for(e, GRACE_S), 'unloaded')    # a pause does not pin the models
        self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])
        self.assertEqual(self.refusals(e), [])

    def test_unwritable_audit_trips_pause_but_the_transition_stands(self):
        e = self.warm_env()
        self.warm_up(e)
        os.chmod(e.audit_dir, 0o500)
        self.addCleanup(os.chmod, e.audit_dir, 0o700)
        e.power.set('off')
        self.assertEqual(e.warm.tick(), 'grace')
        self.assertTrue(e.kill.paused())
        self.assertEqual((e.kill.dir / 'PAUSED').read_text(), 'AUDIT_WRITE\n')
        self.assertEqual(self.run_for(e, GRACE_S), 'unloaded')

    def test_clock_going_backwards_is_refused(self):
        e = self.warm_env()
        self.warm_up(e)
        e.power.set('screen_off')
        self.assertEqual(e.warm.tick(), 'grace')
        mark = self.clock()
        self.clock.now = mark - 100
        for _ in range(3):
            self.assertEqual(e.warm.tick(), 'grace')
            self.clock.wait(1)
        with self.assertRaisesRegex(Refused, '^VOICE_UNAVAILABLE$'):
            e.warm.admit()
        self.assertEqual([r['reason'] for r in self.audit_rows(e) if r.get('reason') == 'CLOCK_REFUSED'],
                         ['CLOCK_REFUSED'])                          # one row per episode
        self.clock.now = mark + GRACE_S - 1
        self.assertEqual(e.warm.tick(), 'grace')                    # back above the mark: timers resume
        self.clock.now = mark + GRACE_S
        self.assertEqual(e.warm.tick(), 'unloaded')
        for bad in (float('nan'), float('inf'), None, True):
            pool = FakePool(self.clock)
            warm = WarmController(pool, FakePowerSource(self.clock, 'active'), clock=lambda bad=bad: bad,
                                  audit=e.audit, killswitch=e.kill)
            with self.subTest(clock=bad):
                self.assertEqual(warm.tick(), 'unloaded')
                self.assertEqual(pool.calls, [])
                with self.assertRaisesRegex(Refused, '^VOICE_UNAVAILABLE$'):
                    warm.admit()

    def test_audit_rows_are_enums_only_and_chain(self):
        e = self.warm_env(model=golden_model('P2'))
        e.channel.open_session(surface='browser_other')
        e.power.set('active')
        e.warm.tick()
        self.press(e)
        self.run_for(e, 84)
        self.say(e, 'search youtube for ' + SENTINEL)
        e.power.set('off')
        e.warm.tick()
        self.run_for(e, GRACE_S)
        rows = [r for r in self.audit_rows(e) if r.get('event') in ('warm', 'warm_power')]
        self.assertTrue(rows)
        self.assertTrue(all(set(r) <= ROW_KEYS for r in rows), rows)
        self.assertTrue(all(r.get('to', 'ready') in ('unloaded', 'warming', 'ready', 'grace', 'unloading',
                                                     'degraded') for r in rows))
        blob = b''.join(p.read_bytes() for p in self.tmp.rglob('*') if p.is_file()).lower()
        for needle in ('zebra', 'marmalade', 'quartz'):
            self.assertNotIn(needle.encode(), blob)
        self.assertEqual(AuditLog.verify(e.audit_dir), len(self.audit_rows(e)))


if __name__ == '__main__':
    unittest.main()
