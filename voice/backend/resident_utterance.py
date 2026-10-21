"""Resident, one-utterance-per-connection mTLS server; inert until start().

The host owns enrollment, identity, WarmHook/power reader and action adapters.
controller_factory(intent_provider) supplies one persistent Controller or
AgentController during construction, before the host starts warming the pool.
Its confirmation/rate/pause state therefore survives individual connections.
The factory must not launch anything on construction. This listener neither warms models on import nor
owns/stops the shared hook. A TLS-authenticated TV can provide activity evidence;
continuous readiness still requires the host's authenticated power-state reader.
"""
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import select
import socket
import ssl
import struct
import threading
import time

if __package__:
    from .controller import Outcome, Refused
    from .ingress import Device, VoiceIngress, MAX_HANDSHAKE, read_exact
else:
    from controller import Outcome, Refused
    from ingress import Device, VoiceIngress, MAX_HANDSHAKE, read_exact

RESIDENT_DOMAIN = b'R2D2-RESIDENT-PTT-v1\0'


def _load_runtime_deps():
    if __package__ == 'voice_control':
        from .physical_capture.ram_credentials import Credentials
        from .pool import pool_providers
    elif not __package__:
        # Explicit migration seam for the existing flat service package. No
        # historical warm_inference/AgentBridge implementation is required.
        from ram_credentials import Credentials
        from pool import pool_providers
    else:
        from voice.backend.physical_capture.ram_credentials import Credentials
        from voice.runtime.pool import pool_providers
    return Credentials, pool_providers


def resident_device_key(voice_key, tv_cert_sha256):
    if any(type(v) is not str or not re.fullmatch(r'[a-fA-F0-9]{64}', v)
           for v in (voice_key, tv_cert_sha256)):
        raise ValueError('RESIDENT_KEY')
    return bytearray(hashlib.sha256(RESIDENT_DOMAIN + bytes.fromhex(voice_key)
                                  + bytes.fromhex(tv_cert_sha256)).digest())


class _UtteranceController:
    """Acquire only after HMAC; revoke action admission on close/port handoff."""
    def __init__(self, controller, coordinator, live, status=None, utterance=None):
        self.controller, self.coordinator, self.live = controller, coordinator, live
        self.status, self.utterance = status, utterance

    def _note(self, name):
        # Enum-only UI progress; a status fault never changes the utterance.
        if self.status is not None:
            try: getattr(self.status, name)(self.utterance)
            except Exception: pass

    def check(self):
        if not self.live():
            raise Refused('VOICE_CANCELLED')

    def arm(self):
        self.check()
        try:
            self.coordinator.acquire()
        except Refused as exc:
            if str(exc) != 'VOICE_BUSY':
                raise
            self.coordinator.release()
            self.coordinator.acquire()
        self.check()
        try:
            lease = self.controller.arm()
        except Refused as exc:
            # Backend Refused carries its code as the message, not a .code attribute.
            if str(exc) != 'CONTROLLER_BUSY':
                raise
            self.controller.cancel()
            deadline = time.monotonic() + 1.0
            while True:
                self.check()
                try:
                    lease = self.controller.arm()
                    break
                except Refused as retry_exc:
                    if str(retry_exc) != 'CONTROLLER_BUSY' or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        if not self.live():
            self.controller.cancel(lease)
            raise Refused('VOICE_CANCELLED')
        self._note('armed')
        return lease

    def execute(self, lease, transcript):
        self.check()
        self._note('thinking')  # the transcript itself never leaves this call
        return self.controller.execute(lease, transcript)

    def cancel(self, lease=None):
        return self.controller.cancel(lease)


