"""Finite local key-event owner. No socket, TV API, model or LG fallback.

The native assembler is disabled. A future authenticated global route must bind
one ordered event source, and a verified factory must return a fresh reviewed
CurrentHostCapture wired to the RAM stop signal. Signals are requests, not proof
that hardware stopped. Only the current full capture/cleanup join permits reuse.
"""
import math
import threading
import time
from .current_host import CurrentHostCapture
from .physical_consumer import OneUsePhysicalConsumer
from .join_schema import qualified_current
from .cleanup_join import cleaned_current


def duration(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError('LIFECYCLE_TIMEOUT')
    return value


class CaptureSignals:
    """One invocation's bounded mailbox; contains no audio, text or identity."""
    def __init__(self):
        self._condition = threading.Condition()
        self._released = self._cancelled = self._closed = False
        self._reason = None

    def _request(self, reason):
        with self._condition:
            if self._closed:
                return False
            if reason == 'release':
                self._released = True
            elif reason in ('cancel', 'suspend', 'close', 'hold-timeout', 'lifetime', 'owner-lost'):
                self._cancelled = True
                if self._reason is None:
                    self._reason = reason
            else:
                raise ValueError('LIFECYCLE_SIGNAL')
            self._condition.notify_all()
            return True

    def snapshot(self):
        with self._condition:
            return dict(released=self._released, cancelled=self._cancelled,
                        closed=self._closed, reason=self._reason)

    def wait_stop(self, timeout_s=1):
        duration(timeout_s, 15)
        with self._condition:
            self._condition.wait_for(
                lambda: self._released or self._cancelled or self._closed, timeout_s)
            return self.snapshot()

    def _close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class RemoteMicLifecycle:
    """Single active attempt, no queue, no daemon persistence or native arming.

    Key event methods are bounded and never wait for capture or Manager locks.
    A worker continues its independent cleanup after cancellation. A local join
    is never substituted for native stop/cleanup evidence.
    """
    def __init__(self, owner, capture_factory, voice_keys, *, qualified=False,
                 max_attempts=256, lifetime_s=1800, clock=time.monotonic,
                 consumer_clock=time.monotonic, consumer_wait=time.sleep):
        if (type(voice_keys) not in (tuple, list) or not 1 <= len(voice_keys) <= 8
                or any(type(k) is not int for k in voice_keys) or len(set(voice_keys)) != len(voice_keys)
                or type(max_attempts) is not int or not 1 <= max_attempts <= 256
                or not callable(capture_factory) or not 0 < duration(lifetime_s, 1800)):
            raise ValueError('LIFECYCLE_CONFIG')
        self.owner, self.factory, self.keys = owner, capture_factory, frozenset(voice_keys)
        self.manager, self.runtime, self.viewer = owner.manager, owner.runtime, owner.viewer
        self.qualified = qualified is True
        self.max_attempts, self.lifetime_s = max_attempts, lifetime_s
        self.clock, self.consumer_clock, self.consumer_wait = clock, consumer_clock, consumer_wait
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.state = 'new'
        self.started = self.last_clock = None
        self.sequence = self.attempts = 0
        self.pressed = False
        self.press_at = None
        self.suspended = self.closing = self.quarantined = False
        self.job = self.watchdog = None
        self.last = None

    def _bound(self):
        try:
            return (self.owner.manager is self.manager and self.owner.runtime is self.runtime
                    and self.owner.viewer is self.viewer and self.owner.enabled is True
                    and self.manager.closed is False)
        except Exception:
            return False

    def start(self):
        with self.lock:
            if self.state != 'new':
                return False
            if not self.qualified or not self._bound():
                self.state = 'unarmed'
                return False
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now):
                self.state = 'unarmed'
                return False
            self.started = self.last_clock = now
            self.state = 'ready'
            self.watchdog = threading.Thread(target=self._watch, name='local-voice-lifecycle', daemon=False)
            self.watchdog.start()
            return True

    def _watch(self):
        while not self.wake.wait(.05):
            self.tick()
            with self.lock:
                if self.closing:
                    return

    def _cancel_locked(self, reason):
        job = self.job
        if job is not None:
            # Invalidate dispatch before waking the capture's stop mailbox.
            job['cancelled'] = True
            consumer = job['consumer']
            if consumer is not None:
                consumer.cancel()
            job['signals']._request(reason)

    def tick(self):
        self.reap()
        with self.lock:
            if self.started is None or self.closing:
                return
            now = self.clock()
            valid = type(now) in (int, float) and math.isfinite(now) and now >= self.last_clock
            if not valid or now - self.started >= self.lifetime_s:
                self.closing = True
                self.state = 'closed' if self.job is None else 'closing'
                self._cancel_locked('lifetime')
                self.wake.set()
                return
            self.last_clock = now
            if self.job is not None and now - self.job['began'] >= 250:
                self.quarantined = True
                self.state = 'quarantined'
                self._cancel_locked('lifetime')
            if not self._bound():
                self.quarantined = True
                self.state = 'quarantined'
                self._cancel_locked('owner-lost')
            elif self.pressed and now - self.press_at >= 10:
                self._cancel_locked('hold-timeout')

    def handle(self, key, pressed, repeat, *, sequence):
        if type(key) is not int or key not in self.keys:
            return False
        self.tick()
        with self.lock:
            # Always swallow protected voice keys, even malformed, stale,
            # repeated, disabled, exhausted or unavailable local requests.
            if (type(sequence) is not int or not self.sequence < sequence <= 2**53 - 1
                    or type(pressed) is not bool or type(repeat) is not bool):
                return True
            self.sequence = sequence
            if self.state in ('new', 'unarmed') or self.closing or self.suspended or self.quarantined:
                return True
            if not pressed:
                self.pressed = False
                self.press_at = None
                if self.job is not None:
                    self.job['signals']._request('release')
                return True
            if repeat or self.pressed or self.job is not None or self.attempts >= self.max_attempts:
                return True
            self.pressed, self.press_at = True, self.clock()
            self.attempts += 1
            job = dict(signals=CaptureSignals(), consumer=None, thread=None,
                       entered=False, joined=False, cancelled=False, done=False, began=self.clock())
            thread = threading.Thread(target=self._run, args=(job,), name='local-voice-attempt', daemon=False)
            job['thread'] = thread
            self.job = job
            self.state = 'busy'
            try:
                thread.start()
            except Exception:
                self.quarantined = True
                self.state = 'quarantined'
                job['done'] = True
                job['signals']._close()
            return True

    def _run(self, job):
        result = None
        factory_ok = False
        try:
            capture = self.factory(job['signals'])
            if type(capture) is not CurrentHostCapture or capture.used:
                raise ValueError('LIFECYCLE_CAPTURE')
            factory_ok = True
            observed = False
            def cleaned(row):
                nonlocal observed
                with self.lock:
                    if (observed or self.job is not job or job['done']
                            or threading.current_thread() is not job['thread']):
                        raise ValueError('LIFECYCLE_CLEANUP_REPLAY')
                    observed = True
                cleaned_current(row)
                with self.lock:
                    job['joined'] = True
            capture.bind_cleanup(cleaned)
            def current(deliver, *, timeout_s):
                with self.lock:
                    if self.job is not job or job['cancelled'] or self.closing or self.suspended:
                        raise ValueError('LIFECYCLE_CANCELLED')
                    job['entered'] = True
                def joined(row):
                    qualified_current(row)
                    with self.lock:
                        if self.job is not job or job['done'] or threading.current_thread() is not job['thread']:
                            return False
                        if not job['joined']:
                            return False
                        signal = job['signals'].snapshot()
                        if job['cancelled'] or signal['cancelled'] or not signal['released']:
                            job['consumer'].cancel()
                    return deliver(row)
                try:
                    capture(joined, timeout_s=timeout_s)
                except ValueError as error:
                    # A current, validated cleanup followed by callback refusal
                    # still lets the consumer report its explicit cancellation.
                    if str(error) != 'CURRENT_HOST_REFUSED' or not job['joined']:
                        raise
            consumer = OneUsePhysicalConsumer(self.owner, current, qualified=self.qualified,
                         clock=self.consumer_clock, wait=self.consumer_wait)
            with self.lock:
                job['consumer'] = consumer
                if job['cancelled'] or self.closing or self.suspended:
                    consumer.cancel()
            result = consumer.run()
        except Exception:
            result = None
        finally:
            with self.lock:
                job['done'] = True
                job['signals']._close()
                # The return of a thread or a stop request is not cleanup proof.
                # An attempt not entered has not invoked the trusted capture.
                clean = factory_ok and (not job['entered'] or job['joined'])
                if not clean:
                    self.quarantined = True
                self.last = result
                self.state = ('quarantined' if self.quarantined else 'closing' if self.closing
                              else 'suspended' if self.suspended else 'draining')

    def reap(self, timeout_s=0):
        duration(timeout_s, 15)
        with self.lock:
            job = self.job
        if job is None:
            return True
        thread = job['thread']
        if thread is threading.current_thread():
            return False
        if thread.ident is not None:
            thread.join(timeout_s)
        with self.lock:
            if self.job is not job or not job['done'] or thread.is_alive():
                return False
            self.job = None
            self.state = ('quarantined' if self.quarantined else 'closed' if self.closing
                          else 'suspended' if self.suspended else 'exhausted'
                          if self.attempts >= self.max_attempts else 'ready')
            return True

    def cancel(self):
        with self.lock:
            self._cancel_locked('cancel')

    def suspend(self):
        with self.lock:
            self.suspended = True
            self.pressed, self.press_at = False, None
            self._cancel_locked('suspend')
            if not self.quarantined and not self.closing:
                self.state = 'suspended'

    def resume(self):
        self.tick()
        self.reap()
        with self.lock:
            if (self.started is None or self.job is not None or self.closing or self.quarantined
                    or self.attempts >= self.max_attempts or not self._bound()):
                return False
            self.suspended = False
            self.state = 'ready'
            return True

    def close(self, timeout_s=5):
        duration(timeout_s, 15)
        end = time.monotonic() + timeout_s
        with self.lock:
            self.closing = True
            self._cancel_locked('close')
            self.wake.set()
            watch = self.watchdog
        if watch is not None and watch is not threading.current_thread():
            watch.join(max(0, end - time.monotonic()))
        done = self.reap(max(0, end - time.monotonic()))
        with self.lock:
            stopped = watch is None or not watch.is_alive()
            if done and stopped and not self.quarantined:
                self.state = 'closed'
            return done and stopped and not self.quarantined

    def status(self):
        with self.lock:
            return dict(schema=1, kind='local-voice-lifecycle', state=self.state,
                        attempts=self.attempts, active=self.job is not None,
                        suspended=self.suspended, cleanupUnresolved=self.quarantined,
                        nativeArmed=False, physicalSpeechProven=False,
                        wholeTvZeroLgTrafficProven=False)
