"""Shared test fixtures. Every test forbids socket creation (as in
the original voice controller tests) and writes only under a private temp dir."""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import tempfile
import types
import unittest
from unittest import mock

from agent.agent import Agent
from agent.audit import AuditLog
from agent.executors.browser import BrowserExecutor, FakeBrowserChannel
from agent.executors.feedback import Feedback, FakeOverlay
from agent.executors.tv_gateway import FakeSSAPTransport, TVExecutor, TVGateway
from agent.killswitch import KillSwitch
from agent.models import FakeModel, ScriptedModel
from agent.registry import prompt_sha256

SENTINEL = 'zebra quartz marmalade'   # a phrase that must never reach disk


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += max(0.0, seconds)

    def wall(self):
        return 1_790_000_000.0 + self.now  # 2026-ish UTC, moves with the fake clock


class Case(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(socket, 'socket', side_effect=AssertionError('NETWORK_FORBIDDEN'))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix='r2d2-agent-test-')).resolve()
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock()

    # -- builders ---------------------------------------------------------------
    def private_file(self, name, value, mode=0o600, base=None):
        path = (base or self.tmp) / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle)
        os.chmod(path, mode)
        return path

    def env(self, phase='P5', *, model=None, table=None, with_tv=None, overlay=True,
            surface=None, deeplinks=None, volume_ceiling=35, events=None, feedback=None):
        e = types.SimpleNamespace()
        e.clock = self.clock
        e.base = Path(tempfile.mkdtemp(dir=self.tmp))     # one private tree per environment
        os.chmod(e.base, 0o700)
        config = e.base / 'config'
        config.mkdir(mode=0o700)
        e.kill = KillSwitch(config)
        e.audit_dir = e.base / 'audit'
        # These fixtures assert audit rows, redaction and disk failures. Runtime
        # construction defaults to discard; test_audit_default covers that path.
        e.audit = AuditLog(e.audit_dir, wall=self.clock.wall, persistent=True)
        e.channel = FakeBrowserChannel(self.clock)
        e.browser = BrowserExecutor(e.channel, clock=self.clock, wait=self.clock.wait)
        e.transport = FakeSSAPTransport()
        e.key_path = self.private_file('client-key.json', {'client-key': secrets.token_hex(16)}, base=e.base)
        e.pin_path = self.private_file('certificate.json', {'sha256': secrets.token_hex(32)}, base=e.base)
        e.open_path = self.private_file('open-url.json',
                                        {'url': 'http://192.0.2.10:4181/%s/open' % secrets.token_hex(32)},
                                        base=e.base)
        e.trips = []
        e.gateway = TVGateway(e.transport, key_path=e.key_path, pin_path=e.pin_path,
                              open_url_path=e.open_path, killswitch=e.kill, phase=phase,
                              clock=self.clock, on_trip=e.trips.append, deeplinks=deeplinks)
        tv_on = with_tv if with_tv is not None else phase in ('P3', 'P4', 'P5')
        e.tv = TVExecutor(e.gateway, clock=self.clock, wait=self.clock.wait) if tv_on else None
        e.overlay = FakeOverlay()
        e.feedback = feedback or Feedback(phase, overlay=e.overlay if overlay else None,
                                          toast=e.tv.toast if e.tv is not None else None)
        if model is None:
            model = FakeModel(table or {}, phase=phase)
        e.model = model
        e.agent = Agent(phase=phase, audit=e.audit, killswitch=e.kill, model=model, browser=e.browser,
                        tv=e.tv, feedback=e.feedback, clock=self.clock,
                        surface_probe=surface, volume_ceiling=volume_ceiling, events=events)
        return e

    def say(self, e, transcript):
        return e.agent.handle_utterance(e.agent.arm(), transcript)

    def audit_bytes(self, e):
        if not e.audit_dir.exists():
            return b''
        return b''.join(p.read_bytes() for p in sorted(e.audit_dir.iterdir()))

    def audit_rows(self, e):
        return [json.loads(line) for line in self.audit_bytes(e).splitlines() if line]


def scripted(raw, phase='P5', exc=None):
    return ScriptedModel(raw, exc=exc, prompt_sha=prompt_sha256(phase))


def call(tool, **args):
    return json.dumps({'tool': tool, 'args': args})
