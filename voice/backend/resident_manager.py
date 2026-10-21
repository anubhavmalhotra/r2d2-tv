"""Resident voice assembly for an existing browser Manager; inert until start.

The host owns enrollment, identity and WarmHook. Construct this helper before
hook.start(): its one AgentController configures the pool's P5 prompt before any
model warms. start() owns only the authenticated power reader and PCM listener.
No pairing, credential writes, model startup, or browser launch occurs here on
construction. Launch acceptance never proves a displayed frame.
"""
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

if __package__:
    from .agent_controller import AgentController, InMemoryKillSwitch, _load_agent_package
    from .channel_adapter import ChannelBrowserAdapter
    from .resident_utterance import ResidentUtteranceServer
    from .tv_transport import TvTransport
    from .viewer_handoff import TVViewerHandoff
    from .voice_status import StatusFeedback, VoiceStatus
else:
    from agent_controller import AgentController, InMemoryKillSwitch, _load_agent_package
    from channel_adapter import ChannelBrowserAdapter
    from resident_utterance import ResidentUtteranceServer
    from tv_transport import TvTransport
    from viewer_handoff import TVViewerHandoff
    from voice_status import StatusFeedback, VoiceStatus


def _power_reader_class():
    if __package__ == 'voice_control':
        from .pool import AuthenticatedTvPowerReader
    elif not __package__:
        from pool import AuthenticatedTvPowerReader
    else:
        from voice.runtime.pool import AuthenticatedTvPowerReader
    return AuthenticatedTvPowerReader