class ResidentUtteranceServer:
    def __init__(self, *, spark_ip, tv_ip, pcm_port, voice_key, enrollment,
                 identity_loader, hook, sessions=None, controller_factory=None,
                 audit=None, clock=time.monotonic, credentials_factory=None, voice_status=None):
        for address in (spark_ip, tv_ip):
            parsed = ipaddress.ip_address(address)
            if parsed.version != 4 or not (parsed.is_private or parsed.is_loopback):
                raise ValueError('RESIDENT_ADDRESS')
        if type(pcm_port) is not int or not 0 <= pcm_port <= 65535:
            raise ValueError('RESIDENT_PORT')
        if voice_key is not None:
            key = resident_device_key(voice_key, '0' * 64)
            key[:] = b'\0' * len(key)
        self.spark_ip, self.tv_ip, self.pcm_port = spark_ip, tv_ip, pcm_port
        self.voice_key, self.enrollment, self.identity_loader = voice_key, enrollment, identity_loader
        self.hook, self.sessions, self.controller_factory = hook, sessions, controller_factory
        self.audit, self.clock, self.credentials_factory = audit, clock, credentials_factory
        self.voice_status = voice_status
        self.stopping, self.stopped = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self._listener = self._active_socket = self._active_controller = self._thread = None
        self._yield_until = 0.0
        self._generation = 0
        self.cleanup_complete = True
        self._coordinator = self._asr = self._controller = self._credentials_cls = None
        if voice_key is not None:
            if not callable(controller_factory):
                raise ValueError('RESIDENT_CONTROLLER_REQUIRED')
            self._credentials_cls, providers = _load_runtime_deps()
            coordinator, asr, intent = providers(hook)
            try:
                controller = controller_factory(intent)
                if not all(callable(getattr(controller, method, None)) for method in ('arm', 'cancel', 'execute')):
                    raise ValueError('RESIDENT_CONTROLLER_REQUIRED')
            except BaseException:
                coordinator.release()
                raise
            self._coordinator, self._asr, self._controller = coordinator, asr, controller

    @property
    def _busy(self):
        # Existing SessionManager checks this before reserving a per-attempt port.
        with self.lock:
            return self._active_socket is not None

    def start(self):
        if self.voice_key is None:
            return False
        if not callable(self.controller_factory):
            raise ValueError('RESIDENT_CONTROLLER_REQUIRED')
        with self.lock:
            if self._thread is not None or self.stopping.is_set():
                return False
            thread = threading.Thread(target=self._serve_loop, name='r2d2-resident-utterance', daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._thread = None
                raise
        return True

    @staticmethod
    def _disconnect(sock):
        if sock is not None:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: sock.close()
            except OSError: pass

    def _revoke(self, *, duration_s=None):
        with self.lock:
            self._generation += 1
            if duration_s is not None:
                self._yield_until = max(self._yield_until, self.clock() + duration_s)
            listener, self._listener = self._listener, None
            active, controller = self._active_socket, self._active_controller
        self._disconnect(listener)
        self._disconnect(active)
        if controller is not None:
            try: controller.cancel()
            except Exception:
                self.cleanup_complete = False

    def yield_port(self, duration_s=16.0):
        """Revoke this connection and release the port before per-attempt bind.

        Caller must also publish its session reservation before this finite hold
        ends. A handoff never resumes an old connection after the hold expires.
        """
        if type(duration_s) not in (int, float) or not math.isfinite(duration_s) or not 0 <= duration_s <= 60:
            raise ValueError('RESIDENT_YIELD')
        self._revoke(duration_s=duration_s)

    def close(self, timeout_s=3.0):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 <= timeout_s <= 30:
            raise ValueError('RESIDENT_CLOSE')
        self.stopping.set()
        self._revoke()
        with self.lock: thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
        if thread is None and not self._busy:
            self.stopped.set()
        return self.stopped.is_set() and self.cleanup_complete

    def _has_per_attempt_session(self):
        try:
            with self.lock: held = self.clock() < self._yield_until
            return held or (self.sessions is not None and self.sessions.has_per_attempt_session() is not False)
        except Exception:
            return True

    def _live(self, generation):
        with self.lock:
            same = generation == self._generation
        return same and not self.stopping.is_set() and not self._has_per_attempt_session()

    def status(self):
        with self.lock:
            listening, active = self._listener is not None, self._active_socket is not None
        state = getattr(self.hook, 'state', 'unavailable')
        if state not in ('unloaded', 'warming', 'ready', 'grace', 'unloading', 'degraded'):
            state = 'unavailable'
        return dict(listening=listening, active=active, stopping=self.stopping.is_set(),
                    stopped=self.stopped.is_set(), cleanupComplete=self.cleanup_complete, warmState=state)

    def _close_listener(self):
        with self.lock: sock, self._listener = self._listener, None
        self._disconnect(sock)

    def _ensure_listener(self):
        with self.lock:
            if self.stopping.is_set() or self.clock() < self._yield_until:
                raise OSError('RESIDENT_STOPPED')
            if self._listener is not None:
                return self._listener
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(0.25)
                sock.bind((self.spark_ip, self.pcm_port))
                sock.listen(2)
            except BaseException:
                sock.close()
                raise
            self._listener = sock
            return sock

    def _audit_event(self, event, code):
        if self.audit is not None:
            try: self.audit.append(dict(schema=1, kind='r2d2-resident-utterance', event=event, code=code))
            except Exception: pass

    def _status(self, name, *args):
        if self.voice_status is not None:
            try: return getattr(self.voice_status, name)(*args)
            except Exception: return None
        return None

    def _serve_loop(self):
        try:
            while not self.stopping.is_set():
                if self._has_per_attempt_session():
                    self._close_listener(); self.stopping.wait(0.25); continue
                try:
                    listener = self._ensure_listener()
                    raw, peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    self._close_listener(); self.stopping.wait(0.25); continue
                self._close_listener()
                if peer[0] != self.tv_ip:
                    self._disconnect(raw)
                    continue
                self._handle_connection(raw)
        finally:
            self._close_listener()
            self.stopped.set()

    def _send_outcome(self, tls, state, code):
        if (state not in ('verified', 'accepted', 'refused', 'rejected', 'unknown') or
                type(code) is not str or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code)):
            state, code = 'refused', 'VOICE_UNAVAILABLE'
        body = json.dumps(dict(schema=1, state=state, code=code), separators=(',', ':')).encode('ascii')
        tls.settimeout(2.0)
        tls.sendall(struct.pack('>I', len(body)) + body)

    def _refuse_after_challenge(self, tls, code):
        # TV client expects a challenge before an outcome, including warmup.
        deadline = self.clock() + 3.0
        tls.settimeout(2.0)
        tls.sendall(os.urandom(32))
        try:
            length = struct.unpack('>I', read_exact(tls, 4, deadline, self.clock))[0]
            if 1 <= length <= MAX_HANDSHAKE:
                read_exact(tls, length, deadline, self.clock)
        except Exception: pass
        self._send_outcome(tls, 'refused', code)

    def _handshake(self, tls, generation):
        # A cross-thread socket.close alone need not wake an SSL handshake on
        # every platform. Poll the owned descriptor within the same 3s budget.
        deadline = self.clock() + 3.0
        tls.setblocking(False)
        while self._live(generation):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError('RESIDENT_TLS_TIMEOUT')
            try:
                tls.do_handshake()
                tls.settimeout(2.0)
                return
            except ssl.SSLWantReadError:
                select.select([tls], [], [], min(.1, remaining))
            except ssl.SSLWantWriteError:
                select.select([], [tls], [], min(.1, remaining))
        raise Refused('VOICE_CANCELLED')

    def _handle_connection(self, raw):
        tls = credentials = coordinator = controller = dev_key = utterance = None
        with self.lock:
            if self._active_socket is not None:
                self._generation += 1
            prev_sock, prev_ctrl = self._active_socket, self._active_controller
            generation = self._generation
            self._active_socket = raw
            self._active_controller = None
        if prev_sock is not None:
            self._disconnect(prev_sock)
        if prev_ctrl is not None:
            try: prev_ctrl.cancel()
            except Exception: pass
        try:
            if not self._live(generation): return
            credentials = (self.credentials_factory or self._credentials_cls)()
            record, identity = self.enrollment.current(), self.identity_loader()
            tv_pin, server_pin = record['tvCertificateSha256'], record['sparkCertificateSha256']
            if (any(type(v) is not str or not re.fullmatch(r'[a-f0-9]{64}', v) for v in (tv_pin, server_pin)) or
                    not hmac.compare_digest(server_pin, identity.sha256)):
                self._audit_event('refused', 'SPARK_PIN_MISMATCH'); return
            context = credentials.context(identity.certificate_pem, identity.key_pem, record['tvCertificate'])
            if (not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED or
                    context.minimum_version != ssl.TLSVersion.TLSv1_3 or context.maximum_version != ssl.TLSVersion.TLSv1_3):
                raise ValueError('RESIDENT_TLS')
            if not self._live(generation): return
            # Transfer socket ownership atomically, then handshake outside lock so
            # close/yield can interrupt it instead of leaving an untracked raw FD.
            with self.lock:
                if generation != self._generation or self.stopping.is_set(): return
                raw.settimeout(3.0)
                tls = context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
                self._active_socket = tls
                raw = None
            self._handshake(tls, generation)
            cert = tls.getpeercert(True)
            if (tls.version() != 'TLSv1.3' or not cert or
                    not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), tv_pin)):
                self._audit_event('refused', 'TV_PIN_MISMATCH'); return
            if not self._live(generation): return
            self.hook.note_tv_activity()
            utterance = self._status('begin_utterance')
            coordinator, asr = self._coordinator, self._asr
            if asr.qualified is not True:
                state = getattr(self.hook, 'state', None)
                code = 'VOICE_WARMING_UP' if state in ('warming', 'unloaded') else 'VOICE_UNAVAILABLE'
                self._status('outcome', utterance, 'refused', code)
                self._refuse_after_challenge(tls, code); return
            controller = _UtteranceController(self._controller, coordinator, lambda: self._live(generation),
                                              self.voice_status, utterance)
            with self.lock: self._active_controller = controller
            if not self._live(generation): return
            dev_key = resident_device_key(self.voice_key, tv_pin)
            ingress = VoiceIngress(controller, Device(tv_pin, bytes(dev_key)), asr, clock=self.clock)
            outcome = ingress.connection(tls)
            if self._live(generation):
                # Record first: a receipt read right after the frame sees this outcome.
                self._status('outcome', utterance, outcome.state, outcome.code)
                self._send_outcome(tls, outcome.state, outcome.code)
        except Exception:
            self._audit_event('error', 'RESIDENT_CONNECTION_FAILED')
            if utterance is not None and self._live(generation):
                self._status('failed', utterance)
        finally:
            clean = True
            if controller is not None:
                try: controller.cancel()
                except Exception: clean = False
            if coordinator is not None:
                try: clean = coordinator.release() is True and clean
                except Exception: clean = False
            if dev_key is not None: dev_key[:] = b'\0' * len(dev_key)
            self._disconnect(tls); self._disconnect(raw)
            if credentials is not None:
                try: clean = credentials.close() is True and clean
                except Exception: clean = False
            with self.lock:
                if self._active_socket is tls or self._active_socket is raw:
                    self._active_socket = None
                if self._active_controller is controller:
                    self._active_controller = None
                self.cleanup_complete = self.cleanup_complete and clean
            if not clean:
                self.stopping.set()  # Never serve another utterance over unknown ownership.
