"""Warm worker pool and TV-power hook for the Spark voice service: a `ModelPool`
(active worker + one standby, with rotation) under the TV-power-aware policy in
warm.py. Offline building block: the worker factory, the clock, the thread spawner and
the power source are injected. Nothing here opens a socket, talks to the TV, or (in
tests) starts a process.

Tags: [V] checked against code or tests in this tree; [D] design choice here;
[H] untested hypothesis.

WorkerPool  (warm.py ModelPool contract + leasing for the utterance providers below)
  * factory() -> a new, unstarted worker shaped like model_worker.ModelWorker:
    start(timeout_s=120), close() -> bool, .process, .sealed, .counter, .created [V].
    On Spark: factory = lambda: ModelWorker(python, asr_path, intent_path, qualified=True).
  * One active worker, at most one standby, at most one warm in flight [D].
  * Alive = sealed is not None, process is not None and process.poll() is None
    (checked again when a lease starts). Serving also needs the worker under its hard
    bounds, 256 calls and 1800 s of age, past which ModelWorker.call refuses [V].
  * lease() hands the active worker to one utterance; give_back() returns it
    (_UtteranceLeaseCoordinator below). give_back of a dead worker closes it and the
    pool reports 'failed', so warm.py unloads (POOL_FAILED) and enters degraded.
  * Rotation stays inside the pool, not in warm.py. When the active
    worker's age >= rotate_after_s or counter >= rotate_after_calls, ONE standby warm
    starts in the background. poll() stays 'ready' while the active can serve. The
    swap happens when the standby is ready and nothing is leased (in poll(), or in
    lease(), so new utterances go to the standby); the old worker is closed
    by the next poll(), and a False close() makes poll() 'failed'. A standby that
    fails is not retried for that active worker: the pool keeps serving until the old
    worker's hard bound, then reports 'failed' (warm.py degrades).
  * unload() closes active, standby, retired and previously unclean workers, cancels
    and signals any in-flight warming worker, and waits up to the remaining shutdown
    budget (default join_s <= SHUTDOWN_BUDGET_S = 30 s) for the warm thread to close
    and exit. True only when every close() returned True and no warm is still running.
    Idempotent. It also revokes an outstanding lease (only service shutdown does that;
    warm.py never unloads under a ticket).
  * Locks: `lock` guards state and is never held across factory/start/close;
    `ops` serializes the slow lifecycle work (begin_warm, poll, unload, a dead
    give_back) so a close is never raced. lease() and summary() take `lock` only: the
    press path never waits for a warm or an unload.

Residuals of the pool [D unless marked]
  * Defaults rotate_after_s=1500 / rotate_after_calls=240. Margin to the hard bounds:
    300 s (>= 120 s warm bound) and 16 calls (8 utterances). [H] Under sustained
    presses at the utterance rate limit the 16 calls can run out before an ~84-120 s
    standby is ready; the pool then reports 'failed' and warm.py degrades (the overlap
    is not qualified on hardware).
  * There is no quiet window before a standby warm: it starts as soon as nothing is
    leased. A press during the ~84 s standby warm still overlaps it on the GB10 [H].
  * Rotation keeps running during an 'unknown' hold (the pool does not see power), so
    a TV that went dark can still cost one standby warm and the two-worker peak.

ArmEvidencePowerSource  (warm.py PowerStateSource)
  [D] Without a read-only SSAP power read, an authenticated TV arm request (peer ==
  TV + voice key over the trusted LAN segment) is treated as a fresh 'active' reading:
  the TV just asked for voice, so it is on. That is NOT ICMP/ARP presence, which
  warm.py forbids as the signal. Callers must call note_active() only from that
  authenticated arm path, never on a bare connection. warm.py's STALE_S (30 s) turns
  old evidence into 'unknown' [V warm.py].
  [D] So an authenticated press starts a warm-up even while the power state is unknown.
  [D] Consequence without an SSAP read: with arm evidence alone the policy reduces to
  "warm for STALE_S + UNKNOWN_HOLD_S (~30.5 min) after the last authenticated arm". A
  TV that is on but unused for longer than that looks the same as one that is off, so
  the next press gets VOICE_WARMING_UP (~84 s). That is in effect an idle timer, which
  the warm policy otherwise avoids; it goes away when a real power read is plugged in
  as `ssap`.
  With `ssap`, a well-formed SSAP reading (state in warm.POWER_STATES) wins, even a
  stale one (warm.py then counts it as 'unknown'); arm evidence is only used when
  the SSAP source returns nothing usable.

Hard TV power-off -> 30 min hold, not 5 min grace (accepted residual)
  [H] A hard power-off (mains pulled, or a power-off that drops the LAN link before a
  final state is pushed) closes the SSAP socket without a final 'off'/'suspend'
  reading. With arm evidence, or an SSAP source that just lost its socket, the last
  'active' reading ages: after STALE_S (30 s) warm.py sees 'unknown', and 'unknown'
  keeps the current state for UNKNOWN_HOLD_S (1800 s) [V warm.py]. So the models stay
  loaded until ~1831 s after the last confirmation (first stale tick at +31 s, then
  1800 s), not GRACE_S (300 s) [V test_pool.py HardPowerOffHoldTests].
  Cost: one worker is ~10 GB and the two-worker rotation peak ~20 GB [H], held up to
  ~30.5 min after the TV went dark.
  Accepted: the 30 min unknown hold is the fail-safe; warm.py policy is not changed
  here.
  Fix path [H]: a read-only SSAP power read passed as `ssap`. If the TV reports
  'screen_off'/'active_standby'/'suspend'/'off' before its socket closes (qualify by
  recording replies across each power transition), the policy applies the 5 min
  GRACE_S [V tests: ssap 'off' -> unloaded at +300 s]. A true mains pull still sends
  no final state and stays a 30 min hold unless UNKNOWN_HOLD_S is shortened.

WarmHook
  Wires WorkerPool + a power source into warm.WarmController and runs its loop:
  tick() every tick_s and at once after poke(); the arm route calls
  note_tv_activity() (evidence, then poke) and admit()/release() around an utterance.
  close() cancels any in-flight warm, stops the loop, and unloads the pool within one
  shared shutdown budget (<= SHUTDOWN_BUDGET_S = 30 s, inside TimeoutStopSec=60).
"""
import math
import os
import signal
import subprocess
import threading
import time

try:
    from .controller import Refused
except ImportError:
    try:
        from controller import Refused
    except ImportError:
        try:
            from ..backend.controller import Refused
        except ImportError:
            from ..controller import Refused

try:
    from . import warm
except ImportError:
    try:
        import warm
    except ImportError:
        try:
            from ..agent.prototype.agent import warm
        except ImportError:
            from agent.prototype.agent import warm

_WARM_REFUSED = getattr(warm, 'Refused', Refused)
_REFUSED_BASES = (Refused,) if _WARM_REFUSED is Refused else (Refused, _WARM_REFUSED)


class _PoolRefused(*_REFUSED_BASES):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code

    def __str__(self):
        return str(self.code)


HARD_CALLS = 256                       # [V] ModelWorker.call refuses at counter >= 256
HARD_AGE_S = 1800                      # [V] ModelWorker.call refuses at age >= 1800 s
UTTERANCE_CALLS = 2                    # [V] shared_owner.py reserves two calls (ASR + intent/agent)
UTTERANCE_RESERVE_S = 30               # [V] shared_owner.py whole-utterance age headroom
START_TIMEOUT_S = warm.WARM_TIMEOUT_S  # 120 s warm-up bound, ModelWorker.start(timeout_s)
SHUTDOWN_BUDGET_S = 30                 # Single stop budget across WarmHook.close() + WorkerPool.unload() (< TimeoutStopSec=60)
LOOP_STOP_S = SHUTDOWN_BUDGET_S
KILL_ESCALATE_S = 2.0
POOL_STATES = ('empty', 'warming', 'ready', 'rotating', 'failed')


