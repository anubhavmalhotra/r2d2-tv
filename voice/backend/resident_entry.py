"""Opt-in resident Manager entry; import and construction never start models/sockets.

Existing owner-private enrollment is read, never minted or updated. TLS still
validates certificate/key structure and possession per connection. Mutable read
buffers are cleared; Python/OpenSSL immutable key objects are not forensic wipes.
"""
import hashlib
import hmac
import importlib
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import ssl
import stat
import threading
import time
from types import SimpleNamespace

if __package__:
    from .resident_manager import ResidentManager
    from .resident_utterance import resident_device_key
    from .ui_capture_broker import UiCaptureBroker
    from .voice_status import VoiceStatus, parse_ping, ping_mac
else:
    from resident_manager import ResidentManager
    from resident_utterance import resident_device_key
    from ui_capture_broker import UiCaptureBroker
    from voice_status import VoiceStatus, parse_ping, ping_mac


ENROLLMENT_KEYS = {'schema', 'kind', 'tvCertificate', 'tvCertificateSha256',
                   'sparkCertificateSha256', 'enrolledAt', 'expiresAt', 'deviceId'}
WARM_STATES = {'unloaded', 'warming', 'ready', 'grace', 'unloading', 'degraded'}
ROTATE_S, SKEW_S = 30 * 86400, 300


def _need(value, code='RESIDENT_CONFIG'):
    if not value:
        raise ValueError(code)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _private_read(path, limit):
    """Bounded held regular-file read. No FIFO waits, symlinks, or named writes."""
    fd, raw = None, bytearray()
    try:
        _need(isinstance(path, (str, Path)), 'RESIDENT_PRIVATE_FILE')
        path = Path(path)
        _need(path.is_absolute() and path.resolve() == path, 'RESIDENT_PRIVATE_FILE')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
              and not before.st_mode & 0o077 and before.st_nlink == 1
              and 0 < before.st_size <= limit, 'RESIDENT_PRIVATE_FILE')
        while len(raw) <= limit:
            piece = os.read(fd, min(4096, limit + 1 - len(raw)))
            if not piece:
                break
            raw.extend(piece)
        def identity(s):
            return tuple(getattr(s, name) for name in (
                'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
                'st_size', 'st_mtime_ns', 'st_ctime_ns'))
        _need(len(raw) == before.st_size and identity(before) == identity(os.fstat(fd))
              and identity(before) == identity(os.lstat(path)), 'RESIDENT_PRIVATE_FILE')
        return raw.decode('ascii')
    except (OSError, UnicodeError, ValueError):
        raise ValueError('RESIDENT_PRIVATE_FILE') from None
    finally:
        raw[:] = b'\0' * len(raw)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                raise ValueError('RESIDENT_PRIVATE_CLOSE') from None


def _json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, 'RESIDENT_ENROLLMENT')
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique)
    except (TypeError, ValueError):
        raise ValueError('RESIDENT_ENROLLMENT') from None


def certificate_sha256(text):
    _need(type(text) is str and len(text) <= 8192 and re.fullmatch(
        r'-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+\n-----END CERTIFICATE-----\n?', text),
        'RESIDENT_CERTIFICATE')
    try:
        der = ssl.PEM_cert_to_DER_cert(text)
    except (ValueError, TypeError):
        raise ValueError('RESIDENT_CERTIFICATE') from None
    _need(0 < len(der) <= 6144, 'RESIDENT_CERTIFICATE')
    return hashlib.sha256(der).hexdigest()


class ExistingIdentity:
    def __init__(self, certificate, private_key):
        self.certificate, self.private_key = certificate, private_key

    def __call__(self):
        certificate = _private_read(self.certificate, 8192)
        private = _private_read(self.private_key, 8192)
        _need(re.fullmatch(r'-----BEGIN (PRIVATE KEY|EC PRIVATE KEY|RSA PRIVATE KEY)-----\n'
                          r'[A-Za-z0-9+/=\n]+\n-----END \1-----\n?', private), 'RESIDENT_KEY')
        return SimpleNamespace(certificate_pem=certificate, key_pem=private,
                               sha256=certificate_sha256(certificate))


