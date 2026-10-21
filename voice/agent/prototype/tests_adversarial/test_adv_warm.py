"""Independent adversarial checks of the TV-aware warm policy (agent/warm.py).

Own harness (does not reuse tests/test_warm.py helpers). Areas: timer races at exact
deadlines, power-state spoofing through malformed source values, unload under in-flight
work, double warm, the unknown hold at exactly 30 min, pause/resume while in grace, and
audit leakage. Fakes only; socket.socket raises in every test (tests.support.Case)."""
from collections import namedtuple
from decimal import Decimal
import threading
import time
import unittest

from agent.agent import Agent, Lease
from agent.audit import AuditLog
from agent.errors import Outcome, Refused
from agent.golden import golden_model
from agent.warm import (BACKOFF_S, GRACE_S, INFLIGHT_MAX_S, POWER_STATES, STALE_S, STATES, TICK_S,
                        UNKNOWN_HOLD_S, WARM_TIMEOUT_S, FakePool, FakePowerSource, WarmController,
                        power_from_reply)
from tests_adversarial.adv_support import TOKEN, Case, call, leak_forms, scripted, tree_bytes

ACCEPTED_BACK = Outcome('accepted', 'BROWSER_HISTORY_ACCEPTED')
REASONS = frozenset(('POWER_ACTIVE', 'WARM_DONE', 'WARM_FAILED', 'WARM_TIMEOUT', 'POWER_INACTIVE',
                     'UNKNOWN_HOLD_EXPIRED', 'POOL_FAILED', 'GRACE_EXPIRED', 'UNLOAD_VERIFIED',
                     'UNLOAD_UNVERIFIED', 'RETRY', 'CLOCK_REFUSED', 'INFLIGHT_EXPIRED'))
POWERS = frozenset(POWER_STATES + ('unknown',))
CHAIN_KEYS = {'schema', 'ts', 'seq', 'prev'}


class Scripted:
    """A source whose next reading is whatever `value` is (or what it raises)."""
    def __init__(self, value=None):
        self.value, self.error = value, None

    def current(self):
        if self.error is not None:
            raise self.error
        return self.value() if callable(self.value) else self.value


class WarmAdv(Case):
    def build(self, phase='P2', *, model=None, warm_s=84, source=None, pool=None):
        e = self.env(phase, model=model or golden_model(phase))
        e.pool = pool if pool is not None else FakePool(self.clock, warm_s=warm_s)
        e.power = source if source is not None else FakePowerSource(self.clock)
        e.warm = WarmController(e.pool, e.power, clock=self.clock, audit=e.audit, killswitch=e.kill)
        e.agent = Agent(phase=phase, audit=e.audit, killswitch=e.kill, model=e.model, browser=e.browser,
                        tv=e.tv, feedback=e.feedback, clock=self.clock, warm=e.warm)
        return e

    def at(self, e, t):
        """Move the fake clock to absolute time t, then one service-loop tick."""
        self.clock.now = t
        return e.warm.tick()

    def run_for(self, e, seconds, step=1.0):
        end = self.clock() + seconds
        while self.clock() < end:
            self.clock.wait(min(step, end - self.clock()))
            e.warm.tick()
        return e.warm.state

    def loaded(self, e):
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'warming')
        self.assertEqual(self.run_for(e, 84), 'ready')

    def press(self, e):
        try:
            return e.agent.arm()
        except Refused as error:
            return error.code

    def rows(self, e):
        return self.audit_rows(e)

    def moves(self, e):
        return [(r['from'], r['to'], r['reason']) for r in self.rows(e) if r.get('event') == 'warm']


