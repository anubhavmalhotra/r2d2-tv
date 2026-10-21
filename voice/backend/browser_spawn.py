"""Explicit opt-in spawn hook for Manager's exact own Trial child.

The inherited descriptor is the only new command transport. No secret enters
argv, environment or disk. Trial must explicitly accept the descriptor and call
attach_trial(); its own CDP main loop must publish fresh selected-page proof.
No monkeypatch, external process attach, TV launch, or automatic activation.
"""
from pathlib import Path
import math
import re
import socket
import subprocess
import threading
import time

if __package__:
    from .browser_channel import channel_pair, TrialControlHost
    from .controller import Refused
else:
    from browser_channel import channel_pair, TrialControlHost
    from controller import Refused


class ControlledSpawn:
    def __init__(self, runtime_path, *, spawn=subprocess.Popen):
        self.runtime_path = str(Path(runtime_path).resolve())
        self.spawn = spawn
        self.manager = None
        self.clients = {}
        self.lock = threading.Lock()

    def bind_manager(self, manager):
        if (self.manager is not None and self.manager is not manager) or not hasattr(manager, 'lock'):
            raise Refused('CONTROL_MANAGER')
        self.manager = manager

    def __getattr__(self, name):
        return getattr(self.spawn, name)

    def __call__(self, argv, **kwargs):
        if (type(argv) is not list or argv[:3] != ['/usr/bin/python3', self.runtime_path, '--run'] or
                argv[3:] not in ([], ['--display-transport', 'webrtc'], ['--display-transport', 'mse']) or
                'pass_fds' in kwargs or kwargs.get('close_fds', True) is not True):
            raise Refused('CONTROL_SPAWN')
        manager = self.manager
        if manager is None: raise Refused('CONTROL_MANAGER')
        with manager.lock:
            session = manager.session
            if (type(session) is not dict or not re.fullmatch('[a-f0-9]{32}', session.get('id', '')) or
                    manager.state != 'starting' or session.get('process') is not None):
                raise Refused('CONTROL_SESSION')
            identity = session['id']
        with self.lock:
            if identity in self.clients: raise Refused('CONTROL_DUPLICATE')
            if len(self.clients)>=256:raise Refused('CONTROL_LIFECYCLE')
            client, child = channel_pair(identity)
            self.clients[identity] = client
        try:
            return self.spawn(argv + ['--voice-control-fd', str(child.fileno())],
                              pass_fds=(child.fileno(),), **kwargs)
        except BaseException:
            client.close()
            raise
        finally:
            child.close()  # Only the child process retains this endpoint after spawn.

    def client(self, identity, *, timeout_s):
        if type(timeout_s)not in (int,float) or not math.isfinite(timeout_s) or not 0<timeout_s<=5:return None
        deadline=time.monotonic()+timeout_s
        manager = self.manager
        if manager is None: return None
        if not manager.lock.acquire(timeout=max(0,deadline-time.monotonic())):return None
        try:
            session = manager.session
            if (not session or session.get('id') != identity or manager.state != 'ready' or
                    not session.get('process') or session['process'].poll() is not None):
                return None
        finally:manager.lock.release()
        if not self.lock.acquire(timeout=max(0,deadline-time.monotonic())):return None
        try:
            client = self.clients.get(identity)
            return None if client is None or client.channel.closed else client
        finally:self.lock.release()

    def current_client(self, *, timeout_s):
        if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 5:
            return None
        deadline = time.monotonic() + timeout_s
        manager = self.manager
        if manager is None or not manager.lock.acquire(timeout=timeout_s): return None
        try:
            session = manager.session
            if not session or manager.state != 'ready': return None
            identity = session.get('id')
        finally: manager.lock.release()
        remaining = deadline - time.monotonic()
        client = self.client(identity, timeout_s=remaining) if remaining > 0 else None
        return (identity,client) if client is not None else None

    def close(self, identity):
        # Called in Manager's finally for this exact session; never terminate viewing.
        with self.lock: client = self.clients.get(identity)
        if client is not None: client.close()


def attach_trial(trial, descriptor):
    if type(descriptor) is not int or not 3 <= descriptor <= 1048576:
        raise Refused('CONTROL_DESCRIPTOR')
    sock = socket.socket(fileno=descriptor)
    try:
        sock.set_inheritable(False)
        host = TrialControlHost(sock, trial)
        thread = threading.Thread(target=host.serve, daemon=True, name='owned-voice-control')
        thread.start()
        return host, thread
    except BaseException:
        sock.close()
        raise


def close_trial(host, thread):
    host.close()
    thread.join(timeout=1)
    return not thread.is_alive()
