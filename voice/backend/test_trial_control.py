"""Actual runtime hooks, private sockets and owner observations; no TV/browser."""
import ast
import io
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from browser_channel import channel_pair
from browser_spawn import attach_trial, close_trial
from trial_control import service_trial_control

ROOT = Path(__file__).resolve().parents[2]


def source_function(path, cls, name, namespace=None):
    tree = ast.parse(path.read_text())
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    function = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(namespace or {})
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), ns)
    return ns[name]


class Trial:
    def __init__(self):
        self.lock = threading.RLock(); self.calls = []
        self.page_ready = True; self.stop = threading.Event()
        self.navigation_pending = None; self.navigation_active = False
        self.owner_verified_at = time.monotonic(); self.input_epoch = 0
        self.sandbox = {'passed': True}

    def queue_navigation(self, body):
        self.calls.append(body); self.navigation_pending = body['url']; self.input_epoch += 1
        return True


class RuntimeControlTests(unittest.TestCase):
    def setUp(self):
        self.trial = Trial()
        self.client, child = channel_pair('a' * 32)
        self.host, self.thread = attach_trial(self.trial, child.detach())
        self.page = types.SimpleNamespace(voice_target_matches=lambda target: True)

    def tearDown(self):
        self.client.close()
        self.assertTrue(close_trial(self.host, self.thread))

    def test_queue_ack_is_not_match_or_tv_presentation(self):
        service_trial_control(self.host, self.trial, self.page)
        self.assertTrue(self.client.observe(timeout_s=1).ready)
        self.assertEqual(self.client.queue_navigation({'url': 'https://www.youtube.com'},
            operation='b'*32+':navigate', timeout_s=1), 'accepted')
        service_trial_control(self.host, self.trial, self.page)
        queued = self.client.observe(timeout_s=1)
        self.assertFalse(queued.ready); self.assertFalse(queued.presented); self.assertIsNone(queued.url)
        self.trial.navigation_pending = None
        self.trial.owner_verified_at = time.monotonic()
        service_trial_control(self.host, self.trial, self.page)
        after = self.client.observe(timeout_s=1)
        self.assertTrue(after.ready); self.assertEqual(after.url, 'https://www.youtube.com')
        self.assertFalse(after.presented)

    def test_concurrent_user_navigation_invalidates_same_target_match(self):
        self.client.queue_navigation({'url': 'https://www.youtube.com'}, operation='b'*32+':navigate', timeout_s=1)
        self.trial.navigation_pending = None
        def changed(_):
            self.trial.input_epoch += 1
            return True
        self.page.voice_target_matches = changed
        service_trial_control(self.host, self.trial, self.page)
        self.assertIsNone(self.client.observe(timeout_s=1).url)

    def test_expired_owner_stopped_trial_and_cdp_failure_do_not_match(self):
        self.client.queue_navigation({'url': 'https://www.youtube.com'}, operation='b'*32+':navigate', timeout_s=1)
        self.trial.navigation_pending = None
        self.trial.owner_verified_at = time.monotonic() - 2
        self.page.voice_target_matches = lambda _: (_ for _ in ()).throw(AssertionError('no expired CDP'))
        service_trial_control(self.host, self.trial, self.page)
        self.assertFalse(self.client.observe(timeout_s=1).ready)
        self.trial.owner_verified_at = time.monotonic()
        self.page.voice_target_matches = lambda _: (_ for _ in ()).throw(RuntimeError('private failure'))
        with self.assertRaisesRegex(RuntimeError, 'private failure'):
            service_trial_control(self.host, self.trial, self.page)
        self.assertTrue(self.host.channel.closed)

    def test_stopped_trial_does_not_query_page(self):
        self.client.queue_navigation({'url': 'https://www.youtube.com'}, operation='b'*32+':navigate', timeout_s=1)
        self.trial.navigation_pending = None
        self.trial.stop.set()
        self.page.voice_target_matches = lambda _: (_ for _ in ()).throw(AssertionError('stopped page query'))
        service_trial_control(self.host, self.trial, self.page)
        self.assertFalse(self.client.observe(timeout_s=1).ready)

    def test_no_repeated_cdp_after_voice_observation_ends(self):
        self.client.queue_navigation({'url': 'https://www.youtube.com'}, operation='b'*32+':navigate', timeout_s=1)
        self.trial.navigation_pending = None
        self.host.requested_at = time.monotonic() - 2
        self.page.voice_target_matches = lambda _: (_ for _ in ()).throw(AssertionError('idle voice CDP'))
        service_trial_control(self.host, self.trial, self.page)
        self.assertIsNone(self.client.observe(timeout_s=1).url)
        self.page.voice_target_matches = lambda _: True
        service_trial_control(self.host, self.trial, self.page)
        self.assertEqual(self.client.observe(timeout_s=1).url, 'https://www.youtube.com')

    def test_first_command_after_normal_startup_delay(self):
        # Regression: the prior five-second hello deadline expired while the
        # private Chromium startup was still running or the user was viewing.
        time.sleep(5.1)
        self.trial.owner_verified_at = time.monotonic()
        service_trial_control(self.host, self.trial, self.page)
        self.assertTrue(self.client.observe(timeout_s=1).ready)


