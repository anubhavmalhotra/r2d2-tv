"""End-to-end integration tests for Channel v2, AgentController, and SharedVoiceOwner(agent_mode=True).

Exercises real UNIX socketpairs, TrialControlHost, service_trial_control,
ChannelBrowserAdapter, AgentController (all 14 tools), and SharedVoiceOwner
with agent_mode=True. Zero network sockets, zero disk persistence.
"""
import json
import ssl
import threading
import time
import types
import unittest
from unittest.mock import patch

from agent_controller import AgentController
from browser_channel import allowed_url, channel_pair
from browser_spawn import ControlledSpawn, attach_trial, close_trial
from channel_adapter import ChannelBrowserAdapter
from controller import Outcome
from test_shared_owner import SharedOwnerTests, VerifiedSocket
from trial_control import service_trial_control
from service import VoiceService


class LiveTrialStub:
    def __init__(self):
        self.lock = threading.RLock()
        self.page_ready = True
        self.stop = threading.Event()
        self.navigation_pending = None
        self.navigation_active = False
        self.owner_verified_at = time.monotonic()
        self.input_epoch = 0
        self.sandbox = {'passed': True}
        self.current_url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        self.surface = 'browser_youtube_watch'
        self.video = {'count': 1, 'paused': False}
        self.history_calls = 0
        self.media_calls = 0
        self.ended = False

    def queue_navigation(self, body):
        self.current_url = body['url']
        self.surface = 'browser_youtube' if 'youtube.com' in body['url'] else 'browser_other'
        self.input_epoch += 1
        return True

    def queue_history(self, body):
        if body != {'action': 'back'}:
            return False
        self.history_calls += 1
        self.input_epoch += 1
        return True

    def queue_media_key(self):
        self.media_calls += 1
        if self.video['paused'] is not None:
            self.video['paused'] = not self.video['paused']
        self.input_epoch += 1
        return True

    def queue_end(self):
        self.ended = True
        self.stop.set()
        self.page_ready = False
        return True


