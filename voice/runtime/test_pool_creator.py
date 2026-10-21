"""Owned no-model children: Linux creator-thread death and pool lifetime.

No sockets, models, GPU, TV, private input or persistent service. Linux applies
PDEATHSIG only inside each short-lived owned fixture child. Other platforms
still exercise actual Popen/thread ownership and cleanup.
"""
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
import unittest

try:
    from . import pool
except ImportError:
    import pool


CHILD = r'''
import ctypes, os, signal, sys
signal.alarm(5)
if sys.platform == 'linux':
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        sys.exit(2)
if sys.argv[1] == 'ready': os.write(1, b'R')
while os.read(0, 1): pass
'''


class PipeWorker:
    def __init__(self, mode='ready'):
        self.mode = mode
        self.process = self.held = self.sealed = self.created = None
        self.counter = 0
        self.lock = threading.Lock()
        self.creator = None
        self.spawned = threading.Event()
        self.allow_close = True

    def start(self, *, timeout_s):
        self.creator = threading.current_thread()
        self.process = self.held = subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', CHILD, self.mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        self.spawned.set()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(min(timeout_s, 2)) or self.process.stdout.read(1) != b'R':
                raise ValueError('FIXTURE_START')
        self.sealed, self.created = {'synthetic': True}, time.monotonic()

    def close(self):
        self.sealed = None
        if not self.allow_close:
            return False
        process = self.process
        if process is None:
            return True
        try:
            process.stdin.close()
            try:
                process.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        finally:
            process.stdin.close()
            process.stdout.close()
        if process.poll() is not None:
            self.process = None
            return True
        return False


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return bool(predicate())


class CreatorTests(unittest.TestCase):
    def make(self, mode='ready', **kwargs):
        workers = []
        def factory():
            worker = PipeWorker(mode)
            workers.append(worker)
            return worker
        owner = pool.WorkerPool(factory, **kwargs)
        def cleanup():
            for worker in workers:
                worker.allow_close = True
            owner.unload(timeout_s=2)
            for worker in workers:
                worker.close()
        self.addCleanup(cleanup)
        return owner, workers

    def ready(self, owner):
        owner.begin_warm()
        self.assertTrue(eventually(lambda: owner.poll() == 'ready'))

    def assert_closed(self, owner, workers):
        self.assertEqual(owner._creators, [])
        for worker in workers:
            self.assertIsNotNone(worker.held.poll())
            self.assertTrue(worker.held.stdin.closed and worker.held.stdout.closed)
            self.assertFalse(worker.creator.is_alive())

    def test_ready_retains_actual_creator_until_owned_reap(self):
        owner, workers = self.make()
        self.ready(owner)
        worker = workers[0]
        time.sleep(.06)  # Let the old transient creator return and die.
        self.assertTrue(worker.creator.is_alive())
        self.assertIsNone(worker.process.poll())
        self.assertEqual(owner.poll(), 'ready')
        self.assertIs(owner.lease(), worker)
        owner.give_back(worker)
        self.assertTrue(owner.unload(timeout_s=2))
        self.assert_closed(owner, workers)

    def test_rotation_keeps_new_creator_and_joins_retired_creator(self):
        owner, workers = self.make(rotate_after_calls=2)
        self.ready(owner)
        first = workers[0]
        first.counter = 2
        self.assertTrue(eventually(lambda: owner.poll() == 'ready' and len(workers) == 2
                                   and owner.active is workers[1]))
        self.assertTrue(eventually(lambda: not first.creator.is_alive()))
        self.assertIsNotNone(first.held.poll())
        self.assertTrue(workers[1].creator.is_alive())
        self.assertTrue(owner.unload(timeout_s=2))
        self.assert_closed(owner, workers)

    def test_unconfirmed_close_keeps_creator_and_refuses_cleanup(self):
        owner, workers = self.make()
        self.ready(owner)
        workers[0].allow_close = False
        started = time.monotonic()
        self.assertFalse(owner.unload(timeout_s=.04))
        self.assertLess(time.monotonic() - started, .3)
        self.assertTrue(workers[0].creator.is_alive())
        self.assertIsNone(workers[0].held.poll())
        self.assertTrue(owner._creators)
        workers[0].allow_close = True
        self.assertTrue(owner.unload(timeout_s=2))
        self.assert_closed(owner, workers)

    def test_cancel_warming_child_joins_creator_and_child(self):
        owner, workers = self.make(mode='stall')
        owner.begin_warm()
        self.assertTrue(eventually(lambda: bool(workers) and workers[0].spawned.is_set()))
        self.assertTrue(owner.unload(timeout_s=2))
        self.assertTrue(eventually(lambda: not workers[0].creator.is_alive()))
        self.assert_closed(owner, workers)

    def test_inline_real_child_never_promotes_ready(self):
        owner, workers = self.make(spawn_thread=lambda run: run())
        owner.begin_warm()
        self.assertEqual(owner.poll(), 'failed')
        self.assertIsNone(owner.active)
        self.assertIsNotNone(workers[0].held.poll())
        self.assertTrue(owner.unload(timeout_s=1))

    @unittest.skipUnless(sys.platform == 'linux', 'Linux PDEATHSIG semantics')
    def test_kernel_parent_death_follows_creator_thread(self):
        worker = PipeWorker()
        creator = threading.Thread(target=lambda: worker.start(timeout_s=2))
        creator.start()
        creator.join(3)
        self.addCleanup(worker.close)
        self.assertFalse(creator.is_alive())
        self.assertTrue(eventually(lambda: worker.held.poll() is not None))
        self.assertEqual(worker.held.returncode, -signal.SIGKILL)
        # This test process is still alive. The child followed its creator.
        self.assertGreater(os.getpid(), 1)


if __name__ == '__main__':
    unittest.main()
