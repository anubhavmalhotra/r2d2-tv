"""One current capture's stop-only protocol. No sockets, keys on disk or start.

Key delivery and the caller's authenticated source admission are deliberately
outside this pure adapter. Never treat its HMAC as proof of confidential delivery.
"""
import hashlib
import hmac
import json
import math
import re
import threading
import time

LIMIT = 384
MAX_GAP = 16
BODY_LIMIT = 512
DOMAIN = b'r2d2-stop-control-v1\n'


def need(value, code='CONTROL_REQUEST'):
    if not value:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def strict(raw):
    need(type(raw) is bytes and 0 < len(raw) <= BODY_LIMIT)
    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out)
            out[key] = value
        return out
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise ValueError('CONTROL_REQUEST') from None
    need(canonical(value) == raw)
    return value


def message(kind, attempt, counter, state=None):
    text = f'{kind}\n{attempt}\n{counter}'
    if state is not None:
        text += '\n' + ''.join('1' if state[key] else '0' for key in ('released', 'cancelled', 'closed'))
    return DOMAIN + text.encode('ascii')


class StopControl:
    """Bind one live mailbox; bounded increasing counters under lock, no receipts.

    `signals` must be the current lifecycle's exact CaptureSignals instance.
    `signals_type` is supplied by the source-bound assembler, not remote input.
    """
    def __init__(self, signals, signals_type, key, attempt, *, clock=time.monotonic, lifetime=45):
        need(type(signals) is signals_type and callable(getattr(signals, 'snapshot', None)), 'CONTROL_BINDING')
        need(type(key) is bytearray and len(key) == 32, 'CONTROL_KEY')
        need(type(attempt) is str and re.fullmatch('[a-f0-9]{48}', attempt), 'CONTROL_ATTEMPT')
        need(type(lifetime) in (int, float) and math.isfinite(lifetime) and 0 < lifetime <= 45, 'CONTROL_TIME')
        self._signals = signals
        self._snapshot = signals.snapshot
        self._signals_type = signals_type
        self._key = bytearray(key)
        self._attempt = attempt
        self._clock = clock
        self._start = self._last_time = clock()
        need(type(self._start) in (int, float) and math.isfinite(self._start), 'CONTROL_TIME')
        self._admission_end = self._start + 25
        self._lifetime = lifetime
        self._end = None
        self._counter = 0
        self._closed = False
        self._released = self._cancelled = False
        self._lock = threading.RLock()

    def respond(self, raw):
        # Parse before taking the mailbox lock. No data is retained on refusal.
        value = strict(raw)
        need(type(value) is dict and set(value) == {'v', 'attempt', 'counter', 'mac'})
        need(type(value['v']) is int and value['v'] == 1 and value['attempt'] == self._attempt)
        need(type(value['counter']) is int and 1 <= value['counter'] <= LIMIT)
        need(type(value['mac']) is str and re.fullmatch('[a-f0-9]{64}', value['mac']))
        with self._lock:
            now = self._clock()
            need(not self._closed and type(now) in (int, float) and math.isfinite(now)
                 and self._last_time <= now < (self._admission_end if self._end is None else self._end), 'CONTROL_EXPIRED')
            self._last_time = now
            # First admission stays counter 1. After that, a lost request may
            # leave a gap; lost replies never replay a consumed counter. Every
            # accepted counter still requires the unchanged canonical HMAC.
            need((value['counter'] == 1 if self._counter == 0 else
                  self._counter < value['counter'] <= self._counter + MAX_GAP), 'CONTROL_REPLAY')
            expected = hmac.new(self._key, message('request', self._attempt, value['counter']), hashlib.sha256).hexdigest()
            need(hmac.compare_digest(expected, value['mac']), 'CONTROL_AUTH')
            need(type(self._signals) is self._signals_type and self._signals.snapshot == self._snapshot, 'CONTROL_BINDING')
            try:
                state = self._snapshot()
            except Exception:
                raise ValueError('CONTROL_STATE') from None
            need(type(state) is dict and set(state) == {'released', 'cancelled', 'closed', 'reason'}
                 and all(type(state[k]) is bool for k in ('released', 'cancelled', 'closed')), 'CONTROL_STATE')
            need(state['reason'] in (None, 'cancel', 'suspend', 'close', 'hold-timeout', 'lifetime', 'owner-lost'), 'CONTROL_STATE')
            # A terminal signal can never be withdrawn by a later observation.
            self._released |= state['released']
            self._cancelled |= state['cancelled'] or state['closed']
            out = dict(v=1, attempt=self._attempt, counter=value['counter'],
                       released=self._released, cancelled=self._cancelled, closed=state['closed'])
            out['mac'] = hmac.new(self._key, message('response', self._attempt, value['counter'], out), hashlib.sha256).hexdigest()
            if self._end is None:self._end=now+self._lifetime
            self._counter = value['counter']  # Consume before returning; lost replies never replay.
            return canonical(out)

    def close(self):
        with self._lock:
            self._closed = True
            self._key[:] = b'\0' * len(self._key)
            self._signals = self._snapshot = None

    def summary(self):
        with self._lock:
            return dict(schema=1, requests=self._counter, closed=self._closed,
                        keyCleared=self._closed and not any(self._key), nativeArmed=False)


def from_current_poll(poll, key, attempt, **kwargs):
    """Adapter seam for bound_host's revocable current-TV callback only.

    Source admission supplies this function; it is never accepted from JSON,
    a receipt or a network message. It grants stop observation, not start.
    """
    need(callable(poll), 'CONTROL_BINDING')
    class CurrentPoll:
        def snapshot(self):
            return poll()
    signals = CurrentPoll()
    return StopControl(signals, CurrentPoll, key, attempt, **kwargs)


def handler_type(parent, control, nonce, *, peer, host):
    """Extend the already owned handler; never create or start a listener.

    The parent's request count, deadline and socket handling still wrap this
    do_POST. Only the exact new route is intercepted; all other routes delegate.
    """
    need(type(control) is StopControl and type(nonce) is str and re.fullmatch('[a-f0-9]{48}', nonce), 'CONTROL_BINDING')
    need(type(peer) is str and type(host) is str and '\n' not in peer + host, 'CONTROL_BINDING')
    route = '/' + nonce + '/capture-control'
    class Handler(parent):
        def do_POST(self):
            if self.path != route:
                return super().do_POST()
            try:
                need(self.client_address[0] == peer and self.headers_ok(), 'CONTROL_HEADERS')
                need(self.headers.get_all('Host', []) == [host]
                     and not self.headers.get_all('Origin', [])
                     and not self.headers.get_all('Transfer-Encoding', [])
                     and self.headers.get_all('Content-Type', []) == ['application/json'], 'CONTROL_HEADERS')
                lengths = self.headers.get_all('Content-Length', [])
                need(len(lengths) == 1 and re.fullmatch('[1-9][0-9]{0,2}', lengths[0]), 'CONTROL_SIZE')
                size = int(lengths[0]); need(size <= BODY_LIMIT, 'CONTROL_SIZE')
                raw = self.rfile.read(size); need(len(raw) == size, 'CONTROL_SIZE')
                body = control.respond(raw)
            except Exception:
                self.reply(400)
                return
            self.reply(200, body, 'application/json')
    return Handler
