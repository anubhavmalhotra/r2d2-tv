"""TV-aware warm policy for the ASR + Qwen workers. Offline prototype: the model pool
and the TV power-state source are injected. Nothing here loads a model, opens a
socket or sends anything to the TV.

Policy:
  * Press-to-talk only. "Warm" means loaded and waiting for a press, never listening.
  * Keep the workers warm while the TV power state is Active. No idle timer while the
    TV is on, so a press on a TV in use never waits for a warm-up.
  * Screen Off / Active Standby / Suspend / Power Off -> GRACE_S (5 min), then unload.
  * Back to Active -> start warming at once (~84 s measured in V4). A press meanwhile
    is refused VOICE_WARMING_UP, which the agent shows on the TV: never a silent drop.
  * The power state comes from a read-only power-state query/subscription over the
    local LAN control pairing (the TV gateway). ICMP/ARP presence is never the signal.
  * Source unavailable or stale -> 'unknown': keep the current state for up to
    UNKNOWN_HOLD_S (30 min), then unload. 'unknown' never starts a warm.

States (WarmController.state):
  unloaded   nothing loaded, pool verified clean.            press: VOICE_UNAVAILABLE
  warming    exactly one warm in progress.                   press: VOICE_WARMING_UP
  ready      loaded; TV Active (or 'unknown' within the hold). press: proceeds
  grace      loaded; TV not Active; unload at the deadline.  press: proceeds (still
             loaded); a press never extends the grace.
  unloading  unload decided; waits until no utterance is in flight, then unloads.
                                                             press: VOICE_UNAVAILABLE
  degraded   the last warm, or the loaded pool, failed.      press: VOICE_UNAVAILABLE
             Retry only on a fresh Active reading after BACKOFF_S: 60 s, then 300 s
             for every further failure. A success resets the count.

Transitions (reason in the audit row):
  unloaded  -> warming    fresh Active                                    POWER_ACTIVE
  warming   -> ready      pool ready, TV Active (or unknown)              WARM_DONE
  warming   -> grace      pool ready, TV not Active; the deadline counts  WARM_DONE
                          from the first non-Active report, not from WARM_DONE
  warming   -> unloading  pool failed / begin_warm raised / > WARM_TIMEOUT_S
                                                             WARM_FAILED / WARM_TIMEOUT
  ready     -> grace      TV not Active                                   POWER_INACTIVE
  ready     -> unloading  'unknown' for UNKNOWN_HOLD_S                    UNKNOWN_HOLD_EXPIRED
  ready/grace -> unloading  pool no longer 'ready'                        POOL_FAILED
  grace     -> ready      fresh Active                                    POWER_ACTIVE
  grace     -> unloading  deadline (keeps running while 'unknown')        GRACE_EXPIRED
  unloading -> ready      fresh Active before the unload ran (grace or hold reason only)
                                                                          POWER_ACTIVE
  unloading -> unloaded   nothing in flight, unload verified              UNLOAD_VERIFIED
  unloading -> degraded   failure reason, or unload not verified    reason / UNLOAD_UNVERIFIED
  degraded  -> warming    backoff over, pool clean, fresh Active          RETRY

Invariants:
  * Never unload while an utterance is in flight: finish it first.
  * Never start a warm while one runs, and never on top of a pool whose unload was not
    verified (degraded retries the unload first, on the backoff schedule).
  * admit() never loads or unloads: the press path never waits for the pool.
    tick() runs every pool call outside self.lock (ticks are serialized by
    self.ticking), so admit() never waits behind a slow unload or poll.
    Safe because admit() issues tickets only in ready/grace, and a warm or an unload
    runs only in warming/unloading/degraded, set before the lock is released.
  * The controller does not read the kill switch. Paused keeps the models warm:
    the agent checks the pause before asking here, so a paused press is
    AGENT_PAUSED whatever the warm state, and the power policy keeps running so
    that resume is instant.
  * Injected monotonic clock. A reading below the previous one (or not finite) is
    refused: tick() makes no transition and admit() refuses VOICE_UNAVAILABLE until
    the clock is back at its high-water mark. One audit row per episode.

Audit rows (state changes only; enums and integers, never a transcript):
  {'event':'warm','from':s,'to':s,'reason':r,'power':p}   (+ failures, retry_s, count)
  {'event':'warm_power','power':p,'state':s}              when the reading changes

ModelPool contract (the P2 pool; rotation stays inside the pool):
  begin_warm() -> None   start loading ASR + Qwen in the background; returns at once.
                         Raising is a failed warm.
  poll() -> 'warming' | 'ready' | 'failed'
                         'ready' once a sealed worker passed its handshake. It stays
                         'ready' through rotation until the pool can no longer serve
                         (standby failed and the old worker hit its hard bound).
                         Anything else, or raising, counts as 'failed'.
  unload() -> bool       close every worker (active and standby, warming or not) and
                         verify cleanup as shared_owner.py does. True only when
                         verified; raising counts as False. Idempotent.

PowerStateSource contract (the real one is a TV-gateway read [H]; P0 ships only
FakePowerSource):
  current() -> (state, observed_at)
      state        one of POWER_STATES (closed enum). Anything else is 'unknown'.
      observed_at  the same monotonic clock: when the TV last confirmed `state`
                   (subscription event or poll reply), not when it changed.
      Raising, None or any other shape is 'unknown' (so is an int too large for a
      float, which would otherwise raise OverflowError out of tick()).
  The source keeps the latest reading only and refreshes it at least every
  POLL_MAX_S (10 s), by subscription or poll. A reading older than STALE_S (30 s), or
  from the future, is 'unknown'. Read-only: the gateway sends only the power-state
  read/subscribe request; never power control, never `register` with a prompt.
  Raw replies stay in the gateway; power_from_reply() is the mapping it would use.

Press path (agent.arm): kill switch -> busy -> utterance rate -> admit() here.
  admit() -> ticket      in ready/grace; the ticket marks an utterance in flight.
  release(ticket)        when the utterance ends (the agent's _run, cancel of a lease
                         that never ran, or its replacement). Idempotent.
  A ticket not released within INFLIGHT_MAX_S (an armed lease the ingress abandoned)
  is dropped with an INFLIGHT_EXPIRED row, so it cannot pin the models forever.

The service loop calls tick() every TICK_S and on every power event.
"""
import math
import secrets
import threading
from types import MappingProxyType