class ChannelV2AndAgentControllerTests(unittest.TestCase):
    def setUp(self):
        self.trial = LiveTrialStub()
        self.session_id = 'a' * 32
        self.client, child = channel_pair(self.session_id)
        self.host, self.thread = attach_trial(self.trial, child.detach())
        self.page = types.SimpleNamespace(
            voice_target_matches=lambda target: self.trial.current_url == target,
            voice_surface_probe=lambda: {
                'surfaceClass': self.trial.surface,
                'video': dict(self.trial.video),
            },
        )

    def tearDown(self):
        self.client.close()
        self.assertTrue(close_trial(self.host, self.thread))

    def _pump_host(self):
        self.trial.owner_verified_at = time.monotonic()
        service_trial_control(self.host, self.trial, self.page)

    def test_channel_v2_allowlist_supports_sites_searches_and_rejects_bangs(self):
        for good in (
            'https://www.youtube.com',
            'https://duckduckgo.com',
            'https://www.wikipedia.org',
            'https://news.ycombinator.com',
            'https://duckduckgo.com/?q=weather%20in%20springfield',
            'https://www.google.com/search?q=weather%20in%20springfield',
            'https://www.youtube.com/results?search_query=lo-fi+music',
        ):
            self.assertTrue(allowed_url(good), good)
        for bad in (
            'https://www.google.com/search?q=weather&source=voice',
            'https://www.google.com/search?q=weather#top',
            'https://duckduckgo.com/?q=!g',
            'https://duckduckgo.com/?q=%21g',
            'https://duckduckgo.com/?q=%2521g',
            'https://duckduckgo.com/?q=%5Cg',
            'https://duckduckgo.com/?q=%EF%BC%81g',
            'https://www.youtube.com/watch?v=123',
            'https://www.youtube.com/results?search_query=cats&sp=1',
        ):
            self.assertFalse(allowed_url(bad), bad)

    def test_channel_v2_observe_navigate_history_media_and_end_over_socketpair(self):
        # First observe triggers requested_at; next service_trial_control publishes surface/video.
        obs0 = self.client.observe(self.session_id, timeout_s=1)
        self.assertIsNotNone(obs0)
        self._pump_host()
        obs1 = self.client.observe(self.session_id, timeout_s=1)
        self.assertEqual(obs1['surfaceClass'], 'browser_youtube_watch')
        self.assertEqual(obs1['video'], {'count': 1, 'paused': False})

        # Navigate to YouTube search
        url = 'https://www.youtube.com/results?search_query=4k+60fps+hdr'
        self.assertEqual(
            self.client.navigate('1' * 32 + ':navigate', url, timeout_s=1),
            'accepted',
        )
        self._pump_host()
        obs2 = self.client.observe(self.session_id, timeout_s=1)
        self.assertTrue(obs2['ready'] and obs2['matched'])
        self.assertEqual(obs2['generation'], 1)
        self.assertEqual(obs2['surfaceClass'], 'browser_youtube')

        # History back
        self.assertEqual(
            self.client.history('2' * 32 + ':history', 'back', timeout_s=1),
            'accepted',
        )
        self.assertEqual(self.trial.history_calls, 1)
        self._pump_host()
        obs3 = self.client.observe(self.session_id, timeout_s=1)
        self.assertEqual(obs3['generation'], 2)

        # Media key
        self.trial.surface = 'browser_youtube_watch'
        self.assertEqual(
            self.client.media_key('3' * 32 + ':media', timeout_s=1),
            'accepted',
        )
        self.assertEqual(self.trial.media_calls, 1)
        self._pump_host()
        obs4 = self.client.observe(self.session_id, timeout_s=1)
        self.assertIs(obs4['video']['paused'], True)

        # End session
        self.assertEqual(
            self.client.end('4' * 32 + ':end', timeout_s=1),
            'accepted',
        )
        self.assertTrue(self.trial.ended)

    def test_agent_controller_14_tool_browser_execution(self):
        # Set up a fake Manager + ControlledSpawn + ChannelBrowserAdapter bound to self.client
        def observe_status():
            if self.trial.stop.is_set():
                manager.state = 'idle'
                manager.session = None
            return {'state': manager.state}

        manager = types.SimpleNamespace(
            lock=threading.RLock(),
            state='ready',
            session={'id': self.session_id, 'claimed': True, 'url': 'http://192.0.2.10:4177/' + 'd' * 48 + '/viewer',
                     'process': types.SimpleNamespace(poll=lambda: 0 if self.trial.stop.is_set() else None)},
            status=observe_status,
            command=lambda name, body: setattr(manager, 'state', 'idle') or setattr(manager, 'session', None) or (200, {'state': 'idle'}),
        )
        spawn = ControlledSpawn('/unused/trial.py')
        spawn.bind_manager(manager)
        spawn.clients[self.session_id] = self.client
        viewer = types.SimpleNamespace(enabled=True, open=lambda *a, **k: 'accepted')
        adapter = ChannelBrowserAdapter(manager, spawn, viewer=viewer, qualified=True)

        # Background pump to simulate Trial's main loop publishing fresh observations
        stop_pump = threading.Event()
        def pump_loop():
            while not stop_pump.is_set():
                if not self.trial.stop.is_set():
                    try:
                        self._pump_host()
                    except Exception:
                        break
                time.sleep(0.02)
        pump_thread = threading.Thread(target=pump_loop, daemon=True)
        pump_thread.start()
        self.addCleanup(lambda: (stop_pump.set(), pump_thread.join(1)))

        responses = {
            'pause the video': json.dumps({'tool': 'control.play_pause', 'args': {'action': 'pause'}}),
            'search youtube for jazz': json.dumps({'tool': 'browser.youtube_search', 'args': {'query': 'jazz'}}),
            'go back': json.dumps({'tool': 'control.back', 'args': {}}),
            'what is playing': json.dumps({'tool': 'meta.whats_playing', 'args': {}}),
            'close the browser': json.dumps({'tool': 'browser.close', 'args': {}}),
        }
        now = [1000.0]
        def clock():
            return now[0]
        def wait(seconds):
            now[0] += max(0.0, seconds)
            time.sleep(min(seconds, 0.03))

        model = types.SimpleNamespace(interpret=lambda text, *, timeout_s: responses[text])
        controller = AgentController(model, adapter, clock=clock, wait=wait)

        self._pump_host()
        self.assertEqual(
            controller.execute(controller.arm(), 'pause the video'),
            Outcome('verified', 'BROWSER_MEDIA_VERIFIED'),
        )
        now[0] += 20.0
        self.assertEqual(
            controller.execute(controller.arm(), 'what is playing'),
            Outcome('accepted', 'BROWSER_STATUS_SHOWN'),
        )
        now[0] += 20.0
        self.assertEqual(
            controller.execute(controller.arm(), 'search youtube for jazz'),
            Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'),
        )
        now[0] += 20.0
        self.assertEqual(
            controller.execute(controller.arm(), 'go back'),
            Outcome('accepted', 'BROWSER_HISTORY_ACCEPTED'),
        )
        now[0] += 20.0
        self.assertEqual(
            controller.execute(controller.arm(), 'close the browser'),
            Outcome('verified', 'BROWSER_SESSION_ENDED'),
        )
        # Closing again when already idle must refuse with BROWSER_NOT_OPENED and never auto-launch!
        now[0] += 20.0
        self.assertEqual(
            controller.execute(controller.arm(), 'close the browser'),
            Outcome('refused', 'BROWSER_NOT_OPENED'),
        )
        self.assertEqual(manager.state, 'idle')

    def test_tv_gateway_security_trip_pauses_agent_and_prompt_sha_is_verified(self):
        from controller import Refused
        from local_qwen import LocalQwen, prompt_for_phase
        # 1. Prompt-aware LocalQwen is automatically configured to P5 14-tool prompt
        qwen = LocalQwen({'endpoint': 'http://127.0.0.1:8000/v1/chat/completions', 'model': 'Qwen3-4B'})
        self.assertNotIn('browser.open_site', qwen.system_prompt)
        viewer = types.SimpleNamespace(enabled=True, open=lambda *a, **k: 'accepted')
        manager = types.SimpleNamespace(lock=threading.RLock(), state='idle', session=None, status=lambda: {'state': 'idle'})
        spawn = ControlledSpawn('/unused/trial.py'); spawn.bind_manager(manager)
        adapter = ChannelBrowserAdapter(manager, spawn, viewer=viewer, qualified=True)

        # 2. A model with a fixed wrong prompt_sha256 and no configure_prompt is refused
        bad_model = types.SimpleNamespace(interpret=lambda t, *, timeout_s: '{}', prompt_sha256='0' * 64)
        with self.assertRaises(Refused):
            AgentController(bad_model, adapter)

        # 3. TVGateway trip (CERT_CHANGED / PAIRING_PROMPT) pauses AgentController
        from agent_controller import InMemoryKillSwitch
        gw_ks = InMemoryKillSwitch()
        tv_gw = types.SimpleNamespace(
            killswitch=gw_ks,
            on_trip=None,
            surface=lambda: 'tv_home',
            create_toast=lambda *a, **k: 'accepted',
        )
        ctrl = AgentController(qwen, adapter, tv_gateway=tv_gw)
        self.assertIn('browser.open_site', qwen.system_prompt)
        self.assertEqual(qwen.system_prompt, prompt_for_phase('P5'))
        armed = ctrl.arm()
        gw_ks.set_tv_off('CERT_CHANGED')
        gw_ks.pause('CERT_CHANGED')
        self.assertTrue(ctrl.killswitch.paused())
        self.assertEqual(
            ctrl.execute(armed, 'open youtube'),
            Outcome('refused', 'AGENT_PAUSED'),
        )
        with self.assertRaises(Refused) as ctx:
            ctrl.arm()
        self.assertEqual(str(ctx.exception), 'AGENT_PAUSED')

    def test_stale_cancel_does_not_disrupt_newer_invocation_and_midflight_cancel_rejects(self):
        starts = []
        manager = types.SimpleNamespace(
            lock=threading.RLock(),
            state='ready',
            session={'id': self.session_id, 'claimed': True, 'url': 'http://192.0.2.10:4177/' + 'd' * 48 + '/viewer',
                     'process': types.SimpleNamespace(poll=lambda: None)},
            status=lambda: {'state': manager.state},
            start=lambda: starts.append(1) or (202, {'id': self.session_id}),
        )
        spawn = ControlledSpawn('/unused/trial.py')
        spawn.bind_manager(manager)
        spawn.clients[self.session_id] = self.client
        viewer = types.SimpleNamespace(enabled=True, open=lambda *a, **k: 'accepted')
        adapter = ChannelBrowserAdapter(manager, spawn, viewer=viewer, qualified=True)

        controller_ref = []
        lease_ref = []
        def model_interpret(text, *, timeout_s):
            return json.dumps({'tool': 'browser.youtube_search', 'args': {'query': 'lofi'}})

        ctrl = AgentController(types.SimpleNamespace(interpret=model_interpret), adapter)
        controller_ref.append(ctrl)

        # Stale lease cancel must return False and not clear active invocation state
        stale_lease = ctrl.arm()
        fresh_lease = ctrl.arm()
        self.assertFalse(ctrl.cancel(stale_lease))

        # Mid-flight cancel right before adapter.navigate must reject without mutating trial
        orig_current = adapter._current
        def cancel_during_lookup(*args, **kwargs):
            ctrl.cancel(fresh_lease)
            return orig_current(*args, **kwargs)
        self._pump_host()
        before_url = self.trial.current_url
        with patch.object(adapter, '_current', side_effect=cancel_during_lookup):
            outcome = ctrl.execute(fresh_lease, 'search youtube for lofi')
        self.assertEqual(outcome, Outcome('refused', 'REQUEST_EXPIRED'))
        self.assertEqual(self.trial.current_url, before_url)

    def test_delayed_model_inference_pauses_live_youtube_video_after_1s_probe_window(self):
        stop = threading.Event()
        manager = types.SimpleNamespace(
            lock=threading.RLock(),
            state='ready',
            session={
                'id': self.session_id,
                'claimed': True,
                'process': types.SimpleNamespace(poll=lambda: None),
            },
        )
        manager.status = lambda: {'state': manager.state}
        spawn = ControlledSpawn('/unused/trial.py')
        spawn.bind_manager(manager)
        spawn.clients[self.session_id] = self.client
        adapter = ChannelBrowserAdapter(manager, spawn, viewer=types.SimpleNamespace(enabled=True), qualified=True)

        def pump_loop():
            while not stop.is_set():
                self._pump_host()
                stop.wait(0.15)

        pump = threading.Thread(target=pump_loop, daemon=True)
        pump.start()
        try:
            self.client.observe(self.session_id, timeout_s=1)
            time.sleep(0.2)
            initial = self.client.observe(self.session_id, timeout_s=1)
            self.assertEqual(initial['surfaceClass'], 'browser_youtube_watch')
            self.assertEqual(initial['video'], {'count': 1, 'paused': False})

            def delayed_interpret(text, *, timeout_s):
                time.sleep(1.3)
                return '{"tool":"control.play_pause","args":{"action":"pause"}}'

            ctrl = AgentController(types.SimpleNamespace(interpret=delayed_interpret), adapter)
            observed = ctrl.execute(ctrl.arm(), 'pause the video')
            self.assertEqual(observed, Outcome('verified', 'BROWSER_MEDIA_VERIFIED'))
            self.assertEqual(self.trial.media_calls, 1)
        finally:
            stop.set()
            pump.join(1)


