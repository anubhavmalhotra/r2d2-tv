"""Offline voice launch/claim lifecycle, bounded startup and no display promotion."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from channel_adapter import ChannelBrowserAdapter
from controller import BrowserObservation, Controller, Outcome, Refused
from model_adapter import LocalIntentAdapter
from viewer_handoff import TVViewerHandoff

ROOT = Path(__file__).resolve().parents[2]
SID, OP, KEY = 'a'*32, 'b'*32+':start', 'c'*64
VIEWER = 'http://192.0.2.10:4177/' + 'd'*48 + '/viewer'


class HandoffTests(unittest.TestCase):
    def test_fixed_launch_fragment_and_no_replay_or_logging(self):
        calls, output = [], io.StringIO()
        def launch(payload, **kwargs):
            calls.append((payload, kwargs)); return {'returnValue': True}
        hook = TVViewerHandoff('192.0.2.10', KEY, launch, qualified=True)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(hook.open(SID, OP, timeout_s=1), 'accepted')
            self.assertEqual(hook.open(SID, OP, timeout_s=1), 'rejected')
        self.assertEqual(calls[0][0], {'id': 'com.webos.app.tinybrowser', 'params': {
            'contentTarget': 'http://192.0.2.10:4181/'+KEY+'/open#voice-session='+SID}})
        self.assertEqual(len(calls), 1); self.assertEqual(output.getvalue(), '')
        self.assertLessEqual(calls[0][1]['timeout_s'], 1)

    def test_defaults_bad_config_and_foreign_resume_no_launch(self):
        calls = []
        launch = lambda *a, **k: calls.append(a)
        self.assertEqual(TVViewerHandoff('192.0.2.10', KEY, launch).open(SID, OP, timeout_s=1), 'rejected')
        for ip in ('8.8.8.8', '127.0.0.1', '192.0.2.10:80', '::1', None):
            with self.assertRaises(ValueError): TVViewerHandoff(ip, KEY, launch, qualified=True)
        hook = TVViewerHandoff('192.0.2.10', KEY, launch, qualified=True)
        for url in (VIEWER+'?x=1', VIEWER+'#x', VIEWER.replace('192.0.2.10','192.0.2.11'), 'https://example.com', 1):
            self.assertEqual(hook.open(SID, OP, viewer_url=url, timeout_s=1), 'rejected')
        self.assertEqual(calls, [])

    def test_resume_bound_url_and_unknown_never_retry(self):
        calls = []
        def launch(body, **kwargs): calls.append(body); raise TimeoutError('private')
        hook = TVViewerHandoff('192.0.2.10', KEY, launch, qualified=True)
        self.assertEqual(hook.open(SID, OP, viewer_url=VIEWER, timeout_s=1), 'unknown')
        self.assertEqual(hook.open(SID, OP, viewer_url=VIEWER, timeout_s=1), 'rejected')
        self.assertEqual(calls, [{'id': 'com.webos.app.tinybrowser', 'params': {'contentTarget': VIEWER}}])

    def test_late_callback_is_unknown_and_fixed_false_is_rejected(self):
        for delay, result, expected in ((.03, {'returnValue':True}, 'unknown'),
                                        (0, {'returnValue':False}, 'rejected'),
                                        (0, {'returnValue':1}, 'unknown')):
            def launch(*a, **k): time.sleep(delay); return result
            hook = TVViewerHandoff('192.0.2.10', KEY, launch, qualified=True)
            self.assertEqual(hook.open(SID, OP, timeout_s=.02), expected)


class Clock:
    def __init__(self): self.now = 100.
    def __call__(self): return self.now
    def wait(self, amount): self.now += amount


class StartupTests(unittest.TestCase):
    def setup_controller(self, delay=40):
        clock = Clock(); calls = []
        runtime = types.SimpleNamespace(browser_enabled=True)
        def launch(*a, **k): calls.append('start'); return 'accepted'
        def navigate(*a, **k): calls.append('navigate'); return 'accepted'
        def observe(**kw):
            return (BrowserObservation(SID, True, True, False,
                'https://www.youtube.com/' if 'navigate' in calls else None)
                if clock.now >= 100+delay else None)
        runtime.launch_browser, runtime.navigate, runtime.browser_observation = launch, navigate, observe
        model = types.SimpleNamespace(interpret=lambda *a, **k: '{"action":"browser_open","destination":"youtube"}')
        controller = Controller(model, runtime, clock=clock, wait=clock.wait)
        return clock, calls, runtime, controller

    def test_slow_start_has_one_execution_window_and_accepts_without_pixel_claim(self):
        clock, calls, _, ctl = self.setup_controller()
        lease = ctl.arm(); result = ctl.execute(lease, 'open YouTube in browser')
        self.assertEqual(result, Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.assertEqual(calls, ['start', 'navigate']); self.assertGreaterEqual(clock.now, 140)
        self.assertEqual(lease.expires, 190); self.assertIsNone(ctl.active)

    def test_missing_readiness_is_finite_and_never_dispatches_navigation(self):
        clock, calls, _, ctl = self.setup_controller(1000)
        result = ctl.execute(ctl.arm(), 'open YouTube in browser')
        self.assertEqual(result.code, 'BROWSER_NOT_READY'); self.assertEqual(calls, ['start'])
        self.assertLessEqual(clock.now, 180.1)

    def test_expired_capture_and_cancellation_cannot_renew_or_dispatch(self):
        clock, calls, runtime, ctl = self.setup_controller()
        lease = ctl.arm(); clock.now = 131
        self.assertEqual(ctl.execute(lease, 'open YouTube in browser').code, 'REQUEST_EXPIRED')
        self.assertEqual(calls, []); self.assertEqual(lease.expires, 130)
        clock.now = 100
        old_wait = ctl.wait
        def wait(amount): old_wait(amount); ctl.cancel()
        ctl.wait = wait
        self.assertEqual(ctl.execute(ctl.arm(), 'open YouTube in browser').code, 'EXECUTION_UNCONFIRMED')
        self.assertEqual(calls, ['start'])

    def test_changed_session_or_unmatched_url_never_accepted(self):
        for foreign, url in ((True, 'https://www.youtube.com'), (False, 'https://example.com')):
            _, calls, runtime, ctl = self.setup_controller(0)
            original = runtime.browser_observation
            def observe(**kw):
                return (BrowserObservation('e'*32 if foreign else SID, True, True, True, url)
                        if 'navigate' in calls else original(**kw))
            runtime.browser_observation = observe
            self.assertEqual(ctl.execute(ctl.arm(), 'open YouTube in browser').state, 'unknown')

    def test_provider_errors_cross_package_as_fixed_refusals(self):
        class ExternalRefused(Exception): pass
        for error, expected in ((ExternalRefused('MODEL_SCHEMA'), 'MODEL_SCHEMA'),
                                (ExternalRefused('private transcript'), 'MODEL_UNAVAILABLE'),
                                (RuntimeError('private transcript'), 'MODEL_UNAVAILABLE')):
            _, calls, _, ctl = self.setup_controller()
            def fail(*a, **k): raise error
            ctl.model = LocalIntentAdapter(types.SimpleNamespace(interpret=fail), ExternalRefused)
            self.assertEqual(ctl.execute(ctl.arm(), 'private command'), Outcome('refused', expected))
            self.assertEqual(calls, [])


class RenderedManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.out = Path(cls.temp.name).resolve()/'build'
        sys.path.insert(0, str(ROOT/'tools'))
        import build
        build.build(build.config((ROOT/'config/example.json').read_bytes()), cls.out)
        sys.path.insert(0, str(cls.out/'spark'))
        cls.saved_modules = {key: value for key, value in sys.modules.items()
                             if key == 'voice_control' or key.startswith('voice_control.')}
        for key in cls.saved_modules: sys.modules.pop(key)
        cls.ns = {'__file__': str(cls.out/'spark/manager.py'), '__name__': 'offline_manager'}
        exec(compile((cls.out/'spark/manager.py').read_text(), str(cls.out/'spark/manager.py'), 'exec'), cls.ns)

    @classmethod
    def tearDownClass(cls):
        for key in list(sys.modules):
            if key == 'voice_control' or key.startswith('voice_control.'): sys.modules.pop(key)
        sys.modules.update(cls.saved_modules)
        sys.path.remove(str(cls.out/'spark')); sys.path.remove(str(ROOT/'tools')); cls.temp.cleanup()

    def quiet_manager(self):
        manager = self.ns['Manager']()
        with patch.object(threading, 'Thread'):
            _, body = manager.start()
        return manager, body['id']

    def test_actual_manager_claim_required_exact_id_and_changed_session_refused(self):
        manager, identity = self.quiet_manager()
        client = types.SimpleNamespace(session=identity, observe=lambda **kw: 'observed', queue_navigation=lambda *a, **kw: 'accepted')
        spawn = types.SimpleNamespace(client=lambda sid, **kw: client if sid == identity else None)
        hook = TVViewerHandoff('192.0.2.10', KEY, lambda *a, **kw: {'returnValue':True}, qualified=True)
        adapter = ChannelBrowserAdapter(manager, spawn, viewer=hook, qualified=True)
        # New adapter cannot adopt a session until a bounded start/resume.
        adapter.identity = identity
        manager.state = 'ready'; manager.session['url'] = VIEWER
        self.assertIsNone(adapter.browser_observation(timeout_s=1))
        self.assertEqual(adapter.launch_browser(OP, timeout_s=1), 'rejected')
        self.assertEqual(manager.command('claim', {'id': identity})[0], 200)
        self.assertEqual(adapter.launch_browser('e'*32+':start', timeout_s=1), 'accepted')
        self.assertEqual(adapter.browser_observation(timeout_s=1), 'observed')
        manager.session['id'] = 'f'*32
        self.assertIsNone(adapter.browser_observation(timeout_s=1))
        self.assertEqual(adapter.navigate(identity, 'https://www.youtube.com', 'e'*32+':navigate', timeout_s=1), 'rejected')

    def test_new_manager_start_is_handed_exact_id_and_rejected_launch_stops_only_owned(self):
        for changed in (False, True):
            manager = self.ns['Manager'](); seen = []
            def launch(body, **kw):
                seen.append(body)
                if changed: manager.session['id'] = 'f'*32
                return {'returnValue':False}
            hook = TVViewerHandoff('192.0.2.10', KEY, launch, qualified=True)
            adapter = ChannelBrowserAdapter(manager, object(), viewer=hook, qualified=True)
            with patch.object(threading, 'Thread'):
                self.assertEqual(adapter.launch_browser(OP, timeout_s=1), 'unknown')
            self.assertEqual(len(seen), 1)
            self.assertEqual(manager.session['stop'].is_set(), not changed)
            self.assertIsNone(adapter.identity)
            self.assertEqual(adapter.launch_browser(OP, timeout_s=1), 'rejected')

    def test_rendered_open_uses_no_referrer_and_csp_still_hashes_script(self):
        page = self.ns['open_page']().decode()
        self.assertIn('<meta name="referrer" content="no-referrer">', page)
        import base64, hashlib
        digest = base64.b64encode(hashlib.sha256((self.out/'spark/open.js').read_bytes()).digest()).decode()
        self.assertIn("script-src 'sha256-" + digest + "'", page)

    def test_actual_worker_fakeprocess_and_real_channel_navigate_after_tv_claim(self):
        from voice_control import controller as built_controller
        from voice_control.channel_adapter import ChannelBrowserAdapter as BuiltAdapter
        from voice_control.browser_spawn import attach_trial, close_trial
        from voice_control.trial_control import service_trial_control
        from voice_control.model_adapter import LocalIntentAdapter as BuiltModel
        # Run the actual Manager worker/lifecycle against synthetic scalar records
        # and a fake exact child process. No executable, browser, TV or LAN socket.
        child = types.SimpleNamespace(lock=threading.RLock(), stop=threading.Event(), page_ready=True,
            sandbox={'passed':True}, navigation_pending=None, navigation_active=False, input_epoch=0)
        child.queue_navigation = lambda body: True
        ready = threading.Event(); ended = threading.Event(); pairs = []; calls = []
        class Proc:
            def __init__(self): self.stdout = io.BytesIO(b'{"run_path":"/synthetic","ready":true}\n')
            def poll(self): return 0 if ended.is_set() else None
            def terminate(self): ended.set()
            def wait(self, timeout): ended.set(); return 0
        def spawn(argv, **kw):
            descriptor = os.dup(kw['pass_fds'][0]); pairs.append(attach_trial(child, descriptor)); ready.set()
            return Proc()
        manager = self.ns['Manager'](spawn=spawn, voice_control=True)
        def private_json(path, *args):
            if path.name == 'handoff.json': return {'token':'d'*48, 'url':VIEWER}
            if path.name == 'ready.json': return {'ready':True}
            return {'cleanup':{'verified':True}, 'privacy':{'owned_profile_cache_config_removed':True},
                    'audio_cleanup':dict.fromkeys(('ownedAbsent','defaultsUnchanged','privateDaemonsExited'),True)}
        def launch(payload, **kw):
            calls.append(payload)
            sid = re.search('#voice-session=([a-f0-9]{32})$', payload['params']['contentTarget'])[1]
            limit = time.monotonic()+kw['timeout_s']
            while time.monotonic()<limit:
                manager.command('lease', {'id':sid})
                if manager.command('claim', {'id':sid})[0] == 200: return {'returnValue':True}
                time.sleep(.02)
            return None
        hook = TVViewerHandoff('192.0.2.10', KEY, launch, qualified=True)
        adapter = BuiltAdapter(manager, manager.control_spawn, viewer=hook, qualified=True)
        def maintain():
            while not ended.is_set():
                if ready.wait(.01):
                    child.owner_verified_at = time.monotonic()
                    service_trial_control(pairs[0][0], child, types.SimpleNamespace(voice_target_matches=lambda _: True))
                time.sleep(.01)
        loop = threading.Thread(target=maintain); loop.start()
        try:
            with patch.dict(self.ns, {'private_json':private_json, 'validate_run':lambda _: Path('/synthetic')}):
                model = BuiltModel(types.SimpleNamespace(interpret=lambda *a, **kw: '{"action":"browser_open","destination":"youtube"}'), Refused)
                ctl = built_controller.Controller(model, adapter)
                result = ctl.execute(ctl.arm(), 'open YouTube in browser')
                self.assertEqual(result.state, 'accepted'); self.assertEqual(result.code, 'BROWSER_NAVIGATION_ACCEPTED')
                self.assertTrue(manager.session['claimed']); self.assertEqual(len(calls), 1)
                self.assertEqual(adapter.navigate('f'*32, 'https://www.youtube.com', 'f'*32+':navigate', timeout_s=1), 'rejected')
                manager.close()
        finally:
            ended.set(); manager.close(); loop.join(1)
            for host, thread in pairs: close_trial(host, thread)
        self.assertEqual(manager.status(), {'state':'idle'})
        self.assertIsNone(manager.session)


if __name__ == '__main__': unittest.main()
