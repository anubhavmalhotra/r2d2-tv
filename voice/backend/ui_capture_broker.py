"""Opt-in metadata bridge to the existing enrolled TV capture owner.

No audio, model, generic RPC, listener, or key provisioning lives here. The
independent UI key is never a URL capability; it must be provisioned privately.
The existing Manager/Trial socket carries only closed status/command objects.
"""
import hashlib
import hmac
import json
import math
import re
import threading
import time

DOMAIN = b'R2D2-ui-capture-v1\0'
MAX_SEQUENCE = 2147483647
STATES = frozenset(('unavailable', 'idle', 'preparing', 'listening', 'finishing',
                    'accepted', 'refused', 'unknown'))
FRESH_S, CAPTURE_S, RESULT_S = 2.0, 10.0, 200.0
UNAVAILABLE = dict(available=False, state='unavailable', request=None,
                   started=False, stopped=False, cleanupComplete=False)


def _hex(value, size):
    return type(value) is str and re.fullmatch('[a-f0-9]{%d}' % size, value) is not None


def valid_status(value):
    if (type(value) is not dict or set(value) != set(UNAVAILABLE)
            or type(value.get('state')) is not str or value['state'] not in STATES
            or not all(type(value.get(key)) is bool for key in
                       ('available', 'started', 'stopped', 'cleanupComplete'))):
        return False
    if value['request'] is None:
        return (not value['started'] and not value['stopped'] and
                (value['state'] == 'unavailable' and not value['available'] or
                 value['state'] == 'idle' and value['available'] and value['cleanupComplete']))
    return _hex(value['request'], 16)