class SharedOwnerAgentModeTests(SharedOwnerTests):
    def test_shared_voice_owner_agent_mode_executes_open_youtube_in_browser(self):
        owner = self.assemble()
        # Re-create with agent_mode=True using a fresh worker
        owner.close()
        self.owners.pop()

        from model_worker import ModelWorker
        from shared_owner import SharedVoiceOwner
        from test_sealed_startup import ready
        import subprocess, sys

        def spawn(argv, **kwargs):
            process = subprocess.Popen(
                [sys.executable, '-B', str(self.stub), json.dumps(['ordinary', ready(argv[-1])])],
                **kwargs,
            )
            self.processes.append(process)
            return process

        worker = ModelWorker(str(sys.executable), '/synthetic/asr', '/synthetic/intent', qualified=True, spawn=spawn)
        manager = self.rendered.Manager(voice_control=True)
        self.managers.append(manager)
        selected = {'url': None, 'gen': 0}

        def launch(payload, **kwargs):
            self.calls.append(payload)
            identity = manager.session['id']
            manager.session['process'] = types.SimpleNamespace(poll=lambda: None)
            manager.session['url'] = 'http://192.0.2.10:4177/' + 'd' * 48 + '/viewer'
            manager.state = 'ready'
            manager.command('claim', {'id': identity})

            def queue(body, **kwargs):
                selected['url'] = body['url']
                selected['gen'] += 1
                return 'accepted'

            def obs(session=None, **kwargs):
                if session is not None:
                    return {
                        'ready': True,
                        'matched': selected['url'] == 'https://www.youtube.com',
                        'generation': selected['gen'],
                        'surfaceClass': 'browser_youtube' if selected['url'] else 'browser_other',
                        'video': {'count': 0, 'paused': None},
                    }
                return self.Observation(identity, True, True, False, selected['url'])

            manager.control_spawn.clients[identity] = types.SimpleNamespace(
                session=identity,
                channel=types.SimpleNamespace(closed=False),
                observe=obs,
                queue_navigation=queue,
            )
            return {'returnValue': True}

        viewer = self.Handoff('192.0.2.10', 'c' * 64, launch, qualified=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        agent_owner = SharedVoiceOwner(
            manager, worker, viewer, 'c' * 64, '192.0.2.10', 4443, context, 'a' * 64,
            b'k' * 32, enabled=True, agent_mode=True,
        )
        agent_owner.ingress.challenge = lambda _: b'q' * 32
        self.owners.append(agent_owner)
        agent_owner.warm(timeout_s=2)

        observed = []
        def serve(service, **kwargs):
            with patch.object(self.rendered.threading, 'Thread'):
                observed.append(service.ingress._verified_connection(VerifiedSocket()))
        with patch.object(VoiceService, 'serve', serve):
            agent_owner.serve()
        self.assertEqual((observed[0].state, observed[0].code), ('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.assertEqual(selected['url'], 'https://www.youtube.com')

    def test_browser_startup_exceeding_30s_admission_lease_succeeds_within_140s_execution_window(self):
        class Clock:
            def __init__(self):
                self.t = 1000.0
            def __call__(self):
                return self.t
            def wait(self, s):
                self.t += s

        clock = Clock()
        calls = []
        sid = 'a' * 32
        manager = types.SimpleNamespace(lock=threading.RLock(), state='idle', session=None)

        def status():
            if manager.state == 'starting' and clock.t >= 1035.0:
                manager.state = 'ready'
                manager.session['claimed'] = True
            return {'state': manager.state}

        def start():
            manager.state = 'starting'
            manager.session = {'id': sid, 'claimed': False}
            calls.append('start')
            return 202, {'id': sid}

        manager.status = status
        manager.start = start
        gen = [0]

        def nav(body, **kw):
            calls.append('navigate')
            gen[0] += 1
            return 'accepted'

        client = types.SimpleNamespace(
            session=sid,
            observe=lambda *a, **k: dict(
                ready=True,
                matched=gen[0] > 0,
                generation=gen[0],
                surfaceClass='browser_other',
                video={'count': 0, 'paused': None},
            ),
            queue_navigation=nav,
        )
        adapter = ChannelBrowserAdapter(
            manager,
            types.SimpleNamespace(client=lambda *a, **k: client),
            viewer=types.SimpleNamespace(enabled=True, open=lambda *a, **k: calls.append('viewer.open') or 'accepted'),
            qualified=True,
        )
        ctrl = AgentController(
            types.SimpleNamespace(interpret=lambda *a, **k: '{"tool":"browser.open_site","args":{"site":"youtube"}}'),
            adapter,
            clock=clock,
            wait=clock.wait,
        )
        lease = ctrl.arm()
        result = ctrl.execute(lease, 'open youtube')
        self.assertEqual(result, Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.assertEqual(calls, ['start', 'viewer.open', 'navigate'])

    def test_rejected_auto_launch_and_mid_poll_cancel_exit_without_60s_wait(self):
        class Clock:
            def __init__(self):
                self.t = 1000.0
                self.on_wait = None
            def __call__(self):
                return self.t
            def wait(self, s):
                self.t += s
                if self.on_wait is not None:
                    self.on_wait()

        # 1. Rejected manager.start() during auto_launch fails immediately (< 1s)
        clock = Clock()
        manager = types.SimpleNamespace(
            lock=threading.RLock(),
            state='idle',
            session=None,
            status=lambda: {'state': manager.state},
            start=lambda: (409, {'error': 'busy'}),
        )
        adapter = ChannelBrowserAdapter(
            manager,
            types.SimpleNamespace(client=lambda *a, **k: None),
            viewer=types.SimpleNamespace(enabled=True, open=lambda *a, **k: 'accepted'),
            qualified=True,
        )
        ctrl = AgentController(
            types.SimpleNamespace(interpret=lambda *a, **k: '{"tool":"browser.open_site","args":{"site":"youtube"}}'),
            adapter,
            clock=clock,
            wait=clock.wait,
        )
        t0 = clock.t
        result = ctrl.execute(ctrl.arm(), 'open youtube')
        self.assertEqual(result, Outcome('refused', 'BROWSER_NOT_OPENED'))
        self.assertLess(clock.t - t0, 2.0)

        # 2. Cancelling while waiting for open.js claim exits _owner_open on the next poll tick
        clock2 = Clock()
        sid = 'b' * 32
        manager2 = types.SimpleNamespace(
            lock=threading.RLock(),
            state='idle',
            session=None,
            status=lambda: {'state': manager2.state},
            start=lambda: setattr(manager2, 'state', 'starting') or setattr(manager2, 'session', {'id': sid, 'claimed': False}) or (202, {'id': sid}),
        )
        adapter2 = ChannelBrowserAdapter(
            manager2,
            types.SimpleNamespace(client=lambda *a, **k: None),
            viewer=types.SimpleNamespace(enabled=True, open=lambda *a, **k: 'accepted'),
            qualified=True,
        )
        ctrl2 = AgentController(
            types.SimpleNamespace(interpret=lambda *a, **k: '{"tool":"browser.open_site","args":{"site":"youtube"}}'),
            adapter2,
            clock=clock2,
            wait=clock2.wait,
        )
        lease2 = ctrl2.arm()
        clock2.on_wait = lambda: ctrl2.cancel(lease2) if manager2.state == 'starting' else None
        t1 = clock2.t
        result2 = ctrl2.execute(lease2, 'open youtube')
        self.assertEqual(result2, Outcome('unknown', 'EXECUTION_UNCONFIRMED'))
        self.assertLess(clock2.t - t1, 2.0)


if __name__ == '__main__':
    unittest.main()

