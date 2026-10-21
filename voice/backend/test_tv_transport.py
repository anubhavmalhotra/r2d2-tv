"""Real TLS/WebSocket tests over owned socketpairs; never contacts a TV.

The injected dial asserts the documentation address and fixed SSAP port. Each
class run creates one disposable, non-authorizing certificate using OpenSSL.
"""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

if __package__:
    from . import tv_transport as T
    from .agent_controller import InMemoryKillSwitch
else:
    import tv_transport as T
    from agent_controller import InMemoryKillSwitch

# Same class namespace used by the actual transport, whether source or build.
TVGateway = sys.modules[T.CertificateChanged.__module__].TVGateway
HOME_ID = sys.modules[T.CertificateChanged.__module__].HOME_ID
KEY = 'ab' * 32
IP = '192.0.2.40'


def exact(sock, size):
    out = bytearray()
    while len(out) < size:
        part = sock.recv(size-len(out))
        if not part: raise EOFError()
        out.extend(part)
    return bytes(out)


def frame(body, opcode=1, final=True):
    if isinstance(body, dict): body = json.dumps(body, separators=(',', ':')).encode()
    n = len(body)
    prefix = bytes(((128 if final else 0) | opcode,))
    if n < 126: return prefix + bytes((n,)) + body
    if n < 65536: return prefix + b'\x7e' + struct.pack('!H', n) + body
    return prefix + b'\x7f' + struct.pack('!Q', n) + body


def client_frame(sock):
    a, b = exact(sock, 2)
    assert a & 128 and b & 128, 'client frames must be final and masked'
    n = b & 127
    if n == 126: n = struct.unpack('!H', exact(sock, 2))[0]
    if n == 127: n = struct.unpack('!Q', exact(sock, 8))[0]
    assert n <= T.LIMIT
    mask = exact(sock, 4)
    body = bytes(v ^ mask[i % 4] for i, v in enumerate(exact(sock, n)))
    return a & 15, body


def message(sock):
    opcode, body = client_frame(sock)
    assert opcode == 1
    return json.loads(body)


def reply(sock, request, payload, kind='response'):
    sock.sendall(frame({'id': request['id'], 'type': kind, 'payload': payload}))