def valid_command(value):
    return (value is None or type(value) is dict and set(value) == {'request', 'action'}
            and _hex(value.get('request'), 16)
            and value.get('action') in ('start', 'release', 'cancel'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def derive_key(secret):
    if not _hex(secret, 64):
        raise ValueError('UI_CAPTURE_KEY')
    return hmac.digest(bytes.fromhex(secret), DOMAIN + b'key', 'sha256')


def sign(value, key, direction):
    if direction not in ('request', 'response'):
        raise ValueError('UI_CAPTURE_AUTH')
    result = dict(value)
    result['mac'] = hmac.digest(key, DOMAIN + direction.encode('ascii') + b'\0' + canonical(value), 'sha256').hex()
    return result


def verify_request(value, key):
    if (type(value) is not dict or set(value) != {'schema', 'owner', 'session', 'seq', 'nonce', 'status', 'mac'}
            or type(value.get('schema')) is not int or value['schema'] != 1
            or not _hex(value.get('owner'), 32) or not _hex(value.get('nonce'), 16)
            or not (value.get('session') is None or _hex(value.get('session'), 32))
            or type(value.get('seq')) is not int or not 1 <= value['seq'] <= MAX_SEQUENCE
            or not valid_status(value.get('status')) or not _hex(value.get('mac'), 64)):
        raise ValueError('UI_CAPTURE_AUTH')
    unsigned = {key: item for key, item in value.items() if key != 'mac'}
    expected = sign(unsigned, key, 'request')['mac']
    if not hmac.compare_digest(expected, value['mac']):
        raise ValueError('UI_CAPTURE_AUTH')
    return unsigned


class UiCaptureBroker:
    """One current Trial and at most one unretired TV request, all in RAM.

    Start/release are consumed before an HTTP response can be lost. Cancel may
    be repeated for the same owned request until actual stopped/cleanup evidence;
    there is no start replay or fabricated cleanup on a timeout/session change.
    """
    def __init__(self, manager, secret, *, clock=time.monotonic):
        if not callable(clock):
            raise ValueError('UI_CAPTURE_CONFIG')
        self.manager, self.clock = manager, clock
        self.key = bytearray(derive_key(secret))
        self.lock = threading.Lock()
        self.owner = None
        self.sequence = 0
        self.sequences = {}
        self.retired_owners = set()
        self.last_seen = None
        self.current = None
        self.seen_session, self.starts = None, set()
        self.closed = False
        self.stopping = threading.Event()

    def _binding(self):
        spawn = getattr(self.manager, 'control_spawn', None)
        read = getattr(spawn, 'current_client', None)
        try:
            value = read(timeout_s=.25) if callable(read) else None
        except Exception:
            return None
        if (type(value) is not tuple or len(value) != 2 or not _hex(value[0], 32)
                or getattr(value[1], 'session', None) != value[0]
                or not callable(getattr(value[1], 'capture_exchange', None))):
            return None
        return value

    def _exchange(self, binding, status):
        try:
            result = binding[1].capture_exchange(dict(status), timeout_s=.5)
        except Exception:
            return None
        if type(result) is not dict or set(result) != {'command'} or not valid_command(result['command']):
            return None
        return result

    def _expired(self, now):
        return self.current is not None and (now - self.current['released_at'] > RESULT_S if self.current['released_at'] is not None else now - self.current['at'] > CAPTURE_S)

    def _retired(self, status):
        return (self.current is not None and status['request'] == self.current['request']
                and status['cleanupComplete'] is True
                and (status['started'] is False or status['stopped'] is True))

    def _cancel(self):
        if self.current is not None:
            self.current['cancel'] = True

    def exchange(self, packet):
        request = verify_request(packet, self.key)
        with self.lock:
            if self.closed or self.stopping.is_set():
                raise ValueError('UI_CAPTURE_CLOSED')
            if request['owner'] in self.retired_owners: raise ValueError('UI_CAPTURE_RETIRED_OWNER')
            previous = self.sequences.get(request['owner'], 0)
            if request['seq'] <= previous:
                raise ValueError('UI_CAPTURE_REPLAY')
            if request['owner'] not in self.sequences and len(self.sequences) >= 16:
                raise ValueError('UI_CAPTURE_OWNER_LIMIT')
            if self.owner is not None and self.owner != request['owner']:
                self.retired_owners.add(self.owner)
                self._cancel()
            self.owner, self.sequence = request['owner'], request['seq']
            self.sequences[self.owner] = self.sequence
            now, status = self.clock(), request['status']
            previous_seen, self.last_seen = self.last_seen, now
            binding = self._binding()
            if self.current is not None and (previous_seen is None or now - previous_seen > FRESH_S
                    or self._expired(now) or binding is None
                    or binding[0] != self.current['session'] or self.owner != self.current['owner']
                    or request['session'] != self.current['session']):
                self._cancel()
            # Only a status echoing its admitted browser session can affect it.
            matching = binding is not None and request['session'] == binding[0]
            if self.current is not None and (request['session'] != self.current['session'] or request['owner'] != self.current['owner']):
                matching = False
            result = self._exchange(binding, status) if matching else None
            if self._retired(status) and request['session'] == self.current['session'] and request['owner'] == self.current['owner']:
                self.current = None
            command = None
            if self.current is not None and self.current['cancel']:
                session = self.current['session']
                command = {'request':self.current['request'], 'action':'cancel'}
            else:
                session = binding[0] if binding is not None else None
                if binding is not None and self.seen_session != session and self.current is None:
                    self.seen_session, self.starts = session, set()
                candidate = result['command'] if result is not None else None
                if candidate is not None:
                    action, identity = candidate['action'], candidate['request']
                    if action == 'start' and self.current is None and identity not in self.starts and len(self.starts) < 256 and (
                            status['available'] is True and status['state'] in ('idle','accepted','refused')
                            and status['cleanupComplete'] is True
                            and (status['started'] is False or status['stopped'] is True)
                            and identity != status['request']):
                        self.starts.add(identity)
                        self.current = dict(session=session, request=identity, owner=self.owner, at=now, cancel=False, released=False, released_at=None)
                        command = dict(candidate)
                    elif self.current is not None and identity == self.current['request']:
                        if action == 'release' and not self.current['released']:
                            self.current['released'] = True
                            self.current['released_at'] = now
                            command = dict(candidate)
                        elif action == 'cancel':
                            self._cancel()
                            command = dict(candidate)
                # A native request surviving a broker restart is cancelled, never adopted.
                if self.current is None and status['request'] is not None and not (
                        status['cleanupComplete'] is True and (status['started'] is False or status['stopped'] is True)):
                    if request['session'] is not None:
                        session = request['session']
                        self.current = dict(session=session, request=status['request'], owner=self.owner, at=now, cancel=True, released=False, released_at=None)
                        command = {'request':status['request'], 'action':'cancel'}
            if self.stopping.is_set():
                self._cancel()
                command = {'request':self.current['request'],'action':'cancel'} if self.current else None
            return sign(dict(schema=1, owner=request['owner'], session=session,
                             seq=request['seq'], nonce=request['nonce'], command=command), self.key, 'response')

    def tick(self):
        """Existing ResidentEntry service loop calls this; no extra polling thread."""
        with self.lock:
            if self.closed or self.stopping.is_set():
                return
            binding = self._binding()
            stale = self.last_seen is None or not 0 <= self.clock() - self.last_seen <= FRESH_S
            if stale or self.current is not None and (binding is None
                    or binding[0] != self.current['session'] or self._expired(self.clock())):
                self._cancel()
                if binding is not None:
                    self._exchange(binding, UNAVAILABLE)

    def close(self, timeout_s=.5):
        self.stopping.set()
        if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or not 0 <= timeout_s <= 30:
            return False
        if not self.lock.acquire(timeout=timeout_s): return False
        try:
            self._cancel()
            self.closed = True
            self.key[:] = b'\0' * len(self.key)
            return self.current is None
        finally: self.lock.release()
