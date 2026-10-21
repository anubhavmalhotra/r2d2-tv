import contextlib
import hashlib
import hmac
import io
import json
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from browser_adapter import OwnedBrowserAdapter
from controller import Controller, Refused, Outcome, BrowserObservation
from ingress import (VoiceIngress, Device, DOMAIN, MAX_AUDIO, MAX_CHUNK, tls_context,
                     strict_json)
from model_worker import ModelWorker
from providers import NativeQwenASR, NativeQwenIntent

KEY = b'k' * 32
CHALLENGE = b'c' * 32


class Clock:
    def __init__(self): self.now = 100.
    def __call__(self): return self.now
    def wait(self, value): self.now += value


def hello(challenge=CHALLENGE, **extra):
    obj = {'schema': 1, 'mode': 'command', 'rate': 16000, 'channels': 1,
           'format': 's16le', 'tag': hmac.digest(KEY, DOMAIN + challenge, 'sha256').hex()}
    obj.update(extra)
    b = json.dumps(obj).encode()
    return struct.pack('>I', len(b)) + b


def audio(chunk=b'\0' * 640, sequence=0, end=True):
    result = struct.pack('>II', sequence, len(chunk)) + chunk
    return result + (struct.pack('>II', sequence + 1, 0) if end else b'')


class Socket:
    def __init__(self, body, fragment=17):
        self.body = bytearray(body)
        self.sent, self.read_max = b'', 0
        self.fragment = fragment
    def settimeout(self, _): pass
    def sendall(self, value): self.sent += value
    def recv(self, count):
        self.read_max = max(self.read_max, count)
        size = min(count, self.fragment, len(self.body))
        value = bytes(self.body[:size]); del self.body[:size]
        return value


class ASR:
    qualified = True
    def __init__(self): self.calls = 0; self.buffer = None
    def transcribe(self, value, *, rate, timeout_s):
        self.calls += 1
        self.buffer = value
        assert rate == 16000 and timeout_s == 8
        return 'open YouTube in browser'


class Model:
    def interpret(self, text, *, timeout_s):
        return '{"action":"browser_open","destination":"youtube"}'


class Manager:
    def __init__(self): self.state = 'ready'; self.starts = 0
    def status(self): return {'state': self.state}
    def start(self): self.starts += 1; self.state = 'starting'; return 202, {'state': 'starting'}