class Peer:
    def __init__(self, context, handler, *, upgrade=None):
        self.left, self.right = socket.socketpair()
        self.context, self.handler, self.upgrade = context, handler, upgrade
        self.http = None
        self.error = None
        self.server = None
        self.dialled = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def dial(self, address, *, timeout):
        assert address == (IP, 3001)
        assert 0 < timeout <= 5
        assert not self.dialled
        self.dialled.append(address)
        return self.left

    def run(self):
        try:
            self.right.settimeout(2)
            with self.context.wrap_socket(self.right, server_side=True) as server:
                self.server = server
                header = bytearray()
                while not header.endswith(b'\r\n\r\n'):
                    part = server.recv(1)
                    if not part: return  # pin refusal: zero application bytes
                    header.extend(part)
                    assert len(header) <= 8192
                self.http = bytes(header)
                fields = dict(line.split(b': ', 1) for line in bytes(header).split(b'\r\n')[1:] if line)
                accept = base64.b64encode(hashlib.sha1(fields[b'Sec-WebSocket-Key'] +
                    b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                response = (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                            b'Connection: keep-alive,Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                server.sendall(self.upgrade(response) if self.upgrade else response)
                if self.handler: self.handler(server)
        except (EOFError, BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
            pass  # the client can refuse malformed/prompted input at any point
        except BaseException as error:
            self.error = error
        finally:
            self.right.close()
            self.done.set()

    def close(self):
        self.left.close()
        self.thread.join(3)
        if self.thread.is_alive():
            if self.server:
                with contextlib.suppress(OSError): self.server.shutdown(socket.SHUT_RDWR)
            self.right.close()
            self.thread.join(1)
        assert not self.thread.is_alive(), 'owned test peer not joined'
        if self.error: raise self.error


class TvTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which('openssl')
        if not openssl: raise unittest.SkipTest('local OpenSSL required for disposable TLS fixture')
        cls.tmp = tempfile.TemporaryDirectory(prefix='tv-transport-test-')
        directory = Path(cls.tmp.name)
        key, cert = directory/'key.pem', directory/'cert.pem'
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                        'ec_paramgen_curve:prime256v1', '-nodes', '-days', '1',
                        '-subj', '/CN=non-authorizing-local-fixture', '-keyout', str(key),
                        '-out', str(cert)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cert, key)
        cls.pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    @contextlib.contextmanager
    def connected(self, handler, *, upgrade=None):
        peer = Peer(self.context, handler, upgrade=upgrade)
        transport = T.TvTransport(IP, connect_socket=peer.dial)
        try:
            yield transport, peer
        finally:
            transport.close()
            self.assertIsNone(transport.sock)
            self.assertFalse(transport.registered)
            self.assertEqual(transport.buffer, bytearray())
            peer.close()

    def register(self, t):
        t.connect(pin_sha256=self.pin)
        self.assertEqual(t.register(client_key=KEY), {'client-key': KEY})

    @staticmethod
    def accept_registration(sock):
        registration = message(sock)
        assert registration['type'] == 'register'
        assert registration['payload'] == {'client-key': KEY}
        assert set(registration) == {'id', 'type', 'payload'}
        reply(sock, registration, {'client-key': KEY}, 'registered')

    def test_import_package_is_inert(self):
        root = Path(__file__).resolve().parents[2]
        source = ('import socket, subprocess; '
                  'socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError("dial")); '
                  'subprocess.Popen=lambda *a,**k: (_ for _ in ()).throw(AssertionError("spawn")); '
                  'import voice.backend.tv_transport')
        subprocess.run([sys.executable, '-B', '-c', source], cwd=root, check=True, timeout=5)

    def test_validation_before_dial(self):
        dial = lambda *a, **k: self.fail('must not dial')
        for address in ('tv.local', '8.8.8.8', '127.0.0.1', '::1', '192.0.02.40'):
            with self.subTest(address=address), self.assertRaises(ValueError): T.TvTransport(address)
        t = T.TvTransport(IP, connect_socket=dial)
        for pin in (None, '', 'g'*64, 'A'*64, 'a'*64+'\n'):
            with self.subTest(pin=pin), self.assertRaises(ValueError): t.connect(pin_sha256=pin)
        for timeout in (False, 0, -1, 5.1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                t.connect(pin_sha256=self.pin, timeout_s=timeout)
        for key in (None, '', 'z'*64, 'a'*64+'\n'):
            with self.subTest(key=key), self.assertRaises(ValueError): t.register(client_key=key)

    def test_wrong_pin_sends_no_http_or_saved_key(self):
        with self.connected(None) as (t, p):
            wrong = ('0' if self.pin[0] != '0' else '1') + self.pin[1:]
            with self.assertRaises(T.CertificateChanged): t.connect(pin_sha256=wrong)
            self.assertTrue(p.done.wait(1))
            self.assertIsNone(p.http)
            self.assertIsNone(t.sock)

    def test_saved_registration_and_actual_gateway(self):
        def serve(sock):
            self.accept_registration(sock)
            req = message(sock)
            self.assertEqual(req['uri'], 'ssap://com.webos.applicationManager/getForegroundAppInfo')
            reply(sock, req, {'returnValue': True, 'appId': HOME_ID})
        with self.connected(serve) as (t, p), tempfile.TemporaryDirectory() as d:
            key, pin = Path(d).resolve()/'key', Path(d).resolve()/'pin'
            key.write_text(json.dumps({'client-key': KEY})); key.chmod(0o600)
            pin.write_text(json.dumps({'sha256': self.pin})); pin.chmod(0o600)
            gw = TVGateway(t, key_path=key, pin_path=pin, killswitch=InMemoryKillSwitch(),
                           phase='P3', clock=time.monotonic)
            self.assertEqual(gw.foreground(), 'r2d2_home')
            self.assertTrue(gw.connected)
            self.assertTrue(t.registered)
            self.assertNotIn(KEY.encode(), p.http)
            self.assertEqual(json.loads(key.read_text()), {'client-key': KEY})

    def test_changed_key_is_returned_but_cannot_authorize_requests(self):
        def serve(sock): reply(sock, message(sock), {'client-key': 'cd'*32}, 'registered')
        with self.connected(serve) as (t, _):
            t.connect(pin_sha256=self.pin)
            self.assertEqual(t.register(client_key=KEY), {'client-key': 'cd'*32})
            self.assertFalse(t.registered)
            with self.assertRaisesRegex(ValueError, 'TV_REQUEST'): t.request(T.POWER_URI, {})

    def test_pairing_prompt_closes_without_pairing_attempt(self):
        def serve(sock):
            req = message(sock)
            self.assertEqual(req['payload'], {'client-key': KEY})
            reply(sock, req, {'pairingType': 'PROMPT'})
            self.assertEqual(sock.recv(1), b'')
        with self.connected(serve) as (t, _):
            t.connect(pin_sha256=self.pin)
            self.assertEqual(t.register(client_key=KEY), {'pairingType': 'PROMPT'})
            self.assertIsNone(t.sock)

    def test_fragmented_response_ping_and_unrelated_reply(self):
        def serve(sock):
            self.accept_registration(sock); req = message(sock)
            sock.sendall(frame({'id': 'unrelated', 'type': 'response', 'payload': {}}))
            body = json.dumps({'id': req['id'], 'type': 'response',
                               'payload': {'returnValue': True, 'state': 'Active'}}).encode()
            sock.sendall(frame(body[:20], final=False) + frame(b'probe', opcode=9))
            self.assertEqual(client_frame(sock), (10, b'probe'))
            sock.sendall(frame(body[20:], opcode=0))
        with self.connected(serve) as (t, _):
            self.register(t)
            self.assertEqual(t.request(T.POWER_URI, {}), {'returnValue': True, 'state': 'Active'})

    def test_large_bounded_reply(self):
        def serve(sock):
            self.accept_registration(sock); req = message(sock)
            reply(sock, req, {'value': 'x'*65536})
        with self.connected(serve) as (t, _):
            self.register(t)
            self.assertEqual(t.request(T.POWER_URI, {}), {'value': 'x'*65536})

    def test_request_allowlist_and_error_not_ack(self):
        def serve(sock):
            self.accept_registration(sock); req = message(sock)
            reply(sock, req, {'returnValue': True}, 'error')
        with self.connected(serve) as (t, _):
            self.register(t)
            for uri in ('ssap://system/turnOff', 'ssap://system.launcher/close', 'http://other'):
                with self.assertRaisesRegex(ValueError, 'TV_REQUEST'): t.request(uri, {})
            self.assertEqual(t.request(T.POWER_URI, {}), {})

    def test_bad_http_upgrade_closes(self):
        transforms = [
            lambda v: v.replace(b'Sec-WebSocket-Accept:', b'Sec-WebSocket-Wrong:'),
            lambda v: v.replace(b'\r\n\r\n', b'\r\nUpgrade: websocket\r\n\r\n'),
            lambda v: b'HTTP/1.1 101 Switching Protocols\r\nX: ' + b'x'*8192,
        ]
        for change in transforms:
            with self.subTest(change=change), self.connected(None, upgrade=change) as (t, _):
                with self.assertRaises(ValueError): t.connect(pin_sha256=self.pin)
                self.assertIsNone(t.sock)

    def test_malformed_frame_refusals(self):
        cases = {
            'masked': b'\x81\x80', 'rsv': b'\xc1\x00', 'binary': frame(b'{}', opcode=2),
            'orphan-continuation': frame(b'{}', opcode=0),
            'second-text': frame(b'{', final=False) + frame(b'}'),
            'fragmented-ping': frame(b'x', opcode=9, final=False),
            'large-control': frame(b'x'*126, opcode=9),
            'nonminimal-short': b'\x81\x7e\x00\x02{}',
            'nonminimal-long': b'\x81\x7f' + struct.pack('!Q', 2) + b'{}',
            'too-large': b'\x81\x7f' + struct.pack('!Q', T.LIMIT+1),
            'aggregate': frame(b' '*T.LIMIT, final=False) + frame(b'x', opcode=0),
            'not-object': frame(b'[]'), 'bad-json': frame(b'{'), 'bad-utf8': frame(b'\xff'),
        }
        for name, data in cases.items():
            def serve(sock):
                self.accept_registration(sock); message(sock); sock.sendall(data)
            with self.subTest(name=name), self.connected(serve) as (t, _):
                self.register(t)
                with self.assertRaises(ValueError): t.request(T.POWER_URI, {})
                self.assertIsNone(t.sock)

    def test_control_frame_budget(self):
        def serve(sock):
            self.accept_registration(sock); message(sock)
            sock.sendall(frame(b'', opcode=10)*64)
        with self.connected(serve) as (t, _):
            self.register(t)
            with self.assertRaisesRegex(ValueError, 'TV_FRAME_BUDGET'): t.request(T.POWER_URI, {})

    def test_unrelated_response_budget(self):
        def serve(sock):
            self.accept_registration(sock); message(sock)
            sock.sendall(frame({'id': 'other'})*32)
        with self.connected(serve) as (t, _):
            self.register(t)
            with self.assertRaisesRegex(ValueError, 'TV_RESPONSE_BUDGET'): t.request(T.POWER_URI, {})

    def test_timeout_closes_owned_tls(self):
        def serve(sock):
            self.accept_registration(sock); message(sock)
            self.assertEqual(sock.recv(1), b'')
        with self.connected(serve) as (t, _):
            self.register(t)
            began = time.monotonic()
            with self.assertRaises(TimeoutError): t.request(T.POWER_URI, {}, timeout_s=.05)
            self.assertLess(time.monotonic()-began, 1)
            self.assertIsNone(t.sock)

    def test_close_interrupts_pending_response(self):
        received = threading.Event()
        def serve(sock):
            self.accept_registration(sock); message(sock); received.set()
            self.assertEqual(sock.recv(1), b'')
        with self.connected(serve) as (t, _):
            self.register(t)
            errors = []
            def call():
                try: t.request(T.POWER_URI, {}, timeout_s=5)
                except Exception as error: errors.append(error)
            worker = threading.Thread(target=call); worker.start()
            self.assertTrue(received.wait(1))
            t.close(); worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)

    def test_close_during_dial_cannot_publish_socket(self):
        entered, release = threading.Event(), threading.Event()
        left, right = socket.socketpair()
        def dial(*args, **kwargs): entered.set(); release.wait(1); return left
        t = T.TvTransport(IP, connect_socket=dial)
        errors = []
        def call():
            try: t.connect(pin_sha256=self.pin)
            except Exception as error: errors.append(error)
        worker = threading.Thread(target=call); worker.start()
        try:
            self.assertTrue(entered.wait(1)); t.close(); release.set(); worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertEqual(left.fileno(), -1)
            right.settimeout(.2)
            self.assertEqual(right.recv(1), b'')  # not even a late TLS ClientHello
            self.assertIsNone(t.sock)
        finally:
            release.set(); t.close(); left.close(); right.close(); worker.join(1)

    def test_close_interrupts_pending_handshake(self):
        left, right = socket.socketpair()
        t = T.TvTransport(IP, connect_socket=lambda *a, **k: left)
        errors = []
        def call():
            try: t.connect(pin_sha256=self.pin, timeout_s=5)
            except Exception as error: errors.append(error)
        worker = threading.Thread(target=call); worker.start()
        try:
            right.settimeout(1)
            self.assertTrue(right.recv(4096))  # real TLS hello, deliberately unanswered
            t.close(); worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsNone(t.sock)
        finally:
            t.close(); right.close(); worker.join(1)

    def test_late_registered_reply_cannot_restore_authority(self):
        def serve(sock): self.accept_registration(sock)
        with self.connected(serve) as (t, _):
            t.connect(pin_sha256=self.pin)
            original = t._receive
            def receive(end):
                value = original(end); t.close(); return value
            with patch.object(t, '_receive', receive), self.assertRaisesRegex(OSError, 'TV_CLOSED'):
                t.register(client_key=KEY)
            self.assertFalse(t.registered)

    def test_late_receive_does_not_retain_buffer_after_close(self):
        t = T.TvTransport(IP)
        class ClosingSocket:
            def settimeout(self, value): pass
            def shutdown(self, how): pass
            def close(self): pass
            def recv(self, count): t.close(); return b'late-response'
        t.sock = ClosingSocket()
        with self.assertRaisesRegex(OSError, 'TV_CLOSED'): t._read(2, time.monotonic()+1)
        self.assertEqual(t.buffer, bytearray())

    def test_request_waiting_for_lock_rechecks_registration(self):
        t = T.TvTransport(IP)
        attempted = threading.Event()
        calls, errors = [], []
        t.registered = True
        def call():
            attempted.set()
            try: t.request(T.POWER_URI, {})
            except Exception as error: errors.append(error)
        with patch.object(t, '_rpc', side_effect=lambda *args: calls.append(args)), t.lock:
            worker = threading.Thread(target=call); worker.start()
            self.assertTrue(attempted.wait(1))
            t.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, [])
        self.assertEqual([str(e) for e in errors], ['TV_REQUEST'])

    def test_connect_deadline_includes_dial(self):
        left, right = socket.socketpair()
        now = [1.0]
        def dial(*args, **kwargs): now[0] = 2.0; return left
        t = T.TvTransport(IP, connect_socket=dial, clock=lambda: now[0])
        try:
            with self.assertRaisesRegex(TimeoutError, 'TV_TIMEOUT'):
                t.connect(pin_sha256=self.pin, timeout_s=1)
            self.assertEqual(left.fileno(), -1)
            right.settimeout(.1)
            self.assertEqual(right.recv(1), b'')
        finally: t.close(); left.close(); right.close()

    def test_slow_fragments_cannot_renew_request_deadline(self):
        def serve(sock):
            self.accept_registration(sock); message(sock)
            sock.sendall(frame(b'{', final=False))
            time.sleep(.07)
            sock.sendall(frame(b' ', opcode=0, final=False))
            self.assertEqual(sock.recv(1), b'')
        with self.connected(serve) as (t, _):
            self.register(t)
            began = time.monotonic()
            with self.assertRaises(TimeoutError): t.request(T.POWER_URI, {}, timeout_s=.12)
            self.assertLess(time.monotonic()-began, .5)

    def test_handshake_deadline_closes_without_http(self):
        left, right = socket.socketpair()
        t = T.TvTransport(IP, connect_socket=lambda *a, **k: left)
        try:
            began = time.monotonic()
            with self.assertRaises(TimeoutError): t.connect(pin_sha256=self.pin, timeout_s=.12)
            self.assertLess(time.monotonic()-began, .5)
            self.assertIsNone(t.sock)
        finally: t.close(); left.close(); right.close()

    def test_outgoing_size_cap_refuses_before_request_frame(self):
        def serve(sock):
            self.accept_registration(sock)
            self.assertEqual(sock.recv(1), b'')
        with self.connected(serve) as (t, _):
            self.register(t)
            with self.assertRaisesRegex(ValueError, 'TV_FRAME'):
                t.request(T.POWER_URI, {'value': 'x'*T.LIMIT})
            self.assertIsNone(t.sock)

    def test_peer_close_is_not_success_and_drops_authority(self):
        def serve(sock):
            self.accept_registration(sock); message(sock)
            sock.sendall(frame(struct.pack('!H', 1000), opcode=8))
        with self.connected(serve) as (t, _):
            self.register(t)
            with self.assertRaises(ValueError): t.request(T.POWER_URI, {})
            self.assertIsNone(t.sock)
            self.assertFalse(t.registered)

    def test_handshake_can_resume_after_poll_without_new_deadline(self):
        context = self.context
        class DelayedContext:
            def wrap_socket(self, *args, **kwargs):
                time.sleep(.15)
                return context.wrap_socket(*args, **kwargs)
        p = Peer(DelayedContext(), self.accept_registration)
        t = T.TvTransport(IP, connect_socket=p.dial)
        try:
            t.connect(pin_sha256=self.pin, timeout_s=1)
            self.assertEqual(t.register(client_key=KEY), {'client-key': KEY})
        finally: t.close(); p.close()

    def test_delayed_response_survives_read_poll(self):
        def serve(sock):
            self.accept_registration(sock); req = message(sock)
            time.sleep(.15)
            reply(sock, req, {'returnValue': True, 'state': 'Active'})
        with self.connected(serve) as (t, _):
            self.register(t)
            self.assertEqual(t.request(T.POWER_URI, {}, timeout_s=1),
                             {'returnValue': True, 'state': 'Active'})

    def test_close_bounds_read_when_fd_close_does_not_wake_it(self):
        # Some SSL/platform combinations leave an already blocked read asleep
        # until its socket timeout even after shutdown/close. Model that exact
        # behavior deterministically, alongside the real TLS close test above.
        entered, release = threading.Event(), threading.Event()
        t = T.TvTransport(IP)
        class StalledRead:
            def settimeout(self, timeout): self.timeout = timeout
            def recv(self, count):
                entered.set(); release.wait(self.timeout); raise TimeoutError()
            def shutdown(self, how): pass
            def close(self): pass
        t.sock = StalledRead()
        errors = []
        def call():
            try: t._read(2, time.monotonic()+5)
            except Exception as error: errors.append(error)
        worker = threading.Thread(target=call); worker.start()
        try:
            self.assertTrue(entered.wait(1))
            t.close(); worker.join(.5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([str(e) for e in errors], ['TV_CLOSED'])
        finally:
            release.set(); t.close(); worker.join(1)


if __name__ == '__main__': unittest.main()