class ExistingEnrollment:
    def __init__(self, path, identity_loader, *, wall=time.time):
        self.path, self.identity_loader, self.wall = path, identity_loader, wall

    def current(self):
        row = _json(_private_read(self.path, 16384))
        _need(type(row) is dict and set(row) == ENROLLMENT_KEYS
              and type(row['schema']) is int and row['schema'] == 1
              and row['kind'] == 'r2d2-tv-enrollment', 'RESIDENT_ENROLLMENT')
        _need(all(type(row[k]) is str and re.fullmatch(r'[a-f0-9]{64}', row[k])
                  for k in ('tvCertificateSha256', 'sparkCertificateSha256'))
              and type(row['deviceId']) is str and re.fullmatch(r'[a-f0-9]{16}', row['deviceId']),
              'RESIDENT_ENROLLMENT')
        _need(not hmac.compare_digest(row['tvCertificateSha256'], row['sparkCertificateSha256'])
              and hmac.compare_digest(certificate_sha256(row['tvCertificate']), row['tvCertificateSha256'])
              and hmac.compare_digest(self.identity_loader().sha256, row['sparkCertificateSha256']),
              'RESIDENT_ENROLLMENT_PIN')
        start, end, now = row['enrolledAt'], row['expiresAt'], self.wall()
        _need(type(start) is int and type(end) is int and 0 < start < end < 2**40
              and end - start <= ROTATE_S and _number(now)
              and start - SKEW_S <= now < end, 'RESIDENT_ENROLLMENT_EXPIRED')
        return row


