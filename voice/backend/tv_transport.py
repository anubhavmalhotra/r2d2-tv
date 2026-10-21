"""Pinned local SSAP transport. Uses an existing key; never pairs or saves keys."""
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import socket
import ssl
import struct
import threading
import time

try:
    from .agent.executors.tv_gateway import CertificateChanged, SSAP
except ImportError:
    if __package__:
        from .agent_controller import _load_agent_package
    else:
        from agent_controller import _load_agent_package
    _load_agent_package()
    from agent.executors.tv_gateway import CertificateChanged, SSAP

POWER_URI = 'ssap://com.webos.service.tvpower/power/getPowerState'
ALLOWED = frozenset(row[0] for row in SSAP.values()) | {POWER_URI}
LIMIT = 262144


class TvTransport:
    def __init__(self, address, *, connect_socket=socket.create_connection, clock=time.monotonic):
        ip = ipaddress.IPv4Address(address)
        if str(ip) != address or not any(ip in ipaddress.IPv4Network(n) for n in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '192.0.2.0/24')):
            raise ValueError('TV_LOCAL_ADDRESS')
        self.address, self.dial, self.clock = address, connect_socket, clock
        self.sock = None
        self.buffer = bytearray()
        self.lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.generation = 0
        self.registered = False

    def close(self):
        # Closing from another thread must interrupt a pending receive.
        with self.state_lock:
            self.generation += 1
            sock, self.sock = self.sock, None
            self.registered = False
            self.buffer.clear()
        if sock is not None:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()

    def _end(self, timeout_s):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 5:
            raise ValueError('TV_TIMEOUT')
        return self.clock() + timeout_s

    def _remaining(self, end, maximum=None):
        left = end - self.clock()
        if left <= 0: raise TimeoutError('TV_TIMEOUT')
        sock = self.sock
        if sock is None: raise OSError('TV_CLOSED')
        sock.settimeout(min(left, maximum) if maximum is not None else left)
        return left

    def _read(self, size, end):
        while True:
            with self.state_lock:
                if self.sock is None: raise OSError('TV_CLOSED')
                if len(self.buffer) >= size:
                    result = bytes(self.buffer[:size]); del self.buffer[:size]
                    return result
                self._remaining(end, .1)
                sock, generation = self.sock, self.generation
                count = min(4096, size - len(self.buffer))
            try:
                chunk = sock.recv(count)
            except TimeoutError:
                continue  # retain the original deadline; observe close() promptly
            if not chunk: raise OSError('TV_CLOSED')
            with self.state_lock:
                if generation != self.generation or self.sock is not sock:
                    raise OSError('TV_CLOSED')
                self.buffer.extend(chunk)

    def connect(self, *, pin_sha256, timeout_s=3):
        if type(pin_sha256) is not str or not re.fullmatch('[a-f0-9]{64}', pin_sha256):
            raise ValueError('TV_PIN_REQUIRED')
        with self.lock:
            self.close()
            with self.state_lock:
                generation = self.generation
            end = self._end(timeout_s)
            raw = self.dial((self.address, 3001), timeout=timeout_s)
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                # Own the descriptor before any blocking handshake. close()
                # can interrupt it and a cancelled dial cannot publish later.
                with self.state_lock:
                    if generation != self.generation: raise OSError('TV_CLOSED')
                    self.sock = raw
                    self._remaining(end, .1)
                    self.sock = context.wrap_socket(raw, server_hostname=self.address,
                                                    do_handshake_on_connect=False)
                    raw = None
                    sock = self.sock
                while True:
                    self._remaining(end, .1)
                    try:
                        sock.do_handshake()
                        break
                    except TimeoutError:
                        continue
                self._remaining(end)
                cert = sock.getpeercert(True)
                if not cert or not hmac.compare_digest(hashlib.sha256(cert).hexdigest(), pin_sha256):
                    raise CertificateChanged()
                key = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
                self._remaining(end)
                sock.sendall(('GET / HTTP/1.1\r\nHost: '+self.address+':3001\r\n'
                    'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                    'Sec-WebSocket-Key: '+key+'\r\n\r\n').encode('ascii'))
                header = bytearray()
                while not header.endswith(b'\r\n\r\n'):
                    if len(header) >= 8192: raise ValueError('TV_UPGRADE')
                    header.extend(self._read(1, end))
                lines = header.decode('ascii').split('\r\n')
                fields = {}
                for line in lines[1:]:
                    if not line: continue
                    name, value = line.split(':', 1); name=name.strip().lower()
                    if name in fields: raise ValueError('TV_UPGRADE')
                    fields[name] = value.strip()
                expected = base64.b64encode(hashlib.sha1((key+
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
                if (lines[0] != 'HTTP/1.1 101 Switching Protocols' or
                        fields.get('sec-websocket-accept') != expected or
                        fields.get('upgrade', '').lower() != 'websocket' or
                        'upgrade' not in [v.strip().lower() for v in
                                          fields.get('connection', '').split(',')]):
                    raise ValueError('TV_UPGRADE')
                with self.state_lock:
                    if generation != self.generation or self.sock is not sock:
                        raise OSError('TV_CLOSED')
            except BaseException:
                self.close()
                raise
            finally:
                if raw is not None: raw.close()

    def _frame(self, opcode, body, end):
        if len(body) > LIMIT: raise ValueError('TV_FRAME')
        mask = secrets.token_bytes(4); size = len(body)
        head = bytes((0x80 | opcode, 0x80 | size)) if size < 126 else (
            bytes((0x80 | opcode, 0xfe)) + struct.pack('!H', size) if size < 65536 else
            bytes((0x80 | opcode, 0xff)) + struct.pack('!Q', size))
        self._remaining(end)
        self.sock.sendall(head + mask + bytes(v ^ mask[i % 4] for i,v in enumerate(body)))

    def _receive(self, end):
        fragments=bytearray(); started=False
        for _ in range(64):
            a,b=self._read(2,end); final=bool(a & 128); opcode=a & 15
            if a & 112 or b & 128: raise ValueError('TV_FRAME')
            size=b & 127
            if size==126:
                size=struct.unpack('!H',self._read(2,end))[0]
                if size<126: raise ValueError('TV_FRAME')
            elif size==127:
                size=struct.unpack('!Q',self._read(8,end))[0]
                if size<65536: raise ValueError('TV_FRAME')
            if size>LIMIT or len(fragments)+size>LIMIT: raise ValueError('TV_FRAME')
            if opcode>=8 and (not final or size>125): raise ValueError('TV_FRAME')
            body=self._read(size,end)
            if opcode in (9,10):
                if not final or size>125: raise ValueError('TV_FRAME')
                if opcode==9:self._frame(10,body,end)
                continue
            if opcode==1 and not started:started=True
            elif opcode!=0 or not started:raise ValueError('TV_FRAME')
            fragments.extend(body)
            if final:
                value=json.loads(fragments.decode('utf8'))
                if type(value) is not dict:raise ValueError('TV_RESPONSE')
                return value
        raise ValueError('TV_FRAME_BUDGET')

    def _rpc(self, kind, payload, end, uri=None):
        with self.state_lock:
            generation, sock = self.generation, self.sock
            if sock is None: raise OSError('TV_CLOSED')
        token=secrets.token_hex(8)
        message={'id':token,'type':kind,'payload':payload}
        if uri is not None:message['uri']=uri
        self._frame(1,json.dumps(message,separators=(',',':')).encode('utf8'),end)
        for _ in range(32):
            reply=self._receive(end)
            body=reply.get('payload',{})
            if type(body) is dict and 'pairingType' in body:
                self.close();return {'pairingType':body['pairingType']}
            if reply.get('id')!=token:continue
            if kind=='register':
                if reply.get('type')=='registered' and type(body) is dict:
                    with self.state_lock:
                        if generation != self.generation or self.sock is not sock:
                            raise OSError('TV_CLOSED')
                        self.registered = body.get('client-key') == payload['client-key']
                    return body
                return {'returnValue':False}
            return body if reply.get('type')=='response' and type(body) is dict else {}
        raise ValueError('TV_RESPONSE_BUDGET')

    def register(self, *, client_key, timeout_s=3):
        if type(client_key) is not str or not re.fullmatch('[a-f0-9]{32,128}',client_key):
            raise ValueError('TV_KEY_REQUIRED')
        with self.lock:
            self.registered = False
            try:return self._rpc('register',{'client-key':client_key},self._end(timeout_s))
            except BaseException:self.close();raise

    def request(self, uri, payload, *, timeout_s=3):
        with self.lock:
            if uri not in ALLOWED or type(payload) is not dict or not self.registered:
                raise ValueError('TV_REQUEST')
            try:return self._rpc('request',payload,self._end(timeout_s),uri)
            except BaseException:self.close();raise
