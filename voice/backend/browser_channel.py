"""Finite private inherited-socket channel from Manager to its own Trial child.

No TCP listener, filesystem socket, access logs, generic CDP or arbitrary commands.
The socketpair descriptor is an inherited capability; a random per-session key
adds message integrity/replay binding. It is not a boundary against a compromised
same-UID process with ptrace access. Each mutation is consumed before dispatch.
"""
import hmac
import json
import math
import re
import secrets
import selectors
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

if __package__:
    from .controller import BrowserObservation, Refused
    from .ingress import strict_json
    from .ui_capture_broker import valid_status, valid_command, MAX_SEQUENCE
else:
    from controller import BrowserObservation, Refused
    from ingress import strict_json
    from ui_capture_broker import valid_status, valid_command, MAX_SEQUENCE

MAX_FRAME = 4096
MAX_REQUESTS = 512
DOMAIN = b'R2D2-owned-trial-v1\0'


def _hex(value, length):
    return type(value) is str and re.fullmatch('[a-f0-9]{%d}' % length, value) is not None


def allowed_url(value):
    import unicodedata
    from urllib.parse import unquote
    if type(value) is not str or not value.isascii() or len(value) > 1600:
        return False
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return False
    if value in ('https://www.youtube.com', 'https://duckduckgo.com',
                 'https://www.wikipedia.org', 'https://news.ycombinator.com'):
        return True
    google = re.fullmatch(r'https://www\.google\.com/search\?q=([A-Za-z0-9%._~-]{1,1500})', value)
    ddg = re.fullmatch(r'https://duckduckgo\.com/\?q=([A-Za-z0-9%._~-]{1,1500})', value)
    yt = re.fullmatch(r'https://www\.youtube\.com/results\?search_query=([A-Za-z0-9%+._~-]{1,1500})', value)
    match = google or ddg or yt
    if match is None:
        return False
    tail = match.group(1)
    bang = google is not None or ddg is not None
    if not re.fullmatch(r'(?:[^%]|%[0-9A-F]{2})*', tail):
        return False
    try:
        text = unquote(tail, errors='strict')
    except UnicodeDecodeError:
        return False
    refused = {'Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp'}
    if any(unicodedata.category(c) in refused for c in text):
        return False
    for _ in range(4):
        folded = unicodedata.normalize('NFKC', text)
        if bang and ('!' in folded or '\\' in folded):
            return False
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in folded):
            return False
        decoded = unquote(text)
        if decoded == text:
            return True
        text = decoded
    return False


_CLASSES = ('browser_youtube_watch', 'browser_youtube', 'browser_other')
_DEFAULT_VIDEO = {'count': 0, 'paused': None}
_CARD_KEYS = frozenset(('v', 'seq', 'phase', 'ageMs', 'tone', 'word', 'headline', 'detail',
                        'badge', 'meter', 'meterMs', 'dwellMs', 'mood', 'echo'))
_CARD_ENUMS = (
    ('phase', ('idle', 'pressed', 'listening', 'released', 'doing', 'confirm', 'result'), False),
    ('tone', ('idle', 'ready', 'listen', 'work', 'done', 'sent', 'unsure', 'nothing', 'failed', 'ask'), False),
    ('mood', ('idle', 'front', 'listening', 'thinking', 'verified', 'asking', 'dim', 'sleepy'), False),
    ('badge', ('confirmed', 'sent', 'unconfirmed', 'nothing', 'changed', 'failed', 'waiting'), True),
    ('meter', ('hold', 'sweep', 'countdown'), True))
_CARD_COPY = r'[ -~\u2018\u2019\u201c\u201d\u2026]{0,%d}'


def _bounded(value, high):
    return type(value) is int and 0 <= value <= high


