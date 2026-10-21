"""Offline tests for voice/runtime/pool.py. Fake workers with fake .process objects by
default, plus bounded local subprocess cancellation checks: no model, no socket, no TV,
no Spark.
"""
import subprocess
import sys
import threading
import time
import unittest

try:
    from . import pool as pool_mod
except ImportError:
    import pool as pool_mod

warm = pool_mod.warm
Refused = pool_mod.Refused
ArmEvidencePowerSource = pool_mod.ArmEvidencePowerSource
WarmHook = pool_mod.WarmHook
WorkerPool = pool_mod.WorkerPool
HARD_AGE_S = pool_mod.HARD_AGE_S
SHUTDOWN_BUDGET_S = pool_mod.SHUTDOWN_BUDGET_S


class FakeClock:
    """Monotonic test clock; thread-safe advance."""
    def __init__(self, start=1000.0):
        self.now = float(start)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds
            return self.now


class ListAudit(list):
    """audit.append(row) sink."""


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class FakeWorker:
    """Shaped like the v4 ModelWorker: start(timeout_s), close() -> bool, .process,
    .sealed, .attempt, .counter, .created. start() can block on `gate`."""
    def __init__(self, clock, *, fail_start=False, close_ok=True, gate=None,
                 cancel_unblocks=False):
        self.clock = clock
        self.fail_start, self.close_ok, self.gate = fail_start, close_ok, gate
        self.cancel_unblocks = cancel_unblocks
        self.entered = threading.Event()
        self.process = self.sealed = self.attempt = self.created = None
        self.counter = 0
        self.start_calls = self.close_calls = self.cancel_calls = 0
        self.timeout_s = None

    def cancel_start(self, *, escalate=False):
        self.cancel_calls += 1
        if self.cancel_unblocks and self.gate is not None:
            self.gate.set()

    def start(self, *, timeout_s=120):
        self.start_calls += 1
        self.timeout_s = timeout_s
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail_start:
            raise Refused('MODEL_UNAVAILABLE')
        self.process = FakeProcess()
        self.sealed, self.attempt = {'fake': True}, 'a' * 32
        self.counter, self.created = 0, self.clock()

    def close(self):
        self.close_calls += 1
        self.sealed = None
        if self.close_ok:
            self.process = None
        return self.close_ok

    @property
    def loaded(self):
        return self.process is not None