def _peer_closed(sock):
    """True once a long-poll client hung up; never consumes request bytes."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b''
    except (BlockingIOError, InterruptedError):
        return False
    except (OSError, ValueError):
        return True


def _card_pusher():
    """The viewer card push helper, when this build ships one; absence is a no-op."""
    for name in ('voice_card_push', 'browser_channel'):
        try:
            module = importlib.import_module('.' + name, __package__) if __package__ else importlib.import_module(name)
        except Exception:
            continue
        push = getattr(module, 'push_voice_card', None)
        if callable(push):
            return push
    return None


def _runtime():
    if __package__ == 'voice_control':
        from .pool import ProductionWorkerFactory, WorkerPool, ArmEvidencePowerSource, create_warm_hook
    elif not __package__:
        from pool import ProductionWorkerFactory, WorkerPool, ArmEvidencePowerSource, create_warm_hook
    else:
        from voice.runtime.pool import ProductionWorkerFactory, WorkerPool, ArmEvidencePowerSource, create_warm_hook
    return ProductionWorkerFactory, WorkerPool, ArmEvidencePowerSource, create_warm_hook


class ResidentEntry:
    """One Manager-owned service; finite per-worker/per-utterance ownership remains."""
    enabled = True

    def __init__(self, manager, manager_key, config, *, spark_ip, tv_ip, http_port,
                 base_handler, clock=time.monotonic, wall=time.time, spawn=None):
        cfg = dict(config)
        _need(cfg.get('enabled') is True and cfg.get('mode') == 'resident'
              and callable(clock) and callable(wall) and isinstance(base_handler, type))
        _need(type(manager_key) is str and re.fullmatch(r'[a-f0-9]{64}', manager_key))
        _need(type(http_port) is int and 1 <= http_port <= 65535)
        port = cfg.get('pcm_port', 4182)
        _need(type(port) is int and 1024 <= port <= 65535 and port != http_port)
        for name in ('python', 'asr_path', 'intent_path', 'voice_key_path', 'enrollment_path',
                     'certificate', 'private_key', 'ssap_key_path', 'ssap_pin_path'):
            _need(type(cfg.get(name)) is str and Path(cfg[name]).is_absolute())
        verify = cfg.get('verify_assets', False)
        _need(type(verify) is bool)
        key_text = _private_read(cfg['voice_key_path'], 65)
        _need(re.fullmatch(r'[a-f0-9]{64}\n?', key_text), 'RESIDENT_VOICE_KEY')
        key = key_text.rstrip('\n')
        _need(not hmac.compare_digest(key, manager_key),
              'RESIDENT_VOICE_KEY')
        ui_enabled = cfg.get('ui_capture', False)
        _need(type(ui_enabled) is bool)
        ui_key = None
        if ui_enabled:
            _need(type(cfg.get('ui_capture_key_path')) is str and Path(cfg['ui_capture_key_path']).is_absolute(), 'UI_CAPTURE_KEY')
            ui_key = _private_read(cfg['ui_capture_key_path'], 65).rstrip('\n')
            _need(re.fullmatch('[a-f0-9]{64}', ui_key) and not hmac.compare_digest(ui_key,key)
                  and not hmac.compare_digest(ui_key,manager_key), 'UI_CAPTURE_KEY')
        self.identity_loader = ExistingIdentity(cfg['certificate'], cfg['private_key'])
        self.enrollment = ExistingEnrollment(cfg['enrollment_path'], self.identity_loader, wall=wall)
        self.enrollment.current()  # Existing identity/enrollment only. Never pair or generate.
        self.manager, self.clock, self.tv_ip = manager, clock, tv_ip
        self._key, self._host = key, '%s:%d' % (spark_ip, http_port)
        self._stop = threading.Event()
        self._lock, self._close_lock = threading.RLock(), threading.Lock()
        self._warm_started = self._serving = self._closed = False
        self._cleanup = True
        self.ui_capture = UiCaptureBroker(manager, ui_key, clock=clock) if ui_enabled else None
        factory_type, pool_type, source_type, make_hook = _runtime()
        factory = factory_type(cfg['python'], cfg['asr_path'], cfg['intent_path'], qualified=True,
                               clock=clock, spawn=spawn, prompt_phase='P5', verify_assets=verify)
        self.pool = pool_type(factory, clock=clock)
        self.source = source_type(clock)
        self.hook = make_hook(self.pool, source=self.source, clock=clock)
        # Exists before ResidentManager: its utterance server builds the controller.
        self.voice_status = VoiceStatus(clock=clock)
        try:
            self.helper = ResidentManager(
                manager=manager, spark_ip=spark_ip, tv_ip=tv_ip, manager_key=manager_key,
                ssap_key_path=cfg['ssap_key_path'], ssap_pin_path=cfg['ssap_pin_path'],
                hook=self.hook, enrollment=self.enrollment, identity_loader=self.identity_loader,
                voice_key=key, pcm_port=port, clock=clock, voice_status=self.voice_status)
            self.http_handler = self._handler(base_handler)
        except BaseException:
            self.hook.close(timeout_s=0)
            raise

    def warm(self, timeout_s=None):
        # timeout_s is accepted for the existing Manager interface, not a model
        # admission deadline. Only the monitor starts; active TV evidence warms.
        with self._lock:
            if self._stop.is_set():
                return False
            if self._warm_started:
                return True
            started = self.hook.start() is True
            self._warm_started = started
            return started

    def serve(self):
        with self._lock:
            if self._stop.is_set() or self._serving:
                return False
            self._serving = True
        try:
            if not self.helper.start():
                raise RuntimeError('RESIDENT_START_FAILED')
            push, seen, tick = _card_pusher(), None, time.monotonic()
            while not self._stop.is_set():
                # 4 Hz service tick; a VoiceStatus change pushes the viewer card at once.
                seq = self.voice_status.wait_change(seen, max(0.0, tick - time.monotonic()))
                if self._stop.is_set():
                    break
                if time.monotonic() >= tick:
                    tick = time.monotonic() + 0.25
                    self.voice_status.readiness(self.hook.state, self._agent_state())
                    if self.ui_capture is not None: self.ui_capture.tick()
                    if self.helper.resident.stopped.is_set():
                        raise RuntimeError('RESIDENT_LISTENER_STOPPED')
                elif seq == seen:
                    continue
                changed, seen = seq != seen, seq
                # Push on change, and keep only a visible card (and its delivery
                # ack) alive at the tick rate. Every exchange is a chance to lose
                # the shared browser channel, so an idle viewer gets no pushes.
                card = self.voice_status.viewer_card() if not changed else None
                if push is not None and (changed or card is not None and card['phase'] != 'idle'):
                    try: push(self.manager, self.voice_status, timeout_s=.25)
                    except Exception: pass
            return True
        finally:
            self.close()

    def close(self, timeout_s=30):
        _need(_number(timeout_s) and 0 <= timeout_s <= 30, 'RESIDENT_CLOSE_BUDGET')
        self._stop.set()
        self.voice_status.close()  # never blocks; releases long-poll and receipt waiters
        deadline = time.monotonic() + timeout_s
        if not self._close_lock.acquire(timeout=timeout_s):
            return False
        try:
            if self._closed:
                return self._cleanup
            self._closed = True
            if self.ui_capture is not None:
                try: self._cleanup = (self.ui_capture.close(timeout_s=max(0,deadline-time.monotonic())) is True) and self._cleanup
                except BaseException: self._cleanup = False
            for owner in (self.helper, self.hook):
                try:
                    result = owner.close(timeout_s=max(0, deadline - time.monotonic()))
                    self._cleanup = (result is True) and self._cleanup
                except BaseException:
                    self._cleanup = False
            return self._cleanup
        finally:
            self._close_lock.release()

    def _resident_get(self, handler):
        # No query/fragment aliases, body, browser origin, duplicate headers or
        # proxy trust. The listener itself also restricts the direct peer.
        pairs = list(handler.headers.items())
        names = [name.lower() for name, _ in pairs]
        _need(not self._stop.is_set() and handler.client_address[0] == self.tv_ip
              and len(pairs) <= 16 and len(names) == len(set(names))
              and sum(len(k) + len(v) for k, v in pairs) <= 2048
              and handler.headers.get('Host') == self._host
              and all(k not in names for k in ('origin', 'transfer-encoding', 'expect'))
              and handler.headers.get('Content-Length', '0') == '0', 'RESIDENT_HTTP_AUTH')
        return self.enrollment.current()

    def _heartbeat(self, handler):
        self._resident_get(handler)
        state = handler.headers.get('X-R2D2-TV-Power')
        state = state if state in ('active', 'standby', 'off') else 'unknown'
        # Unknown does not manufacture or renew active evidence; a fresh pinned
        # SSAP result can still supply power state independently.
        self.hook.note_tv_power(state)
        warm = self.hook.state
        warm = warm if warm in WARM_STATES else 'unavailable'
        agent = self._agent_state()
        self.voice_status.readiness(warm, agent)
        return {'warm': warm, 'session': 'none', 'agent': agent}

    def _agent_state(self):
        return 'paused' if self.helper.killswitch.paused() else 'ok'

    def _voice_phase(self, handler):
        # Same gates as ui-capture, plus a per-TV MAC and strictly increasing
        # (boot, seq): the plaintext voice key alone cannot fake "Listening".
        pairs = list(handler.headers.items()); names = [name.lower() for name, _ in pairs]
        length = handler.headers.get('Content-Length', '')
        _need(not self._stop.is_set() and handler.client_address[0] == self.tv_ip and len(pairs) <= 16
              and len(names) == len(set(names)) and sum(len(k)+len(v) for k,v in pairs) <= 2048
              and handler.headers.get('Host') == self._host
              and all(key not in names for key in ('origin','transfer-encoding','expect'))
              and handler.headers.get('Content-Type') == 'application/json'
              and re.fullmatch('[1-9][0-9]{0,2}', length) is not None
              and int(length) <= 256, 'VOICE_PHASE_HTTP_AUTH')
        record = self.enrollment.current()
        handler.connection.settimeout(1)
        body = handler.rfile.read(int(length))
        _need(len(body) == int(length), 'VOICE_PHASE_HTTP_BODY')
        try: value = _json(body.decode('ascii'))
        except (ValueError, UnicodeError): raise ValueError('VOICE_PHASE_HTTP_BODY') from None
        row, mac = parse_ping(value)
        key = resident_device_key(self._key, record['tvCertificateSha256'])
        try: expected = ping_mac(key, row)
        finally: key[:] = b'\0' * len(key)
        _need(hmac.compare_digest(expected, mac), 'VOICE_PHASE_MAC')
        surface = self.voice_status.phase_ping(row)
        _need(surface is not None, 'VOICE_PHASE_REPLAY')
        return {'ok': True, 'surface': surface}

    def _voice_receipt(self, handler, press):
        self._resident_get(handler)
        return self.voice_status.receipt(press)

    def _voice_feed(self, handler, since):
        # Browser-key auth already ran in route(). False: the client left, write nothing.
        if self._stop.is_set():
            # Voice has stopped for this process: a 404 lets Home show its own offline
            # chip and back off, instead of a frozen "ready" chip re-polled at 4 Hz.
            return None
        view = self.voice_status.wait_home(
            since, gone=lambda: self._stop.is_set() or _peer_closed(handler.connection))
        return False if view is None else view

    def _ui_exchange(self, handler):
        pairs = list(handler.headers.items()); names = [name.lower() for name, _ in pairs]
        length = handler.headers.get('Content-Length', '')
        _need(self.ui_capture is not None and not self._stop.is_set()
              and handler.client_address[0] == self.tv_ip and len(pairs) <= 16
              and len(names) == len(set(names)) and sum(len(k)+len(v) for k,v in pairs) <= 2048
              and handler.headers.get('Host') == self._host
              and all(key not in names for key in ('origin','transfer-encoding','expect'))
              and handler.headers.get('Content-Type') == 'application/json'
              and re.fullmatch('[1-9][0-9]{0,3}',length) is not None
              and int(length) <= 2048, 'UI_CAPTURE_HTTP_AUTH')
        self.enrollment.current()
        handler.connection.settimeout(1)
        body = handler.rfile.read(int(length))
        _need(len(body) == int(length), 'UI_CAPTURE_HTTP_BODY')
        try: value = _json(body.decode('ascii'))
        except (ValueError,UnicodeError): raise ValueError('UI_CAPTURE_HTTP_BODY') from None
        return self.ui_capture.exchange(value)

    def _handler(self, base):
        entry = self
        path = '/' + self._key + '/state'
        ui_path = '/' + self._key + '/ui-capture'
        voice_path = re.compile(r'/([a-f0-9]{64})/(?:voice-phase|voice-receipt/([a-f0-9]{16}))')
        def voice_route(raw):
            m = voice_path.fullmatch(raw)
            return m if m is not None and hmac.compare_digest(m[1], entry._key) else None
        class Handler(base):
            def voice_feed(self, since):
                return entry._voice_feed(self, since)

            def do_GET(self):
                if self.path == ui_path: return self.reply(405 if entry.ui_capture is not None else 404)
                m = voice_route(self.path)
                if m is not None:
                    if m[2] is None: return self.reply(405)
                    try: result = entry._voice_receipt(self, m[2])
                    except Exception: return self.reply(403)
                    return self.reply(200, result)
                if self.path != path:
                    return super().do_GET()
                try:
                    result = entry._heartbeat(self)
                except Exception:
                    return self.reply(403)
                return self.reply(200, result)

            def do_POST(self):
                if self.path == ui_path:
                    if entry.ui_capture is None: return self.reply(404)
                    try: result = entry._ui_exchange(self)
                    except Exception: return self.reply(403)
                    return self.reply(200,result)
                m = voice_route(self.path)
                if m is not None:
                    if m[2] is not None: return self.reply(405)
                    try: result = entry._voice_phase(self)
                    except Exception: return self.reply(403)
                    return self.reply(200, result)
                if self.path == path:
                    return self.reply(405)
                return super().do_POST()
        return Handler