class Trial:
    def __init__(self): self.calls = []; self.url = None; self.accept = True
    def queue_navigation(self, body):
        self.calls.append(body)
        if self.accept: self.url = body['url']
        return self.accept


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.asr, self.manager, self.trial = Clock(), ASR(), Manager(), Trial()
        self.session = 'a' * 32
        self.owned = self.ready = self.presented = True
        def observation(**_):
            return BrowserObservation(self.session, self.owned, self.ready,
                                      self.presented, self.trial.url)
        self.adapter = OwnedBrowserAdapter(self.manager,
                                          lambda session, **_: (session, self.trial),
                                          observation, qualified=True)
        self.controller = Controller(Model(), self.adapter, clock=self.clock, wait=self.clock.wait)
        self.ingress = VoiceIngress(self.controller, Device('a' * 64, KEY), self.asr,
                                    clock=self.clock, challenge=lambda _: CHALLENGE)
        self.payload_logs = io.StringIO()

    def run_voice(self, body=None):
        sock = Socket(body if body is not None else hello() + audio())
        with contextlib.redirect_stdout(self.payload_logs), contextlib.redirect_stderr(self.payload_logs):
            result = self.ingress._verified_connection(sock)
        return result, sock

    def test_fixed_owned_browser_pipeline_and_ram_buffer_erased(self):
        result, sock = self.run_voice()
        self.assertEqual(result, Outcome('verified', 'BROWSER_TARGET_PRESENTED'))
        self.assertEqual(sock.sent, CHALLENGE)
        self.assertEqual(self.trial.calls, [{'url': 'https://www.youtube.com'}])
        self.assertEqual(self.asr.buffer, bytearray())
        self.assertIsNone(self.controller.active)
        self.assertEqual(self.payload_logs.getvalue(), '')

    def test_no_plaintext_socket_or_unqualified_provider(self):
        self.assertEqual(self.ingress.connection(Socket(b'')).code, 'TRANSPORT_UNQUALIFIED')
        self.ingress.asr.qualified = False
        result, sock = self.run_voice()
        self.assertEqual(result.code, 'ASR_UNQUALIFIED')
        self.assertEqual(sock.sent, b'')
        self.assertEqual(self.trial.calls, [])

    def test_challenge_replay_and_wrong_hmac_never_arm(self):
        for tag in ('0' * 64, hmac.digest(KEY, DOMAIN + b'x' * 32, 'sha256').hex()):
            result, _ = self.run_voice(hello(tag=tag) + audio())
            self.assertEqual(result.code, 'DEVICE_NOT_AUTHENTICATED')
        self.assertEqual(self.asr.calls, 0)
        self.assertIsNone(self.controller.active)

    def test_every_closed_format_field_and_unknown_keys_refused(self):
        for field, value in [('schema', True), ('mode', 'dictation'), ('rate', 48000),
                             ('rate', True), ('channels', 2), ('channels', True),
                             ('format', 'float32'), ('tag', 'X' * 64), ('shell', 'echo x')]:
            result, _ = self.run_voice(hello(**{field: value}) + audio())
            self.assertEqual(result.code, 'INPUT_SCHEMA', field)
        self.assertEqual(self.asr.calls, 0)

    def test_duplicate_handshake_keys_refused(self):
        b = hello()[4:-1] + b',"mode":"command"}'
        result, _ = self.run_voice(struct.pack('>I', len(b)) + b + audio())
        self.assertEqual(result.code, 'INPUT_SCHEMA')

    def test_strict_json_duplicate_nan_oversize_and_invalid_utf8(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'x' * 513):
            with self.assertRaises(Refused): strict_json(raw, 512)

    def test_bad_sequence_reordering_and_duplicate_audio_refused(self):
        for body in (audio(sequence=1), audio(end=False) + audio(sequence=0)):
            result, _ = self.run_voice(hello() + body)
            self.assertEqual(result.code, 'INPUT_SEQUENCE')
        self.assertEqual(self.asr.calls, 0)

    def test_oversized_audio_before_body_read_and_odd_sample_refused(self):
        for size in (MAX_CHUNK + 2, 1, 641):
            result, sock = self.run_voice(hello() + struct.pack('>II', 0, size))
            self.assertEqual(result.code, 'INPUT_SIZE')
            self.assertLessEqual(sock.read_max, 512)

    def test_aggregate_audio_bound_and_chunk_count(self):
        body = b''.join(struct.pack('>II', i, 3200) + b'\0' * 3200 for i in range(101))
        result, _ = self.run_voice(hello() + body)
        self.assertEqual(result.code, 'INPUT_SIZE')
        self.assertEqual(self.asr.calls, 0)

    def test_disconnect_cancel_and_short_release_erase_authority(self):
        for body, expected in ((audio(end=False), 'INPUT_DISCONNECTED'),
                               (struct.pack('>II', 0, 0xffffffff), 'VOICE_CANCELLED'),
                               (struct.pack('>II', 0, 0), 'AUDIO_EMPTY_OR_SHORT')):
            result, _ = self.run_voice(hello() + body)
            self.assertEqual(result.code, expected)
            self.assertIsNone(self.controller.active)
            self.assertEqual(self.trial.calls, [])

    def test_read_deadline_and_nonblocking_one_session(self):
        self.ingress.guard.acquire()
        self.assertEqual(self.run_voice()[0].code, 'VOICE_BUSY')
        self.ingress.guard.release()
        self.ingress.clock = lambda: 100 if self.controller.active is None else 120
        result, _ = self.run_voice()
        self.assertEqual(result.code, 'INPUT_EXPIRED')
        self.assertIsNone(self.controller.active)

    def test_asr_error_details_never_logged_or_returned(self):
        def fail(*args, **kw): raise RuntimeError('private speech payload')
        self.asr.transcribe = fail
        result, _ = self.run_voice()
        self.assertEqual(result.code, 'VOICE_UNAVAILABLE')
        self.assertNotIn('private', repr(result))
        self.assertEqual(self.payload_logs.getvalue(), '')

    def test_silent_audio_refuses_without_action_or_worker_fault(self):
        self.asr.transcribe = lambda value, **kw: ''
        result, _ = self.run_voice()
        self.assertEqual(result, Outcome('refused', 'AUDIO_EMPTY_OR_SHORT'))
        self.assertEqual(self.trial.calls, [])
        self.assertIsNone(self.controller.active)
        self.assertEqual(self.payload_logs.getvalue(), '')

    def test_arm_refusal_codes_reach_the_tv_but_unknown_codes_stay_generic(self):
        for code, expected in (('AGENT_PAUSED', 'AGENT_PAUSED'), ('RATE_LIMITED', 'RATE_LIMITED'),
                               ('VOICE_WARMING_UP', 'VOICE_WARMING_UP'), ('VOICE_BUSY', 'VOICE_BUSY'),
                               ('CONTROLLER_BUSY', 'CONTROLLER_BUSY'), ('PRIVATE_DETAIL', 'VOICE_UNAVAILABLE')):
            class Refusing:
                def arm(self): raise Refused(code)
                def cancel(self, lease=None): pass
                def execute(self, lease, transcript): raise AssertionError('refused arm executed')
            self.ingress.controller = Refusing()
            result, _ = self.run_voice()
            self.assertEqual(result, Outcome('refused', expected))
        self.assertEqual(self.asr.calls, 0)
        self.assertEqual(self.payload_logs.getvalue(), '')

    def test_model_generated_tools_cannot_dispatch(self):
        self.controller.model.interpret = lambda *args, **kw: '{"action":"shell","command":"rm -rf x"}'
        self.assertEqual(self.run_voice()[0].code, 'MODEL_SCHEMA')
        self.assertEqual(self.trial.calls, [])

    def test_unowned_or_unpresented_session_cannot_verify(self):
        self.owned = False
        self.assertEqual(self.run_voice()[0].code, 'BROWSER_NOT_READY')
        self.assertEqual(self.trial.calls, [])
        self.owned = True; self.presented = False
        self.assertEqual(self.run_voice()[0], Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))

    def test_browser_default_disabled_and_manager_launch(self):
        self.adapter.browser_enabled = False
        self.assertEqual(self.run_voice()[0].code, 'BROWSER_BRIDGE_UNQUALIFIED')
        self.assertEqual(self.manager.starts, 0)
        self.adapter.browser_enabled = True; self.manager.state = 'idle'
        self.assertEqual(self.adapter.launch_browser('b' * 32 + ':start', timeout_s=1), 'accepted')
        self.assertEqual(self.manager.starts, 1)

    def test_navigation_binding_change_and_replay_never_requeue(self):
        op = 'b' * 32 + ':navigate'
        self.assertEqual(self.adapter.navigate(self.session, 'https://www.youtube.com', op, timeout_s=1), 'accepted')
        self.assertEqual(self.adapter.navigate(self.session, 'https://www.youtube.com', op, timeout_s=1), 'rejected')
        self.assertEqual(len(self.trial.calls), 1)
        self.adapter.binding = lambda *a, **k: ('changed', self.trial)
        self.assertEqual(self.adapter.navigate(self.session, 'https://www.youtube.com', 'c' * 32 + ':navigate', timeout_s=1), 'rejected')

    def test_apple_title_capability_not_invented(self):
        self.controller.model.interpret = lambda *a, **k: '{"action":"tv_content_open","provider":"apple_tv","title":"Ted Lasso"}'
        self.assertEqual(self.run_voice()[0].code, 'TITLE_CATALOGUE_UNQUALIFIED')
        self.assertEqual(self.trial.calls, [])