class TimerRaces(WarmAdv):
    def test_grace_deadline_is_exact_and_active_at_the_deadline_wins(self):
        for back_on in (False, True):
            with self.subTest(back_on=back_on):
                e = self.build()
                self.loaded(e)
                e.power.set('off')
                e.warm.tick()
                t0 = self.clock()
                self.assertEqual(self.at(e, t0 + GRACE_S - 0.001), 'grace')
                self.clock.now = t0 + GRACE_S
                if back_on:
                    e.power.set('active')                   # the TV is back in the deadline tick
                    self.assertEqual(e.warm.tick(), 'ready')
                    self.assertEqual(e.pool.calls, ['begin_warm'])
                    self.assertNotIn('unloading', [to for _, to, _ in self.moves(e)])
                else:
                    self.assertEqual(e.warm.tick(), 'unloaded')
                    self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])

    def test_press_after_the_deadline_but_before_the_tick_never_unloads_under_it(self):
        """[Observation] admit() trusts the state of the last tick, so a press in the
        <= TICK_S window after the grace deadline is still admitted. The safety property
        holds: the unload waits for that utterance and runs at the first tick after it."""
        e = self.build()
        e.channel.open_session()
        self.loaded(e)
        e.power.set('screen_off')
        e.warm.tick()
        t0 = self.clock()
        self.clock.now = t0 + GRACE_S + 0.5 * TICK_S             # the service loop has not ticked yet
        lease = e.agent.arm()
        self.assertIsInstance(lease, Lease)
        at_unload = []
        e.pool.on_unload = lambda: at_unload.append(len(e.warm.inflight))
        self.assertEqual(e.warm.tick(), 'unloading')
        self.assertEqual(self.press(e), 'VOICE_UNAVAILABLE')     # decided: no new press
        self.assertEqual(e.agent.handle_utterance(lease, 'go back'), ACCEPTED_BACK)
        self.assertEqual(e.pool.calls, ['begin_warm'])
        self.assertEqual(e.warm.tick(), 'unloaded')
        self.assertEqual(at_unload, [0])

    def test_warm_timeout_and_backoff_boundaries_are_exact(self):
        e = self.build()
        e.pool.hang = True
        e.power.set('active')
        e.warm.tick()
        t0 = self.clock()
        self.assertEqual(self.at(e, t0 + WARM_TIMEOUT_S - 0.001), 'warming')
        self.assertEqual(self.at(e, t0 + WARM_TIMEOUT_S), 'degraded')
        t1 = self.clock()
        e.pool.hang = False
        self.assertEqual(self.at(e, t1 + BACKOFF_S[0] - 0.001), 'degraded')
        self.assertEqual(e.pool.calls.count('begin_warm'), 1)
        self.assertEqual(self.at(e, t1 + BACKOFF_S[0]), 'warming')
        self.assertEqual(e.pool.overlaps, 0)

    def test_press_during_a_slow_unload_returns_at_once(self):
        """Regression: the press path used to wait on the controller lock for the
        whole of pool.unload() (seconds while workers close and cleanup is verified),
        with Agent.lock held. admit() now answers from the state alone."""
        e = self.build()
        self.loaded(e)
        e.power.set('off')
        e.warm.tick()
        self.clock.wait(GRACE_S)
        entered, gate = threading.Event(), threading.Event()
        e.pool.on_unload = lambda: (entered.set(), gate.wait(5))
        ticker = threading.Thread(target=e.warm.tick)
        result = {}
        presser = threading.Thread(target=lambda: result.setdefault('code', self.press(e)))
        self.addCleanup(lambda: (gate.set(), ticker.join(5), presser.join(5)))
        ticker.start()
        self.assertTrue(entered.wait(5))
        presser.start()
        presser.join(1.0)
        self.assertFalse(presser.is_alive(), 'the press waited for pool.unload()')
        self.assertEqual(result, {'code': 'VOICE_UNAVAILABLE'})
        self.assertEqual(e.warm.status()['state'], 'unloading')
        gate.set()
        ticker.join(5)
        self.assertEqual((e.warm.state, e.pool.calls), ('unloaded', ['begin_warm', 'unload']))

    def test_racing_presses_and_ticks_never_unload_under_a_ticket(self):
        clock = self.clock
        e = self.build()
        self.loaded(e)
        at_unload, errors, stop = [], [], threading.Event()

        def slow_unload():
            at_unload.append(len(e.warm.inflight))
            time.sleep(0.01)                                     # widen the race window
        e.pool.on_unload = slow_unload

        def presser():
            while not stop.is_set():
                try:
                    ticket = e.warm.admit()
                except Refused:
                    time.sleep(0.0005)
                    continue
                except Exception as error:                      # pragma: no cover - reported below
                    errors.append(repr(error))
                    return
                time.sleep(0.0005)
                if not e.warm.release(ticket):
                    errors.append('RELEASE_LOST')
        threads = [threading.Thread(target=presser) for _ in range(4)]
        self.addCleanup(lambda: (stop.set(), [t.join(5) for t in threads]))
        for thread in threads:
            thread.start()
        script = {0: 'off', 80: 'active', 100: 'off'}           # unload at 60, warm 80..97, unload at 160
        for step in range(170):
            if step in script:
                e.power.set(script[step])
            clock.wait(5)
            e.warm.tick()
            time.sleep(0.002)                                    # real time per tick >> a ticket's hold
        stop.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(at_unload, [0, 0])
        self.assertNotIn('INFLIGHT_EXPIRED', [reason for _, _, reason in self.moves(e)])
        self.assertEqual((e.pool.calls.count('begin_warm'), e.pool.overlaps), (2, 0))
        self.assertEqual((e.warm.state, e.warm.inflight), ('unloaded', {}))
        self.assertEqual(AuditLog.verify(e.audit_dir), len(self.rows(e)))