class SubprocessWarmWorker:
    """Worker whose start() spawns a real child process in a new session and waits for
    its stdout EOF, mirroring ModelWorker.start() blocking on _read_frame()."""
    def __init__(self, clock):
        self.clock = clock
        self.entered = threading.Event()
        self.process = self.sealed = self.created = None
        self.counter = 0
        self.close_calls = 0

    def start(self, *, timeout_s=120):
        self.process = subprocess.Popen(
            [sys.executable, '-c', 'import sys, time; sys.stdout.flush(); time.sleep(60)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
        self.entered.set()
        raw = self.process.stdout.read(4)
        if len(raw) < 4:
            self.close()
            raise Refused('MODEL_UNAVAILABLE')
        self.sealed = {'fake': True}
        self.created = self.clock()

    def close(self):
        self.close_calls += 1
        self.sealed = None
        proc = self.process
        if proc is None:
            return True
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        done = proc.poll() is not None
        if done:
            self.process = None
        return done


class Factory:
    def __init__(self, clock):
        self.clock = clock
        self.plan = []       # kwargs for the next workers, in order
        self.made = []

    def __call__(self):
        worker = FakeWorker(self.clock, **(self.plan.pop(0) if self.plan else {}))
        self.made.append(worker)
        return worker


class StepSpawn:
    """Deferred spawner: the test decides when the background warm runs."""
    def __init__(self):
        self.pending = []

    def __call__(self, target):
        self.pending.append(target)

    def run(self):
        while self.pending:
            self.pending.pop(0)()


class DropSsap(warm.FakePowerSource):
    """warm.FakePowerSource (a live subscription) plus drop(): current() then returns
    None, like a gateway whose SSAP socket closed without a final state."""
    def __init__(self, clock):
        super().__init__(clock)
        self.dropped = False

    def drop(self):
        self.dropped = True

    def current(self):
        return None if self.dropped else super().current()


def sync_spawn(target):
    target()


def reasons(audit):
    return [row['reason'] for row in audit if row.get('event') == 'warm']


class PoolBase(unittest.TestCase):
    def make(self, spawn=sync_spawn, **kwargs):
        self.clock = FakeClock()
        self.factory = Factory(self.clock)
        self.pool = WorkerPool(self.factory, clock=self.clock, spawn_thread=spawn, **kwargs)
        return self.pool

    def make_hook(self, spawn=sync_spawn, ssap=False, **kwargs):
        pool = self.make(spawn, **kwargs)
        self.audit = ListAudit()
        self.ssap = DropSsap(self.clock) if ssap else None
        self.source = ArmEvidencePowerSource(self.clock, ssap=self.ssap)
        self.hook = WarmHook(pool, self.source, clock=self.clock, audit=self.audit)
        return self.hook

    def ready_pool(self, **kwargs):
        pool = self.make(**kwargs)
        pool.begin_warm()
        self.assertEqual(pool.poll(), 'ready')
        return pool, self.factory.made[0]

    def all_closed(self):
        return all(not w.loaded for w in self.factory.made)


class LifecycleTests(PoolBase):
    def test_hook_unloaded_warming_ready_with_arm_evidence(self):
        spawn = StepSpawn()
        hook = self.make_hook(spawn)
        self.assertEqual(hook.tick(), 'unloaded')          # no evidence: 'unknown' never warms
        with self.assertRaises(Refused) as caught:
            hook.admit()
        self.assertEqual(str(caught.exception), 'VOICE_UNAVAILABLE')
        hook.note_tv_activity()                            # authenticated arm -> 'active'
        with self.assertRaises(Refused) as caught:
            hook.admit()
        self.assertEqual(str(caught.exception), 'VOICE_WARMING_UP')
        self.assertEqual(caught.exception.code, 'VOICE_WARMING_UP')
        self.assertEqual(hook.state, 'warming')
        self.assertEqual(hook.tick(), 'warming')
        self.assertEqual(self.pool.poll(), 'warming')
        with self.assertRaises(Refused) as caught:
            hook.lease()
        self.assertEqual(str(caught.exception), 'VOICE_UNAVAILABLE')
        self.assertEqual(hook.status()['pool']['state'], 'warming')
        spawn.run()                                        # the background warm finishes
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')
        self.assertEqual(reasons(self.audit), ['POWER_ACTIVE', 'WARM_DONE'])
        self.assertEqual(self.factory.made[0].timeout_s, 120)
        ticket = hook.admit()
        worker = hook.lease()
        self.assertIs(worker, self.factory.made[0])
        self.assertTrue(hook.give_back(worker))
        self.assertTrue(hook.release(ticket))
        self.assertFalse(hook.release(ticket))
        status = hook.status()
        self.assertEqual(status['warm'], {'state': 'ready', 'power': 'active', 'failures': 0, 'inflight': 0})
        self.assertEqual(status['pool'], {'state': 'ready', 'leased': False, 'rotations': 0, 'cleanupFailures': 0})

    def test_begin_warm_busy_and_start_failure(self):
        spawn = StepSpawn()
        pool = self.make(spawn)
        self.assertEqual(pool.poll(), 'failed')            # empty pool is not ready
        pool.begin_warm()
        with self.assertRaises(ValueError) as caught:
            pool.begin_warm()                              # never two warms
        self.assertEqual(str(caught.exception), 'POOL_BUSY')
        spawn.run()
        self.assertEqual(pool.poll(), 'ready')
        with self.assertRaises(ValueError):
            pool.begin_warm()                              # active worker exists
        self.assertTrue(pool.unload())
        self.factory.plan.append({'fail_start': True})
        pool.begin_warm()
        spawn.run()
        self.assertEqual(pool.poll(), 'failed')
        self.assertEqual(self.factory.made[1].close_calls, 1)   # cleanup verified after a failed start
        with self.assertRaises(Refused):
            pool.lease()
        self.assertTrue(pool.unload())
        self.assertEqual(pool.summary()['state'], 'empty')
        pool.begin_warm()                                  # clean again: a new warm may start
        spawn.run()
        self.assertEqual(pool.poll(), 'ready')

    def test_factory_raising_is_failed(self):
        clock = FakeClock()
        def factory():
            raise OSError('nope')
        pool = WorkerPool(factory, clock=clock, spawn_thread=sync_spawn)
        pool.begin_warm()
        self.assertEqual(pool.poll(), 'failed')
        self.assertTrue(pool.unload())

    def test_spawn_failure_raises_and_counts_failed(self):
        def broken(_target):
            raise RuntimeError('no threads')
        pool = self.make(broken)
        with self.assertRaises(RuntimeError):
            pool.begin_warm()
        self.assertEqual(pool.poll(), 'failed')
        self.assertTrue(pool.unload())

    def test_config_validation(self):
        clock = FakeClock()
        for kwargs in ({'rotate_after_s': 1800}, {'rotate_after_s': 0}, {'rotate_after_calls': 256},
                       {'rotate_after_calls': True}, {'join_s': -1},
                       {'join_s': SHUTDOWN_BUDGET_S + 1}):
            with self.assertRaises(ValueError):
                WorkerPool(lambda: None, clock=clock, **kwargs)


class LeaseTests(PoolBase):
    def test_lease_give_back_and_busy(self):
        pool, worker = self.ready_pool()
        leased = pool.lease()
        self.assertIs(leased, worker)
        self.assertTrue(pool.summary()['leased'])
        with self.assertRaises(Refused) as caught:
            pool.lease()
        self.assertEqual(str(caught.exception), 'VOICE_BUSY')
        self.assertFalse(pool.give_back(FakeWorker(self.clock)))   # not the leased one
        self.assertFalse(pool.give_back(None))
        self.assertTrue(pool.give_back(leased))
        self.assertFalse(pool.give_back(leased))                    # already returned
        self.assertIs(pool.lease(), worker)                         # retained, same worker
        self.assertEqual(worker.close_calls, 0)

    def test_hard_bound_refuses_lease(self):
        pool, worker = self.ready_pool()
        pool.standby_failed = True
        worker.counter = 254
        self.assertIs(pool.lease(), worker)
        self.assertTrue(pool.give_back(worker))
        worker.counter = 255                             # needs 2 calls for ASR + intent/agent
        self.assertEqual(pool.poll(), 'failed')
        with self.assertRaises(Refused) as caught:
            pool.lease()
        self.assertEqual(str(caught.exception), 'VOICE_UNAVAILABLE')
        worker.counter = 0
        self.clock.advance(1769)
        self.assertEqual(pool.poll(), 'ready')
        self.assertIs(pool.lease(), worker)
        self.assertTrue(pool.give_back(worker))
        self.clock.advance(1)                            # age 1770: < 30s headroom before 1800s bound
        self.assertEqual(pool.poll(), 'failed')
        with self.assertRaises(Refused) as caught:
            pool.lease()
        self.assertEqual(str(caught.exception), 'VOICE_UNAVAILABLE')

    def test_unhealthy_give_back_fails_pool_and_controller_degrades(self):
        hook = self.make_hook()
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')
        ticket = hook.admit()
        worker = hook.lease()
        worker.process.code = -9                         # child died mid-utterance
        self.assertTrue(hook.give_back(worker))          # the close() result
        self.assertEqual(worker.close_calls, 1)
        self.assertEqual(self.pool.poll(), 'failed')
        with self.assertRaises(Refused):
            hook.lease()
        self.assertEqual(hook.tick(), 'unloading')       # never unload under a ticket
        hook.release(ticket)
        self.assertEqual(hook.tick(), 'degraded')
        self.assertEqual(reasons(self.audit)[-2:], ['POOL_FAILED', 'POOL_FAILED'])
        self.assertEqual(self.pool.summary()['state'], 'empty')
        with self.assertRaises(Refused) as caught:
            hook.admit()
        self.assertEqual(str(caught.exception), 'VOICE_UNAVAILABLE')
        # Backoff 60 s, then a fresh Active reading re-warms.
        self.clock.advance(60)
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')
        self.assertEqual(len(self.factory.made), 2)

    def test_unsealed_give_back_close_failure(self):
        pool, worker = self.ready_pool()
        leased = pool.lease()
        leased.sealed = None
        leased.close_ok = False
        self.assertFalse(pool.give_back(leased))
        self.assertEqual(pool.summary(), {'state': 'failed', 'leased': False, 'rotations': 0,
                                          'cleanupFailures': 1})
        self.assertFalse(pool.unload())                  # retried, still unclean
        with self.assertRaises(ValueError):
            pool.begin_warm()                            # never warm over unverified cleanup
        leased.close_ok = True
        self.assertTrue(pool.unload())
        self.assertTrue(pool.unload())                   # idempotent


class RotationTests(PoolBase):
    def test_rotation_by_age_keeps_ready_and_swaps_only_when_not_leased(self):
        spawn = StepSpawn()
        pool = self.make(spawn)
        pool.begin_warm()
        spawn.run()
        old = self.factory.made[0]
        self.clock.advance(1499)
        self.assertEqual(pool.poll(), 'ready')
        self.assertEqual(len(self.factory.made), 1)
        self.clock.advance(1)                            # age 1500
        self.assertEqual(pool.poll(), 'ready')           # standby warm started in the background
        self.assertEqual(len(spawn.pending), 1)
        self.assertEqual(pool.poll(), 'ready')
        self.assertEqual(len(spawn.pending), 1)          # never two warms
        with self.assertRaises(ValueError):
            pool.begin_warm()
        self.assertEqual(pool.summary()['state'], 'rotating')
        leased = pool.lease()
        self.assertIs(leased, old)
        spawn.run()                                      # standby sealed and ready
        new = self.factory.made[1]
        self.assertEqual(pool.poll(), 'ready')
        self.assertTrue(old.loaded)                      # leased: no swap, old not closed
        self.assertEqual(pool.summary()['rotations'], 0)
        self.assertTrue(pool.give_back(leased))
        self.assertEqual(pool.poll(), 'ready')           # swap + close the old one
        self.assertFalse(old.loaded)
        self.assertEqual(old.close_calls, 1)
        self.assertIs(pool.lease(), new)
        self.assertEqual(pool.summary(), {'state': 'ready', 'leased': True, 'rotations': 1,
                                          'cleanupFailures': 0})

    def test_rotation_not_started_while_leased(self):
        pool, old = self.ready_pool()
        leased = pool.lease()
        self.clock.advance(1500)
        self.assertEqual(pool.poll(), 'ready')
        self.assertEqual(len(self.factory.made), 1)
        pool.give_back(leased)
        self.assertEqual(pool.poll(), 'ready')
        self.assertEqual(len(self.factory.made), 2)
        self.assertEqual(pool.summary()['rotations'], 1)

    def test_rotation_by_calls_and_lease_takes_ready_standby(self):
        spawn = StepSpawn()
        pool = self.make(spawn)
        pool.begin_warm()
        spawn.run()
        old = self.factory.made[0]
        old.counter = 239
        self.assertEqual(pool.poll(), 'ready')
        self.assertEqual(spawn.pending, [])
        old.counter = 240
        self.assertEqual(pool.poll(), 'ready')
        spawn.run()
        new = self.factory.made[1]
        self.assertIs(pool.lease(), new)                 # new utterances go to the standby
        self.assertTrue(old.loaded)                      # closed by the next poll
        self.assertEqual(pool.poll(), 'ready')
        self.assertFalse(old.loaded)
        self.assertTrue(pool.give_back(new))

    def test_standby_failure_serves_until_hard_bound(self):
        pool, old = self.ready_pool()
        self.factory.plan.append({'fail_start': True})
        self.clock.advance(1500)
        self.assertEqual(pool.poll(), 'ready')           # standby failed, old still serves
        self.assertEqual(self.factory.made[1].close_calls, 1)
        self.clock.advance(200)
        self.assertEqual(pool.poll(), 'ready')
        self.assertEqual(len(self.factory.made), 2)      # no second standby for this worker
        self.clock.advance(HARD_AGE_S - 1700)            # age 1800: hard bound
        self.assertEqual(pool.poll(), 'failed')
        self.assertTrue(pool.unload())
        self.assertTrue(self.all_closed())

    def test_old_worker_close_failure_makes_poll_failed(self):
        pool, old = self.ready_pool()
        old.close_ok = False
        self.clock.advance(1500)
        self.assertEqual(pool.poll(), 'failed')
        self.assertEqual(pool.summary()['cleanupFailures'], 1)
        self.assertFalse(pool.unload())
        old.close_ok = True
        self.assertTrue(pool.unload())
        self.assertTrue(self.all_closed())


class UnloadTests(PoolBase):
    def test_unload_closes_active_and_cancels_inflight_warm(self):
        spawn = StepSpawn()
        pool = self.make(spawn, join_s=0)
        pool.begin_warm()
        spawn.run()
        active = self.factory.made[0]
        self.clock.advance(1500)
        pool.poll()                                      # standby warm in flight
        self.assertEqual(len(spawn.pending), 1)
        self.assertFalse(pool.unload())                  # warm still running (join_s=0)
        self.assertFalse(active.loaded)
        spawn.run()                                      # already cancelled: skips start(), closes worker
        standby = self.factory.made[1]
        self.assertEqual(standby.start_calls, 0)
        self.assertEqual(standby.close_calls, 1)
        self.assertFalse(standby.loaded)
        self.assertTrue(pool.unload())
        self.assertEqual(pool.summary()['state'], 'empty')
        self.assertTrue(pool.unload())                   # idempotent

    def test_delayed_factory_skips_start_when_already_cancelled(self):
        clock = FakeClock()
        factory_entered, release_factory = threading.Event(), threading.Event()
        worker = SubprocessWarmWorker(clock)
        def factory():
            factory_entered.set()
            if not release_factory.wait(2):
                raise RuntimeError('bounded factory gate')
            return worker
        pool = WorkerPool(factory, clock=clock)
        pool.begin_warm()
        self.assertTrue(factory_entered.wait(1))
        job = pool._warm
        self.assertFalse(pool.unload(timeout_s=0.02))
        release_factory.set()
        self.assertTrue(job.finished.wait(2))
        self.assertIsNone(worker.process)
        self.assertGreaterEqual(worker.close_calls, 1)
        self.assertTrue(pool.unload(timeout_s=1))

    def test_slow_worker_close_exceeding_deadline_fails_and_bounds_unload(self):
        pool, worker = self.ready_pool()
        entered_close = threading.Event()
        release_close = threading.Event()
        active_closes = [0]
        overlapping = [False]
        orig_close = worker.close
        def slow_close():
            active_closes[0] += 1
            if active_closes[0] > 1:
                overlapping[0] = True
            entered_close.set()
            try:
                release_close.wait(2)
                return orig_close()
            finally:
                active_closes[0] -= 1
        worker.close = slow_close
        begin = time.monotonic()
        self.assertFalse(pool.unload(timeout_s=0.05))
        self.assertLess(time.monotonic() - begin, 0.5)
        self.assertTrue(entered_close.is_set())
        self.assertFalse(pool.unload(timeout_s=0.02))        # reuses pending close, still running
        self.assertFalse(overlapping[0])
        self.assertEqual(pool.summary()['state'], 'failed')
        release_close.set()
        self.assertTrue(pool.unload(timeout_s=1))
        self.assertFalse(overlapping[0])
        self.assertEqual(worker.close_calls, 1)
        self.assertEqual(pool.summary()['state'], 'empty')

    def test_zero_budget_unload_bounds_slow_worker_close(self):
        pool, worker = self.ready_pool(join_s=0)
        entered_close = threading.Event()
        release_close = threading.Event()
        orig_close = worker.close
        def slow_close():
            entered_close.set()
            release_close.wait(2)
            return orig_close()
        worker.close = slow_close
        begin = time.monotonic()
        self.assertFalse(pool.unload())
        self.assertLess(time.monotonic() - begin, 0.5)
        self.assertTrue(entered_close.is_set())
        self.assertEqual(pool.summary()['state'], 'failed')
        release_close.set()
        self.assertTrue(pool.unload(timeout_s=1))
        self.assertEqual(worker.close_calls, 1)
        self.assertEqual(pool.summary()['state'], 'empty')

    def test_abandoned_warm_reaper_does_not_cancel_replacement_warm(self):
        clock = FakeClock()
        factory = Factory(clock)
        gate1 = threading.Event()
        gate2 = threading.Event()
        factory.plan.append({'gate': gate1, 'cancel_unblocks': False})
        factory.plan.append({'gate': gate2, 'cancel_unblocks': True})
        pool = WorkerPool(factory, clock=clock, join_s=5)
        pool.begin_warm()
        self.assertTrue(_wait(lambda: factory.made and factory.made[0].entered.is_set()))
        job1 = pool._warm
        self.assertFalse(pool.unload(timeout_s=0.02))    # spawns _reap_abandoned_warm(job1)
        gate1.set()
        self.assertTrue(job1.finished.wait(2))
        self.assertTrue(pool.unload(timeout_s=1))
        pool.begin_warm()
        self.assertTrue(_wait(lambda: len(factory.made) >= 2 and factory.made[1].entered.is_set()))
        job2 = pool._warm
        time.sleep(0.08)                                 # let any lingering job1 reaper loop
        self.assertFalse(job2.cancelled)
        self.assertEqual(factory.made[1].cancel_calls, 0)
        gate2.set()
        self.assertTrue(_wait(lambda: pool.poll() == 'ready'))
        self.assertTrue(pool.unload(timeout_s=1))

    def test_close_thread_exhaustion_retains_worker_for_retry(self):
        from unittest import mock
        pool, worker = self.ready_pool()
        with mock.patch.object(pool_mod, '_daemon', side_effect=RuntimeError("can't start new thread")):
            self.assertFalse(pool.unload(timeout_s=0.1))
        self.assertTrue(worker.loaded)
        self.assertEqual(pool.summary()['state'], 'failed')
        self.assertEqual(pool.summary()['cleanupFailures'], 1)
        self.assertTrue(pool.unload(timeout_s=0.1))
        self.assertFalse(worker.loaded)
        self.assertEqual(worker.close_calls, 1)
        self.assertEqual(pool.summary()['state'], 'empty')

    def test_unload_serializes_with_leased_worker_call(self):
        pool, worker = self.ready_pool()
        worker.lock = threading.Lock()
        leased = pool.lease()
        in_call = threading.Event()
        interrupted = threading.Event()
        active_closes = [0]
        max_closes = [0]
        close_lock = threading.Lock()
        orig_close = leased.close
        def tracked_close():
            with close_lock:
                active_closes[0] += 1
                max_closes[0] = max(max_closes[0], active_closes[0])
            try:
                return orig_close()
            finally:
                with close_lock:
                    active_closes[0] -= 1
        leased.close = tracked_close
        leased.cancel_start = lambda **_kw: interrupted.set()
        def run_call():
            with leased.lock:
                in_call.set()
                interrupted.wait(2)
                leased.close()
        t = threading.Thread(target=run_call, daemon=True)
        t.start()
        self.assertTrue(in_call.wait(1))
        self.assertTrue(pool.unload(timeout_s=2))
        t.join(2)
        self.assertEqual(max_closes[0], 1)
        self.assertFalse(leased.loaded)
        self.assertEqual(pool.summary()['state'], 'empty')

    def test_unload_waits_for_threaded_warm(self):
        clock = FakeClock()
        factory = Factory(clock)
        gate = threading.Event()
        factory.plan.append({'gate': gate})
        pool = WorkerPool(factory, clock=clock, join_s=5)   # default daemon-thread spawner
        pool.begin_warm()
        self.assertTrue(_wait(lambda: factory.made and factory.made[0].entered.is_set()))
        self.assertEqual(pool.poll(), 'warming')
        result = []
        unloader = threading.Thread(target=lambda: result.append(pool.unload()), daemon=True)
        unloader.start()
        self.assertTrue(_wait(lambda: pool._warm is not None and pool._warm.cancelled))
        gate.set()
        unloader.join(5)
        self.assertEqual(result, [True])
        worker = factory.made[0]
        self.assertEqual(worker.close_calls, 1)
        self.assertFalse(worker.loaded)
        self.assertEqual(pool.summary()['state'], 'empty')

    def test_unload_interrupts_warming_worker_process_within_budget(self):
        clock = FakeClock()
        made = []
        def factory():
            w = SubprocessWarmWorker(clock)
            made.append(w)
            return w
        pool = WorkerPool(factory, clock=clock, join_s=5)
        pool.begin_warm()
        self.assertTrue(_wait(lambda: made and made[0].entered.is_set()))
        begin = time.monotonic()
        self.assertTrue(pool.unload(timeout_s=5))
        self.assertLess(time.monotonic() - begin, 2.5)
        self.assertIsNone(made[0].process)
        self.assertGreaterEqual(made[0].close_calls, 1)
        self.assertEqual(pool.summary()['state'], 'empty')

    def test_unload_close_failure_is_false(self):
        pool, worker = self.ready_pool()
        worker.close_ok = False
        self.assertFalse(pool.unload())
        self.assertEqual(pool.poll(), 'failed')
        self.assertEqual(pool.summary()['state'], 'failed')
        worker.close_ok = True
        self.assertTrue(pool.unload())

    def test_cancelled_warm_close_failure_is_false(self):
        spawn = StepSpawn()
        pool = self.make(spawn, join_s=0)
        self.factory.plan.append({'close_ok': False})
        pool.begin_warm()
        self.assertFalse(pool.unload())
        spawn.run()
        self.assertEqual(pool.summary()['cleanupFailures'], 1)
        self.assertFalse(pool.unload())
        self.factory.made[0].close_ok = True
        self.assertTrue(pool.unload())

    def test_hook_close_unloads(self):
        hook = self.make_hook()
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')
        self.assertTrue(hook.close())
        self.assertTrue(self.all_closed())
        self.assertTrue(hook.close())

    def test_hook_close_cancels_inflight_warm_and_shares_single_budget(self):
        clock = FakeClock()
        factory = Factory(clock)
        gate = threading.Event()
        factory.plan.append({'gate': gate, 'cancel_unblocks': True})
        pool = WorkerPool(factory, clock=clock, join_s=5)
        source = ArmEvidencePowerSource(clock)
        hook = WarmHook(pool, source, clock=clock, audit=ListAudit(), tick_s=60)
        self.assertTrue(hook.start())
        hook.note_tv_activity()
        self.assertTrue(_wait(lambda: factory.made and factory.made[0].entered.is_set()))
        begin = time.monotonic()
        self.assertTrue(hook.close(timeout_s=3))
        self.assertLess(time.monotonic() - begin, 1.5)
        self.assertGreaterEqual(factory.made[0].cancel_calls, 1)
        self.assertEqual(factory.made[0].close_calls, 1)
        self.assertEqual(pool.summary()['state'], 'empty')

    def test_hook_close_timeout_preserves_failed_state_within_single_budget(self):
        clock = FakeClock()
        factory = Factory(clock)
        gate = threading.Event()
        factory.plan.append({'gate': gate, 'cancel_unblocks': False})
        pool = WorkerPool(factory, clock=clock, join_s=5)
        source = ArmEvidencePowerSource(clock)
        hook = WarmHook(pool, source, clock=clock, audit=ListAudit(), tick_s=60)
        self.assertTrue(hook.start())
        hook.note_tv_activity()
        self.assertTrue(_wait(lambda: factory.made and factory.made[0].entered.is_set()))
        begin = time.monotonic()
        self.assertFalse(hook.close(timeout_s=0.15))
        self.assertLess(time.monotonic() - begin, 1.0)
        self.assertTrue(pool.failed)
        gate.set()
        self.assertTrue(_wait(lambda: pool._warm is None))
        self.assertTrue(pool.unload(timeout_s=1))

    def test_hook_close_fences_inflight_tick_past_shutdown_budget(self):
        clock = FakeClock()
        made = []
        def factory():
            worker = SubprocessWarmWorker(clock)
            made.append(worker)
            return worker
        entered, proceed = threading.Event(), threading.Event()
        class DelayedSource:
            def current(self):
                entered.set()
                if not proceed.wait(2):
                    raise RuntimeError('bounded read gate')
                return 'active', clock()
        pool = WorkerPool(factory, clock=clock)
        hook = WarmHook(pool, DelayedSource(), clock=clock, audit=ListAudit(), tick_s=60)
        try:
            self.assertTrue(hook.start())
            self.assertTrue(entered.wait(1))
            self.assertFalse(hook.close(timeout_s=0.03))
            proceed.set()
            self.assertTrue(hook.stopped.wait(1))
            self.assertEqual(made, [])
            self.assertIsNone(pool._warm)
            self.assertEqual(pool.summary()['state'], 'empty')
            self.assertTrue(hook.close(timeout_s=1))
        finally:
            proceed.set()
            hook.close(timeout_s=1)


class ArmEvidenceTests(PoolBase):
    def test_standby_heartbeat_unloads_after_grace_without_ssap(self):
        hook = self.make_hook()
        class Unavailable:
            def current(self):
                raise TimeoutError()
        self.source.ssap = Unavailable()
        hook.note_tv_power('active')
        hook.tick()
        self.assertEqual(hook.tick(), 'ready')
        self.clock.advance(1)
        hook.note_tv_power('standby')
        self.assertEqual(self.source.current(), ('active_standby', self.clock()))
        self.assertEqual(hook.tick(), 'grace')
        self.clock.advance(warm.GRACE_S - 1)
        hook.note_tv_power('standby')
        self.assertEqual(hook.tick(), 'grace')
        self.clock.advance(2)
        self.assertEqual(hook.tick(), 'unloaded')
        self.assertTrue(self.all_closed())
        self.assertTrue(hook.close(timeout_s=1))

    def test_active_heartbeat_resumes_and_rewarms_after_standby(self):
        hook = self.make_hook()
        hook.note_tv_power('active')
        hook.tick()
        self.assertEqual(hook.tick(), 'ready')
        first = self.pool.active
        self.clock.advance(1)
        hook.note_tv_power('standby')
        self.assertEqual(hook.tick(), 'grace')
        self.clock.advance(20)
        hook.note_tv_power('active')
        self.assertEqual(hook.tick(), 'ready')
        self.assertIs(self.pool.active, first)
        self.clock.advance(1)
        hook.note_tv_power('standby')
        self.assertEqual(hook.tick(), 'grace')
        self.clock.advance(warm.GRACE_S + 1)
        self.assertEqual(hook.tick(), 'unloaded')
        self.clock.advance(1)
        hook.note_tv_power('active')
        hook.tick()
        self.assertEqual(hook.tick(), 'ready')
        self.assertIsNot(self.pool.active, first)
        self.assertEqual(len(self.factory.made), 2)
        self.assertTrue(hook.close(timeout_s=1))

    def test_goes_stale_after_30s(self):
        hook = self.make_hook()
        self.assertIsNone(self.source.current())
        hook.note_tv_activity()
        self.assertEqual(self.source.current(), ('active', self.clock()))
        hook.tick()
        self.clock.advance(warm.STALE_S)
        hook.tick()
        self.assertEqual(hook.status()['warm']['power'], 'active')
        self.clock.advance(1)
        hook.tick()
        self.assertEqual(hook.status()['warm']['power'], 'unknown')
        self.assertEqual(hook.status()['warm']['state'], 'ready')   # unknown holds

    def test_ssap_reading_wins_and_garbage_falls_back(self):
        clock = FakeClock()

        class Ssap:
            value = None
            def current(self):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value
        ssap = Ssap()
        source = ArmEvidencePowerSource(clock, ssap=ssap)
        source.note_active()
        evidence = ('active', clock())
        for bad in (None, ('on', clock()), ('off',), ('off', None), ('off', 'bad'),
                    ('off', clock() + 3600), ['off', clock()], OSError('drop')):
            ssap.value = bad
            self.assertEqual(source.current(), evidence)
        ssap.value = ('off', clock())
        self.assertEqual(source.current(), ('off', clock()))

    def test_note_active_preserves_newer_ssap_evidence(self):
        clock = FakeClock()

        class Ssap:
            value = None
            def current(self):
                return self.value
        ssap = Ssap()
        arm_paused, release_arm = threading.Event(), threading.Event()
        arm_thread_id = [None]
        def stepped_clock():
            val = clock()
            if threading.get_ident() == arm_thread_id[0] and not arm_paused.is_set():
                arm_paused.set()
                release_arm.wait(2)
            return val
        source = ArmEvidencePowerSource(stepped_clock, ssap=ssap)
        def run_arm():
            arm_thread_id[0] = threading.get_ident()
            source.note_active()
        t = threading.Thread(target=run_arm, daemon=True)
        t.start()
        self.assertTrue(arm_paused.wait(1))
        clock.advance(5)
        t_off = clock()
        ssap.value = ('off', t_off)
        self.assertEqual(source.current(), ('off', t_off))
        release_arm.set()
        t.join(2)
        ssap.value = None                                # socket drops after newer 'off'
        self.assertEqual(source.current(), ('off', t_off))

    def test_unknown_alone_never_warms(self):
        hook = self.make_hook()
        for _ in range(5):
            self.clock.advance(1)
            self.assertEqual(hook.tick(), 'unloaded')
        self.assertEqual(self.factory.made, [])


class HardPowerOffHoldTests(PoolBase):
    """A hard power-off leaves no final state, so the policy holds UNKNOWN_HOLD_S
    (30 min) after STALE_S, not GRACE_S (5 min). Accepted residual."""

    def tick_until(self, hook, offset, start):
        while self.clock() < start + offset:
            self.clock.advance(1)
            hook.tick()
        return hook.controller.state

    def test_a_arm_then_silence_holds_30_min(self):
        hook = self.make_hook()
        hook.note_tv_activity()
        t0 = self.clock()
        self.assertEqual(hook.tick(), 'ready')
        self.assertEqual(self.tick_until(hook, 300, t0), 'ready')       # grace would have unloaded here
        self.assertEqual(hook.status()['warm']['power'], 'unknown')
        self.assertTrue(self.factory.made[0].loaded)
        self.assertEqual(self.tick_until(hook, 1800, t0), 'ready')
        self.assertEqual(self.tick_until(hook, 1830, t0), 'ready')
        self.assertTrue(any(w.loaded for w in self.factory.made))
        self.assertEqual(self.tick_until(hook, 1831, t0), 'unloaded')   # 31 s to stale + 1800 s hold
        self.assertEqual(reasons(self.audit)[-2:], ['UNKNOWN_HOLD_EXPIRED', 'UNLOAD_VERIFIED'])
        self.assertTrue(self.all_closed())
        # Pinned residual: rotation (age 1500 s) still ran during the hold.
        self.assertEqual(self.pool.summary()['rotations'], 1)

    def test_b_ssap_off_unloads_after_grace(self):
        hook = self.make_hook(ssap=True)
        ssap = self.ssap
        ssap.set('active')
        self.assertEqual(hook.tick(), 'ready')
        ssap.set('off')                                  # the power read reports a final state
        t_off = self.clock()
        self.assertEqual(hook.tick(), 'grace')
        self.assertEqual(self.tick_until(hook, warm.GRACE_S - 1, t_off), 'grace')
        self.assertTrue(self.factory.made[0].loaded)
        self.assertEqual(self.tick_until(hook, warm.GRACE_S, t_off), 'unloaded')
        self.assertIn('GRACE_EXPIRED', reasons(self.audit))
        self.assertTrue(self.all_closed())

    def test_b2_ssap_socket_drop_without_final_state_still_holds(self):
        hook = self.make_hook(ssap=True)
        ssap = self.ssap
        ssap.set('active')
        hook.note_tv_activity()
        t0 = self.clock()
        self.assertEqual(hook.tick(), 'ready')
        ssap.drop()                                      # socket gone, no 'off' ever arrives
        self.assertEqual(self.tick_until(hook, 300, t0), 'ready')
        self.assertEqual(self.tick_until(hook, 1830, t0), 'ready')
        self.assertEqual(self.tick_until(hook, 1831, t0), 'unloaded')

    def test_b3_arm_then_ssap_off_then_socket_drop_still_unloads_after_grace(self):
        hook = self.make_hook(ssap=True)
        ssap = self.ssap
        ssap.set('active')
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')
        self.clock.advance(5)
        ssap.set('off')
        t_off = self.clock()
        self.assertEqual(hook.tick(), 'grace')
        self.clock.advance(1)
        ssap.drop()                                      # socket closes right after 'off' (< STALE_S)
        self.assertEqual(hook.tick(), 'grace')           # superseded arm evidence must not cancel grace
        self.assertEqual(self.tick_until(hook, warm.GRACE_S - 1, t_off), 'grace')
        self.assertEqual(self.tick_until(hook, warm.GRACE_S, t_off), 'unloaded')
        self.assertIn('GRACE_EXPIRED', reasons(self.audit))
        self.assertTrue(self.all_closed())

    def test_c_fresh_arm_during_hold_keeps_ready_without_rewarm(self):
        hook = self.make_hook()
        hook.note_tv_activity()
        t0 = self.clock()
        self.assertEqual(hook.tick(), 'ready')
        self.assertEqual(self.tick_until(hook, 900, t0), 'ready')
        self.assertEqual(hook.status()['warm']['power'], 'unknown')
        hook.note_tv_activity()                          # an authenticated arm mid-hold
        t1 = self.clock()
        self.assertEqual(hook.tick(), 'ready')
        self.assertEqual(hook.status()['warm']['power'], 'active')
        self.assertEqual(len(self.factory.made), 1)      # no rewarm
        self.assertEqual(reasons(self.audit).count('POWER_ACTIVE'), 1)
        # The hold restarts from the new evidence (t1 + 31 + 1800). Worker rotated at
        # age 1500 meanwhile, still one warm at a time.
        self.assertEqual(self.tick_until(hook, 1830, t1), 'ready')
        self.assertEqual(self.tick_until(hook, 1831, t1), 'unloaded')

    def test_c2_fresh_arm_while_unload_waits_returns_to_ready(self):
        hook = self.make_hook()
        hook.note_tv_activity()
        t0 = self.clock()
        self.assertEqual(hook.tick(), 'ready')
        self.tick_until(hook, 1800, t0)
        ticket = hook.admit()                            # an utterance in flight at expiry
        self.assertEqual(self.tick_until(hook, 1831, t0), 'unloading')
        made = len(self.factory.made)
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')           # POWER_ACTIVE, no warm
        self.assertEqual(len(self.factory.made), made)
        self.assertEqual(reasons(self.audit)[-1], 'POWER_ACTIVE')
        self.assertNotIn('UNLOAD_VERIFIED', reasons(self.audit))
        hook.release(ticket)


class LoopTests(PoolBase):
    def test_poke_ticks_at_once_and_close_stops_loop(self):
        clock = FakeClock()
        factory = Factory(clock)
        pool = WorkerPool(factory, clock=clock, spawn_thread=sync_spawn)
        source = ArmEvidencePowerSource(clock)
        hook = WarmHook(pool, source, clock=clock, audit=ListAudit(), tick_s=60)
        self.assertTrue(hook.start())
        self.assertFalse(hook.start())                   # one loop
        self.assertTrue(_wait(lambda: hook.status()['warm']['power'] == 'unknown'))
        hook.note_tv_activity()                          # tick_s=60: only the poke can tick now
        self.assertTrue(_wait(lambda: hook.controller.state == 'ready'))
        self.assertTrue(hook.close())
        self.assertTrue(hook.stopped.is_set())
        self.assertFalse(factory.made[0].loaded)

    def test_audit_failure_uses_noop_pauser(self):
        clock = FakeClock()

        class BrokenAudit:
            def append(self, row):
                raise OSError('disk')
        pool = WorkerPool(Factory(clock), clock=clock, spawn_thread=sync_spawn)
        source = ArmEvidencePowerSource(clock)
        hook = WarmHook(pool, source, clock=clock, audit=BrokenAudit())
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')

    def test_arm_triggered_warm_visible_to_admit_while_loop_runs(self):
        clock = FakeClock()
        spawner = StepSpawn()
        pool = WorkerPool(Factory(clock), clock=clock, spawn_thread=spawner)
        hook = WarmHook(pool, ArmEvidencePowerSource(clock), clock=clock, audit=ListAudit(), tick_s=60)
        try:
            self.assertTrue(hook.start())
            self.assertTrue(_wait(lambda: hook.controller.unknown_since is not None))
            hook.note_tv_activity()
            with self.assertRaises(Refused) as caught:
                hook.admit()
            self.assertEqual(str(caught.exception), 'VOICE_WARMING_UP')
            self.assertEqual(caught.exception.code, 'VOICE_WARMING_UP')
            self.assertEqual(hook.state, 'warming')
            self.assertTrue(_wait(lambda: len(spawner.pending) == 1))
            spawner.run()
            self.assertEqual(hook.tick(), 'ready')
        finally:
            hook.close(timeout_s=1)

    def test_hook_close_fences_foreground_tick(self):
        clock = FakeClock()
        factory = Factory(clock)
        entered, proceed = threading.Event(), threading.Event()
        class DelayedSource:
            def current(self):
                entered.set()
                if not proceed.wait(2):
                    raise RuntimeError('bounded read gate')
                return 'active', clock()
        pool = WorkerPool(factory, clock=clock)
        hook = WarmHook(pool, DelayedSource(), clock=clock, audit=ListAudit(), tick_s=60)
        fg = threading.Thread(target=hook.tick, daemon=True)
        try:
            fg.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(hook.close(timeout_s=0.05))
            proceed.set()
            fg.join(1)
            self.assertEqual(factory.made, [])
            self.assertIsNone(pool._warm)
            self.assertTrue(hook.close(timeout_s=1))
        finally:
            proceed.set()
            hook.close(timeout_s=1)

    def test_admit_does_not_block_behind_inflight_unload(self):
        clock = FakeClock()
        factory = Factory(clock)
        close_entered, close_release = threading.Event(), threading.Event()
        pool = WorkerPool(factory, clock=clock, spawn_thread=sync_spawn, join_s=5)
        ssap = DropSsap(clock)
        source = ArmEvidencePowerSource(clock, ssap=ssap)
        hook = WarmHook(pool, source, clock=clock, audit=ListAudit(), tick_s=60)
        ssap.set('active')
        self.assertEqual(hook.tick(), 'ready')
        worker = factory.made[0]
        orig_close = worker.close
        def slow_close():
            close_entered.set()
            close_release.wait(2)
            return orig_close()
        worker.close = slow_close
        ssap.set('off')
        self.assertEqual(hook.tick(), 'grace')
        clock.advance(warm.GRACE_S)
        t = threading.Thread(target=hook.tick, daemon=True)
        t.start()
        self.assertTrue(close_entered.wait(1))
        try:
            hook.note_tv_activity()
            begin = time.monotonic()
            with self.assertRaises(Refused) as caught:
                hook.admit()
            self.assertLess(time.monotonic() - begin, 0.2)
            self.assertEqual(str(caught.exception), 'VOICE_UNAVAILABLE')
        finally:
            close_release.set()
            t.join(2)
            hook.close(timeout_s=1)

    def test_stopped_tick_does_not_block_close_when_source_holds_controller_lock(self):
        clock = FakeClock()
        entered, release = threading.Event(), threading.Event()
        class Source:
            def current(self):
                entered.set()
                release.wait(2)
                return 'active', clock()
        pool = WorkerPool(Factory(clock), clock=clock)
        hook = WarmHook(pool, Source(), clock=clock, audit=ListAudit())
        t = threading.Thread(target=hook.tick, daemon=True)
        t.start()
        self.assertTrue(entered.wait(1))
        real_cancel = pool.cancel_warm
        late = []
        def cancel(**kw):
            if not late:
                q = threading.Thread(target=hook.tick, daemon=True)
                late.append(q)
                q.start()
                q.join(0.1)
            return real_cancel(**kw)
        pool.cancel_warm = cancel
        try:
            begin = time.monotonic()
            self.assertFalse(hook.close(timeout_s=0.05))
            self.assertLess(time.monotonic() - begin, 0.25)
            self.assertFalse(late[0].is_alive())
        finally:
            release.set()
            t.join(1)
            hook.close(timeout_s=1)


class ProductionIntegrationTests(PoolBase):
    def test_cancel_during_pool_acquire_cannot_publish_a_late_lease(self):
        clock = FakeClock()
        factory = Factory(clock)
        pool = WorkerPool(factory, clock=clock, spawn_thread=sync_spawn)
        hook = pool_mod.create_warm_hook(pool, clock=clock, audit=ListAudit())
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')
        coord = pool_mod._UtteranceLeaseCoordinator(hook)
        entered, proceed = threading.Event(), threading.Event()
        original = pool.lease
        def delayed():
            worker = original()
            entered.set()
            self.assertTrue(proceed.wait(1))
            return worker
        pool.lease = delayed
        outcomes = []
        def acquire():
            try:
                coord.acquire()
                outcomes.append('accepted')
            except Refused:
                outcomes.append('refused')
        thread = threading.Thread(target=acquire)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(coord.release())
        finally:
            proceed.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ['refused'])
        self.assertIsNone(pool.leased)
        self.assertIsNone(coord._worker)
        self.assertTrue(hook.close())

    def test_hook_close_preserves_power_source_cleanup_failure(self):
        for result in (False, None, RuntimeError('source close')):
            with self.subTest(result=type(result).__name__):
                clock = FakeClock()
                pool = WorkerPool(Factory(clock), clock=clock, spawn_thread=sync_spawn)
                class Source:
                    def current(self): return ('active', clock())
                    def close(self, *, timeout_s):
                        if isinstance(result, Exception): raise result
                        return result
                hook = pool_mod.create_warm_hook(pool, clock=clock, ssap=Source(), audit=ListAudit())
                self.assertEqual(hook.tick(), 'ready')
                self.assertFalse(hook.close(timeout_s=1))
                self.assertEqual(pool.summary()['state'], 'empty')

    def test_production_worker_factory_validates_endpoints_and_propagates_prompt_phase(self):
        endpoints = pool_mod.expected_model_endpoints('/opt/models/asr', '/opt/models/intent')
        self.assertEqual(endpoints['asr']['repo'], 'Qwen/Qwen3-ASR-0.6B-hf')
        self.assertEqual(endpoints['intent']['repo'], 'Qwen/Qwen3-4B-Instruct-2507')
        self.assertEqual(endpoints['asr']['path'], '/opt/models/asr')
        self.assertEqual(endpoints['intent']['path'], '/opt/models/intent')
        seen = []
        class StubWorker(FakeWorker):
            def __init__(self, python, asr_path, intent_path, *, qualified=False, clock=time.monotonic,
                         spawn=None, prompt_phase=None):
                super().__init__(clock)
                self.python = python
                self.asr_path = asr_path
                self.intent_path = intent_path
                self.qualified = qualified
                self.spawn = spawn
                self.prompt_phase = prompt_phase
                seen.append(self)
            def configure_prompt_phase(self, phase):
                self.prompt_phase = phase
        clock = FakeClock()
        factory = pool_mod.model_factory(
            '/usr/bin/python3', '/opt/models/asr', '/opt/models/intent',
            qualified=True, clock=clock, worker_cls=StubWorker)
        pool = WorkerPool(factory, clock=clock, spawn_thread=sync_spawn)
        pool.configure_prompt_phase('P5')
        pool.begin_warm()
        self.assertEqual(pool.poll(), 'ready')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].prompt_phase, 'P5')
        self.assertTrue(seen[0].qualified)
        with self.assertRaises(Refused):
            pool_mod.model_factory('relative/python', '/opt/models/asr', '/opt/models/intent', worker_cls=StubWorker)
        with self.assertRaises(Refused):
            pool_mod.model_factory('/usr/bin/python3', '/opt/models/same', '/opt/models/same', worker_cls=StubWorker)

    def test_authenticated_tv_power_reader_verifies_credentials_and_feeds_warm_hook(self):
        import json
        import os
        from pathlib import Path
        import tempfile
        clock = FakeClock()
        calls = []
        replies = [{'returnValue': True, 'state': 'Active'}]
        class Conn:
            def __init__(self, pin_sha256, timeout_s):
                self.pin = pin_sha256
                self.closed = False
            def register(self, client_key, timeout_s):
                calls.append(('register', self.pin, client_key))
                return True
            def request(self, uri, payload, timeout_s):
                calls.append(('request', uri, payload))
                return dict(replies[-1])
            def close(self):
                self.closed = True
        class Transport:
            def connect(self, pin_sha256, timeout_s):
                return Conn(pin_sha256, timeout_s)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            key_path = base / 'client.key'
            pin_path = base / 'pin.json'
            key_path.write_text('valid_client_key_123456\n')
            pin_path.write_text(json.dumps({'certificate_sha256': 'a' * 64}))
            os.chmod(key_path, 0o600)
            os.chmod(pin_path, 0o600)
            reader = pool_mod.AuthenticatedTvPowerReader(
                clock=clock, transport=Transport(), key_path=key_path, pin_path=pin_path,
                poll_interval_s=5.0)
            factory = Factory(clock)
            pool = WorkerPool(factory, clock=clock, spawn_thread=sync_spawn)
            hook = pool_mod.create_warm_hook(pool, clock=clock, ssap=reader, audit=ListAudit())
            self.assertEqual(hook.tick(), 'ready')
            self.assertEqual(calls[0], ('register', 'a' * 64, 'valid_client_key_123456'))
            self.assertEqual(calls[1][1], 'ssap://com.webos.service.tvpower/power/getPowerState')
            clock.advance(6.0)
            replies.append({'returnValue': True, 'state': 'Power Off'})
            self.assertEqual(hook.tick(), 'grace')
            clock.advance(warm.GRACE_S)
            self.assertEqual(hook.tick(), 'unloaded')
            self.assertTrue(hook.close())

    def test_pool_asr_and_intent_share_utterance_lease_and_release(self):
        clock = FakeClock()
        class UtteranceWorker(FakeWorker):
            def __init__(self):
                super().__init__(clock)
                self.qualified = True
                self.prompt_phase = None
            def configure_prompt_phase(self, phase):
                self.prompt_phase = phase
            def call(self, op, payload, *, timeout_s):
                self.counter += 1
                if op == 'asr':
                    return 'open youtube'
                return '{"schema":1,"status":"match","intent":"OPEN_YOUTUBE","entities":{}}'
        made = []
        def make():
            w = UtteranceWorker()
            made.append(w)
            return w
        pool = WorkerPool(make, clock=clock, spawn_thread=sync_spawn)
        hook = pool_mod.create_warm_hook(pool, clock=clock, audit=ListAudit())
        coord, asr, intent = pool_mod.pool_providers(hook)
        intent.configure_prompt_phase('P5')
        hook.note_tv_activity()
        self.assertEqual(hook.tick(), 'ready')
        self.assertTrue(asr.qualified)
        text = asr.transcribe(b'\x01\x00' * 320, rate=16000, timeout_s=2)
        self.assertEqual(text, 'open youtube')
        # Lease stays held between ASR and intent within the same utterance:
        self.assertIsNotNone(pool.leased)
        raw = intent.interpret(text, timeout_s=2)
        self.assertIn('OPEN_YOUTUBE', raw)
        # Lease is released immediately after intent completes:
        self.assertIsNone(pool.leased)
        self.assertEqual(made[0].counter, 2)
        self.assertEqual(made[0].prompt_phase, 'P5')
        self.assertTrue(hook.close())