class WorkerTests(unittest.TestCase):
    # Authored sealed-ready replies below are protocol fixtures only; no native
    # CUDA/filter/model privacy qualification follows from these fake children.
    def test_real_filled_pipe_nonreading_child_obeys_deadline_and_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / 'stub.py'
            stub.write_text('import os,json,struct,time\nimport sys\nisolation={"schema": 2, "attempt": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sealed": True, "policySha256": "9f38390e5ea6847aaa3ba3ff2263d32710c186d700b9f271d8cc287f9de436c4", "publicTensorMatched": True, "TSYNCZero": True, "noNewPrivs": 1, "seccompMode": 2, "filterCount": 2, "threadCount": 2, "descriptorCount": 4, "knownCudaRolesObserved": True, "persistentFileWritesDenied": True, "socketOperationsDenied": True, "resourceIdentitiesVerified": True, "privateASRQualified": False, "GPUOrBrokerPrivacyQualified": False, "filesystemPrivacyQualified": False, "forensicRetentionQualified": False, "LGBackendBlockedProved": False}\nisolation["attempt"]=sys.argv[1]\nb=json.dumps({"schema":2,"state":"ready","isolation":isolation}).encode()\nos.write(1,struct.pack(">I",len(b))+b)\ntime.sleep(30)\n')
            processes = []
            def spawn(argv, **kw):
                p = subprocess.Popen([sys.executable, '-B', str(stub), argv[-1]], **kw)
                processes.append(p); return p
            worker = ModelWorker(sys.executable, directory, directory, spawn=spawn)
            worker.start(timeout_s=2)
            before = time.monotonic()
            with self.assertRaises(Refused):
                worker.call('asr', bytearray(320000), timeout_s=.1)
            self.assertLess(time.monotonic() - before, 1.5)
            self.assertIsNone(worker.process)
            self.assertIsNotNone(processes[0].poll())

    def test_actual_owned_pipe_worker_contract_and_timeout_cleanup_without_models(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / 'stub.py'
            stub.write_text('''import os,json,struct,time\nimport sys\nisolation={"schema": 2, "attempt": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sealed": True, "policySha256": "9f38390e5ea6847aaa3ba3ff2263d32710c186d700b9f271d8cc287f9de436c4", "publicTensorMatched": True, "TSYNCZero": True, "noNewPrivs": 1, "seccompMode": 2, "filterCount": 2, "threadCount": 2, "descriptorCount": 4, "knownCudaRolesObserved": True, "persistentFileWritesDenied": True, "socketOperationsDenied": True, "resourceIdentitiesVerified": True, "privateASRQualified": False, "GPUOrBrokerPrivacyQualified": False, "filesystemPrivacyQualified": False, "forensicRetentionQualified": False, "LGBackendBlockedProved": False}\nisolation["attempt"]=sys.argv[1]\ndef read(n):\n b=b""\n while len(b)<n:\n  p=os.read(0,n-len(b))\n  if not p: raise SystemExit()\n  b+=p\n return b\ndef send(o):\n b=json.dumps(o).encode();os.write(1,struct.pack(">I",len(b))+b)\nsend({"schema":2,"state":"ready","isolation":isolation})\nwhile True:\n n=struct.unpack(">I",read(4))[0];q=json.loads(read(n))\n if q["action"]=="asr":read(q["bytes"]);s="open YouTube in browser"\n else:\n  if q["text"]=="stall":time.sleep(5)\n  s='{"action":"browser_open","destination":"youtube"}'\n send({"schema":1,"id":q["id"],"action":q["action"],"text":s})\n''')
            processes = []
            def spawn(argv, **kw):
                p = subprocess.Popen([sys.executable, '-B', str(stub), argv[-1]], **kw)
                processes.append(p); return p
            worker = ModelWorker(sys.executable, directory, directory, spawn=spawn)
            worker.start(timeout_s=3)
            self.assertEqual(worker.call('asr', bytearray(640), timeout_s=1), 'open YouTube in browser')
            self.assertIn('browser_open', worker.call('intent', 'open YouTube in browser', timeout_s=1))
            with self.assertRaises(Refused): worker.call('intent', 'stall', timeout_s=.1)
            self.assertIsNone(worker.process)
            self.assertIsNotNone(processes[0].poll())
            self.assertFalse(worker.qualified)

    def test_empty_asr_reply_keeps_the_worker_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / 'stub.py'
            stub.write_text('''import os,json,struct,time\nimport sys\nisolation={"schema": 2, "attempt": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sealed": True, "policySha256": "9f38390e5ea6847aaa3ba3ff2263d32710c186d700b9f271d8cc287f9de436c4", "publicTensorMatched": True, "TSYNCZero": True, "noNewPrivs": 1, "seccompMode": 2, "filterCount": 2, "threadCount": 2, "descriptorCount": 4, "knownCudaRolesObserved": True, "persistentFileWritesDenied": True, "socketOperationsDenied": True, "resourceIdentitiesVerified": True, "privateASRQualified": False, "GPUOrBrokerPrivacyQualified": False, "filesystemPrivacyQualified": False, "forensicRetentionQualified": False, "LGBackendBlockedProved": False}\nisolation["attempt"]=sys.argv[1]\ndef read(n):\n b=b""\n while len(b)<n:\n  p=os.read(0,n-len(b))\n  if not p: raise SystemExit()\n  b+=p\n return b\ndef send(o):\n b=json.dumps(o).encode();os.write(1,struct.pack(">I",len(b))+b)\nsend({"schema":2,"state":"ready","isolation":isolation})\nwhile True:\n n=struct.unpack(">I",read(4))[0];q=json.loads(read(n))\n read(q["bytes"]);s="" if q["id"]==1 else (" padded " if q["id"]==3 else "open YouTube in browser")\n send({"schema":1,"id":q["id"],"action":q["action"],"text":s})\n''')
            processes = []
            def spawn(argv, **kw):
                p = subprocess.Popen([sys.executable, '-B', str(stub), argv[-1]], **kw)
                processes.append(p); return p
            worker = ModelWorker(sys.executable, directory, directory, spawn=spawn)
            worker.start(timeout_s=3)
            self.assertEqual(worker.call('asr', bytearray(640), timeout_s=1), '')
            self.assertIsNotNone(worker.process)
            self.assertEqual(worker.call('asr', bytearray(640), timeout_s=1), 'open YouTube in browser')
            # Other out-of-bounds text is refused; an in-sync reply keeps the worker.
            with self.assertRaises(Refused): worker.call('asr', bytearray(640), timeout_s=1)
            self.assertIsNotNone(worker.process)
            self.assertIsNone(processes[0].poll())
            worker.close()

    def test_native_asr_silence_is_empty_and_outer_whitespace_is_trimmed(self):
        class Array:
            def astype(self, _): return self
            def __truediv__(self, _): return self
            def fill(self, _): pass
        class Values(dict):
            def to(self, *_): return self
        numpy = mock.Mock()
        numpy.frombuffer.return_value = Array()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {'numpy': numpy}):
            asr = NativeQwenASR(directory, torch_module=mock.MagicMock())
            asr.processor, asr.model = mock.Mock(), mock.MagicMock()
            asr.processor.apply_transcription_request.return_value = Values(input_ids=mock.Mock(shape=(1, 4)))
            for decoded, expected in (('', ''), ('   ', ''), ([' '], ''), (' open youtube\n', 'open youtube')):
                asr.processor.decode.return_value = decoded
                self.assertEqual(asr.transcribe(b'\0' * 640, rate=16000, timeout_s=8), expected)
            asr.processor.decode.return_value = 'open\x00youtube'
            with self.assertRaises(Refused):
                asr.transcribe(b'\0' * 640, rate=16000, timeout_s=8)

    def test_native_provider_load_is_explicit_and_closed_local_path(self):
        for bad in ('Qwen/Qwen3-ASR-0.6B-hf', 'https://example.invalid/model'):
            with self.assertRaises(Refused): NativeQwenASR(bad)
        with tempfile.TemporaryDirectory() as directory:
            torch = mock.Mock()
            torch.cuda.is_available.return_value = True
            processor, model = mock.Mock(), mock.Mock()
            asr = NativeQwenASR(directory, torch_module=torch, processor_class=processor, model_class=model)
            self.assertIsNone(asr.model)
            asr.load()
            self.assertFalse(asr.qualified)
            self.assertEqual(processor.from_pretrained.call_args.kwargs,
                             {'local_files_only': True, 'trust_remote_code': False})
            self.assertFalse(model.from_pretrained.call_args.kwargs['trust_remote_code'])
            self.assertTrue(model.from_pretrained.call_args.kwargs['local_files_only'])
            with self.assertRaises(Refused): asr.load()

    def test_native_intent_cannot_load_without_cuda_or_from_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            torch = mock.Mock(); torch.cuda.is_available.return_value = False
            intent = NativeQwenIntent(directory, torch_module=torch)
            with self.assertRaises(Refused): intent.load()
            link = Path(directory) / 'model'; link.symlink_to(directory)
            with self.assertRaises(Refused): NativeQwenIntent(link)


class TLSProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        for kind, use in [('server', 'serverAuth'), ('client', 'clientAuth')]:
            p = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                                '-nodes', '-days', '1', '-subj', '/CN=localhost',
                                '-addext', 'extendedKeyUsage=' + use,
                                '-keyout', str(cls.root / (kind + '.key')),
                                '-out', str(cls.root / (kind + '.crt'))],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
            if p.returncode:
                raise AssertionError('TLS_FIXTURE_CREATION_FAILED')

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def run_tls(self, *, pin=None, certificate=True, tls12=False):
        fixture = BackendTests('test_fixed_owned_browser_pipeline_and_ram_buffer_erased')
        fixture.setUp()
        der = ssl.PEM_cert_to_DER_cert((self.root / 'client.crt').read_text())
        fixture.ingress.device = Device(pin or hashlib.sha256(der).hexdigest(), KEY)
        server_context = tls_context(self.root / 'server.crt', self.root / 'server.key', self.root / 'client.crt')
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.load_verify_locations(cafile=self.root / 'server.crt')
        if certificate:
            client_context.load_cert_chain(self.root / 'client.crt', self.root / 'client.key')
        if tls12:
            client_context.minimum_version = client_context.maximum_version = ssl.TLSVersion.TLSv1_2
        left, right = socket.socketpair()
        left.settimeout(2); right.settimeout(2)
        result = []
        def serve():
            try:
                with server_context.wrap_socket(left, server_side=True) as tls:
                    result.append(fixture.ingress.connection(tls))
            except ssl.SSLError:
                result.append(Outcome('refused', 'TLS_HANDSHAKE_REJECTED'))
            finally:
                left.close()
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with client_context.wrap_socket(right, server_hostname='localhost') as tls:
                challenge = b''
                while len(challenge) < 32:
                    b = tls.recv(32 - len(challenge))
                    if not b: break
                    challenge += b
                if len(challenge) == 32:
                    tls.sendall(hello(challenge) + audio())
                    thread.join(3)
        except (ssl.SSLError, BrokenPipeError):
            pass
        finally:
            right.close()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        return result, fixture

    def test_real_tls13_mutual_certificate_and_challenge_pipeline(self):
        result, fixture = self.run_tls()
        self.assertEqual(result, [Outcome('verified', 'BROWSER_TARGET_PRESENTED')])
        self.assertEqual(fixture.asr.calls, 1)

    def test_real_tls_trusted_client_but_wrong_pin_cannot_capture(self):
        result, fixture = self.run_tls(pin='0' * 64)
        self.assertEqual(result, [Outcome('refused', 'DEVICE_NOT_AUTHENTICATED')])
        self.assertEqual(fixture.asr.calls, 0)

    def test_real_tls_missing_client_certificate_cannot_capture(self):
        result, fixture = self.run_tls(certificate=False)
        self.assertEqual(result, [Outcome('refused', 'TLS_HANDSHAKE_REJECTED')])
        self.assertEqual(fixture.asr.calls, 0)

    def test_real_tls12_downgrade_cannot_capture(self):
        result, fixture = self.run_tls(tls12=True)
        self.assertEqual(result, [Outcome('refused', 'TLS_HANDSHAKE_REJECTED')])
        self.assertEqual(fixture.asr.calls, 0)


if __name__ == '__main__': unittest.main()