class SourceSpoofing(WarmAdv):
    def test_timestamps_no_float_can_hold_are_unknown_not_a_crash(self):
        """Regression: math.isfinite(10**400) raises OverflowError, which used to
        escape tick() (the service loop) on a malformed gateway timestamp."""
        for at in (10 ** 400, -10 ** 400, 2 ** 1024, -(2 ** 1024)):
            with self.subTest(at=str(at)[:12]):
                e = self.build(source=Scripted(('active', at)))
                self.assertEqual(e.warm.tick(), 'unloaded')
                self.assertEqual((e.warm.status()['power'], e.pool.calls), ('unknown', []))
        e = self.build()
        self.loaded(e)
        e.power = Scripted(('active', 10 ** 400))
        e.warm.source = e.power
        self.assertEqual(e.warm.tick(), 'ready')                 # loaded: the unknown hold starts
        self.assertEqual(e.warm.status()['power'], 'unknown')

    def test_clock_values_no_float_can_hold_are_refused_not_a_crash(self):
        e = self.build()
        warm = WarmController(e.pool, FakePowerSource(self.clock, 'active'), clock=lambda: 10 ** 400,
                              audit=e.audit, killswitch=e.kill)
        self.assertEqual(warm.tick(), 'unloaded')
        with self.assertRaisesRegex(Refused, '^VOICE_UNAVAILABLE$'):
            warm.admit()
        self.assertEqual(e.pool.calls, [])
        self.assertEqual([r['reason'] for r in self.rows(e) if r.get('event') == 'warm'], ['CLOCK_REFUSED'])

    def test_lookalike_subclass_and_odd_shape_readings_are_unknown(self):
        now = self.clock()

        class S(str):
            pass

        class T(tuple):
            pass

        class F(float):
            pass
        Reading = namedtuple('Reading', 'state at')
        values = [(S('active'), now), T(('active', now)), Reading('active', now), ('active', F(now)),
                  ('active ', now), (' active', now), ('Active', now), ('active\x00', now),
                  ('аctive', now), (b'active', now), ('active', str(now)), ('active', Decimal(now)),
                  ('active', complex(now)), ('active', float('-inf')), ('active', now + 1e-6),
                  ('active', now - STALE_S - 0.001), {'state': 'active', 'at': now}, ('active',), (),
                  ('active', now, 'extra'), ('active', None), (None, now), (1, now)]
        for value in values:
            with self.subTest(value=repr(value)):
                e = self.build(source=Scripted(value))
                self.assertEqual(e.warm.tick(), 'unloaded')
                self.assertEqual((e.warm.status()['power'], e.pool.calls), ('unknown', []))
        e = self.build(source=Scripted(('active', now - STALE_S)))  # exactly 30 s old: still fresh
        self.assertEqual(e.warm.tick(), 'warming')

    def test_any_source_exception_is_unknown(self):
        for error in (OSError(TOKEN), ValueError(TOKEN), KeyError(TOKEN), TimeoutError(), RecursionError(),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, TOKEN), ArithmeticError()):
            with self.subTest(error=type(error).__name__):
                source = Scripted(('active', self.clock()))
                source.error = error
                e = self.build(source=source)
                self.assertEqual(e.warm.tick(), 'unloaded')
                self.assertEqual(e.pool.calls, [])

    def test_power_reply_near_misses_are_unknown(self):
        class D(dict):
            pass
        good = {'returnValue': True, 'state': 'Active'}
        self.assertEqual(power_from_reply(good), 'active')
        self.assertEqual(power_from_reply(dict(good, processing='')), 'active')
        bad = [dict(good, processing=False), dict(good, processing=0), dict(good, processing=[]),
               dict(good, processing={}), dict(good, processing='Screen On'),
               {'returnValue': 'true', 'state': 'Active'}, {'returnValue': 1.0, 'state': 'Active'},
               {'returnValue': True, 'state': 'Active '}, {'returnValue': True, 'state': 'active standby'},
               {'returnValue': True, 'state': 'Power Off '}, {'returnValue': True, 'state': None},
               {'returnValue': True}, D(good), 'Active', ('Active',), {'returnValue': True, 'state': TOKEN}]

        class S(str):
            pass
        bad.append({'returnValue': True, 'state': S('Active')})
        for reply in bad:
            with self.subTest(reply=repr(reply)):
                self.assertEqual(power_from_reply(reply), 'unknown')


