"""Unit tests for ResidentUtteranceServer and resident_device_key."""
import hashlib
import unittest

from voice.backend.resident_utterance import RESIDENT_DOMAIN, ResidentUtteranceServer, resident_device_key


class _StubSessions:
    def __init__(self, active=False):
        self.active = active
        self.launched = []

    def has_per_attempt_session(self):
        return self.active

    def publish_resident_launch(self, attempt, ttl_s=15.0, *, force=False):
        self.launched.append((attempt, ttl_s, force))


class _ArmController:
    def __init__(self, *codes):
        self.codes, self.arms, self.cancels = list(codes), 0, []

    def arm(self):
        from voice.backend.controller import Refused
        self.arms += 1
        if self.codes:
            raise Refused(self.codes.pop(0))
        return 'lease'

    def cancel(self, lease=None):
        self.cancels.append(lease)


class _Coordinator:
    def acquire(self):
        return None


class UtteranceControllerTests(unittest.TestCase):
    # The real AgentController raises the backend Refused, whose code is its message.
    def test_busy_controller_lease_is_cancelled_and_rearmed(self):
        from voice.backend.resident_utterance import _UtteranceController
        controller = _ArmController('CONTROLLER_BUSY')
        self.assertEqual(_UtteranceController(controller, _Coordinator(), lambda: True).arm(), 'lease')
        self.assertEqual(controller.arms, 2)
        self.assertEqual(controller.cancels, [None])

    def test_other_arm_refusals_propagate_with_their_code(self):
        from voice.backend.controller import Refused
        from voice.backend.resident_utterance import _UtteranceController
        controller = _ArmController('AGENT_PAUSED')
        with self.assertRaises(Refused) as caught:
            _UtteranceController(controller, _Coordinator(), lambda: True).arm()
        self.assertEqual(str(caught.exception), 'AGENT_PAUSED')
        self.assertEqual((controller.arms, controller.cancels), (1, []))


class ResidentUtteranceTests(unittest.TestCase):
    def test_resident_device_key_matches_tv_derivation(self):
        voice_key = '11' * 32
        tv_cert = '22' * 32
        expected = hashlib.sha256(
            RESIDENT_DOMAIN + bytes.fromhex(voice_key) + bytes.fromhex(tv_cert)
        ).digest()
        derived = resident_device_key(voice_key, tv_cert)
        self.assertEqual(bytes(derived), expected)
        self.assertEqual(len(derived), 32)

    def test_yield_port_and_close_lifecycle(self):
        clock_val = [100.0]
        sessions = _StubSessions(active=False)
        srv = ResidentUtteranceServer(
            spark_ip='127.0.0.1',
            tv_ip='127.0.0.1',
            pcm_port=0,
            voice_key=None,
            enrollment=None,
            identity_loader=lambda: None,
            hook=None,
            sessions=sessions,
            clock=lambda: clock_val[0],
        )
        self.assertFalse(srv.start())
        self.assertFalse(srv._has_per_attempt_session())
        srv.yield_port(10.0)
        self.assertTrue(srv._has_per_attempt_session())
        clock_val[0] = 111.0
        self.assertFalse(srv._has_per_attempt_session())



# These tests use newly generated disposable certificates, socketpair/loopback,
# real TLS/VoiceIngress/pool providers, and scalar-only fake model worker calls.
import contextlib
import hmac
import json
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch, Mock
from voice.backend import resident_utterance as ru
from voice.backend.controller import Controller, BrowserObservation
from voice.backend.ingress import DOMAIN, tls_context
from voice.runtime.pool import WorkerPool, create_warm_hook


class Worker:
    qualified = True
    def __init__(self):
        self.created = time.monotonic()
        self.counter = 0
        self.process = types.SimpleNamespace(poll=lambda: None)
        self.sealed = {'synthetic': True}
        self.calls = []
        self.after_asr = None
        self.audio_reference = None
    def call(self, kind, value, *, timeout_s):
        self.counter += 1
        self.calls.append(kind)
        if kind == 'asr':
            assert type(value) is bytearray and len(value) == 640
            self.audio_reference = value
            if self.after_asr: self.after_asr()
            return 'open the test site'
        return '{"action":"browser_open","destination":"youtube"}'
    def close(self): self.process = None; return True


class Runtime:
    browser_enabled = True
    def __init__(self): self.calls = []; self.url = None
    def launch_browser(self, token, *, timeout_s): self.calls.append('launch'); return 'accepted'
    def browser_observation(self, *, timeout_s):
        return BrowserObservation('a' * 32, True, True, False, self.url)
    def navigate(self, session, url, token, *, timeout_s):
        self.calls.append('navigate'); self.url = url; return 'accepted'


class ResidentProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='r2d2-resident-tls-')
        cls.root = Path(cls.temporary.name)
        for name, use in [('server', 'serverAuth'), ('client', 'clientAuth'), ('foreign', 'clientAuth')]:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'extendedKeyUsage=' + use,
                            '-keyout', str(cls.root / (name + '.key')), '-out', str(cls.root / (name + '.crt'))],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=True)
    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def fixture(self, *, cold=False, pin=None):
        self.worker = Worker()
        self.pool = WorkerPool(lambda: self.worker)
        if not cold: self.pool.active = self.worker
        self.hook = create_warm_hook(self.pool)
        self.hook.note_tv_activity()
        if not cold:
            # Actual state policy admits a ready pool; no warm process started.
            self.hook.controller.state = 'ready'
        self.runtime = Runtime()
        self.sessions = _StubSessions()
        cert = (self.root/'client.crt').read_text()
        client_pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest()
        server_cert = (self.root/'server.crt').read_text()
        server_pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(server_cert)).hexdigest()
        context = tls_context(self.root/'server.crt', self.root/'server.key', self.root/'client.crt')
        self.credentials = types.SimpleNamespace(context=lambda *args: context, close=Mock(return_value=True))
        self.server = ResidentUtteranceServer(
            spark_ip='127.0.0.1', tv_ip='127.0.0.1', pcm_port=0, voice_key='11'*32,
            enrollment=types.SimpleNamespace(current=lambda: {'sparkCertificateSha256':server_pin,
                'tvCertificateSha256':pin or client_pin,'tvCertificate':cert}),
            identity_loader=lambda: types.SimpleNamespace(sha256=server_pin,certificate_pem=server_cert,key_pem='synthetic-only'),
            hook=self.hook,sessions=self.sessions,controller_factory=lambda intent: Controller(intent,self.runtime),
            credentials_factory=lambda:self.credentials)
        self.hook.note_tv_activity=Mock(wraps=self.hook.note_tv_activity)
        self.client_pin = client_pin
        return self.server

    def client_context(self, *, cert='client', tls12=False):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2 if tls12 else ssl.TLSVersion.TLSv1_3
        ctx.load_verify_locations(cafile=self.root/'server.crt')
        if cert: ctx.load_cert_chain(self.root/(cert+'.crt'), self.root/(cert+'.key'))
        return ctx

    @staticmethod
    def read(sock, count):
        data = b''
        while len(data) < count:
            part = sock.recv(count-len(data))
            if not part: raise EOFError()
            data += part
        return data

    def exchange(self, *, tag='valid', sequence=0, end=0, cert='client', tls12=False, replay=None):
        left,right=socket.socketpair()
        thread=threading.Thread(target=self.server._handle_connection,args=(left,))
        thread.start(); result=None; challenge=None
        try:
            right.settimeout(2)
            with self.client_context(cert=cert,tls12=tls12).wrap_socket(right,server_hostname='localhost') as client:
                challenge=self.read(client,32)
                key=resident_device_key('11'*32,self.client_pin)
                digest=hmac.digest(key,DOMAIN+(replay or challenge),'sha256').hex()
                if tag!='valid': digest='0'*64
                hello=json.dumps(dict(schema=1,mode='command',rate=16000,channels=1,format='s16le',tag=digest)).encode()
                client.sendall(struct.pack('>I',len(hello))+hello)
                if not getattr(self,'cold',False):
                    frame=struct.pack('>II',sequence,640)+b'\0'*640+struct.pack('>II',sequence+1,end)
                    client.sendall(frame)
                size=struct.unpack('>I',self.read(client,4))[0]
                self.assertLessEqual(size,512)
                result=json.loads(self.read(client,size))
        except (ssl.SSLError, OSError, EOFError):
            pass
        finally:
            right.close(); thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.server._busy)
        self.assertIsNone(self.pool.leased)
        self.assertEqual(self.hook.controller.status()['inflight'],0)
        self.assertTrue(self.server.cleanup_complete)
        return result,challenge

    def test_real_tls_hmac_asr_intent_action_pipeline_and_repeat(self):
        self.fixture()
        for _ in range(2):
            outcome,_=self.exchange()
            self.assertEqual(outcome,dict(schema=1,state='accepted',code='BROWSER_NAVIGATION_ACCEPTED'))
        self.assertEqual(self.worker.calls,['asr','intent','asr','intent'])
        self.assertEqual(self.runtime.calls,['launch','navigate','launch','navigate'])
        self.assertEqual(self.credentials.close.call_count,2)
        self.assertEqual(self.worker.audio_reference, bytearray())

    def test_hmac_replay_and_wrong_hmac_do_not_lease_or_call_models(self):
        self.fixture()
        _,challenge=self.exchange(tag='wrong')
        outcome,_=self.exchange(replay=challenge)
        self.assertEqual(outcome['code'],'DEVICE_NOT_AUTHENTICATED')
        self.assertEqual(self.worker.calls,[]);self.assertEqual(self.runtime.calls,[])

    def test_tls_pin_client_certificate_and_version_fail_closed(self):
        for settings in ({'cert':None},{'cert':'foreign'},{'tls12':True},{'pin':'0'*64}):
            with self.subTest(settings=settings):
                self.fixture(pin=settings.get('pin'))
                outcome,_=self.exchange(**{k:v for k,v in settings.items() if k!='pin'})
                self.assertIsNone(outcome);self.assertEqual(self.worker.calls,[])
                self.hook.note_tv_activity.assert_not_called()

    def test_bad_sequence_and_cancel_never_infer(self):
        for args,code in (({'sequence':1},'INPUT_SEQUENCE'),({'end':0xffffffff},'VOICE_CANCELLED')):
            with self.subTest(args=args):
                self.fixture();outcome,_=self.exchange(**args)
                self.assertEqual(outcome['code'],code);self.assertEqual(self.worker.calls,[])

    def test_cold_listener_returns_challenge_then_warming_without_loading_model(self):
        self.fixture(cold=True);self.cold=True
        outcome,_=self.exchange()
        self.assertEqual(outcome['code'],'VOICE_WARMING_UP')
        self.assertEqual(self.worker.calls,[])
        self.assertIsNone(self.pool._warm)

    def test_port_handoff_during_asr_revokes_action_and_releases_lease(self):
        self.fixture()
        self.worker.after_asr=lambda:self.server.yield_port(1)
        outcome,_=self.exchange()
        self.assertIsNone(outcome)
        self.assertEqual(self.worker.calls,['asr']);self.assertEqual(self.runtime.calls,[])

    def test_shutdown_during_asr_revokes_action_and_releases_lease(self):
        self.fixture()
        self.worker.after_asr=lambda:self.server.close(0)
        self.exchange()
        self.assertEqual(self.worker.calls,['asr']);self.assertEqual(self.runtime.calls,[])

    def test_close_interrupts_owned_pending_tls_handshake(self):
        self.fixture()
        left,right=socket.socketpair()
        t=threading.Thread(target=self.server._handle_connection,args=(left,));t.start()
        try:
            until=time.monotonic()+2
            while not isinstance(self.server._active_socket,ssl.SSLSocket) and time.monotonic()<until: time.sleep(.005)
            self.assertIsInstance(self.server._active_socket,ssl.SSLSocket)
            self.server.close(0);t.join(.5)
            self.assertFalse(t.is_alive())
            self.assertEqual(self.worker.calls,[])
        finally:
            right.close();t.join(4)

    def test_real_listener_yield_rebind_and_resume(self):
        self.fixture();self.server.start()
        try:
            until=time.monotonic()+2
            while self.server._listener is None and time.monotonic()<until: time.sleep(.005)
            self.assertIsNotNone(self.server._listener)
            port=self.server._listener.getsockname()[1]
            self.server.yield_port(.1)
            with socket.socket() as other:
                other.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                other.bind(('127.0.0.1',port));other.listen(1)
            until=time.monotonic()+2
            while self.server._listener is None and time.monotonic()<until: time.sleep(.005)
            self.assertIsNotNone(self.server._listener)
        finally:
            self.assertTrue(self.server.close())
        self.assertFalse(self.server.start())

    def test_bind_failure_closes_socket_and_close_before_start_is_complete(self):
        self.fixture()
        owned=Mock();owned.bind.side_effect=OSError('synthetic')
        with patch.object(ru.socket,'socket',return_value=owned):
            with self.assertRaises(OSError): self.server._ensure_listener()
        owned.close.assert_called_once()
        self.assertTrue(self.server.close())
        self.assertFalse(self.server.start())

    def test_credential_constructor_failure_closes_raw_socket(self):
        self.fixture();left,right=socket.socketpair()
        try:
            self.server.credentials_factory=Mock(side_effect=ValueError('synthetic'))
            self.server._handle_connection(left)
            self.assertEqual(left.fileno(),-1)
        finally:right.close()

    def test_shared_controller_and_providers_survive_connections(self):
        self.fixture()
        controller=self.server._controller;coordinator=self.server._coordinator
        self.server.controller_factory=Mock(side_effect=AssertionError('must not recreate agent'))
        for _ in range(2):
            outcome,_=self.exchange()
            self.assertEqual(outcome['code'],'BROWSER_NAVIGATION_ACCEPTED')
            self.assertIs(self.server._controller,controller)
            self.assertIs(self.server._coordinator,coordinator)
        self.server.controller_factory.assert_not_called()

    def test_session_reservation_after_capture_blocks_action(self):
        self.fixture()
        self.worker.after_asr=lambda:setattr(self.sessions,'active',True)
        self.exchange()
        self.assertEqual(self.worker.calls,['asr']);self.assertEqual(self.runtime.calls,[])

    def test_unverified_cleanup_stops_listener_readmission(self):
        self.fixture()
        self.credentials.close.return_value=False
        left,right=socket.socketpair()
        right.close()  # TLS refuses; independent credential close still runs.
        self.server._handle_connection(left)
        self.assertFalse(self.server.cleanup_complete)
        self.assertTrue(self.server.stopping.is_set())
        self.assertFalse(self.server.start())
        self.assertFalse(self.server.close())

    def test_thread_start_failure_leaves_no_phantom_owner(self):
        self.fixture()
        with patch.object(ru.threading.Thread,'start',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.server.start()
        self.assertIsNone(self.server._thread)
        self.assertTrue(self.server.close())

    def test_yield_and_close_reject_unbounded_or_nonfinite_durations(self):
        self.fixture()
        for value in (float('nan'),float('inf'),-1,True,61):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):self.server.yield_port(value)
                with self.assertRaises(ValueError):self.server.close(value)
        self.assertFalse(self.server.stopping.is_set())

    def test_dependency_loader_uses_only_present_packaged_sources(self):
        credentials,providers=ru._load_runtime_deps()
        self.assertEqual(credentials.__name__,'Credentials')
        self.assertEqual(providers.__name__,'pool_providers')

    def status_recorder(self, broken=False):
        events = []
        class Recorder:
            def __getattr__(_, name):
                def record(*args):
                    events.append((name,) + args)
                    if broken: raise RuntimeError('status')
                    return 7 if name == 'begin_utterance' else None
                return record
        self.server.voice_status = Recorder()
        send = self.server._send_outcome
        self.server._send_outcome = lambda tls, state, code: events.append(('send', state, code)) or send(tls, state, code)
        return events

    def test_status_feed_points_in_order_and_before_the_frame(self):
        self.fixture()
        events = self.status_recorder()
        outcome, _ = self.exchange()
        self.assertEqual(outcome['code'], 'BROWSER_NAVIGATION_ACCEPTED')
        self.assertEqual(events, [('begin_utterance',), ('armed', 7), ('thinking', 7),
                                  ('outcome', 7, 'accepted', 'BROWSER_NAVIGATION_ACCEPTED'),
                                  ('send', 'accepted', 'BROWSER_NAVIGATION_ACCEPTED')])
        self.assertNotIn('open the test site', repr(events))

    def test_status_records_warm_refusal_before_the_frame(self):
        self.fixture(cold=True); self.cold = True
        events = self.status_recorder()
        self.assertEqual(self.exchange()[0]['code'], 'VOICE_WARMING_UP')
        self.assertEqual(events, [('begin_utterance',), ('outcome', 7, 'refused', 'VOICE_WARMING_UP'),
                                  ('send', 'refused', 'VOICE_WARMING_UP')])

    def test_status_exception_path_and_broken_status_change_nothing(self):
        self.fixture()
        events = self.status_recorder()
        with patch.object(ru, 'resident_device_key', side_effect=ValueError('synthetic')):
            self.assertIsNone(self.exchange()[0])
        self.assertEqual(events, [('begin_utterance',), ('failed', 7)])
        self.fixture()
        events = self.status_recorder(broken=True)
        self.assertEqual(self.exchange()[0], dict(schema=1, state='accepted', code='BROWSER_NAVIGATION_ACCEPTED'))
        self.assertEqual([row[0] for row in events], ['begin_utterance', 'armed', 'thinking', 'outcome', 'send'])

    def test_real_voice_status_sees_the_sent_outcome(self):
        from voice.backend.voice_status import VoiceStatus
        self.fixture()
        self.server.voice_status = status = VoiceStatus()
        self.exchange()
        view = status.home_view()
        self.assertEqual((view['phase'], view['badge'], view['headline']), ('result', 'sent', 'Sent.'))

    def test_stale_controller_lease_is_cancelled_and_rearmed_on_new_utterance(self):
        self.fixture()
        stale_lease=self.server._controller.arm()
        self.assertIsNotNone(stale_lease)
        outcome,_=self.exchange()
        self.assertEqual(outcome['code'],'BROWSER_NAVIGATION_ACCEPTED')


if __name__ == '__main__': unittest.main()

