"""Explicit local-only finite voice listener; disabled until providers qualify.

Caller owns credentials and controller wiring. Importing this module opens no
socket, loads no model, and changes no TV route. One client/utterance at a time.
"""
import ipaddress
import json
import socket
import ssl
import struct
import threading
import time

if __package__:
    from .controller import Refused
else:
    from controller import Refused


class VoiceService:
    def __init__(self, address, port, context, ingress, *, qualified=False,
                 clock=time.monotonic):
        parsed = ipaddress.ip_address(address)
        if (not (parsed.is_loopback or parsed.is_private) or parsed.is_unspecified or
                parsed.is_multicast or type(port) is not int or not 1024 <= port <= 65535 or
                not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED or
                context.minimum_version != ssl.TLSVersion.TLSv1_3 or
                context.maximum_version != ssl.TLSVersion.TLSv1_3):
            raise Refused('LISTENER_CONFIG')
        self.address, self.port, self.context, self.ingress = address, port, context, ingress
        self.qualified = qualified is True
        self.clock = clock
        self.stop = threading.Event()
        self.listener = self.active = None
        self.lock = threading.Lock()

    def close(self):
        self.stop.set()
        with self.lock:
            sockets = [self.active, self.listener]
        for sock in sockets:
            if sock is None: continue
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: sock.close()
            except OSError: pass
        self.ingress.controller.cancel()

    def serve(self, *, max_connections=256, lifetime_s=1800):
        if (not self.qualified or self.ingress.asr.qualified is not True or
                type(max_connections) is not int or not 1 <= max_connections <= 256 or
                type(lifetime_s) not in (int, float) or not 0 < lifetime_s <= 1800):
            raise Refused('SERVICE_UNQUALIFIED')
        if self.listener is not None or self.stop.is_set():
            raise Refused('SERVICE_STATE')
        family = socket.AF_INET6 if ':' in self.address else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        with self.lock: self.listener = listener
        start = self.clock()
        try:
            listener.settimeout(1)
            listener.bind((self.address, self.port))
            listener.listen(1)
            for _ in range(max_connections):
                while not self.stop.is_set() and self.clock() - start < lifetime_s:
                    try: raw, _ = listener.accept(); break
                    except socket.timeout: continue
                else: return
                raw.settimeout(2)
                with self.lock: self.active = raw
                try:
                    # SSL negotiation itself remains bounded before any PTT lease.
                    with self.context.wrap_socket(raw, server_side=True) as tls:
                        with self.lock: self.active = tls
                        outcome = self.ingress.connection(tls)
                        body = json.dumps({'schema': 1, 'state': outcome.state,
                                           'code': outcome.code}, separators=(',', ':')).encode()
                        tls.settimeout(1)
                        tls.sendall(struct.pack('>I', len(body)) + body)
                except (OSError, ssl.SSLError):
                    pass
                finally:
                    raw.close()
                    with self.lock: self.active = None
        finally:
            self.close()