class InFlightUnload(WarmAdv):
    def test_hold_expiry_waits_for_the_utterance_and_active_cancels_the_unload(self):
        for back_on in (True, False):
            with self.subTest(back_on=back_on):
                e = self.build()
                self.loaded(e)
                e.power.broken = True
                e.warm.tick()
                t0 = self.clock()
                self.at(e, t0 + UNKNOWN_HOLD_S - 10)
                ticket = e.warm.admit()                          # an utterance starts 10 s before
                self.assertEqual(self.at(e, t0 + UNKNOWN_HOLD_S), 'unloading')
                self.assertEqual(self.run_for(e, 100, step=10), 'unloading')
                self.assertEqual(e.pool.calls, ['begin_warm'])
                if back_on:
                    e.power.broken = False
                    e.power.set('active')
                    self.assertEqual(e.warm.tick(), 'ready')     # hold expiry is resumable
                    self.assertTrue(e.warm.release(ticket))
                    self.assertEqual(self.run_for(e, 600, step=10), 'ready')
                    self.assertEqual(e.pool.calls, ['begin_warm'])
                else:
                    self.assertTrue(e.warm.release(ticket))
                    self.assertEqual(e.warm.tick(), 'unloaded')
                    self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])

    def test_forged_and_repeated_releases_cannot_drain_early(self):
        e = self.build()
        self.loaded(e)
        e.power.set('off')
        e.warm.tick()
        self.run_for(e, GRACE_S - 10)
        a, b = e.warm.admit(), e.warm.admit()                    # two utterances in the last 10 s
        self.assertEqual(self.run_for(e, 10), 'unloading')
        for forged in ('0' * 32, a.upper(), a[:-1], a + '0', None, 7, b'x', ''):
            self.assertFalse(e.warm.release(forged))
        self.assertTrue(e.warm.release(a))
        self.assertFalse(e.warm.release(a))                      # idempotent, never twice
        self.assertEqual(e.warm.tick(), 'unloading')
        self.assertEqual(e.pool.calls, ['begin_warm'])
        self.assertTrue(e.warm.release(b))
        self.assertEqual(e.warm.tick(), 'unloaded')

    def test_late_release_of_an_expired_ticket_is_harmless(self):
        e = self.build()
        self.loaded(e)
        e.power.set('off')
        e.warm.tick()
        self.run_for(e, 100)
        stale = e.warm.admit()                                    # abandoned 200 s before the deadline
        self.assertEqual(self.run_for(e, GRACE_S - 100), 'unloading')
        self.assertEqual(self.run_for(e, INFLIGHT_MAX_S - (GRACE_S - 100) - 1), 'unloading')
        self.assertEqual(self.run_for(e, 1), 'unloaded')
        self.loaded(e)
        fresh = e.warm.admit()
        self.assertFalse(e.warm.release(stale))
        self.assertEqual(list(e.warm.inflight), [fresh])
        self.assertEqual(e.warm.tick(), 'ready')

    def test_agent_failure_paths_always_release_the_ticket(self):
        """Every way an armed press can end returns its ticket: refused before the model,
        model failure, expiry, cancel, replacement."""
        def boom(transcript, *, timeout_s):
            raise RuntimeError('MODEL')
        e = self.build(model=scripted(call('control.back'), 'P2'))
        e.channel.open_session()
        self.loaded(e)
        e.model.interpret = boom
        self.assertEqual(e.agent.handle_utterance(e.agent.arm(), 'go back'), Outcome('refused', 'MODEL_UNAVAILABLE'))
        self.assertEqual(e.warm.inflight, {})
        lease = e.agent.arm()
        self.clock.wait(31)                                       # lease expired
        e.warm.tick()
        self.assertEqual(e.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'REQUEST_EXPIRED'))
        self.assertEqual(e.warm.inflight, {})
        self.assertEqual(e.agent.handle_utterance(e.agent.arm(), 'x' * 5000), Outcome('refused', 'TRANSCRIPT_SCOPE'))
        self.assertEqual(e.warm.inflight, {})
        e.agent.arm()
        self.assertTrue(e.agent.cancel())
        self.assertEqual(e.warm.inflight, {})