def _wait(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


if __name__ == '__main__':
    unittest.main()

class StopAdmissionTests(unittest.TestCase):
    def test_stopping_hook_cannot_lease_still_healthy_worker(self):
        clock = FakeClock()
        pool = WorkerPool(lambda: FakeWorker(clock), clock=clock, spawn_thread=lambda fn: fn())
        pool.begin_warm()
        self.assertEqual(pool.poll(), 'ready')
        stop = threading.Event()
        pool._stop_events.append(stop)
        stop.set()  # Pool unload may still be waiting behind a slow lifecycle operation.
        with self.assertRaisesRegex(Refused, 'VOICE_UNAVAILABLE'):
            pool.lease()
        self.assertIsNone(pool.leased)
        self.assertTrue(pool.unload())

    def test_live_prompt_change_cannot_relabel_sealed_worker(self):
        clock = FakeClock()
        pool = WorkerPool(lambda: FakeWorker(clock), clock=clock, spawn_thread=lambda fn: fn())
        pool.configure_prompt_phase(None)
        previous = pool.prompt_sha256
        pool.begin_warm()
        self.assertEqual(pool.poll(), 'ready')
        with self.assertRaisesRegex(Refused, 'MODEL_CONFIG'):
            pool.configure_prompt_phase('P5')
        self.assertEqual(pool.prompt_sha256, previous)
        self.assertIsNone(pool.prompt_phase)
        pool.configure_prompt_phase(None)  # Identical metadata remains idempotent.
        self.assertTrue(pool.unload())
        pool.configure_prompt_phase('P5')  # A new unstarted generation may change.
        self.assertNotEqual(pool.prompt_sha256, previous)

    def test_pending_factory_prompt_change_is_rejected_before_child_exists(self):
        queued = []
        pool = WorkerPool(lambda: None, spawn_thread=queued.append)
        pool.configure_prompt_phase(None)
        previous = pool.prompt_sha256
        pool.begin_warm()
        self.assertIsNone(pool._warm.worker)
        with self.assertRaisesRegex(Refused, 'MODEL_CONFIG'):
            pool.configure_prompt_phase('P5')
        self.assertEqual(pool.prompt_sha256, previous)
        queued[0]()  # Complete the inert failed factory; no process is started.
        self.assertTrue(pool.unload())