from .errors import Refused

STATES = ('unloaded', 'warming', 'ready', 'grace', 'unloading', 'degraded')
POWER_STATES = ('active', 'screen_off', 'active_standby', 'suspend', 'off')
GRACE_S = 300
UNKNOWN_HOLD_S = 1800
WARM_TIMEOUT_S = 120        # warm-up bound (<= 120 s); measured ~84 s in V4
BACKOFF_S = (60, 300)       # 60 s, then 300 s
POLL_MAX_S = 10             # source refresh contract
STALE_S = 30                # three missed polls
INFLIGHT_MAX_S = 300        # >= lease 30 s + model 5 s + owner_open 140 s + slack 15 s (+ reads)
TICK_S = 1
_LOADED = ('ready', 'grace')
_RESUMABLE = frozenset(('GRACE_EXPIRED', 'UNKNOWN_HOLD_EXPIRED'))
_FAILURES = frozenset(('WARM_FAILED', 'WARM_TIMEOUT', 'POOL_FAILED'))

# [H] webOS strings for the power-state read. Unlisted strings, and any
# non-empty `processing` (a transition in progress), are 'unknown'.
SSAP_POWER = MappingProxyType({'Active': 'active', 'Screen Off': 'screen_off',
                               'Active Standby': 'active_standby', 'Suspend': 'suspend',
                               'Power Off': 'off'})


def _seconds(value):
    """int or float (never bool) -> a finite float, else None. Never raises: an int
    too large for a float is None (math.isfinite would raise OverflowError)."""
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def power_from_reply(reply):
    """For the gateway (raw replies never leave it): one reply -> one enum."""
    if type(reply) is not dict or reply.get('returnValue') is not True:
        return 'unknown'
    state, processing = reply.get('state'), reply.get('processing')
    if type(state) is not str or state not in SSAP_POWER or processing not in (None, ''):
        return 'unknown'
    return SSAP_POWER[state]