class DoubleWarm(WarmAdv):
    def test_concurrent_ticks_start_exactly_one_warm(self):
        clock = self.clock

        class SlowPool(FakePool):
            def begin_warm(self):
                time.sleep(0.02)                                 # runs outside the state lock
                super().begin_warm()
        e = self.build(pool=SlowPool(clock))
        e.power.set('active')
        barrier = threading.Barrier(8)
        codes = []

        def go():
            barrier.wait(5)
            e.warm.tick()
            try:
                e.warm.admit()
            except Refused as error:
                codes.append(error.code)
        threads = [threading.Thread(target=go) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual((e.pool.calls, e.warm.state), (['begin_warm'], 'warming'))
        self.assertEqual(codes, ['VOICE_WARMING_UP'] * 8)
        self.assertEqual([m for m in self.moves(e) if m[1] == 'warming'], [('unloaded', 'warming', 'POWER_ACTIVE')])

    def test_instant_pool_and_flapping_never_overlap(self):
        e = self.build(warm_s=0)                                  # ready in the same tick
        for state in ('active', 'off', 'active', 'off', 'active', 'screen_off', 'active', 'suspend') * 3:
            e.power.set(state)
            e.warm.tick()
            self.clock.wait(1)
        self.assertEqual((e.pool.calls, e.pool.overlaps), (['begin_warm'], 0))
        self.assertEqual(e.warm.state, 'grace')

    def test_backoff_expiry_while_off_waits_for_active_then_warms_at_once(self):
        e = self.build()
        e.pool.fail_warm = True
        e.power.set('active')
        e.warm.tick()
        self.assertEqual(self.run_for(e, 84), 'degraded')
        e.pool.fail_warm = False
        e.power.set('off')
        self.assertEqual(self.run_for(e, 600, step=10), 'degraded')   # no warm while off
        self.assertEqual(e.pool.calls.count('begin_warm'), 1)
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'warming')                 # Active again: start warming at once
        self.assertEqual((e.pool.calls.count('begin_warm'), e.pool.overlaps), (2, 0))