def valid_card(value):
    """Closed viewer receipt: copy-table text, enums, and an optional search echo."""
    import unicodedata
    if type(value) is not dict or set(value) != _CARD_KEYS or type(value['v']) is not int or value['v'] != 1:
        return False
    if (not _bounded(value['seq'], MAX_SEQUENCE) or not _bounded(value['ageMs'], 600000) or
            not all(value[k] is None and nullable or type(value[k]) is str and value[k] in options
                    for k, options, nullable in _CARD_ENUMS) or
            not all(value[k] is None or _bounded(value[k], 20000) for k in ('meterMs', 'dwellMs')) or
            not all(type(value[k]) is str for k in ('word', 'headline', 'detail')) or
            re.fullmatch(r"[A-Z' \u2019]{0,20}", value['word']) is None or
            re.fullmatch(_CARD_COPY % 40, value['headline']) is None or
            re.fullmatch(_CARD_COPY % 72, value['detail']) is None):
        return False
    echo = value['echo']
    # The normalized search query is the only free text, and only for its own action.
    if echo is not None and (type(echo) is not str or not 1 <= len(echo) <= 48 or
                             value['phase'] not in ('doing', 'result') or
                             any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp') for c in echo)):
        return False
    return value['phase'] != 'idle' or (value['word'] == value['headline'] == value['detail'] == '' and
                                        value['badge'] is None and value['meter'] is None and echo is None)


def _delivered(value):
    return type(value) is dict and set(value) == {'delivered'} and (
        value['delivered'] is None or _bounded(value['delivered'], MAX_SEQUENCE))


def _valid_video(video):
    return (type(video) is dict and set(video) == {'count', 'paused'} and
            type(video['count']) is int and 0 <= video['count'] <= 4 and
            (video['paused'] is None or type(video['paused']) is bool))


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def signed(row, key, direction):
    result = dict(row)
    result['mac'] = hmac.digest(key, DOMAIN + direction + canonical(row), 'sha256').hex()
    return result


def verified(row, key, direction):
    if type(row) is not dict or not _hex(row.get('mac'), 64):
        raise Refused('CONTROL_AUTH')
    unsigned = {k: v for k, v in row.items() if k != 'mac'}
    tag = hmac.digest(key, DOMAIN + direction + canonical(unsigned), 'sha256').hex()
    if not hmac.compare_digest(row['mac'], tag):
        raise Refused('CONTROL_AUTH')
    return unsigned


class FramedSocket:
    def __init__(self, sock):
        if not isinstance(sock, socket.socket) or sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
            raise Refused('CONTROL_SOCKET')
        self.sock = sock
        self.sock.setblocking(False)
        self.closed = False

    def _io(self, value, *, write, deadline):
        offset = 0
        output = bytearray()
        needed = len(value) if write else value
        with selectors.DefaultSelector() as wait:
            wait.register(self.sock, selectors.EVENT_WRITE if write else selectors.EVENT_READ)
            while offset < needed:
                if self.closed: raise Refused('CONTROL_CLOSED')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Refused('CONTROL_TIMEOUT')
                # Poll closure as well as descriptor readiness. Closing a socket
                # from another thread need not wake every platform's selector.
                if not wait.select(min(remaining, .1)): continue
                try:
                    if write:
                        count = self.sock.send(memoryview(value)[offset:])
                    else:
                        data = self.sock.recv(needed - offset)
                        count = len(data)
                        output.extend(data)
                except BlockingIOError:
                    continue
                if not count:
                    raise Refused('CONTROL_CLOSED')
                offset += count
        return bytes(output)

    def send(self, row, deadline):
        payload = canonical(row)
        if not 1 <= len(payload) <= MAX_FRAME:
            raise Refused('CONTROL_SIZE')
        self._io(struct.pack('>I', len(payload)) + payload, write=True, deadline=deadline)

    def receive(self, deadline):
        size, = struct.unpack('>I', self._io(4, write=False, deadline=deadline))
        if not 1 <= size <= MAX_FRAME:
            raise Refused('CONTROL_SIZE')
        return strict_json(self._io(size, write=False, deadline=deadline), MAX_FRAME)

    def close(self):
        self.closed = True
        try: self.sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.sock.close()


def _deadline(timeout_s):
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 5:
        raise Refused('CONTROL_TIMEOUT')
    return time.monotonic() + timeout_s


class ControlClient:
    """Manager-owned endpoint. Any lost or malformed reply permanently poisons it."""
    def __init__(self, sock, session, *, key=None):
        if not _hex(session, 32): raise Refused('CONTROL_SESSION')
        self.channel = FramedSocket(sock)
        self.session = session
        self.key = bytearray(key or secrets.token_bytes(32))
        if len(self.key) != 32: raise Refused('CONTROL_KEY')
        self.lock = threading.Lock()
        self.started = False
        self.seq = 0
        self.regular_requests = 0
        self.operations = set()
        self.target = None
        self.generation = 0

    def close(self):
        self.channel.close()
        self.key[:] = b'\0' * len(self.key)
        self.target = None

    def _start(self, deadline):
        self.channel.send({'schema': 1, 'kind': 'hello', 'session': self.session,
                           'key': self.key.hex()}, deadline)
        row = verified(self.channel.receive(deadline), self.key, b'hello')
        if set(row) != {'schema', 'kind', 'session'} or type(row['schema']) is not int or row != {
                'schema': 1, 'kind': 'ready', 'session': self.session}:
            raise Refused('CONTROL_SCHEMA')
        self.started = True

    def request(self, action, *, operation=None, url=None, history_action=None, capture_status=None, card=None, timeout_s):
        deadline = _deadline(timeout_s)
        exempt = action in ('capture_exchange', 'voice_card')
        # A receipt push waits only briefly: a long wait would eat the shared
        # deadline, and an exchange that then times out poisons navigation too.
        wait = max(0, deadline-time.monotonic())
        if not self.lock.acquire(timeout=min(.05, wait) if action == 'voice_card' else wait):
            raise Refused('CONTROL_BUSY')
        if action == 'voice_card' and deadline - time.monotonic() < .2:
            self.lock.release()
            raise Refused('CONTROL_BUSY')
        try:
            if self.channel.closed or self.seq >= MAX_SEQUENCE or (not exempt and self.regular_requests >= MAX_REQUESTS):
                raise Refused('CONTROL_CLOSED')
            if (action != 'capture_exchange' and capture_status is not None or
                    action != 'voice_card' and card is not None):
                raise Refused('CONTROL_ACTION')
            if action == 'capture_exchange':
                if operation is not None or url is not None or history_action is not None or not valid_status(capture_status):
                    raise Refused('CONTROL_ACTION')
            elif action == 'voice_card':
                if operation is not None or url is not None or history_action is not None or card is not None and not valid_card(card):
                    raise Refused('CONTROL_ACTION')
            elif action == 'navigate':
                if (type(operation) is not str or re.fullmatch('[a-f0-9]{32}:navigate', operation) is None or
                        history_action is not None):
                    raise Refused('CONTROL_OPERATION')
                if operation in self.operations or not allowed_url(url):
                    raise Refused('CONTROL_OPERATION')
                self.operations.add(operation)  # Never repeat a mutation after an uncertain write.
            elif action == 'history':
                if (type(operation) is not str or re.fullmatch('[a-f0-9]{32}:history', operation) is None or
                        url is not None or history_action != 'back' or operation in self.operations):
                    raise Refused('CONTROL_OPERATION')
                self.operations.add(operation)
            elif action == 'media_key':
                if (type(operation) is not str or re.fullmatch('[a-f0-9]{32}:(?:media|media_key)', operation) is None or
                        url is not None or history_action is not None or operation in self.operations):
                    raise Refused('CONTROL_OPERATION')
                self.operations.add(operation)
            elif action == 'end':
                if (type(operation) is not str or re.fullmatch('[a-f0-9]{32}:end', operation) is None or
                        url is not None or history_action is not None or operation in self.operations):
                    raise Refused('CONTROL_OPERATION')
                self.operations.add(operation)
            elif action != 'observe' or operation is not None or url is not None or history_action is not None:
                raise Refused('CONTROL_ACTION')
            if not self.started: self._start(deadline)
            self.seq += 1
            if not exempt: self.regular_requests += 1
            row = {'schema': 1, 'session': self.session, 'seq': self.seq, 'action': action,
                   'deadline': deadline}
            if action == 'capture_exchange': row['status'] = dict(capture_status)
            elif action == 'voice_card': row['card'] = dict(card) if card is not None else None
            elif action == 'navigate': row.update(operation=operation, url=url)
            elif action == 'history': row.update(operation=operation, history_action=history_action)
            elif action in ('media_key', 'end'): row.update(operation=operation)
            self.channel.send(signed(row, self.key, b'request'), deadline)
            reply = verified(self.channel.receive(deadline), self.key, b'response')
            reply_keys = {'schema', 'session', 'seq', 'result', 'observation'} | (
                {'capture'} if action == 'capture_exchange' else {'voice'} if action == 'voice_card' else set())
            if (set(reply) != reply_keys or
                type(reply['schema']) is not int or reply['schema'] != 1 or
                type(reply['seq']) is not int or reply['seq'] != self.seq or
                reply['session'] != self.session or reply['result'] not in ('accepted', 'rejected', 'observed', 'exchanged')):
                raise Refused('CONTROL_SCHEMA')
            if action == 'capture_exchange':
                capture = reply['capture']
                if reply['result'] != 'exchanged' or type(capture) is not dict or set(capture) != {'command'} or not valid_command(capture['command']):
                    raise Refused('CONTROL_SCHEMA')
            if action == 'voice_card' and (reply['result'] != 'exchanged' or not _delivered(reply['voice'])):
                raise Refused('CONTROL_SCHEMA')
            if not exempt and reply['result'] == 'exchanged': raise Refused('CONTROL_SCHEMA')
            observation = reply['observation']
            if (type(observation) is not dict or
                set(observation) not in ({'ready', 'matched', 'generation'},
                                         {'ready', 'matched', 'generation', 'surfaceClass', 'video'}) or
                type(observation['ready']) is not bool or type(observation['matched']) is not bool or
                type(observation['generation']) is not int or not 0 <= observation['generation'] <= 256 or
                (action == 'observe') != (reply['result'] == 'observed')):
                raise Refused('CONTROL_SCHEMA')
            surface_class = observation.get('surfaceClass', 'browser_other')
            video = observation.get('video', _DEFAULT_VIDEO)
            if surface_class not in _CLASSES or not _valid_video(video):
                raise Refused('CONTROL_SCHEMA')
            observation = {'ready': observation['ready'], 'matched': observation['matched'],
                           'generation': observation['generation'], 'surfaceClass': surface_class,
                           'video': dict(video)}
            if action == 'capture_exchange':
                return 'exchanged', {'command':dict(capture['command']) if capture['command'] is not None else None}
            if action == 'voice_card':
                return 'exchanged', {'delivered': reply['voice']['delivered']}
            if action == 'navigate' and reply['result'] == 'accepted':
                self.target = url
                self.generation = observation['generation']
            elif action == 'history' and reply['result'] == 'accepted':
                self.target = None
                self.generation = observation['generation']
            if observation['generation'] != self.generation:
                observation = {'ready': False, 'matched': False, 'generation': self.generation,
                               'surfaceClass': 'browser_other', 'video': dict(_DEFAULT_VIDEO)}
            return reply['result'], observation
        except Exception:
            self.close()
            raise Refused('CONTROL_UNCONFIRMED') from None
        finally:
            self.lock.release()

    def capture_exchange(self, status, *, timeout_s):
        _, capture = self.request('capture_exchange', capture_status=status, timeout_s=timeout_s)
        return capture

    def voice_card(self, card, *, timeout_s):
        """Best-effort receipt push; a refused card or a busy lock never poisons."""
        if card is not None and not valid_card(card):
            return None
        try:
            _, voice = self.request('voice_card', card=card, timeout_s=timeout_s)
        except Refused:
            return None
        return voice

    def queue_navigation(self, body, *, operation, timeout_s):
        if type(body) is not dict or set(body) != {'url'}: return 'rejected'
        try:
            result, _ = self.request('navigate', operation=operation, url=body['url'], timeout_s=timeout_s)
            return result
        except Refused: return 'unknown'

    def navigate(self, operation, url, *, timeout_s):
        if type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:navigate', operation) or not allowed_url(url):
            return 'rejected'
        try:
            result, _ = self.request('navigate', operation=operation, url=url, timeout_s=timeout_s)
            return result
        except Refused:
            return 'unknown'

    def history(self, operation, action, *, timeout_s):
        if type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:history', operation) or action != 'back':
            return 'rejected'
        try:
            result, _ = self.request('history', operation=operation, history_action=action, timeout_s=timeout_s)
            return result
        except Refused:
            return 'unknown'

    def media_key(self, operation, *, timeout_s):
        if type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:(?:media|media_key)', operation):
            return 'rejected'
        try:
            result, _ = self.request('media_key', operation=operation, timeout_s=timeout_s)
            return result
        except Refused:
            return 'unknown'

    def end(self, operation, *, timeout_s):
        if type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:end', operation):
            return 'rejected'
        try:
            result, _ = self.request('end', operation=operation, timeout_s=timeout_s)
            return result
        except Refused:
            return 'unknown'

    def observe(self, session=None, *, timeout_s, presentation=None):
        if session is not None and session != self.session:
            return None
        deadline = _deadline(timeout_s)
        _, row = self.request('observe', timeout_s=timeout_s)
        if session is not None:
            return row
        # Only a separate trusted TV observer can authorize presented=True.
        remaining = deadline - time.monotonic()
        presented = (remaining > 0 and row['ready'] and row['matched'] and presentation is not None and
                     presentation(self.session, self.generation, timeout_s=remaining) is True)
        return BrowserObservation(self.session, True, row['ready'], presented,
                                  self.target if row['matched'] else None)


class TrialControlHost:
    """Child endpoint: binds only Trial.queue_navigation and main-loop page proof.

    publish() must run in Trial's own main/CDP thread, after checking its selected
    owned page against the requested URL. A queue ACK never sets matched=True.
    """
    def __init__(self, sock, trial, *, clock=time.monotonic):
        self.channel, self.trial = FramedSocket(sock), trial
        self.clock = clock
        self.session = self.key = None
        self.seq = 0
        self.regular_requests = 0
        self.operations = set()
        self.lock = threading.Lock()
        self.target = None
        self.generation = 0
        self.ready = self.matched = False
        self.surface_class = 'browser_other'
        self.video = dict(_DEFAULT_VIDEO)
        self.proved_at = None
        self.requested_at = None

    def close(self):
        self.channel.close()
        if self.key is not None: self.key[:] = b'\0' * len(self.key)
        self.target = None

    def publish(self, check_selected_owned_page, *, ready, probe=None):
        # Callback is trusted integration code, never a webpage/model-provided boolean.
        with self.lock:
            target, generation = self.target, self.generation
            requested = self.requested_at
        active = requested is not None and 0 <= self.clock() - requested <= 1
        # Do not keep probing a page after the voice controller has stopped
        # asking. A later observe request schedules a fresh main-thread check.
        if (target is not None or requested is not None) and not active:
            return False
        matched = ready is True and target is not None and check_selected_owned_page(target) is True
        surface_class = 'browser_other'
        video = dict(_DEFAULT_VIDEO)
        if ready is True and (active or requested is None) and callable(probe):
            info = probe()
            if (type(info) is dict and info.get('surfaceClass') in _CLASSES and
                    _valid_video(info.get('video'))):
                surface_class = info['surfaceClass']
                video = dict(info['video'])
        with self.lock:
            if generation != self.generation: return False
            self.ready, self.matched, self.proved_at = ready is True, matched, self.clock()
            self.surface_class, self.video = surface_class, video
        return True

    def observation(self):
        with self.lock:
            fresh = self.proved_at is not None and 0 <= self.clock() - self.proved_at <= 1
            active = self.ready and fresh
            return {'ready': active, 'matched': self.matched and fresh,
                    'generation': self.generation,
                    'surfaceClass': self.surface_class if active else 'browser_other',
                    'video': dict(self.video) if active else dict(_DEFAULT_VIDEO)}

    def _hello(self, deadline):
        row = self.channel.receive(deadline)
        if (type(row) is not dict or set(row) != {'schema', 'kind', 'session', 'key'} or
                type(row['schema']) is not int or row['schema'] != 1 or row['kind'] != 'hello' or
                not _hex(row['session'], 32) or not _hex(row['key'], 64)):
            raise Refused('CONTROL_SCHEMA')
        self.session, self.key = row['session'], bytearray.fromhex(row['key'])
        self.channel.send(signed({'schema': 1, 'kind': 'ready', 'session': self.session},
                                 self.key, b'hello'), deadline)

    def _acquire_trial(self, deadline_ts, fn):
        remaining = deadline_ts - time.monotonic()
        acquired = remaining > 0 and self.trial.lock.acquire(timeout=remaining)
        if not acquired:
            return False
        try:
            if time.monotonic() >= deadline_ts:
                return False
            return fn() is True
        finally:
            self.trial.lock.release()

    def handle(self, row):
        row = verified(row, self.key, b'request')
        base = {'schema', 'session', 'seq', 'action', 'deadline'}
        action = row.get('action')
        expected_keys = {
            'observe': base,
            'capture_exchange': base | {'status'},
            'voice_card': base | {'card'},
            'navigate': base | {'operation', 'url'},
            'history': base | {'operation', 'history_action'},
            'media_key': base | {'operation'},
            'end': base | {'operation'},
        }.get(action)
        if (expected_keys is None or type(row['schema']) is not int or row['schema'] != 1 or
                row.get('session') != self.session or type(row.get('seq')) is not int or
                row['seq'] != self.seq + 1 or type(row.get('deadline')) not in (int, float) or
                not math.isfinite(row['deadline']) or row['deadline'] - time.monotonic() > 5 or
                set(row) != expected_keys):
            raise Refused('CONTROL_SCHEMA')
        # Periodic status exchanges neither spend the session's mutation budget
        # nor ask the main loop for page proof.
        exempt = action in ('capture_exchange', 'voice_card')
        if not exempt:
            if self.regular_requests >= MAX_REQUESTS: raise Refused('CONTROL_LIFETIME')
            self.regular_requests += 1
        self.seq = row['seq']
        if not exempt:
            with self.lock: self.requested_at = self.clock()
        result = 'observed'
        capture = voice = None
        if action == 'voice_card':
            card = row['card']
            if card is not None and not valid_card(card): raise Refused('CONTROL_SCHEMA')
            exchange = getattr(self.trial, 'voice_card_exchange', None)
            # The mirror has its own leaf lock; Trial.lock is never taken here.
            if time.monotonic() >= row['deadline'] or not callable(exchange):
                raise Refused('CONTROL_UNCONFIRMED')
            voice = exchange({'card': dict(card) if card is not None else None})
            if not _delivered(voice) or time.monotonic() >= row['deadline']:
                raise Refused('CONTROL_UNCONFIRMED')
            voice, result = {'delivered': voice['delivered']}, 'exchanged'
        elif action == 'capture_exchange':
            if not valid_status(row['status']): raise Refused('CONTROL_SCHEMA')
            exchange = getattr(self.trial, 'capture_exchange', None)
            captured = []
            def perform_capture():
                if not callable(exchange): return False
                value = exchange({'session':self.session,'status':dict(row['status'])})
                if type(value) is not dict or set(value) != {'command'} or not valid_command(value['command']): return False
                captured.append(value); return True
            # Mailbox has its own locks; taking Trial.lock here would invert
            # the RTC media->Trial ordering used by its readiness checks.
            if time.monotonic() >= row['deadline'] or not perform_capture() or time.monotonic() >= row['deadline']:
                raise Refused('CONTROL_UNCONFIRMED')
            capture, result = captured[0], 'exchanged'
        elif action == 'navigate':
            operation, url = row['operation'], row['url']
            if (type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:navigate', operation) or
                    operation in self.operations or len(self.operations) >= 256 or not allowed_url(url)):
                result = 'rejected'
            else:
                self.operations.add(operation)
                accepted = self._acquire_trial(row['deadline'], lambda: self.trial.queue_navigation({'url': url}))
                # Never hold host.lock while acquiring Trial.lock: the main loop
                # publishes under the opposite order. Invalidate before the ACK.
                if accepted:
                    with self.lock:
                        self.target, self.generation = url, min(256, self.generation + 1)
                        self.matched, self.proved_at = False, None
                result = 'accepted' if accepted else 'rejected'
        elif action == 'history':
            operation, hist = row['operation'], row['history_action']
            queue_hist = getattr(self.trial, 'queue_history', None)
            if (type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:history', operation) or
                    hist != 'back' or operation in self.operations or len(self.operations) >= 256 or
                    not callable(queue_hist)):
                result = 'rejected'
            else:
                self.operations.add(operation)
                accepted = self._acquire_trial(row['deadline'], lambda: queue_hist({'action': 'back'}))
                if accepted:
                    with self.lock:
                        self.target, self.generation = None, min(256, self.generation + 1)
                        self.matched, self.proved_at = False, None
                result = 'accepted' if accepted else 'rejected'
        elif action == 'media_key':
            operation = row['operation']
            queue_media = getattr(self.trial, 'queue_media_key', None)
            if (type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:(?:media|media_key)', operation) or
                    operation in self.operations or len(self.operations) >= 256 or not callable(queue_media)):
                result = 'rejected'
            else:
                self.operations.add(operation)
                accepted = self._acquire_trial(row['deadline'], queue_media)
                result = 'accepted' if accepted else 'rejected'
        elif action == 'end':
            operation = row['operation']
            queue_end = getattr(self.trial, 'queue_end', None)
            if (type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:end', operation) or
                    operation in self.operations or len(self.operations) >= 256 or not callable(queue_end)):
                result = 'rejected'
            else:
                self.operations.add(operation)
                accepted = self._acquire_trial(row['deadline'], queue_end)
                result = 'accepted' if accepted else 'rejected'
        response = {'schema': 1, 'session': self.session, 'seq': self.seq,
                    'result': result, 'observation': self.observation()}
        if action == 'capture_exchange': response['capture'] = capture
        elif action == 'voice_card': response['voice'] = voice
        return signed(response, self.key, b'response')

    def serve(self, *, lifetime_s=315360000):
        if type(lifetime_s) not in (int, float) or not math.isfinite(lifetime_s) or not 0 < lifetime_s <= 315360000:
            raise Refused('CONTROL_LIFETIME')
        deadline = time.monotonic() + lifetime_s
        try:
            # This private descriptor may sit idle until the first voice command.
            # Browser startup and normal viewing can exceed five seconds. Keep
            # the finite session deadline; close() still interrupts the wait.
            self._hello(deadline)
            for _ in range(MAX_SEQUENCE):
                row = self.channel.receive(deadline)
                response = self.handle(row)
                self.channel.send(response, min(deadline, time.monotonic() + 5))
        except Exception:
            pass  # Fail closed, fixed metadata only, no payload/error log.
        finally:
            self.close()


def channel_pair(session):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try: return ControlClient(parent, session), child
    except Exception:
        parent.close(); child.close(); raise