class SelectedPageTests(unittest.TestCase):
    def setUp(self):
        self.match = source_function(ROOT/'spark/runtime/session_page.py.in', 'TabSession', 'voice_target_matches')
        self.page = types.SimpleNamespace(popup_lock=threading.RLock(), active='tab', target='owned', session='session',
            context='private', tabs={'tab': {'target': 'owned', 'session': 'session'}}, _assert_alive=lambda: True)
        self.row = {'targetId': 'owned', 'browserContextId': 'private', 'type': 'page', 'attached': True,
                    'url': 'https://www.youtube.com/'}
        self.page._call = lambda method, body: {'targetInfo': dict(self.row)}

    def test_owned_selected_page_and_only_root_slash_equivalence(self):
        self.assertTrue(self.match(self.page, 'https://www.youtube.com'))
        self.row['url'] = 'https://www.youtube.com/watch?v=private'
        self.assertFalse(self.match(self.page, 'https://www.youtube.com'))
        self.row['url'] = 'https://duckduckgo.com/?q=test&foreign=1'
        self.assertFalse(self.match(self.page, 'https://duckduckgo.com/?q=test'))

    def test_foreign_context_target_and_attachment_rejected(self):
        for key, value in [('browserContextId', 'foreign'), ('targetId', 'foreign'), ('attached', False), ('type', 'worker')]:
            old = self.row[key]; self.row[key] = value
            self.assertFalse(self.match(self.page, 'https://www.youtube.com'))
            self.row[key] = old
        self.page.session = 'foreign'
        self.assertFalse(self.match(self.page, 'https://www.youtube.com'))

    def test_uses_native_target_metadata_not_webpage_evaluation(self):
        calls = []
        self.page._call = lambda method, body: (calls.append((method, body)) or {'targetInfo': self.row})
        self.assertTrue(self.match(self.page, 'https://www.youtube.com'))
        self.assertEqual(calls, [('Target.getTargetInfo', {'targetId': 'owned'})])


class BuiltRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.temp.name).resolve()/'built'
        sys.path.insert(0, str(ROOT/'tools'))
        import build
        build.build(build.config((ROOT/'config/example.json').read_bytes()), cls.out)
        sys.path.insert(0, str(cls.out/'spark'))
        ns = {'__file__': str(cls.out/'spark/manager.py'), '__name__': 'inert_manager'}
        exec(compile((cls.out/'spark/manager.py').read_text(), str(cls.out/'spark/manager.py'), 'exec'), ns)
        cls.manager_ns = ns

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.out/'spark')); sys.path.remove(str(ROOT/'tools')); cls.temp.cleanup()

    def test_manager_default_enables_control_channel_and_allows_opt_out(self):
        manager = self.manager_ns['Manager']()
        self.assertIsNotNone(manager.control_spawn)
        self.assertIs(manager.spawn, manager.control_spawn)
        disabled = self.manager_ns['Manager'](voice_control=False)
        self.assertIsNone(disabled.control_spawn)

    def test_actual_manager_opt_in_spawn_failure_closes_owned_capability(self):
        def fail(*args, **kwargs): raise OSError('synthetic spawn failure')
        manager = self.manager_ns['Manager'](spawn=fail, voice_control=True)
        with patch.object(threading, 'Thread'):
            code, body = manager.start()
        self.assertEqual(code, 202)
        manager.work(manager.session)
        self.assertEqual(manager.status(), {'state': 'idle'})
        client = manager.control_spawn.clients[body['id']]
        self.assertTrue(client.channel.closed); self.assertEqual(client.key, bytearray(32))

    def test_actual_manager_completed_child_closes_channel_before_idle(self):
        peers = []
        class Proc:
            stdout = io.BytesIO(b'')
            def poll(self): return 0
            def wait(self, timeout): return 0
        def spawn(argv, **kwargs):
            self.assertEqual(argv[-2], '--voice-control-fd')
            self.assertEqual(kwargs['pass_fds'], (int(argv[-1]),))
            peers.append(socket.socket(fileno=os.dup(int(argv[-1]))))
            return Proc()
        manager = self.manager_ns['Manager'](spawn=spawn, voice_control=True)
        with patch.object(threading, 'Thread'):
            _, body = manager.start()
            manager.work(manager.session)
        client = manager.control_spawn.clients[body['id']]
        self.assertTrue(client.channel.closed); self.assertEqual(client.key, bytearray(32))
        peers[0].settimeout(.2)
        self.assertEqual(peers[0].recv(1), b''); peers[0].close()
        self.assertIsNone(manager.session)

    def test_actual_trial_startup_failure_closes_attached_channel(self):
        runtime = self.out/'spark/runtime'
        sys.path.insert(0, str(runtime))
        try:
            spec = importlib.util.spec_from_file_location('owned_trial_under_test', runtime/'trial.py')
            trial = importlib.util.module_from_spec(spec); spec.loader.exec_module(trial)
            from voice_control import browser_spawn
            client, child = channel_pair('a'*32)
            descriptor = child.detach()
            observed = []
            original = browser_spawn.attach_trial
            def attach(instance, fd):
                pair = original(instance, fd); observed.append(pair); return pair
            with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
                owned = types.SimpleNamespace(cleanup=lambda: {'verified': True})
                for target, name, value in ((trial.os, 'getuid', lambda: 1000),
                    (trial.os, 'geteuid', lambda: 1000), (trial.platform, 'machine', lambda: 'aarch64'),
                    (trial.socket, 'gethostname', lambda: 'tv-compute'), (trial.os, 'access', lambda *a: True),
                    (trial.Path, 'is_file', lambda *a: True), (trial.resource, 'setrlimit', lambda *a: None),
                    (trial, 'ProcessOwner', lambda: owned), (trial, 'prepare_run', lambda: Path(temp)),
                    (browser_spawn, 'attach_trial', attach)):
                    stack.enter_context(patch.object(target, name, value))
                stack.enter_context(patch.object(trial, 'PrivateProfile', side_effect=RuntimeError('TEST_STARTUP')))
                stack.enter_context(patch.object(sys, 'argv', ['trial.py', '--run', '--voice-control-fd', str(descriptor)]))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                trial.main()
                self.assertEqual(len(observed), 1)
                host, thread = observed[0]
                self.assertTrue(host.channel.closed); self.assertFalse(thread.is_alive())
                self.assertEqual(json.loads((Path(temp)/'result.json').read_bytes())['error'], 'RuntimeError:TEST_STARTUP')
                client.channel.sock.settimeout(.2)
                self.assertEqual(client.channel.sock.recv(1), b'')
            client.close()
        finally:
            # main inserts its own sibling package directory only for the explicit FD.
            sys.path.remove(str(runtime))
            if sys.path.count(str(self.out/'spark')) > 1: sys.path.remove(str(self.out/'spark'))

    def test_trial_refuses_fd_outside_owned_run_before_native_start(self):
        for argv in (['--voice-control-fd', '3'], ['--run', '--voice-control-fd', '2'],
                     ['--run', '--preflight-only', '--voice-control-fd', '3'],
                     ['--run', '--input-worker', 'unused', '--voice-control-fd', '3']):
            p = subprocess.run([sys.executable, '-B', str(self.out/'spark/runtime/trial.py')] + argv,
                capture_output=True, timeout=3)
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn(b'voice control requires', p.stderr)


if __name__ == '__main__': unittest.main()