class WarmController:
    def __init__(self, pool, source, *, clock, audit, killswitch):
        if not all(callable(getattr(pool, name, None)) for name in ('begin_warm', 'poll', 'unload')):
            raise ValueError('WARM_POOL')
        if not callable(getattr(source, 'current', None)):
            raise ValueError('WARM_SOURCE')
        self.pool, self.source, self.clock = pool, source, clock
        self.audit, self.killswitch = audit, killswitch
        self.lock = threading.Lock()      # state, timers, tickets; never held across a pool call
        self.ticking = threading.Lock()   # one tick() at a time
        self.state = 'unloaded'
        self.reading = 'unknown'      # last reading: a POWER_STATES value or 'unknown'
        self.known = None             # last known class: 'active' | 'inactive' | None
        self.inactive_since = None    # when `known` became 'inactive'
        self.unknown_since = None     # start of the current 'unknown' run
        self.grace_until = None
        self.warm_started = None
        self.unload_reason = None
        self.failures, self.retry_at = 0, None
        self.clean = True             # pool verified empty
        self.inflight = {}            # ticket -> issued at
        self.high_water = None
        self.clock_fault = False

    # -- press path -------------------------------------------------------------
    def admit(self):
        with self.lock:
            if self._now() is None:
                raise Refused('VOICE_UNAVAILABLE')
            if self.state in _LOADED:
                ticket = secrets.token_hex(16)
                self.inflight[ticket] = self.high_water
                return ticket
            raise Refused('VOICE_WARMING_UP' if self.state == 'warming' else 'VOICE_UNAVAILABLE')

    def release(self, ticket):
        with self.lock:
            return self.inflight.pop(ticket, None) is not None

    # -- service loop -------------------------------------------------------------
    def tick(self):
        with self.ticking, self.lock:
            now = self._now()
            if now is not None:
                self._expire(now)
                self._observe(now)
                for _ in range(6):            # bounded: at most a few transitions per tick
                    before = self.state
                    getattr(self, '_in_' + self.state)(now)
                    if self.state == before:
                        break
            return self.state

    def status(self):
        with self.lock:
            return {'state': self.state, 'power': self.reading, 'failures': self.failures,
                    'inflight': len(self.inflight)}

    # -- clock and power ---------------------------------------------------------------
    def _now(self):
        now = _seconds(self.clock())
        if now is None or (self.high_water is not None and now < self.high_water):
            if not self.clock_fault:
                self.clock_fault = True
                self._row(self.state, 'CLOCK_REFUSED')
            return None
        self.clock_fault = False
        self.high_water = now
        return now

    def _read(self, now):
        try:
            value = self.source.current()
        except Exception:
            return 'unknown'
        if type(value) is not tuple or len(value) != 2:
            return 'unknown'
        state, at = value
        if type(state) is not str or state not in POWER_STATES:
            return 'unknown'
        at = _seconds(at)
        if at is None or at > now or now - at > STALE_S:
            return 'unknown'
        return state

    def _observe(self, now):
        reading = self._read(now)
        if reading != self.reading:
            self.reading = reading
            self._audit({'event': 'warm_power', 'power': reading, 'state': self.state})
        if reading == 'unknown':
            if self.unknown_since is None:
                self.unknown_since = now
            return
        self.unknown_since = None
        cls = 'active' if reading == 'active' else 'inactive'
        if cls != self.known:
            self.known = cls
            self.inactive_since = now if cls == 'inactive' else None

    def _fresh_active(self):
        return self.unknown_since is None and self.known == 'active'

    def _expire(self, now):
        stale = [ticket for ticket, at in self.inflight.items() if now - at >= INFLIGHT_MAX_S]
        for ticket in stale:
            del self.inflight[ticket]
        if stale:
            self._row(self.state, 'INFLIGHT_EXPIRED', count=len(stale))

    # -- states ----------------------------------------------------------------------
    def _in_unloaded(self, now):
        if self._fresh_active():
            self._begin_warm(now, 'POWER_ACTIVE')

    def _in_warming(self, now):
        status = self._poll()
        if status == 'ready':
            self.failures, self.retry_at = 0, None
            if self.known == 'inactive':
                self.grace_until = self.inactive_since + GRACE_S
                self._row('grace', 'WARM_DONE')
            else:
                self._row('ready', 'WARM_DONE')
        elif status == 'failed':
            self._begin_unload('WARM_FAILED')
        elif now - self.warm_started >= WARM_TIMEOUT_S:
            self._begin_unload('WARM_TIMEOUT')

    def _in_ready(self, now):
        if self._poll() != 'ready':
            self._begin_unload('POOL_FAILED')
        elif self.unknown_since is not None:
            if now - self.unknown_since >= UNKNOWN_HOLD_S:
                self._begin_unload('UNKNOWN_HOLD_EXPIRED')
        elif self.known == 'inactive':
            self.grace_until = self.inactive_since + GRACE_S
            self._row('grace', 'POWER_INACTIVE')

    def _in_grace(self, now):
        if self._poll() != 'ready':
            self._begin_unload('POOL_FAILED')
        elif self._fresh_active():
            self.grace_until = None
            self._row('ready', 'POWER_ACTIVE')
        elif now >= self.grace_until:
            self._begin_unload('GRACE_EXPIRED')

    def _in_unloading(self, now):
        if self.unload_reason in _RESUMABLE and self._fresh_active() and self._poll() == 'ready':
            self.unload_reason = None
            self._row('ready', 'POWER_ACTIVE')
            return
        if self.inflight:
            return  # never unload under an utterance: finish it first
        reason, self.unload_reason = self.unload_reason, None
        self.clean = self._unload()
        if self.clean and reason not in _FAILURES:
            self._row('unloaded', 'UNLOAD_VERIFIED')
        else:
            self._fail(now, reason if self.clean else 'UNLOAD_UNVERIFIED')

    def _in_degraded(self, now):
        if now < self.retry_at:
            return
        if not self.clean:
            self.clean = self._unload()
            if not self.clean:
                self._fail(now, 'UNLOAD_UNVERIFIED')
                return
        if self._fresh_active():
            self._begin_warm(now, 'RETRY')

    # -- actions ----------------------------------------------------------------------
    def _begin_warm(self, now, reason):
        if self.state == 'warming' or not self.clean:
            return  # never two warms; never over a pool whose unload was not verified
        self.clean = False  # from here on the pool may hold memory until a verified unload
        self.warm_started = now
        self._row('warming', reason)
        try:
            self._pool_call('begin_warm')
        except Exception:
            self._begin_unload('WARM_FAILED')

    def _begin_unload(self, reason):
        self.unload_reason, self.grace_until = reason, None
        self._row('unloading', reason)

    def _fail(self, now, reason):
        self.failures += 1
        delay = BACKOFF_S[min(self.failures, len(BACKOFF_S)) - 1]
        self.retry_at = now + delay
        self._row('degraded', reason, failures=min(self.failures, 9999), retry_s=delay)

    def _poll(self):
        try:
            status = self._pool_call('poll')
        except Exception:
            return 'failed'
        return status if status in ('warming', 'ready') else 'failed'

    def _unload(self):
        try:
            return self._pool_call('unload') is True
        except Exception:
            return False

    def _pool_call(self, name):
        """Called from tick() only (self.ticking and self.lock held). Drops self.lock
        for the call so that admit()/release()/status() never wait for the pool."""
        self.lock.release()
        try:
            return getattr(self.pool, name)()
        finally:
            self.lock.acquire()

    # -- audit ------------------------------------------------------------------------
    def _row(self, to, reason, **extra):
        row = {'event': 'warm', 'from': self.state, 'to': to, 'reason': reason, 'power': self.reading}
        row.update(extra)
        self.state = to
        self._audit(row)

    def _audit(self, row):
        """The transition stands either way (residency must match the pool); a row
        that cannot be written trips Paused, as in the agent."""
        try:
            self.audit.append(row)
        except Exception:
            self.killswitch.pause('AUDIT_WRITE')