class UnknownEdge(WarmAdv):
    def test_hold_expires_at_exactly_30_min_from_the_first_unknown_tick(self):
        e = self.build()
        self.loaded(e)
        e.power.broken = True
        e.warm.tick()
        t0 = self.clock()
        self.assertEqual(self.at(e, t0 + UNKNOWN_HOLD_S - 0.001), 'ready')
        lease = self.press(e)
        self.assertIsInstance(lease, Lease)                         # still loaded: served
        e.agent.cancel(lease)
        self.assertEqual(self.at(e, t0 + UNKNOWN_HOLD_S), 'unloaded')
        self.assertIn(('ready', 'unloading', 'UNKNOWN_HOLD_EXPIRED'), self.moves(e))

    def test_active_at_exactly_30_min_keeps_the_models(self):
        e = self.build()
        self.loaded(e)
        e.power.broken = True
        e.warm.tick()
        t0 = self.clock()
        self.clock.now = t0 + UNKNOWN_HOLD_S
        e.power.broken = False
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'ready')
        self.assertEqual(e.pool.calls, ['begin_warm'])
        self.assertNotIn('unloading', [to for _, to, _ in self.moves(e)])

    def test_hold_counts_from_an_unknown_that_began_during_warming(self):
        e = self.build()
        e.power.set('active')
        e.warm.tick()
        self.clock.wait(10)
        e.power.broken = True
        e.warm.tick()
        t0 = self.clock()
        self.assertEqual(self.run_for(e, 74), 'ready')               # the warm completes under 'unknown'
        self.assertEqual(self.at(e, t0 + UNKNOWN_HOLD_S - 0.001), 'ready')
        self.assertEqual(self.at(e, t0 + UNKNOWN_HOLD_S), 'unloaded')

    def test_switching_between_kinds_of_unknown_does_not_restart_the_hold(self):
        clock = self.clock
        source = Scripted()
        e = self.build(source=source)
        source.value = lambda: ('active', clock())
        e.warm.tick()
        self.run_for(e, 84)
        self.assertEqual(e.warm.state, 'ready')
        last = clock()
        kinds = [lambda: None, lambda: ('active', last), lambda: ('garbage', clock()),
                 lambda: ('active', 10 ** 400), lambda: 'active']
        t0 = None
        for index in range(UNKNOWN_HOLD_S // 100):
            source.value = kinds[index % len(kinds)]
            clock.wait(1 if t0 is None else 100)
            e.warm.tick()
            t0 = t0 if t0 is not None else clock()
        self.assertEqual(e.warm.state, 'ready')
        self.assertEqual(self.at(e, t0 + UNKNOWN_HOLD_S - 0.001), 'ready')
        self.assertEqual(self.at(e, t0 + UNKNOWN_HOLD_S), 'unloaded')

    def test_stalled_source_envelope_is_stale_plus_hold_plus_one_tick(self):
        """[Observation] 'up to 30 min' runs from when the source is detected as
        unavailable: at most STALE_S + one tick after the TV last confirmed Active."""
        e = self.build()
        self.loaded(e)
        e.power.live = False
        last = e.power.observed_at
        self.run_for(e, STALE_S + TICK_S + UNKNOWN_HOLD_S - 1)
        self.assertEqual(e.warm.state, 'ready')
        self.assertEqual(self.run_for(e, 1), 'unloaded')
        self.assertEqual(self.clock() - last, STALE_S + TICK_S + UNKNOWN_HOLD_S)

    def test_unknown_never_starts_a_warm_or_a_retry(self):
        e = self.build()
        e.pool.fail_warm = True
        e.power.set('active')
        e.warm.tick()
        self.assertEqual(self.run_for(e, 84), 'degraded')
        e.power.broken = True
        self.assertEqual(self.run_for(e, 2 * UNKNOWN_HOLD_S, step=30), 'degraded')
        self.assertEqual(e.pool.calls.count('begin_warm'), 1)
        e2 = self.build()
        e2.power.broken = True
        self.assertEqual(self.run_for(e2, 2 * UNKNOWN_HOLD_S, step=30), 'unloaded')
        self.assertEqual(e2.pool.calls, [])


class PauseInGrace(WarmAdv):
    def test_pause_in_grace_neither_extends_nor_shortens_it(self):
        e = self.build()
        e.channel.open_session()
        self.loaded(e)
        e.power.set('off')
        e.warm.tick()
        t0 = self.clock()
        self.at(e, t0 + 60)
        e.kill.pause()
        self.assertEqual(self.press(e), 'AGENT_PAUSED')
        self.assertEqual(e.warm.inflight, {})                       # a paused press holds nothing
        self.assertEqual(self.at(e, t0 + 200), 'grace')
        e.kill.resume()
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)      # instant: still loaded
        e.kill.pause()
        self.assertEqual(self.at(e, t0 + GRACE_S - 0.001), 'grace')
        self.assertEqual(self.at(e, t0 + GRACE_S), 'unloaded')
        self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])
        e.kill.resume()
        self.assertEqual(self.press(e), 'VOICE_UNAVAILABLE')
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'warming')
        self.assertEqual(self.press(e), 'VOICE_WARMING_UP')

    def test_paused_tv_back_in_grace_keeps_the_models_and_resume_is_instant(self):
        e = self.build()
        e.channel.open_session()
        self.loaded(e)
        e.power.set('active_standby')
        e.warm.tick()
        self.run_for(e, 100)
        e.kill.pause()
        e.power.set('active')
        self.assertEqual(e.warm.tick(), 'ready')
        self.assertEqual(self.run_for(e, 3 * GRACE_S, step=10), 'ready')
        e.kill.resume()
        self.assertEqual(self.say(e, 'go back'), ACCEPTED_BACK)
        self.assertEqual(e.pool.calls, ['begin_warm'])

    def test_pause_with_an_armed_lease_in_grace_releases_it_when_run(self):
        e = self.build()
        e.channel.open_session()
        self.loaded(e)
        e.power.set('off')
        e.warm.tick()
        self.run_for(e, GRACE_S - 5)
        lease = e.agent.arm()
        e.kill.pause()
        self.assertEqual(self.run_for(e, 5), 'unloading')           # waits for the armed lease
        self.assertEqual(e.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'AGENT_PAUSED'))
        self.assertEqual(e.warm.inflight, {})
        self.assertEqual(e.warm.tick(), 'unloaded')
        self.assertEqual(e.pool.calls, ['begin_warm', 'unload'])


