"""One-use authenticated PTT framing. No listener, payload logs or files by default.

Wire: TLS 1.3 with verified/pinned client cert; server sends 32 random challenge
bytes. Client sends >I length + closed JSON handshake HMAC-bound to this challenge,
then >II sequence/length + signed16 little-endian PCM chunks. Zero length releases
PTT; 0xffffffff length cancels. One connection carries exactly one utterance.
This is our new protocol, not a claim about LG's local microphone socket format.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import socket
import ssl
import struct
import threading
import time

if __package__:
    from .controller import Refused, Outcome, bounded_text
else:
    from controller import Refused, Outcome, bounded_text

RATE = 16000
MAX_AUDIO = RATE * 2 * 10
MAX_CHUNK = 3200
MAX_CHUNKS = 500
MAX_HANDSHAKE = 512
# Challenge to release frame: the TV's 9.2 s hold plus stop and reap fit its own 15 s
# hello-to-release bound (tls_pcm_client.js AUDIO_MS); this is 1 s more.
AUDIO_DEADLINE = 16
DOMAIN = b'R2D2-PTT-v1\0'


def unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise Refused('INPUT_SCHEMA')
        obj[key] = value
    return obj


def strict_json(raw, maximum):
    if type(raw) is not bytes or len(raw) > maximum:
        raise Refused('INPUT_SCHEMA')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise Refused('INPUT_SCHEMA') from None


def read_exact(sock, count, deadline, clock):
    if not 0 <= count <= MAX_CHUNK:
        raise Refused('INPUT_SIZE')
    data = bytearray()
    try:
        while len(data) < count:
            remaining = deadline - clock()
            if remaining <= 0:
                raise Refused('INPUT_EXPIRED')
            sock.settimeout(min(remaining, 2))
            try:
                chunk = sock.recv(count - len(data))
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                raise Refused('INPUT_DISCONNECTED')
            data.extend(chunk)
        return bytes(data)
    finally:
        data[:] = b'\0' * len(data)


@dataclass(frozen=True, repr=False)
class Device:
    certificate_sha256: str = field(repr=False)
    key: bytes = field(repr=False)

    def __post_init__(self):
        if (not re.fullmatch('[a-f0-9]{64}', self.certificate_sha256) or
                type(self.key) is not bytes or len(self.key) != 32):
            raise Refused('DEVICE_CONFIG')


class DisabledASR:
    qualified = False


class VoiceIngress:
    def __init__(self, controller, device, asr=None, *, clock=time.monotonic,
                 challenge=secrets.token_bytes):
        self.controller, self.device = controller, device
        self.asr = asr or DisabledASR()
        self.clock, self.challenge = clock, challenge
        self.guard = threading.Lock()

    def connection(self, sock):
        # Production accepts only stdlib SSL socket with peer-chain validation.
        # Unit fixtures call _verified_connection after proving protocol guards.
        if (not isinstance(sock, ssl.SSLSocket) or
                sock.context.verify_mode != ssl.CERT_REQUIRED or
                sock.version() != 'TLSv1.3'):
            return Outcome('refused', 'TRANSPORT_UNQUALIFIED')
        cert = sock.getpeercert(binary_form=True)
        if not cert or not hmac.compare_digest(hashlib.sha256(cert).hexdigest(),
                                              self.device.certificate_sha256):
            return Outcome('refused', 'DEVICE_NOT_AUTHENTICATED')
        return self._verified_connection(sock)

    def _verified_connection(self, sock):
        if not self.guard.acquire(blocking=False):
            return Outcome('refused', 'VOICE_BUSY')
        audio = bytearray()
        lease = transcript = None
        try:
            if self.asr.qualified is not True:
                return Outcome('refused', 'ASR_UNQUALIFIED')
            challenge = self.challenge(32)
            if type(challenge) is not bytes or len(challenge) != 32:
                raise Refused('DEVICE_CONFIG')
            deadline = self.clock() + AUDIO_DEADLINE
            sock.settimeout(2)
            sock.sendall(challenge)
            length = struct.unpack('>I', read_exact(sock, 4, deadline, self.clock))[0]
            if not 1 <= length <= MAX_HANDSHAKE:
                raise Refused('INPUT_SIZE')
            hello = strict_json(read_exact(sock, length, deadline, self.clock), MAX_HANDSHAKE)
            expected_keys = {'schema', 'mode', 'rate', 'channels', 'format', 'tag'}
            if (type(hello) is not dict or set(hello) != expected_keys or
                    type(hello['schema']) is not int or hello['schema'] != 1 or
                    hello['mode'] != 'command' or type(hello['rate']) is not int or
                    hello['rate'] != RATE or type(hello['channels']) is not int or
                    hello['channels'] != 1 or hello['format'] != 's16le' or
                    type(hello['tag']) is not str or
                    not re.fullmatch('[a-f0-9]{64}', hello['tag'])):
                raise Refused('INPUT_SCHEMA')
            expected = hmac.digest(self.device.key, DOMAIN + challenge, 'sha256').hex()
            if not hmac.compare_digest(hello['tag'], expected):
                raise Refused('DEVICE_NOT_AUTHENTICATED')
            challenge = expected = hello = None
            lease = self.controller.arm()
            for index in range(MAX_CHUNKS + 1):
                sequence, size = struct.unpack('>II', read_exact(sock, 8, deadline, self.clock))
                if sequence != index:
                    raise Refused('INPUT_SEQUENCE')
                if size == 0xffffffff:
                    return Outcome('refused', 'VOICE_CANCELLED')
                if size == 0:
                    if len(audio) < 640:
                        raise Refused('AUDIO_EMPTY_OR_SHORT')
                    break
                if (index >= MAX_CHUNKS or not 2 <= size <= MAX_CHUNK or size % 2 or
                        len(audio) + size > MAX_AUDIO):
                    raise Refused('INPUT_SIZE')
                audio.extend(read_exact(sock, size, deadline, self.clock))
            else:
                raise Refused('INPUT_SIZE')
            if self.clock() >= deadline:
                raise Refused('INPUT_EXPIRED')
            # Trusted local worker must honor cancellation/hard timeout. Its
            # qualification receipt covers weights, egress and retention separately.
            transcript = self.asr.transcribe(audio, rate=RATE, timeout_s=8)
            audio[:] = b'\0' * len(audio)
            audio.clear()
            if transcript == '':
                # No recognizable speech is a normal outcome, never an action.
                raise Refused('AUDIO_EMPTY_OR_SHORT')
            if not bounded_text(transcript, 1024, 4096):
                raise Refused('ASR_OUTPUT_INVALID')
            return self.controller.execute(lease, transcript)
        except Refused as error:
            # Fixed enum codes only. Arm-time refusals are kept so the TV can say
            # why nothing happened (paused, busy, warming up, rate limited).
            allowed = {'INPUT_SCHEMA', 'INPUT_SIZE', 'INPUT_EXPIRED', 'INPUT_DISCONNECTED',
                       'INPUT_SEQUENCE', 'DEVICE_NOT_AUTHENTICATED', 'AUDIO_EMPTY_OR_SHORT',
                       'ASR_OUTPUT_INVALID', 'ASR_UNAVAILABLE', 'CONTROLLER_BUSY',
                       'AGENT_PAUSED', 'RATE_LIMITED', 'VOICE_WARMING_UP', 'VOICE_BUSY'}
            code = str(error)
            return Outcome('refused', code if code in allowed else 'VOICE_UNAVAILABLE')
        except Exception:
            return Outcome('refused', 'VOICE_UNAVAILABLE')
        finally:
            if lease is not None:
                self.controller.cancel(lease)
            transcript = None
            audio[:] = b'\0' * len(audio)
            audio.clear()
            self.guard.release()


def tls_context(certificate, private_key, client_ca):
    """Operator owns private credential paths; no listener opened here."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certificate, private_key)
    context.load_verify_locations(cafile=client_ca)
    context.options |= ssl.OP_NO_COMPRESSION
    context.num_tickets = 0
    return context