class FakePowerSource:
    """Test source. live=True behaves like a working subscription (every read is
    fresh); live=False freezes observed_at (a stalled poll); broken raises."""
    def __init__(self, clock, state=None):
        self.clock = clock
        self.state, self.observed_at = state, (clock() if state is not None else None)
        self.live, self.broken = True, False
        self.reads = 0

    def set(self, state):
        self.state, self.observed_at = state, self.clock()

    def current(self):
        self.reads += 1
        if self.broken:
            raise OSError('POWER_SOURCE')
        if self.state is None:
            return None
        if self.live:
            self.observed_at = self.clock()
        return self.state, self.observed_at


class FakePool:
    """Test pool. A warm turns 'ready' warm_s after begin_warm (~84 s, V4) unless
    fail_warm or hang. Records lifecycle calls and counts overlapping warms."""
    def __init__(self, clock, *, warm_s=84):
        self.clock, self.warm_s = clock, warm_s
        self.calls = []
        self.loaded = 'empty'          # 'empty' | 'warming' | 'ready' | 'failed'
        self.started = None
        self.fail_warm = self.raise_warm = self.hang = False
        self.unload_ok = True
        self.overlaps = 0              # begin_warm while anything is still loaded
        self.on_unload = None

    def begin_warm(self):
        self.calls.append('begin_warm')
        if self.loaded != 'empty':
            self.overlaps += 1
        if self.raise_warm:
            raise RuntimeError('WARM')
        self.loaded, self.started = 'warming', self.clock()

    def poll(self):
        if self.loaded == 'warming' and not self.hang and self.clock() - self.started >= self.warm_s:
            self.loaded = 'failed' if self.fail_warm else 'ready'
        return self.loaded

    def unload(self):
        self.calls.append('unload')
        if self.on_unload is not None:
            self.on_unload()
        if self.unload_ok:
            self.loaded = 'empty'
        return self.unload_ok