def _private_scalar(path, field, pattern):
    """Read one held, bounded owner-only JSON file without FIFO/symlink waits."""
    path = Path(path)
    if path.resolve() != path.absolute():
        raise ValueError('TV_CREDENTIAL_INVALID')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    raw = bytearray()
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_nlink != 1
                or not 0 < before.st_size <= 4096):
            raise ValueError('TV_CREDENTIAL_INVALID')
        while len(raw) <= 4096:
            chunk = os.read(fd, 4097 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        def identity(st):
            return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
                    st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if (len(raw) != before.st_size or identity(before) != identity(os.fstat(fd))
                or identity(before) != identity(os.lstat(path))):
            raise ValueError('TV_CREDENTIAL_INVALID')
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError('TV_CREDENTIAL_INVALID')
                out[key] = value
            return out
        value = json.loads(raw, object_pairs_hook=unique)
        if (type(value) is not dict or set(value) != {field}
                or type(value[field]) is not str or not re.fullmatch(pattern, value[field])):
            raise ValueError('TV_CREDENTIAL_INVALID')
        return value[field]
    finally:
        raw[:] = b'\0' * len(raw)
        os.close(fd)


class ResidentManager:
    """One persistent P5 controller, separate pinned SSAP owners, shared Manager.

    transport_factory(address) must construct an inert transport implementing
    TvTransport's bounded connect/register/request/close contract. The actual
    saved-key registration payload must echo the exact existing client key.
    The helper never closes the caller's Manager, WarmHook or model pool.
    """
    def __init__(self, *, manager, spark_ip, tv_ip, manager_key, ssap_key_path,
                 ssap_pin_path, hook, enrollment, identity_loader, voice_key,
                 pcm_port=4182, sessions=None, transport_factory=TvTransport,
                 clock=time.monotonic, audit=None, voice_status=None):
        spawn = getattr(manager, 'control_spawn', None)
        source = getattr(hook, 'source', None)
        if (spawn is None or getattr(spawn, 'manager', None) is not manager
                or source is None or not hasattr(source, 'ssap')
                or not callable(getattr(source, 'note_power', None))
                or not callable(transport_factory) or not callable(clock)
                or voice_key is None):
            raise ValueError('RESIDENT_MANAGER_CONFIG')
        self.manager, self.hook, self.clock = manager, hook, clock
        self.tv_ip, self.key_path, self.pin_path = tv_ip, ssap_key_path, ssap_pin_path
        self.transport_factory = transport_factory
        self.lock = threading.RLock()
        self._settled = threading.Condition(self.lock)
        self.closed = self.started = False
        self._auth_refused = False
        self.cleanup_complete = True
        self._active = {}
        self.controller = self._intent = None
        self.killswitch = InMemoryKillSwitch()
        # Before ResidentUtteranceServer: its constructor builds the controller.
        self.voice_status = voice_status if voice_status is not None else VoiceStatus(clock=clock)
        self.viewer = TVViewerHandoff(spark_ip, manager_key, self.launch, qualified=True)
        self.runtime = ChannelBrowserAdapter(manager, spawn, viewer=self.viewer, qualified=True)
        tv_module = importlib.import_module(_load_agent_package()[5].__module__)
        self._certificate_changed = tv_module.CertificateChanged
        self._gateway_transport = transport_factory(tv_ip)
        try:
            self.gateway = tv_module.TVGateway(
                self._gateway_transport, key_path=ssap_key_path, pin_path=ssap_pin_path,
                killswitch=self.killswitch, phase='P5', clock=clock, spark_ip=spark_ip)
            self.resident = ResidentUtteranceServer(
                spark_ip=spark_ip, tv_ip=tv_ip, pcm_port=pcm_port, voice_key=voice_key,
                enrollment=enrollment, identity_loader=identity_loader, hook=hook,
                sessions=sessions, controller_factory=self.controller_factory,
                clock=clock, audit=audit, voice_status=self.voice_status)
            reader_class = _power_reader_class()
            self.power_reader = reader_class(
                clock=clock, read_fn=self.read_power, poll_interval_s=5,
                timeout_s=3, auto_poll=False)
        except BaseException:
            self._gateway_transport.close()
            raise
        self._source, self._previous_reader = source, source.ssap
        source.ssap = self.power_reader

    def controller_factory(self, intent):
        with self.lock:
            if self.closed:
                raise ValueError('RESIDENT_MANAGER_CLOSED')
            if self.controller is None:
                # VoiceStatus replaces Spark's SSAP toasts; Home, the viewer and the
                # resident's own receipt toast render outcomes instead.
                self.controller = AgentController(
                    intent, self.runtime, phase='P5', tv_gateway=self.gateway, clock=self.clock,
                    feedback=StatusFeedback(self.voice_status, phase='P5'), voice_status=self.voice_status)
                self._intent = intent
            elif intent is not self._intent:
                raise ValueError('RESIDENT_MODEL_CHANGED')
            return self.controller

    def _remaining(self, end, *, action=False):
        with self.lock:
            if self.closed or self._auth_refused or (action and (self.killswitch.paused() or self.killswitch.tv_off())):
                raise ValueError('RESIDENT_MANAGER_CLOSED')
        remaining = end - self.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError('TV_DEADLINE')
        return min(3.0, remaining)

    def _request(self, uri, payload, *, timeout_s, action=False):
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 5):
            raise ValueError('TV_DEADLINE')
        end = self.clock() + timeout_s
        key = pin = transport = None
        try:
            self._remaining(end, action=action)
            key = _private_scalar(self.key_path, 'client-key', r'[a-f0-9]{32,128}')
            pin = _private_scalar(self.pin_path, 'sha256', r'[a-f0-9]{64}')
            with self.lock:
                self._remaining(end, action=action)
                if len(self._active) >= 2:
                    raise ValueError('TV_BUSY')
                transport = self.transport_factory(self.tv_ip)
                self._active[transport] = threading.get_ident()
            transport.connect(pin_sha256=pin, timeout_s=self._remaining(end, action=action))
            reply = transport.register(client_key=key, timeout_s=self._remaining(end, action=action))
            self._remaining(end, action=action)
            if type(reply) is not dict or 'pairingType' in reply or reply.get('client-key') != key:
                with self.lock:
                    self._auth_refused = True
                self.killswitch.pause('TV_AUTH_FAILED')
                self.killswitch.set_tv_off('TV_AUTH_FAILED')
                raise ValueError('TV_AUTH_FAILED')
            reply = transport.request(uri, payload, timeout_s=self._remaining(end, action=action))
            self._remaining(end, action=action)
            return reply
        except self._certificate_changed:
            with self.lock:
                self._auth_refused = True
            self.killswitch.pause('CERT_CHANGED')
            self.killswitch.set_tv_off('CERT_CHANGED')
            raise
        finally:
            key = pin = None
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    self.cleanup_complete = False
                finally:
                    with self.lock:
                        self._active.pop(transport, None)
                        self._settled.notify_all()

    def read_power(self):
        # Reader projects only the closed power-state vocabulary. Each poll owns
        # a separate connection; it never shares the global mutation transport.
        return self._request(self.power_reader.POWER_URI, {}, timeout_s=3)

    def launch(self, payload, *, timeout_s):
        if (type(payload) is not dict or set(payload) != {'id', 'params'}
                or payload['id'] != 'com.webos.app.tinybrowser'
                or type(payload['params']) is not dict or set(payload['params']) != {'contentTarget'}):
            return {'returnValue': False}
        target = payload['params']['contentTarget']
        if (type(target) is not str or not (
                re.fullmatch(re.escape(self.viewer.open_url) + r'#voice-session=[a-f0-9]{32}', target)
                or self.viewer.viewer_pattern.fullmatch(target))):
            return {'returnValue': False}
        # Copy only the admitted fixed fields, not caller-owned mutable mappings.
        reply = self._request('ssap://system.launcher/launch', {
            'id': 'com.webos.app.tinybrowser', 'params': {'contentTarget': target}},
            timeout_s=timeout_s, action=True)
        return {'returnValue': reply['returnValue']} if (
            type(reply) is dict and type(reply.get('returnValue')) is bool) else {}

    def start(self):
        try:
            with self.lock:
                if self.closed or self.started:
                    return False
                if not self.power_reader.start() or not self.resident.start():
                    raise RuntimeError('RESIDENT_START_FAILED')
                self.started = True
                return True
        except BaseException:
            # Never join a reader while holding the state lock it needs to exit.
            self.close()
            raise

    def close(self, timeout_s=8):
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or not 0 <= timeout_s <= 30):
            raise ValueError('RESIDENT_CLOSE')
        end = time.monotonic() + timeout_s
        with self.lock:
            self.closed = True
            self.killswitch.pause('RESIDENT_CLOSED')
            self.killswitch.set_tv_off('RESIDENT_CLOSED')
            active = tuple(self._active)
        # Stop action authority before joining the listener, and interrupt any
        # separately owned SSAP operation so its bounded join can finish.
        for transport in (*active, self._gateway_transport):
            try:
                transport.close()
            except Exception:
                self.cleanup_complete = False
        for close in (self.resident.close, self.power_reader.close):
            try:
                if close(timeout_s=max(0, end - time.monotonic())) is not True:
                    self.cleanup_complete = False
            except Exception:
                self.cleanup_complete = False
        with self.lock:
            while self._active:
                remaining = end - time.monotonic()
                if threading.get_ident() in self._active.values() or remaining <= 0:
                    self.cleanup_complete = False
                    break
                self._settled.wait(remaining)
            if self._source.ssap is self.power_reader:
                self._source.ssap = self._previous_reader
        return self.cleanup_complete