def _daemon(target):
    threading.Thread(target=target, name='r2d2-warm', daemon=True).start()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


class _Warm:
    """One background warm. Fields change under WorkerPool.lock only."""
    __slots__ = ('role', 'cancelled', 'close_ok', 'finished', 'worker',
                 'origin', 'creator', 'process', 'creator_done')

    def __init__(self, role):
        self.role = role              # 'active' | 'standby'
        self.cancelled = False
        self.close_ok = True          # result of the thread's own close() of a dropped worker
        self.finished = threading.Event()
        self.worker = None            # owned worker instance while start() is in flight
        self.origin = self.creator = self.process = None
        self.creator_done = threading.Event()


class _CloseTask:
    """One in-flight worker.close() call so retries never race a running close."""
    __slots__ = ('worker', 'done', 'ok')

    def __init__(self, worker):
        self.worker = worker
        self.done = threading.Event()
        self.ok = False


class WorkerPool:
    def __init__(self, factory, *, clock=time.monotonic, spawn_thread=None,
                 rotate_after_s=1500, rotate_after_calls=240, join_s=SHUTDOWN_BUDGET_S):
        if not callable(factory) or not callable(clock):
            raise ValueError('POOL_CONFIG')
        if (not _number(rotate_after_s) or not 0 < rotate_after_s < HARD_AGE_S - UTTERANCE_RESERVE_S or
                type(rotate_after_calls) is not int or not 0 < rotate_after_calls <= HARD_CALLS - UTTERANCE_CALLS or
                not _number(join_s) or not 0 <= join_s <= SHUTDOWN_BUDGET_S):
            raise ValueError('POOL_CONFIG')
        self.factory, self.clock = factory, clock
        self.spawn_thread = spawn_thread if spawn_thread is not None else _daemon
        self.rotate_after_s, self.rotate_after_calls, self.join_s = rotate_after_s, rotate_after_calls, join_s
        self.lock = threading.Lock()    # state below; never held across factory/start/close
        self.ops = threading.Lock()     # begin_warm / poll / unload / dead give_back, one at a time
        self.active = None
        self.standby = None
        self.leased = None
        self.retiring = []              # swapped-out workers, closed by the next poll()
        self.unclean = []               # close() returned False; retried by unload()
        self._closing = []              # (worker, _CloseTask) while a close thread is in flight
        self._warm = None               # the one warm in flight
        self._creators = []             # actual child creators retained through reap
        self._stop_events = []          # attached WarmHook stopping events
        self.failed = False
        self.standby_failed = False     # no second standby for the same active worker
        self.rotations = 0
        self.cleanup_failures = 0
        self.prompt_phase = getattr(factory, 'prompt_phase', None)
        self.prompt_sha256 = getattr(factory, 'prompt_sha256', None)

    def configure_prompt(self, system_prompt, *, phase=None):
        import hashlib as _hashlib
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        try:
            expected = prompt_for_phase(phase)
        except Exception:
            raise _PoolRefused('MODEL_CONFIG') from None
        if type(system_prompt) is not str or system_prompt != expected:
            raise _PoolRefused('MODEL_CONFIG')
        sha = _hashlib.sha256(system_prompt.encode('utf-8')).hexdigest()
        # A sealed child cannot change its prompt. Serialize against begin_warm
        # and rotation so metadata can never advertise a new prompt over an old
        # running child, or a child currently being constructed.
        with self.ops:
            with self.lock:
                occupied = (self._warm is not None or any(w is not None for w in
                            (self.active, self.standby, self.leased)) or
                            bool(self.retiring or self.unclean))
                if occupied:
                    if self.prompt_sha256 != sha or self.prompt_phase != phase:
                        raise _PoolRefused('MODEL_CONFIG')
                    return
            configure_factory = getattr(self.factory, 'configure_prompt', None)
            if callable(configure_factory):
                configure_factory(system_prompt, phase=phase)
            else:
                configure_phase = getattr(self.factory, 'configure_prompt_phase', None)
                if callable(configure_phase):
                    configure_phase(phase)
            with self.lock:
                self.prompt_phase = phase
                self.prompt_sha256 = sha

    def configure_prompt_phase(self, phase):
        if phase not in (None, 'P5'):
            raise _PoolRefused('WORKER_INIT')
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        self.configure_prompt(prompt_for_phase(phase), phase=phase)

    def _is_stopping(self):
        return any(ev.is_set() for ev in self._stop_events)

    # -- ModelPool contract (warm.py) ------------------------------------------------
    def begin_warm(self):
        if self._is_stopping():
            raise RuntimeError('WARM_STOPPING')
        with self.ops:
            self._join_creators()
            with self.lock:
                if self._is_stopping():
                    raise RuntimeError('WARM_STOPPING')
                if (self._warm is not None or self.active is not None or self.standby is not None
                        or self.retiring or self.unclean or self._creators):
                    raise ValueError('POOL_BUSY')
                self.failed = self.standby_failed = False
                job = self._warm = _Warm('active')
            if not self._spawn(job):
                raise RuntimeError('POOL_SPAWN')

    def poll(self):
        with self.ops:
            with self.lock:
                self._promote()
                job = self._rotation_job()
            if job is not None and self._spawn(job):
                with self.lock:
                    self._promote()   # a synchronous spawner finishes the standby inline
            self._drain()
            self._join_creators()
            with self.lock:
                return self._status()

    def cancel_warm(self, *, escalate=False, target=None):
        """Mark the in-flight warm cancelled and signal its warming worker without
        waiting on `ops`, so a concurrent tick()/unload() unblocks promptly."""
        with self.lock:
            job = self._warm
            if job is None or (target is not None and job is not target) or job.finished.is_set():
                return None
            job.cancelled = True
            worker = job.worker
        if worker is not None:
            self._interrupt_worker(worker, escalate=escalate)
        return job

    def unload(self, *, timeout_s=None):
        if timeout_s is not None and (not _number(timeout_s) or not 0 <= timeout_s <= SHUTDOWN_BUDGET_S):
            raise ValueError('POOL_CONFIG')
        budget = float(self.join_s if timeout_s is None else timeout_s)
        deadline = time.monotonic() + budget
        self.cancel_warm(escalate=False)
        wait_ops = max(0.0, deadline - time.monotonic())
        acquired = self.ops.acquire(timeout=wait_ops) if wait_ops > 0 else self.ops.acquire(blocking=False)
        if not acquired:
            with self.lock:
                self.failed = True
            return False
        try:
            with self.lock:
                workers = []
                for worker in (self.active, self.standby, *self.retiring, *self.unclean):
                    if worker is not None and all(worker is not seen for seen in workers):
                        workers.append(worker)
                self.active = self.standby = self.leased = None
                self.retiring, self.unclean = [], []
                job = self._warm
                if job is not None:
                    job.cancelled = True   # its thread closes the worker when start() returns
            ok = True
            close_deadline = max(deadline, time.monotonic() + 0.02) if budget == 0 else deadline
            for worker in workers:
                if not self._close_bounded(worker, close_deadline, strict_deadline=True):
                    ok = False
                    with self.lock:
                        self.unclean.append(worker)
                        self.cleanup_failures += 1
            if job is not None and (not self._wait_warm_cancelled(job, deadline) or not job.close_ok):
                ok = False
            if not self._join_creators(deadline):
                ok = False
            with self.lock:
                if self._warm is not None:
                    ok = False             # a warm is still running (or a new one started)
                if ok:
                    self.failed = self.standby_failed = False
                else:
                    self.failed = True
            return ok
        finally:
            self.ops.release()

    # -- leasing (one utterance at a time, see _UtteranceLeaseCoordinator) -------------
    def lease(self):
        with self.lock:
            if self._is_stopping():
                raise Refused('VOICE_UNAVAILABLE')
            if self.leased is not None:
                raise Refused('VOICE_BUSY')
            if self.failed or self.unclean:
                raise Refused('VOICE_UNAVAILABLE')
            self._promote()   # a ready standby takes new utterances
            worker = self.active
            if worker is None or not self._servable(worker):
                raise Refused('VOICE_UNAVAILABLE')
            self.leased = worker
            return worker

    def give_back(self, worker):
        with self.lock:
            if worker is None or worker is not self.leased:
                return False
            if self._alive(worker):
                self.leased = None
                return True      # healthy: retained for the next utterance
        with self.ops:           # dead: close it, serialized with poll()/unload()
            with self.lock:
                if worker is not self.leased:
                    return False  # unload() revoked the lease and closed it meanwhile
                self.leased = None
                if self.active is worker:
                    self.active = None
                self.failed = True
            closed = self._close(worker)
            if not closed:
                with self.lock:
                    self.unclean.append(worker)
                    self.cleanup_failures += 1
                    self.failed = True
            return closed

    def summary(self):
        """Enums, bools and ints only."""
        with self.lock:
            if self.failed or self.unclean:
                state = 'failed'
            elif self._warm is not None and self._warm.role == 'active':
                state = 'warming'
            elif self.active is None and self.standby is None and self._warm is None and not self.retiring:
                state = 'empty'
            else:
                state = self._status()
                if state == 'ready' and (self.standby is not None or self._warm is not None):
                    state = 'rotating'
            return {'state': state, 'leased': self.leased is not None,
                    'rotations': self.rotations, 'cleanupFailures': self.cleanup_failures}

    # -- internals (lock held unless noted) -----------------------------------------
    @staticmethod
    def _alive(worker):
        try:
            process = worker.process
            return worker.sealed is not None and process is not None and process.poll() is None
        except Exception:
            return False

    def _age_calls(self, worker):
        try:
            created, counter, now = worker.created, worker.counter, self.clock()
        except Exception:
            return None
        if not _number(created) or type(counter) is not int or not _number(now):
            return None
        return now - created, counter

    def _servable(self, worker):
        if not self._alive(worker):
            return False
        bounds = self._age_calls(worker)
        return (bounds is not None and 0 <= bounds[0] < HARD_AGE_S - UTTERANCE_RESERVE_S
                and 0 <= bounds[1] <= HARD_CALLS - UTTERANCE_CALLS)

    def _rotation_job(self):
        active = self.active
        if (self._is_stopping() or active is None or self.failed or self._warm is not None
                or self.standby is not None or self.standby_failed or self.leased is not None
                or not self._alive(active)):
            return None
        bounds = self._age_calls(active)
        if bounds is None or (bounds[0] < self.rotate_after_s and bounds[1] < self.rotate_after_calls):
            return None
        job = self._warm = _Warm('standby')
        return job

    def _promote(self):
        standby = self.standby
        if standby is None or self.failed:
            return
        if not self._servable(standby):
            self.standby = None
            self.retiring.append(standby)
            self.standby_failed = True
            return
        if self.leased is not None:
            return       # the old worker finishes its utterance first
        if self.active is not None:
            self.retiring.append(self.active)
        self.active, self.standby = standby, None
        self.standby_failed = False
        self.rotations += 1

    def _status(self):
        if self.failed or self.unclean:
            return 'failed'
        if self.active is not None and self._servable(self.active):
            return 'ready'
        if self.standby is not None and self._servable(self.standby):
            return 'ready'
        if self._warm is not None and self._warm.role == 'active':
            return 'warming'
        return 'failed'

    def _drain(self):
        """ops held, lock not held: close swapped-out workers."""
        with self.lock:
            workers, self.retiring = self.retiring, []
        for worker in workers:
            if not self._close(worker):
                with self.lock:
                    self.unclean.append(worker)
                    self.cleanup_failures += 1
                    self.failed = True

    def _close(self, worker):
        wlock = getattr(worker, 'lock', None)
        if callable(getattr(wlock, 'acquire', None)) and callable(getattr(wlock, 'release', None)):
            started = time.monotonic()
            try:
                worker.sealed = None
            except Exception:
                pass
            acquired = wlock.acquire(blocking=False)
            while not acquired:
                self._interrupt_worker(worker, escalate=(time.monotonic() - started >= KILL_ESCALATE_S))
                acquired = wlock.acquire(timeout=0.02)
            try:
                return worker.close() is True
            except Exception:
                return False
            finally:
                wlock.release()
        try:
            return worker.close() is True
        except Exception:
            return False

    def _find_close_task(self, worker):
        with self.lock:
            for w, task in self._closing:
                if w is worker:
                    return task
        return None

    def _clear_close_task(self, worker, task):
        with self.lock:
            self._closing = [(w, t) for w, t in self._closing if not (w is worker and t is task)]

    def _await_close_task(self, worker, task, deadline, *, strict_deadline):
        if not strict_deadline:
            task.done.wait()
        else:
            remaining = max(0.0, deadline - time.monotonic())
            if not task.done.wait(remaining):
                self._interrupt_worker(worker, escalate=True)
                return False
        self._clear_close_task(worker, task)
        return task.ok

    def _close_bounded(self, worker, deadline, *, strict_deadline):
        existing = self._find_close_task(worker)
        if existing is not None:
            if not existing.done.is_set():
                return self._await_close_task(worker, existing, deadline, strict_deadline=strict_deadline)
            self._clear_close_task(worker, existing)
            if existing.ok:
                return True
        if not strict_deadline:
            return self._close(worker)
        task = _CloseTask(worker)
        with self.lock:
            self._closing.append((worker, task))
        def run_close():
            try:
                task.ok = self._close(worker)
            finally:
                task.done.set()
        try:
            _daemon(run_close)
        except Exception:
            self._clear_close_task(worker, task)
            self._interrupt_worker(worker, escalate=True)
            return False
        return self._await_close_task(worker, task, deadline, strict_deadline=True)

    @staticmethod
    def _interrupt_worker(worker, *, escalate=False):
        """Interrupt a warming worker without closing parent pipe fds across threads."""
        cancel = getattr(worker, 'cancel_start', None)
        if callable(cancel):
            try:
                cancel(escalate=escalate)
            except TypeError:
                try:
                    cancel()
                except Exception:
                    pass
            except Exception:
                pass
        try:
            proc = getattr(worker, 'process', None)
            if proc is None:
                return
            poll = getattr(proc, 'poll', None)
            if callable(poll) and poll() is not None:
                return
            pid = getattr(proc, 'pid', None)
            sig = signal.SIGKILL if escalate else signal.SIGTERM
            if type(pid) is int and pid > 0:
                try:
                    os.killpg(pid, sig)
                    return
                except OSError:
                    pass
            action = getattr(proc, 'kill' if escalate else 'terminate', None)
            if callable(action):
                action()
        except Exception:
            pass

    def _reap_abandoned_warm(self, job):
        deadline = time.monotonic() + SHUTDOWN_BUDGET_S
        while not job.finished.is_set() and time.monotonic() < deadline:
            if self.cancel_warm(escalate=True, target=job) is None and job.finished.is_set():
                break
            if job.finished.wait(0.05):
                break

    def _wait_warm_cancelled(self, job, deadline):
        started = time.monotonic()
        while not job.finished.is_set():
            self.cancel_warm(escalate=(time.monotonic() - started >= KILL_ESCALATE_S), target=job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if job.finished.wait(min(0.05, remaining)):
                break
        finished = job.finished.is_set()
        if not finished:
            try:
                _daemon(lambda: self._reap_abandoned_warm(job))
            except Exception:
                pass
        return finished

    def _spawn(self, job):
        """lock not held. A synchronous spawner runs the whole warm here."""
        try:
            job.origin = threading.current_thread()
            self.spawn_thread(lambda: self._run(job))
            return True
        except Exception:
            with self.lock:
                if self._warm is job:
                    self._warm = None
                if job.role == 'active':
                    self.failed = True
                else:
                    self.standby_failed = True
            job.finished.set()
            return False

    def _run(self, job):
        """The warm thread: factory() + start(); installs the worker unless cancelled."""
        worker, ok = None, False
        try:
            worker = self.factory()
            with self.lock:
                job.worker = worker
                phase = self.prompt_phase
                if self._is_stopping():
                    job.cancelled = True
                cancelled = job.cancelled
            if phase is not None and getattr(worker, 'prompt_phase', None) != phase:
                configure_prompt = getattr(worker, 'configure_prompt', None)
                if callable(configure_prompt):
                    prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
                    configure_prompt(prompt_for_phase(phase), phase=phase)
                else:
                    configure = getattr(worker, 'configure_prompt_phase', None)
                    if callable(configure):
                        configure(phase)
            if not cancelled:
                worker.start(timeout_s=START_TIMEOUT_S)
                ok = True
        except BaseException:   # background thread: nothing above it; record and clean up
            ok = False
        process = getattr(worker, 'process', None)
        if isinstance(process, subprocess.Popen):
            job.process, job.creator = process, threading.current_thread()
            if job.creator is job.origin:
                # The inline spawner is a synthetic-test seam, not an owner for
                # a real child. Do not publish a worker whose caller can exit.
                ok = False
            else:
                with self.lock:
                    self._creators.append(job)
        with self.lock:
            if self._is_stopping():
                job.cancelled = True
            install = ok and not job.cancelled and self._warm is job
            if install:
                if job.role == 'active':
                    self.active = worker
                else:
                    self.standby = worker
                self._warm = None
        if not install:
            closed = True if worker is None else self._close(worker)
            with self.lock:
                job.close_ok = closed
                if not closed:
                    self.unclean.append(worker)
                    self.cleanup_failures += 1
                    self.failed = True
                if not job.cancelled:
                    if job.role == 'active':
                        self.failed = True
                    else:
                        self.standby_failed = True
                if self._warm is job:
                    self._warm = None
        job.finished.set()
        if job.process is not None and job.creator is not job.origin:
            # Linux PDEATHSIG follows the thread which created the process,
            # not the lifetime of the containing parent process. Publishing
            # ready must not let this exact creator exit and kill its worker.
            # Warm completion is separate: callers can use/close the worker.
            try:
                while job.process.poll() is None:
                    job.creator_done.wait(0.02)
            except BaseException:
                # Keep the exact handle for bounded cleanup; no error text.
                with self.lock:
                    self.failed = True
            finally:
                job.creator_done.set()

    def _join_creators(self, deadline=None):
        """Prune exited owners, or join them inside the caller's stop budget."""
        with self.lock:
            jobs = list(self._creators)
        complete = True
        for job in jobs:
            if deadline is not None:
                job.creator.join(max(0.0, deadline - time.monotonic()))
            if job.creator.is_alive() or not job.creator_done.is_set():
                complete = False
                continue
            try:
                reaped = job.process.poll() is not None
            except Exception:
                reaped = False
            if not reaped:
                complete = False
                continue
            with self.lock:
                self._creators = [current for current in self._creators if current is not job]
        return complete


class ArmEvidencePowerSource:
    """warm.py PowerStateSource. See the module docstring: [D] without an SSAP power
    read, an authenticated TV arm request is a fresh 'active' reading; never ICMP/ARP
    presence. `clock` must be the controller's monotonic clock."""

    def __init__(self, clock, ssap=None):
        if not callable(clock):
            raise ValueError('POWER_CLOCK')
        self.clock, self.ssap = clock, ssap
        self.lock = threading.Lock()
        self.evidence = None

    def note_active(self):
        """Only from the authenticated arm path (peer == TV + voice key, trusted LAN)."""
        self.note_power('active')

    def note_power(self, state):
        if state not in ('active', 'standby', 'off'):
            return
        # The TV heartbeat has a coarse standby value. Normalize it into the
        # warm policy's existing inactive bucket, not an unknown observation.
        # This does not identify the TV's precise hardware sleep state.
        if state == 'standby':
            state = 'active_standby'
        try:
            now = self.clock()
        except Exception:
            return
        at = warm._seconds(now)
        if at is None:
            return
        with self.lock:
            if self.evidence is None or at >= self.evidence[1]:
                self.evidence = (state, now)

    def current(self):
        if self.ssap is not None:
            try:
                value = self.ssap.current()
            except Exception:
                value = None
            if (type(value) is tuple and len(value) == 2 and type(value[0]) is str
                    and value[0] in warm.POWER_STATES):
                # The fresher valid observation (not in the future) wins and supersedes
                # older arm evidence so a later SSAP socket drop cannot resurrect it.
                at = warm._seconds(value[1])
                if at is not None:
                    with self.lock:
                        try:
                            now = warm._seconds(self.clock())
                        except Exception:
                            now = None
                        if now is not None and at <= now:
                            if self.evidence is None or at >= self.evidence[1]:
                                self.evidence = value
                                return value
                            return self.evidence
        with self.lock:
            return self.evidence


class _NoPause:
    """Default kill switch: warm.py calls pause('AUDIT_WRITE') only when an audit row
    cannot be written. The service passes its real kill switch."""
    def pause(self, *_args):
        return None


class _HookSource:
    """Suppress 'active' power observations once WarmHook shutdown has started."""
    def __init__(self, source, stopping):
        self._source, self._stopping = source, stopping

    def current(self):
        if self._stopping.is_set():
            return None
        value = self._source.current()
        return None if self._stopping.is_set() else value

    def __getattr__(self, name):
        return getattr(self._source, name)


class _HookPool:
    """Prevent an in-flight WarmController tick from starting a warm after shutdown."""
    def __init__(self, pool, stopping):
        self._pool, self._stopping = pool, stopping

    def begin_warm(self):
        if self._stopping.is_set():
            raise RuntimeError('WARM_STOPPING')
        try:
            out = self._pool.begin_warm()
        finally:
            if self._stopping.is_set():
                cancel = getattr(self._pool, 'cancel_warm', None)
                if callable(cancel):
                    try:
                        cancel(escalate=False)
                    except Exception:
                        pass
        if self._stopping.is_set():
            raise RuntimeError('WARM_STOPPING')
        return out

    def poll(self):
        if self._stopping.is_set():
            return 'failed'
        return self._pool.poll()

    def unload(self):
        if self._stopping.is_set():
            return False
        return self._pool.unload()

    def __getattr__(self, name):
        return getattr(self._pool, name)


class WarmHook:
    def __init__(self, pool, source, *, clock, audit, killswitch=None, tick_s=1.0,
                 spawn_thread=None):
        if not _number(tick_s) or not 0 < tick_s <= 60:
            raise ValueError('WARM_TICK')
        self.pool, self.source = pool, source
        self.stopping = threading.Event()
        stop_events = getattr(pool, '_stop_events', None)
        if type(stop_events) is list:
            stop_events.append(self.stopping)
        self.controller = warm.WarmController(
            _HookPool(pool, self.stopping), _HookSource(source, self.stopping),
            clock=clock, audit=audit,
            killswitch=killswitch if killswitch is not None else _NoPause())
        self.tick_s = tick_s
        self.spawn_thread = spawn_thread if spawn_thread is not None else _daemon
        self.lock = threading.Lock()
        self.running = False
        self._close_abandoned = False
        self._active_ticks = 0
        self._ticks_idle = threading.Event()
        self._ticks_idle.set()
        self._activity_seq = 0
        self._ticked_seq = 0
        self._activity_at = None
        self.wake = threading.Event()
        self.stopped = threading.Event()

    def _warm_pending(self):
        if self.stopping.is_set():
            return False
        with self.lock:
            pending = self._activity_seq > self._ticked_seq
            at = self._activity_at
        if not pending or at is None:
            return False
        if (self.controller.state != 'unloaded' or not self.controller.clean
                or self.controller.clock_fault):
            return False
        try:
            now = warm._seconds(self.controller.clock())
        except Exception:
            return False
        high_water = self.controller.high_water
        if now is None or (high_water is not None and now < high_water):
            return False
        return at <= now and now - at <= warm.STALE_S

    @property
    def state(self):
        state = self.controller.state
        if state == 'unloaded' and self._warm_pending():
            return 'warming'
        return state

    # -- loop ------------------------------------------------------------------------
    def start(self):
        with self.lock:
            if self.running or self.stopping.is_set():
                return False
            self.running = True
        try:
            self.spawn_thread(self._loop)
        except Exception:
            with self.lock:
                self.running = False
            raise
        return True

    def _cleanup_abandoned(self):
        cancel = getattr(self.pool, 'cancel_warm', None)
        if callable(cancel):
            try:
                cancel(escalate=False)
            except Exception:
                pass
        try:
            self.pool.unload(timeout_s=0)
        except TypeError:
            try:
                self.pool.unload()
            except Exception:
                pass
        except Exception:
            pass

    def _run_tick(self):
        if self.stopping.is_set():
            return self.controller.state
        with self.lock:
            if self.stopping.is_set():
                stopped = True
            else:
                stopped = False
                self._active_ticks += 1
                self._ticks_idle.clear()
                seq = self._activity_seq
        if stopped:
            return self.controller.state
        try:
            state = self.controller.tick()
            with self.lock:
                if seq > self._ticked_seq:
                    self._ticked_seq = seq
            return state
        finally:
            abandoned = False
            with self.lock:
                self._active_ticks -= 1
                if self._active_ticks == 0:
                    self._ticks_idle.set()
                    abandoned = self._close_abandoned
            if self.stopping.is_set() and abandoned:
                self._cleanup_abandoned()

    def _loop(self):
        try:
            while not self.stopping.is_set():
                try:
                    self._run_tick()
                except Exception:
                    pass    # warm.tick() does not raise by contract; one bad tick never ends the loop
                self.wake.wait(self.tick_s)
                self.wake.clear()   # a poke after this is covered by the tick that follows
        finally:
            if self.stopping.is_set():
                with self.lock:
                    abandoned = self._close_abandoned
                if abandoned:
                    self._cleanup_abandoned()
            self.stopped.set()

    def tick(self):
        return self._run_tick()

    def poke(self):
        self.wake.set()

    def note_tv_activity(self):
        self.note_tv_power('active')

    def note_tv_power(self, state):
        if self.stopping.is_set() or state not in ('active', 'standby', 'off'):
            return
        note_power = getattr(self.source, 'note_power', None)
        if callable(note_power):
            note_power(state)
        elif state == 'active':
            note = getattr(self.source, 'note_active', None)
            if callable(note):
                note()
        if state == 'active':
            try:
                at = warm._seconds(self.controller.clock())
            except Exception:
                at = None
            with self.lock:
                if at is not None and (self._activity_at is None or at >= self._activity_at):
                    self._activity_at = at
                self._activity_seq += 1
        self.poke()

    # -- press path ------------------------------------------------------------------
    def admit(self):
        if self.stopping.is_set():
            raise _PoolRefused('VOICE_UNAVAILABLE')
        try:
            return self.controller.admit()
        except Exception as exc:
            code = getattr(exc, 'code', str(exc))
            if code == 'VOICE_UNAVAILABLE' and self._warm_pending():
                raise _PoolRefused('VOICE_WARMING_UP') from None
            if isinstance(exc, _PoolRefused):
                raise
            if isinstance(exc, Refused) or type(exc).__name__ == 'Refused':
                raise _PoolRefused(code) from None
            raise

    def release(self, ticket):
        return self.controller.release(ticket)

    def lease(self):
        return self.pool.lease()

    def give_back(self, worker):
        return self.pool.give_back(worker)

    def configure_prompt(self, system_prompt, *, phase=None):
        configure = getattr(self.pool, 'configure_prompt', None)
        if not callable(configure):
            raise _PoolRefused('MODEL_CONFIG')
        return configure(system_prompt, phase=phase)

    def configure_prompt_phase(self, phase):
        configure = getattr(self.pool, 'configure_prompt_phase', None)
        if not callable(configure):
            raise _PoolRefused('WORKER_INIT')
        configure(phase)

    def status(self):
        warm_status = dict(self.controller.status())
        if warm_status.get('state') == 'unloaded' and self._warm_pending():
            warm_status['state'] = 'warming'
            warm_status['power'] = 'active'
        return {'warm': warm_status, 'pool': self.pool.summary()}

    def close(self, *, timeout_s=None):
        """Cancel any in-flight warm, stop the loop, wait for any in-flight tick, and
        unload the pool within one shared shutdown budget. True only when the loop and
        in-flight ticks stopped and unload was verified."""
        if timeout_s is not None and (not _number(timeout_s) or not 0 <= timeout_s <= SHUTDOWN_BUDGET_S):
            raise ValueError('POOL_CONFIG')
        budget = float(getattr(self.pool, 'join_s', SHUTDOWN_BUDGET_S) if timeout_s is None else timeout_s)
        deadline = time.monotonic() + budget
        self.stopping.set()
        self.wake.set()
        sources_closed = True
        seen_sources = []
        for source_obj in (self.source, getattr(self.source, 'ssap', None)):
            if any(source_obj is seen for seen in seen_sources):
                continue
            seen_sources.append(source_obj)
            close_source = getattr(source_obj, 'close', None)
            if callable(close_source):
                try:
                    closed = close_source(timeout_s=min(1.0, max(0.0, deadline - time.monotonic())))
                except TypeError:
                    try:
                        closed = close_source()
                    except Exception:
                        closed = False
                except Exception:
                    closed = False
                sources_closed = closed is True and sources_closed
        cancel = getattr(self.pool, 'cancel_warm', None)
        if callable(cancel):
            try:
                cancel(escalate=False)
            except Exception:
                pass
        with self.lock:
            running = self.running
        stopped = self.stopped.wait(max(0.0, deadline - time.monotonic())) if running else True
        ticks_idle = self._ticks_idle.wait(max(0.0, deadline - time.monotonic()))
        if not (stopped and ticks_idle):
            with self.lock:
                self._close_abandoned = True
        remaining = max(0.0, deadline - time.monotonic())
        try:
            unloaded = self.pool.unload(timeout_s=remaining) is True
        except TypeError:
            try:
                unloaded = self.pool.unload() is True
            except Exception:
                unloaded = False
        except Exception:
            unloaded = False
        return stopped and ticks_idle and unloaded and sources_closed


def _load_backend_symbol(module_name, symbol):
    import importlib
    from pathlib import Path
    import sys
    here = Path(__file__).resolve().parent
    pkg = __package__ or ''
    candidates = []
    if pkg:
        candidates.append(pkg + '.' + module_name)
        if '.' in pkg:
            parent = pkg.rsplit('.', 1)[0]
            candidates.append(parent + '.backend.' + module_name)
            candidates.append(parent + '.runtime.' + module_name)
    if not pkg:
        for extra in (here, here.parent / 'backend'):
            if (extra / (module_name + '.py')).is_file() and str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
    candidates.extend((
        module_name,
        'voice_control.' + module_name,
        'voice.backend.' + module_name,
        'voice.runtime.' + module_name,
    ))
    for target in candidates:
        try:
            mod = importlib.import_module(target)
            return getattr(mod, symbol)
        except (ImportError, AttributeError):
            continue
    raise ImportError(module_name + '.' + symbol)


def expected_model_endpoints(asr_path=None, intent_path=None):
    from pathlib import Path
    models = _load_backend_symbol('pinned_models', 'MODELS')
    by_role = {}
    for entry in models:
        role = entry.get('role')
        repo = entry.get('repository') or entry.get('repo')
        if role not in ('asr', 'intent') or role in by_role or not repo:
            raise ValueError('MODEL_ENDPOINT_SPEC')
        by_role[role] = {
            'role': role,
            'repository': repo,
            'repo': repo,
            'revision': entry['revision'],
            'fileCount': len(entry['files']),
            'files': tuple(f['file'] for f in entry['files']),
            'path': None,
        }
    if set(by_role) != {'asr', 'intent'}:
        raise ValueError('MODEL_ENDPOINT_SPEC')
    if asr_path is not None:
        by_role['asr']['path'] = str(Path(asr_path))
    if intent_path is not None:
        by_role['intent']['path'] = str(Path(intent_path))
    return by_role


class ProductionWorkerFactory:
    """Production factory wrapping ModelWorker with pinned model endpoint verification."""

    def __init__(self, python, asr_path, intent_path, *, qualified=True, clock=time.monotonic,
                 spawn=None, prompt_phase=None, verify_assets=False, worker_cls=None):
        import hashlib as _hashlib
        from pathlib import Path
        if (type(qualified) is not bool or not callable(clock) or prompt_phase not in (None, 'P5')
                or type(verify_assets) is not bool):
            raise _PoolRefused('WORKER_INIT')
        if any(type(p) not in (str, Path) for p in (python, asr_path, intent_path)):
            raise _PoolRefused('WORKER_INIT')
        self.python = Path(python)
        self.asr_path = Path(asr_path)
        self.intent_path = Path(intent_path)
        if (not self.python.is_absolute() or not self.asr_path.is_absolute()
                or not self.intent_path.is_absolute() or self.asr_path == self.intent_path):
            raise _PoolRefused('WORKER_INIT')
        if spawn is not None and not callable(spawn):
            raise _PoolRefused('WORKER_INIT')
        self.qualified = qualified
        self.clock = clock
        self.spawn = spawn
        self.prompt_phase = prompt_phase
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        self.prompt_sha256 = _hashlib.sha256(prompt_for_phase(prompt_phase).encode('utf-8')).hexdigest()
        self.worker_cls = worker_cls if worker_cls is not None else _load_backend_symbol('model_worker', 'ModelWorker')
        self.endpoints = expected_model_endpoints(self.asr_path, self.intent_path)
        if verify_assets:
            self.verify_endpoints()

    def expected_endpoints(self):
        return {role: dict(info) for role, info in self.endpoints.items()}

    def verify_endpoints(self):
        verify = _load_backend_symbol('model_assets', 'verify')
        try:
            return verify(self.asr_path, self.intent_path)
        except Exception:
            raise _PoolRefused('WORKER_ASSETS_UNQUALIFIED') from None

    def configure_prompt(self, system_prompt, *, phase=None):
        import hashlib as _hashlib
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        try:
            expected = prompt_for_phase(phase)
        except Exception:
            raise _PoolRefused('MODEL_CONFIG') from None
        if type(system_prompt) is not str or system_prompt != expected:
            raise _PoolRefused('MODEL_CONFIG')
        self.prompt_phase = phase
        self.prompt_sha256 = _hashlib.sha256(system_prompt.encode('utf-8')).hexdigest()

    def configure_prompt_phase(self, phase):
        if phase not in (None, 'P5'):
            raise _PoolRefused('WORKER_INIT')
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        self.configure_prompt(prompt_for_phase(phase), phase=phase)

    def __call__(self):
        kwargs = {'qualified': self.qualified, 'clock': self.clock, 'prompt_phase': self.prompt_phase}
        if self.spawn is not None:
            kwargs['spawn'] = self.spawn
        return self.worker_cls(self.python, self.asr_path, self.intent_path, **kwargs)


def model_factory(python, asr_path, intent_path, **kwargs):
    return ProductionWorkerFactory(python, asr_path, intent_path, **kwargs)


create_worker_factory = model_factory


def create_production_pool(python, asr_path, intent_path, *, qualified=True, clock=time.monotonic,
                           spawn=None, prompt_phase=None, verify_assets=False, worker_cls=None,
                           rotate_after_s=1500, rotate_after_calls=240, join_s=SHUTDOWN_BUDGET_S,
                           spawn_thread=None):
    factory = ProductionWorkerFactory(
        python, asr_path, intent_path, qualified=qualified, clock=clock,
        spawn=spawn, prompt_phase=prompt_phase, verify_assets=verify_assets, worker_cls=worker_cls)
    return WorkerPool(
        factory, clock=clock, spawn_thread=spawn_thread,
        rotate_after_s=rotate_after_s, rotate_after_calls=rotate_after_calls, join_s=join_s)


def _private_bytes(path, limit=4096):
    from pathlib import Path
    import stat as _stat
    p = Path(path)
    if not p.is_absolute():
        raise _PoolRefused('TV_CREDENTIAL_UNSAFE')
    fd = None
    buf = bytearray()
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (info.st_mode & 0o077):
            raise _PoolRefused('TV_CREDENTIAL_UNSAFE')
        if info.st_size <= 0 or info.st_size > limit:
            raise _PoolRefused('TV_CREDENTIAL_INVALID')
        while len(buf) < info.st_size:
            chunk = os.read(fd, info.st_size - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        now = os.fstat(fd)
        if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise _PoolRefused('TV_CREDENTIAL_CHANGED')
        return buf
    except _PoolRefused:
        buf[:] = b'\0' * len(buf)
        raise
    except OSError:
        buf[:] = b'\0' * len(buf)
        raise _PoolRefused('TV_CREDENTIAL_MISSING') from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class AuthenticatedTvPowerReader:
    """Authenticated read-only SSAP power/activity reader for ArmEvidencePowerSource.

    Supports either:
      * transport + key_path + pin_path (pinned TLS certificate SHA-256 + 0600 client key,
        querying ssap://com.webos.service.tvpower/power/getPowerState and parsing with
        warm.power_from_reply),
      * gateway (a TVGateway-compatible object providing transport/key_path/pin_path or
        read_power()), or
      * read_fn (callable returning an SSAP reply dict, power state string, or
        (state, timestamp) tuple).
    """

    POWER_URI = 'ssap://com.webos.service.tvpower/power/getPowerState'
    FOREGROUND_URI = 'ssap://com.webos.applicationManager/getForegroundAppInfo'

    def __init__(self, *, clock=time.monotonic, transport=None, key_path=None, pin_path=None,
                 gateway=None, read_fn=None, timeout_s=3.0, poll_interval_s=warm.POLL_MAX_S,
                 auto_poll=True, allow_foreground_fallback=False, spawn_thread=None):
        if not callable(clock):
            raise ValueError('POWER_CLOCK')
        if (not _number(timeout_s) or not 0 < timeout_s <= 10 or
                not _number(poll_interval_s) or not 0 < poll_interval_s <= warm.POLL_MAX_S or
                type(auto_poll) is not bool or type(allow_foreground_fallback) is not bool):
            raise ValueError('POWER_CONFIG')
        if gateway is not None:
            if transport is None:
                transport = getattr(gateway, 'transport', None)
            if key_path is None:
                key_path = getattr(gateway, 'key_path', None)
            if pin_path is None:
                pin_path = getattr(gateway, 'pin_path', None)
        has_ssap = (transport is not None and callable(getattr(transport, 'connect', None))
                    and key_path is not None and pin_path is not None)
        has_gateway_fn = gateway is not None and (
            callable(getattr(gateway, 'read_power', None)) or callable(getattr(gateway, 'power_state', None)))
        if not (callable(read_fn) or has_ssap or has_gateway_fn):
            raise ValueError('POWER_CONFIG')
        self.clock = clock
        self.transport = transport
        self.key_path = key_path
        self.pin_path = pin_path
        self.gateway = gateway
        self.read_fn = read_fn
        self.timeout_s = float(timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        self.auto_poll = auto_poll
        self.allow_foreground_fallback = allow_foreground_fallback
        self.spawn_thread = spawn_thread if spawn_thread is not None else _daemon
        self.lock = threading.Lock()
        self.io_lock = getattr(gateway, 'lock', None) or threading.Lock()
        self._observation = None
        self._last_poll_at = None
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.running = False

    def _read_ssap_material(self):
        import json as _json
        import re as _re
        raw_key = _private_bytes(self.key_path, 256)
        raw_pin = _private_bytes(self.pin_path, 4096)
        try:
            key = raw_key.decode('utf-8').strip()
            if not _re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
                raise _PoolRefused('TV_CREDENTIAL_INVALID')
            pin_doc = _json.loads(raw_pin.decode('utf-8'))
            if type(pin_doc) is not dict or set(pin_doc) != {'certificate_sha256'}:
                raise _PoolRefused('TV_CREDENTIAL_INVALID')
            pin = pin_doc.get('certificate_sha256')
            if type(pin) is not str or not _re.fullmatch(r'[a-f0-9]{64}', pin):
                raise _PoolRefused('TV_CREDENTIAL_INVALID')
            return key, pin, raw_key
        except _PoolRefused:
            raw_key[:] = b'\0' * len(raw_key)
            raise
        except Exception:
            raw_key[:] = b'\0' * len(raw_key)
            raise _PoolRefused('TV_CREDENTIAL_INVALID') from None
        finally:
            raw_pin[:] = b'\0' * len(raw_pin)

    def _query_authenticated_ssap(self):
        key = pin = raw_key = conn = None
        try:
            key, pin, raw_key = self._read_ssap_material()
            with self.io_lock:
                conn = self.transport.connect(pin_sha256=pin, timeout_s=self.timeout_s)
                if conn.register(client_key=key, timeout_s=self.timeout_s) is not True:
                    raise _PoolRefused('TV_AUTH_FAILED')
                reply = conn.request(self.POWER_URI, {}, timeout_s=self.timeout_s)
                state = warm.power_from_reply(reply)
                if state is None and self.allow_foreground_fallback:
                    fg = conn.request(self.FOREGROUND_URI, {}, timeout_s=self.timeout_s)
                    if (type(fg) is dict and fg.get('returnValue') is True
                            and type(fg.get('appId')) is str and bool(fg.get('appId').strip())):
                        state = 'active'
                return state
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if raw_key is not None:
                raw_key[:] = b'\0' * len(raw_key)
            key = pin = None

    def _coerce_reading(self, raw, observed_at):
        if type(raw) is tuple and len(raw) == 2:
            state, ts = raw
            if type(state) is str and state in warm.POWER_STATES and warm._seconds(ts) is not None:
                return (state, ts)
            return None
        if type(raw) is dict:
            state = warm.power_from_reply(raw)
            if state is None and self.allow_foreground_fallback:
                if raw.get('returnValue') is True and type(raw.get('appId')) is str and bool(raw.get('appId').strip()):
                    state = 'active'
            return (state, observed_at) if state in warm.POWER_STATES else None
        if type(raw) is str and raw in warm.POWER_STATES:
            return (raw, observed_at)
        return None

    def poll_once(self):
        if self.stopping.is_set():
            return None
        try:
            now = self.clock()
        except Exception:
            return None
        if warm._seconds(now) is None:
            return None
        with self.lock:
            self._last_poll_at = now
        try:
            if callable(self.read_fn):
                raw = self.read_fn()
            elif self.gateway is not None and callable(getattr(self.gateway, 'read_power', None)):
                raw = self.gateway.read_power()
            elif self.gateway is not None and callable(getattr(self.gateway, 'power_state', None)):
                raw = self.gateway.power_state()
            else:
                raw = self._query_authenticated_ssap()
            observed_at = self.clock()
        except Exception:
            return self._current_cached()
        if warm._seconds(observed_at) is None:
            return self._current_cached()
        reading = self._coerce_reading(raw, observed_at)
        if reading is not None:
            at = warm._seconds(reading[1])
            with self.lock:
                if at is not None and (self._observation is None or at >= self._observation[1]):
                    self._observation = reading
        return self._current_cached()

    def _current_cached(self):
        with self.lock:
            return self._observation

    def current(self):
        if self.stopping.is_set():
            return None
        with self.lock:
            running = self.running
            last_poll = self._last_poll_at
            obs = self._observation
        if self.auto_poll and not running:
            try:
                now = warm._seconds(self.clock())
            except Exception:
                now = None
            if now is not None and (last_poll is None or now - last_poll >= self.poll_interval_s):
                return self.poll_once()
        return obs

    def start(self):
        with self.lock:
            if self.running or self.stopping.is_set():
                return False
            self.running = True
        try:
            self.spawn_thread(self._loop)
        except Exception:
            with self.lock:
                self.running = False
            raise
        return True

    def _loop(self):
        try:
            while not self.stopping.is_set():
                try:
                    self.poll_once()
                except Exception:
                    pass
                self.wake.wait(self.poll_interval_s)
                self.wake.clear()
        finally:
            self.stopped.set()

    def close(self, *, timeout_s=5.0):
        self.stopping.set()
        self.wake.set()
        with self.lock:
            running = self.running
        if not running:
            return True
        wait_s = max(0.0, min(float(timeout_s), SHUTDOWN_BUDGET_S)) if _number(timeout_s) else 5.0
        return self.stopped.wait(wait_s)


SsapPowerSource = AuthenticatedTvPowerReader


def create_warm_hook(pool, *, clock=None, ssap=None, source=None, audit=None, killswitch=None,
                     tick_s=1.0, spawn_thread=None):
    if clock is None:
        clock = getattr(pool, 'clock', time.monotonic)
    if source is None:
        source = ArmEvidencePowerSource(clock, ssap=ssap)
    if audit is None:
        try:
            ChainedAudit = _load_backend_symbol('result', 'ChainedAudit')
        except ImportError:
            from result import ChainedAudit
        audit = ChainedAudit('', persistent=False)
    return WarmHook(
        pool, source, clock=clock, audit=audit, killswitch=killswitch,
        tick_s=tick_s, spawn_thread=spawn_thread)


class _UtteranceLeaseCoordinator:
    """Shares one leased pool worker (and optional WarmHook ticket) across ASR + intent
    for a single utterance, guaranteeing release on completion, failure, or cancellation."""

    def __init__(self, target):
        if isinstance(target, WarmHook) or (hasattr(target, 'pool') and hasattr(target, 'admit')):
            self.hook = target
            self.pool = target.pool
        else:
            self.hook = None
            self.pool = target
        self.lock = threading.Lock()
        self._worker = None
        self._ticket = None
        self._generation = 0
        self._acquiring = False

    def note_activity(self):
        if self.hook is not None:
            note = getattr(self.hook, 'note_tv_activity', None)
            if callable(note):
                note()

    def acquire(self):
        with self.lock:
            if self._worker is not None:
                return self._worker, False
            if self._acquiring:
                raise _PoolRefused('VOICE_BUSY')
            generation = self._generation
            self._acquiring = True
        ticket = None
        worker = None
        published = False
        try:
            if self.hook is not None:
                ticket = self.hook.admit()
            worker = self.pool.lease()
            with self.lock:
                if generation != self._generation:
                    raise _PoolRefused('VOICE_CANCELLED')
                self._worker = worker
                self._ticket = ticket
                published = True
            return worker, True
        finally:
            with self.lock:
                self._acquiring = False
            if not published:
                try:
                    if worker is not None:
                        self.pool.give_back(worker)
                finally:
                    if ticket is not None and self.hook is not None:
                        self.hook.release(ticket)

    def release(self):
        with self.lock:
            self._generation += 1
            worker, self._worker = self._worker, None
            ticket, self._ticket = self._ticket, None
        ok = True
        if worker is not None:
            try:
                ok = self.pool.give_back(worker) is True and ok
            except Exception:
                ok = False
        if ticket is not None and self.hook is not None:
            try:
                ok = self.hook.release(ticket) is True and ok
            except Exception:
                ok = False
        return ok


class PoolASR:
    """Utterance-scoped ASR provider backed by WorkerPool or WarmHook."""

    def __init__(self, pool_or_hook, *, refused=Refused, coordinator=None, hold_for_intent=False):
        self.coordinator = coordinator or _UtteranceLeaseCoordinator(pool_or_hook)
        self.refused = refused
        self.hold_for_intent = hold_for_intent
        self._worker_asr = _load_backend_symbol('model_worker', 'WorkerASR')

    @property
    def qualified(self):
        pool = self.coordinator.pool
        hook = self.coordinator.hook
        if hook is not None and (hook.stopping.is_set() or hook.controller.state in ('degraded', 'paused')):
            return False
        with pool.lock:
            worker = pool.standby if (pool.standby is not None and pool._servable(pool.standby) and pool.leased is None) else pool.active
            if worker is None or not pool._servable(worker):
                return False
            return getattr(worker, 'qualified', False) is True

    def transcribe(self, pcm, *, rate=16000, timeout_s=8):
        self.coordinator.note_activity()
        try:
            worker, _ = self.coordinator.acquire()
        except Exception as exc:
            code = getattr(exc, 'code', str(exc))
            raise self.refused(code if code in ('VOICE_WARMING_UP', 'VOICE_BUSY') else 'ASR_UNAVAILABLE') from None
        try:
            return self._worker_asr(worker).transcribe(pcm, rate=rate, timeout_s=timeout_s)
        except Exception:
            self.coordinator.release()
            raise self.refused('ASR_UNAVAILABLE') from None
        finally:
            if not self.hold_for_intent:
                self.coordinator.release()


class PoolIntent:
    """Utterance-scoped Intent provider backed by WorkerPool or WarmHook."""

    def __init__(self, pool_or_hook, *, refused=Refused, prompt_phase=None, coordinator=None):
        import hashlib as _hashlib
        if prompt_phase not in (None, 'P5'):
            raise refused('WORKER_INIT')
        self.coordinator = coordinator or _UtteranceLeaseCoordinator(pool_or_hook)
        self.refused = refused
        self.prompt_phase = prompt_phase
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        self._prompt_sha256 = _hashlib.sha256(prompt_for_phase(prompt_phase).encode('utf-8')).hexdigest()
        self._worker_intent = _load_backend_symbol('model_worker', 'WorkerIntent')
        if prompt_phase is not None:
            self.configure_prompt_phase(prompt_phase)

    @property
    def prompt_sha256(self):
        return getattr(self.coordinator.pool, 'prompt_sha256', None) or self._prompt_sha256

    def configure_prompt(self, system_prompt, *, phase=None):
        import hashlib as _hashlib
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        try:
            expected = prompt_for_phase(phase)
        except Exception:
            raise self.refused('MODEL_CONFIG') from None
        if type(system_prompt) is not str or system_prompt != expected:
            raise self.refused('MODEL_CONFIG')
        self.prompt_phase = phase
        self._prompt_sha256 = _hashlib.sha256(system_prompt.encode('utf-8')).hexdigest()
        configure = getattr(self.coordinator.pool, 'configure_prompt', None)
        if callable(configure):
            configure(system_prompt, phase=phase)

    def configure_prompt_phase(self, phase):
        if phase not in (None, 'P5'):
            raise self.refused('WORKER_INIT')
        prompt_for_phase = _load_backend_symbol('local_qwen', 'prompt_for_phase')
        self.configure_prompt(prompt_for_phase(phase), phase=phase)

    def interpret(self, transcript, *, timeout_s=8):
        try:
            worker, _ = self.coordinator.acquire()
        except Exception as exc:
            code = getattr(exc, 'code', str(exc))
            raise self.refused(code if code in ('VOICE_WARMING_UP', 'VOICE_BUSY') else 'MODEL_UNAVAILABLE') from None
        try:
            provider = self._worker_intent(worker)
            return provider.interpret(transcript, timeout_s=timeout_s)
        finally:
            self.coordinator.release()


def pool_providers(pool_or_hook, *, asr_refused=Refused, intent_refused=Refused, prompt_phase=None):
    coordinator = _UtteranceLeaseCoordinator(pool_or_hook)
    asr = PoolASR(pool_or_hook, refused=asr_refused, coordinator=coordinator, hold_for_intent=True)
    intent = PoolIntent(pool_or_hook, refused=intent_refused, prompt_phase=prompt_phase, coordinator=coordinator)
    return coordinator, asr, intent


WarmModelPool = WorkerPool
TvPowerHook = WarmHook
TvPowerObservationAdapter = ArmEvidencePowerSource