class WarmAvailability(WarmAdv):
    def test_hammering_the_button_during_warm_up_trips_pause(self):
        """[Observation] A refused press still counts against the utterance
        rate (12/min), and 3 rate hits in 10 min trip Paused, which only the owner
        clears. 15 presses inside one 84 s warm-up are enough; in P2 with Home in front
        none of the 12 'warming up' refusals is visible on the TV."""
        e = self.build()
        e.power.set('active')
        e.warm.tick()
        codes = []
        for _ in range(15):
            codes.append(self.press(e))
            self.clock.wait(3)
            e.warm.tick()
        self.assertEqual(codes, ['VOICE_WARMING_UP'] * 12 + ['RATE_LIMITED'] * 3)
        self.assertTrue(e.kill.paused())
        self.assertEqual((e.kill.dir / 'PAUSED').read_text(), 'RATE_TRIPS\n')
        self.assertEqual(self.run_for(e, 84), 'ready')                # the models warm regardless
        self.assertEqual(self.press(e), 'AGENT_PAUSED')
        self.assertEqual({r['feedback']['shown'] for r in self.rows(e) if r.get('event') == 'press_refused'},
                         {False})                                     # Home in front: nothing on the TV


class WarmAuditLeakage(WarmAdv):
    def check_rows(self, e):
        rows = self.rows(e)
        for row in rows:
            event = row.get('event')
            if event == 'warm':
                self.assertLessEqual(set(row), CHAIN_KEYS | {'event', 'from', 'to', 'reason', 'power',
                                                             'failures', 'retry_s', 'count'}, row)
                self.assertIn(row['from'], STATES)
                self.assertIn(row['to'], STATES)
                self.assertIn(row['reason'], REASONS)
                self.assertIn(row['power'], POWERS)
                for key in ('failures', 'retry_s', 'count'):
                    if key in row:
                        self.assertIs(type(row[key]), int)
                        self.assertTrue(0 < row[key] <= 9999, row)
            elif event == 'warm_power':
                self.assertEqual(set(row), CHAIN_KEYS | {'event', 'power', 'state'}, row)
                self.assertIn(row['power'], POWERS)
                self.assertIn(row['state'], STATES)
            elif event == 'press_refused':
                self.assertEqual(set(row), CHAIN_KEYS | {'event', 'phase', 'code', 'warm', 'feedback'}, row)
                self.assertIn(row['code'], ('VOICE_WARMING_UP', 'VOICE_UNAVAILABLE'))
                self.assertIn(row['warm'], STATES)
        self.assertEqual(AuditLog.verify(e.audit_dir), len(rows))
        return rows

    def test_hostile_source_and_pool_values_never_reach_disk(self):
        clock = self.clock

        class HostilePool(FakePool):
            poison = None

            def begin_warm(self):
                if self.poison == 'warm':
                    raise RuntimeError(TOKEN)
                super().begin_warm()

            def poll(self):
                return TOKEN if self.poison == 'poll' else super().poll()

            def unload(self):
                super().unload()
                if self.poison == 'unload':
                    raise OSError(TOKEN)
                return TOKEN if self.poison == 'unload-value' else True
        source, pool = Scripted(), HostilePool(clock)
        e = self.build(source=source, pool=pool)
        for value in ((TOKEN, clock()), ('active', TOKEN), TOKEN, {TOKEN: 'active'}):
            source.value = value
            e.warm.tick()
            clock.wait(1)
        source.error = OSError(TOKEN)
        e.warm.tick()
        source.error = None
        power = {'state': 'active'}
        source.value = lambda: (power['state'], clock())
        for poison, state, seconds in (('warm', 'active', 400), ('poll', 'active', 400), (None, 'active', 800),
                                       ('unload', 'off', 400), ('unload-value', 'off', 400),
                                       (None, 'active', 800)):
            pool.poison, power['state'] = poison, state
            for second in range(seconds):
                clock.wait(1)
                e.warm.tick()
                if second % 30 == 0:                             # well inside the utterance rate
                    try:
                        e.agent.handle_utterance(e.agent.arm(), TOKEN)
                    except Refused:
                        pass
        self.assertEqual((e.warm.state, e.kill.paused()), ('ready', False))
        e.channel.open_session(surface='browser_other')
        e.model = e.agent.model = scripted(call('browser.youtube_search', query=TOKEN), 'P2')
        e.agent.handle_utterance(e.agent.arm(), TOKEN)
        reasons = {r['reason'] for r in self.check_rows(e) if r.get('event') == 'warm'}
        self.assertLessEqual({'WARM_FAILED', 'UNLOAD_UNVERIFIED', 'RETRY', 'WARM_DONE', 'GRACE_EXPIRED'}, reasons)
        blob = tree_bytes(self.tmp)
        for form in leak_forms(TOKEN):
            self.assertNotIn(form, blob)
        self.assertLessEqual(set(e.warm.status()), {'state', 'power', 'failures', 'inflight'})

    def test_every_row_kind_is_closed_and_tickets_never_reach_disk(self):
        e = self.build()
        tickets = []
        e.power.set('active')
        e.warm.tick()
        self.press(e)                                               # VOICE_WARMING_UP
        self.run_for(e, 84)
        tickets.append(e.warm.admit())                              # abandoned: INFLIGHT_EXPIRED
        e.power.set('off')
        e.warm.tick()
        self.run_for(e, INFLIGHT_MAX_S)
        self.press(e)                                               # VOICE_UNAVAILABLE
        self.clock.now -= 5
        e.warm.tick()                                               # CLOCK_REFUSED
        self.clock.now += 10
        e.pool.hang = True
        e.power.set('active')
        e.warm.tick()
        self.run_for(e, WARM_TIMEOUT_S)                             # WARM_TIMEOUT -> degraded
        e.pool.hang = False
        self.run_for(e, BACKOFF_S[0] + 84)                          # RETRY -> ready
        tickets.append(e.warm.admit())
        e.pool.loaded = 'failed'
        e.warm.tick()                                               # POOL_FAILED, waits for the ticket
        e.warm.release(tickets[-1])
        e.warm.tick()
        e.power.broken = True
        self.run_for(e, BACKOFF_S[1] + 84 + UNKNOWN_HOLD_S, step=10)
        reasons = {r['reason'] for r in self.check_rows(e) if r.get('event') == 'warm'}
        self.assertLessEqual({'POWER_ACTIVE', 'WARM_DONE', 'POWER_INACTIVE', 'GRACE_EXPIRED', 'UNLOAD_VERIFIED',
                              'INFLIGHT_EXPIRED', 'CLOCK_REFUSED', 'WARM_TIMEOUT', 'RETRY', 'POOL_FAILED'}, reasons)
        blob = tree_bytes(self.tmp)
        for ticket in tickets:
            self.assertNotIn(ticket.encode(), blob)


if __name__ == '__main__':
    unittest.main()
