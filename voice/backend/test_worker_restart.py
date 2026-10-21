"""Actual owned pipe/subprocess lifecycle with authored scalar-only child.

No model, CUDA, seccomp, socket listener, microphone, TV or remote host is used.
The stub requires the same strictly increasing per-child ids as worker_child.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from controller import Refused
from model_worker import ModelWorker
from test_sealed_startup import ready

STUB = r'''
import json, os, struct, sys, time
mode, handshake = sys.argv[1], json.loads(sys.argv[2])
def exact(n):
    data = bytearray()
    while len(data) < n:
        part = os.read(0, n-len(data))
        if not part: raise EOFError()
        data.extend(part)
    return bytes(data)
def send(row):
    raw = json.dumps(row, separators=(',', ':')).encode()
    raw = struct.pack('>I', len(raw)) + raw
    while raw:
        count = os.write(1, raw)
        if count <= 0: raise EOFError()
        raw = raw[count:]
send({'schema':2,'state':'ready','isolation':handshake})
try:
    for expected in range(1, 257):
        size = struct.unpack('>I', exact(4))[0]
        if not 1 <= size <= 8192: raise ValueError()
        row = json.loads(exact(size))
        if row != {'schema':1,'id':expected,'action':'intent','text':'synthetic command'}:
            raise ValueError()
        if mode == 'stall': time.sleep(30)
        send({'schema':1,'id':expected,'action':'intent','text':'{"action":"unsupported"}'})
except EOFError:
    pass
except Exception:
    raise SystemExit(3)
'''


class WorkerRestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stub = Path(self.temp.name)/'synthetic_child.py'
        self.stub.write_text(STUB)
        self.processes, self.workers, self.modes = [], [], []

    def tearDown(self):
        for worker in self.workers:
            self.assertTrue(worker.close())
        for process in self.processes:
            self.assertIsNotNone(process.poll())
        self.temp.cleanup()

    def worker(self, modes=()):
        self.modes = list(modes)
        def spawn(argv, **kwargs):
            mode = self.modes.pop(0) if self.modes else 'ordinary'
            # Only the public synthetic readiness and mode enter this authored
            # fixture's argv. The actual worker never puts payloads there.
            process = subprocess.Popen([sys.executable, '-B', str(self.stub), mode,
                         json.dumps(ready(argv[-1]), separators=(',', ':'))], **kwargs)
            self.processes.append(process)
            return process
        worker = ModelWorker(str(Path(sys.executable).resolve()), '/synthetic/asr', '/synthetic/intent', spawn=spawn)
        self.workers.append(worker)
        return worker

    def command(self, worker, timeout=1):
        return worker.call('intent', 'synthetic command', timeout_s=timeout)

    def test_restart_after_success_starts_a_fresh_child_sequence(self):
        worker = self.worker()
        worker.start(timeout_s=2)
        first = worker.process
        self.assertEqual(self.command(worker), '{"action":"unsupported"}')
        self.assertEqual(self.command(worker), '{"action":"unsupported"}')
        self.assertEqual(worker.counter, 2)
        self.assertTrue(worker.close()); self.assertIsNone(worker.sealed)
        worker.start(timeout_s=2)
        self.assertIsNot(worker.process, first); self.assertEqual(worker.counter, 0)
        self.assertEqual(self.command(worker), '{"action":"unsupported"}')
        self.assertEqual(worker.counter, 1)

    def test_restart_after_exact_budget_exhaustion_gets_new_budget(self):
        worker = self.worker()
        worker.start(timeout_s=2)
        for _ in range(256):
            self.assertEqual(self.command(worker), '{"action":"unsupported"}')
        self.assertEqual(worker.counter, 256)
        with self.assertRaises(Refused): self.command(worker)
        self.assertTrue(worker.cleanup_complete); self.assertIsNone(worker.process)
        worker.start(timeout_s=2)
        self.assertEqual(self.command(worker), '{"action":"unsupported"}')
        self.assertEqual(worker.counter, 1)

    def test_timeout_reaps_owned_child_then_restart_has_no_delayed_reply(self):
        worker = self.worker(('stall', 'ordinary'))
        worker.start(timeout_s=2); old = worker.process
        began = time.monotonic()
        with self.assertRaises(Refused): self.command(worker, .05)
        self.assertLess(time.monotonic()-began, 3.5)
        self.assertIsNotNone(old.poll()); self.assertIsNone(worker.process)
        self.assertTrue(worker.cleanup_complete); self.assertIsNone(worker.sealed)
        worker.start(timeout_s=2)
        self.assertEqual(self.command(worker), '{"action":"unsupported"}')
        self.assertEqual(worker.counter, 1)

    def test_interruption_during_owned_startup_reaps_child_and_preserves_exception(self):
        for kind in (KeyboardInterrupt, lambda: SystemExit(23)):
            for phase in ('metadata', 'handshake'):
                with self.subTest(kind=kind, phase=phase):
                    worker = self.worker()
                    interruption = kind()
                    def interrupt(*args):
                        self.assertIsNotNone(worker.process)
                        self.assertIsNone(worker.process.poll())
                        raise interruption
                    target = 'clock' if phase == 'metadata' else '_read_frame'
                    with patch.object(worker, target, side_effect=interrupt):
                        with self.assertRaises(type(interruption)) as caught:
                            worker.start(timeout_s=2)
                    self.assertIs(caught.exception, interruption)
                    process = self.processes[-1]
                    self.assertIsNotNone(process.poll())
                    self.assertTrue(process.stdin.closed); self.assertTrue(process.stdout.closed)
                    self.assertIsNone(worker.process); self.assertIsNone(worker.sealed)
                    self.assertTrue(worker.cleanup_complete)

    def test_missing_cleanup_proof_never_spawns_fresh_child(self):
        spawn = Mock(side_effect=AssertionError('must not spawn'))
        worker = ModelWorker('/synthetic/python', '/synthetic/asr', '/synthetic/intent', spawn=spawn)
        worker.cleanup_complete = False
        with self.assertRaises(Refused): worker.start(timeout_s=1)
        spawn.assert_not_called()
        self.assertIsNone(worker.process); self.assertIsNone(worker.sealed)


if __name__ == '__main__': unittest.main()
