"""Production assembly with actual controller/gateway/pool, inert SSAP peers."""
import json
import hmac
import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import struct
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from voice.backend import resident_manager as rm
from voice.backend.controller import Outcome
from voice.backend.ingress import Device, VoiceIngress, DOMAIN
from voice.backend.resident_utterance import _UtteranceController
from voice.backend import channel_adapter
from voice.backend.voice_status import StatusFeedback, VoiceStatus
from voice.runtime.pool import WorkerPool, create_warm_hook

KEY, PIN, MANAGER_KEY = 'a' * 64, 'b' * 64, 'c' * 64
SID, TOKEN = 'd' * 32, 'e' * 48


class Clock:
    def __init__(self): self.now = 1000.
    def __call__(self): return self.now
    def wait(self, seconds): self.now += seconds


class Worker:
    qualified = True
    def __init__(self, clock):
        self.created = clock()
        self.process = types.SimpleNamespace(poll=lambda: None)
        self.sealed = {'synthetic': True}
        self.counter = 0
        self.call = Mock(return_value='{"tool":"tv.volume","args":{"direction":"up","steps":1}}')
    def close(self): return True


class Peer:
    def __init__(self):
        self.connections = []
        self.calls = []
        self.power = 'Active'
        self.volume = 10
        self.foreground = 'com.r2d2.home'
        self.on_connect = self.on_register = self.on_request = None
        self.registration = {'client-key': KEY}
        self.launch_reply = {'returnValue': True}
        self.exception = None
    def factory(self, address):
        peer = self
        class Transport:
            def __init__(self): self.calls = []; self.closed = False
            def connect(self, **kw):
                peer.calls.append(('connect', kw)); self.calls.append('connect')
                self.closed = False
                if peer.on_connect: peer.on_connect()
                if peer.exception: raise peer.exception
            def register(self, **kw):
                peer.calls.append(('register', kw)); self.calls.append('register')
                if peer.on_register: peer.on_register()
                return peer.registration
            def request(self, uri, body, **kw):
                self.calls.append(uri); peer.calls.append((uri, body, kw))
                if peer.on_request: peer.on_request()
                if uri.endswith('getPowerState'): return {'returnValue': True, 'state': peer.power}
                if uri.endswith('getForegroundAppInfo'): return {'returnValue': True, 'appId': peer.foreground}
                if uri.endswith('listApps'): return {'returnValue': True, 'apps': [{'id': 'netflix'}]}
                if uri.endswith('getVolume'): return {'returnValue': True, 'volume': peer.volume}
                if uri.endswith('volumeUp'): peer.volume += 1
                if uri.endswith('/launch'):
                    peer.foreground = body['id']
                    return peer.launch_reply
                return {'returnValue': True}
            def close(self): self.closed = True
        out = Transport()
        self.connections.append(out)
        return out


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='resident-manager-')
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name).resolve() / 'key.json'
        self.pin = Path(self.temp.name).resolve() / 'pin.json'
        self.key.write_text(json.dumps({'client-key': KEY})); self.key.chmod(0o600)
        self.pin.write_text(json.dumps({'sha256': PIN})); self.pin.chmod(0o600)
        self.clock = Clock()
        self.worker = Worker(self.clock)
        self.factory = Mock(return_value=self.worker)
        self.pool = WorkerPool(self.factory, clock=self.clock)
        self.hook = create_warm_hook(self.pool, clock=self.clock)
        self.hook.start = Mock()
        self.hook.close = Mock()
        self.manager = types.SimpleNamespace(lock=threading.RLock(), state='idle', session=None)
        self.manager.status = lambda: {'state': self.manager.state}
        self.manager.start = Mock(side_effect=self.start_browser)
        self.client = Mock()
        self.manager.control_spawn = types.SimpleNamespace(manager=self.manager, client=lambda *a, **k: self.client)
        self.peer = Peer()
        self.owner = rm.ResidentManager(
            manager=self.manager, spark_ip='192.0.2.10', tv_ip='192.0.2.20', manager_key=MANAGER_KEY,
            ssap_key_path=self.key, ssap_pin_path=self.pin, hook=self.hook,
            enrollment=object(), identity_loader=lambda: None, voice_key='f' * 64,
            transport_factory=self.peer.factory, clock=self.clock)
        self.addCleanup(self.owner.close)

    def start_browser(self):
        self.manager.state = 'ready'
        self.manager.session = {'id': SID, 'claimed': True,
                                'url': 'http://192.0.2.10:4177/' + TOKEN + '/viewer',
                                'process': types.SimpleNamespace(poll=lambda: None)}
        return 202, {'id': SID}

    def ready(self):
        self.pool.active = self.worker
        self.hook.note_tv_activity()
        self.hook.controller.state = 'ready'
        self.owner.controller.wait = self.clock.wait
        self.owner.controller.tv.wait = self.clock.wait
        self.owner.controller.browser.wait = self.clock.wait

    def payload(self, target=None):
        return {'id': 'com.webos.app.tinybrowser', 'params': {
            'contentTarget': target or self.owner.viewer.open_url + '#voice-session=' + SID}}

    def test_constructor_is_inert_and_one_controller_prepares_p5_before_warm(self):
        self.factory.assert_not_called(); self.hook.start.assert_not_called()
        self.assertEqual(self.peer.calls, [])
        self.assertEqual(self.pool.prompt_phase, 'P5')
        self.assertIs(self.owner.controller, self.owner.resident._controller)
        self.assertIs(self.owner.controller_factory(self.owner._intent), self.owner.resident._controller)
        with self.assertRaisesRegex(ValueError, 'RESIDENT_MODEL_CHANGED'):
            self.owner.controller_factory(object())
        self.assertIs(self.owner.runtime.manager, self.manager)
        self.assertIs(self.owner.runtime.control_spawn, self.manager.control_spawn)
        self.assertIs(self.owner.controller.tv.gateway, self.owner.gateway)
        self.assertIs(self.hook.source.ssap, self.owner.power_reader)
        self.assertIsNone(self.owner.power_reader.current())
        self.assertEqual(self.peer.calls, [])

    def test_actual_persistent_agent_executes_global_volume_without_browser_launch(self):
        self.ready()
        controller = self.owner.controller
        result = controller.execute(controller.arm(), 'turn the volume up')
        self.assertEqual(result, Outcome('verified', 'TV_VOLUME_VERIFIED'))
        self.assertEqual(self.peer.volume, 11)
        self.manager.start.assert_not_called()
        self.assertIsNone(self.pool.leased)
        self.assertFalse(self.owner._gateway_transport.closed)
        self.assertEqual(sum('register' in t.calls for t in self.peer.connections), 1)
        self.clock.now += 10
        controller.execute(controller.arm(), 'turn the volume up')
        self.assertEqual(self.peer.volume, 12)
        self.assertEqual(sum('register' in t.calls for t in self.peer.connections), 1)
        self.assertIs(controller, self.owner.resident._controller)

    def test_actual_ingress_cold_browser_result_fits_tv_terminal_wait(self):
        # Actual ingress/pool/controller/adapter; only worker and external peers
        # are inert. Every delay stays inside its existing operation contract.
        self.clock.now = 100000.
        self.worker.created = self.clock()
        self.ready()
        began = self.clock()
        def call(kind, value, *, timeout_s):
            duration = 7.9 if kind == 'asr' else 4.9
            self.assertLess(duration, timeout_s)
            self.clock.wait(duration)
            return ('open YouTube in the browser' if kind == 'asr' else
                    '{"tool":"browser.open_site","args":{"site":"youtube"}}')
        self.worker.call.side_effect = call
        def peer_step():
            self.clock.wait(2.9 if len(self.peer.connections) == 1 else .05)
        self.peer.on_connect = self.peer.on_register = self.peer.on_request = peer_step
        start_at, navigation_at = [None], [None]
        def start():
            start_at[0] = self.clock()
            self.manager.state = 'starting'
            self.manager.session = {'id': SID, 'claimed': False,
                                    'url': 'http://192.0.2.10:4177/' + TOKEN + '/viewer'}
            return 202, {'id': SID}
        def status():
            if start_at[0] is not None and self.clock() - start_at[0] >= 74:
                self.manager.state = 'ready'
                self.manager.session['claimed'] = True
            return {'state': self.manager.state}
        self.manager.start.side_effect, self.manager.status = start, status
        self.client.session = SID
        self.client.observe.side_effect = lambda *a, **k: {
            'ready': True, 'matched': navigation_at[0] is not None and self.clock() - navigation_at[0] >= 9,
            'generation': int(navigation_at[0] is not None), 'surfaceClass': 'browser_youtube',
            'video': {'count': 0, 'paused': None}}
        def navigate(body, **kw):
            self.assertGreater(kw['timeout_s'], 4.8)
            self.clock.wait(4.8); navigation_at[0] = self.clock()
            return 'accepted'
        self.client.queue_navigation.side_effect = navigate
        key, challenge = b'k' * 32, b'q' * 32
        hello = json.dumps({'schema': 1, 'mode': 'command', 'rate': 16000, 'channels': 1,
                            'format': 's16le', 'tag': hmac.digest(key, DOMAIN + challenge, 'sha256').hex()}).encode()
        wire = (struct.pack('>I', len(hello)) + hello + struct.pack('>II', 0, 640)
                + bytes(640) + struct.pack('>II', 1, 0))
        class Socket:
            data = wire
            def settimeout(self, value): pass
            def sendall(self, data): pass
            def recv(self, size):
                out, self.data = self.data[:size], self.data[size:]
                return out
        controller = _UtteranceController(self.owner.controller, self.owner.resident._coordinator, lambda: True)
        ingress = VoiceIngress(controller, Device('a' * 64, key), self.owner.resident._asr,
                               clock=self.clock, challenge=lambda count: challenge)
        try:
            with patch.object(channel_adapter, 'time', types.SimpleNamespace(monotonic=self.clock, sleep=self.clock.wait)):
                result = ingress._verified_connection(Socket())
        finally:
            self.owner.resident._coordinator.release()
        self.assertEqual(result, Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        elapsed = self.clock() - began
        self.assertGreater(elapsed, 105)
        import re
        client_source = Path(__file__).resolve().parents[1] / 'tv/transport/tls_pcm_client.js'
        terminal_ms = int(re.search(r'RESULT_TIMEOUT_MS=(\d+)', client_source.read_text())[1])
        self.assertLess(elapsed * 1000, terminal_ms)
        self.assertLessEqual(terminal_ms, 200000)
        self.assertIsNone(self.pool.leased)

    def test_status_feedback_replaces_ssap_toasts_and_keeps_enum_confirmations(self):
        self.ready()
        controller = self.owner.controller
        self.assertIsInstance(controller.feedback, StatusFeedback)
        self.assertIs(controller.voice_status, self.owner.voice_status)
        self.assertIs(self.owner.resident.voice_status, self.owner.voice_status)
        self.peer.volume = 35
        self.assertEqual(controller.execute(controller.arm(), 'turn the volume up'),
                         Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(self.peer.volume, 35)
        self.clock.now += 1
        self.worker.call.return_value = '{"tool":"tv.open_app","args":{"app":"netflix"}}'
        self.assertEqual(controller.execute(controller.arm(), 'open Netflix'),
                         Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertFalse([row for row in self.peer.calls if 'createToast' in str(row[0])])
        # Free text still cannot be confirmed without a surface that can show it.
        self.worker.call.return_value = ('{"tool":"tv.open_content","args":{"app":"apple_tv",'
                                         '"kind":"title","text":"severance"}}')
        self.assertEqual(controller.execute(controller.arm(), 'open severance on apple tv'),
                         Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))

    def test_agent_events_reach_voice_status_for_the_open_utterance_only(self):
        self.ready()
        status, controller = self.owner.voice_status, self.owner.controller
        self.worker.call.return_value = '{"tool":"tv.open_app","args":{"app":"netflix"}}'
        token = status.begin_utterance()
        seen = []
        original = status.doing
        status.doing = lambda tool, slot: seen.append((tool, slot, self.peer.foreground)) or original(tool, slot)
        result = controller.execute(controller.arm(), 'open Netflix')
        self.assertEqual(seen, [('tv.open_app', 'app:netflix', 'com.r2d2.home')])  # before the launch
        status.outcome(token, result.state, result.code)
        self.assertEqual(status.home_view()['headline'], 'Netflix is open.')
        # A superseded lease's late events are dropped.
        stale = controller.arm()
        controller.arm()
        before = status.home_view()['seq']
        controller._agent_event(stale, 'doing', {'tool': 'tv.mute', 'slot': 'mute:on'})
        self.assertEqual(status.home_view()['seq'], before)

    def test_supplied_voice_status_is_shared_before_the_utterance_server_exists(self):
        status = VoiceStatus(clock=self.clock)
        seen = []
        original = rm.ResidentUtteranceServer
        def server(**kw):
            seen.append(kw['voice_status'])
            return original(**kw)
        with patch.object(rm, 'ResidentUtteranceServer', side_effect=server):
            owner = rm.ResidentManager(
                manager=self.manager, spark_ip='192.0.2.10', tv_ip='192.0.2.20', manager_key=MANAGER_KEY,
                ssap_key_path=self.key, ssap_pin_path=self.pin, hook=self.hook,
                enrollment=object(), identity_loader=lambda: None, voice_key='f' * 64,
                transport_factory=self.peer.factory, clock=self.clock, voice_status=status)
        self.addCleanup(owner.close)
        self.assertEqual(seen, [status])
        self.assertIs(owner.controller.voice_status, status)
        self.assertIs(owner.controller.feedback.status, status)

    def test_actual_agent_launches_global_app_via_saved_gateway_not_browser(self):
        self.ready()
        self.worker.call.return_value = '{"tool":"tv.open_app","args":{"app":"netflix"}}'
        result = self.owner.controller.execute(self.owner.controller.arm(), 'open Netflix')
        self.assertEqual(result, Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(self.peer.foreground, 'netflix')
        self.manager.start.assert_not_called()
        launches = [row[1] for row in self.peer.calls if row[0].endswith('/launch')]
        self.assertEqual(launches, [{'id': 'netflix'}])

    def test_actual_adapter_new_and_existing_browser_handoff_uses_dedicated_connections(self):
        self.assertEqual(self.owner.runtime.launch_browser('1' * 32 + ':start', timeout_s=2), 'accepted')
        self.manager.start.assert_called_once()
        launches = [row[1] for row in self.peer.calls if row[0].endswith('/launch')]
        self.assertEqual(launches, [self.payload()])
        self.assertEqual(self.owner.runtime.launch_browser('2' * 32 + ':start', timeout_s=2), 'accepted')
        launches = [row[1] for row in self.peer.calls if row[0].endswith('/launch')]
        self.assertEqual(launches[-1], self.payload(self.manager.session['url']))
        self.assertEqual(self.peer.connections[0].calls, [])
        self.assertTrue(all(t.closed for t in self.peer.connections[1:]))

    def test_actual_p5_browser_route_launches_viewer_then_owned_navigation(self):
        self.ready()
        self.worker.call.return_value = '{"tool":"browser.open_site","args":{"site":"youtube"}}'
        generation = [0]
        self.client.session = SID
        self.client.observe.side_effect = lambda *a, **k: {
            'ready': True, 'matched': True, 'generation': generation[0],
            'surfaceClass': 'browser_youtube', 'video': {'count': 0, 'paused': None}}
        def navigate(body, **kwargs):
            generation[0] += 1
            return 'accepted'
        self.client.queue_navigation.side_effect = navigate
        result = self.owner.controller.execute(self.owner.controller.arm(), 'open YouTube in the browser')
        self.assertEqual(result, Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.manager.start.assert_called_once()
        self.client.queue_navigation.assert_called_once()
        self.assertEqual(self.client.queue_navigation.call_args.args, ({'url': 'https://www.youtube.com'},))
        self.assertTrue(self.client.queue_navigation.call_args.kwargs['operation'].endswith(':navigate'))
        launches = [row[1] for row in self.peer.calls if row[0].endswith('/launch')]
        self.assertEqual(launches, [self.payload()])
        self.assertIsNone(self.pool.leased)

    def test_power_reader_projects_authenticated_state_preserves_heartbeat(self):
        self.assertEqual(self.owner.power_reader.poll_once(), ('active', self.clock()))
        self.assertEqual(self.hook.source.current(), ('active', self.clock()))
        self.clock.now += 1
        self.hook.note_tv_power('standby')
        self.assertEqual(self.hook.source.current(), ('active_standby', self.clock()))
        self.assertEqual(self.peer.connections[0].calls, [])
        self.assertTrue(self.peer.connections[-1].closed)
        self.assertEqual(self.peer.calls[0][1]['pin_sha256'], PIN)
        self.assertEqual(self.peer.calls[1][1]['client_key'], KEY)
        self.assertEqual(self.owner.power_reader.poll_interval_s, 5)

    def test_start_close_owns_reader_and_listener_not_shared_hook(self):
        order = []
        self.owner.resident.start = Mock(side_effect=lambda: order.append('listener-start') or True)
        self.owner.resident.close = Mock(side_effect=lambda **k: order.append('listener-close') or True)
        self.owner.power_reader.spawn_thread = lambda target: order.append('reader-start')
        self.owner.power_reader.close = Mock(side_effect=lambda **k: order.append('reader-close') or True)
        self.assertTrue(self.owner.start()); self.assertFalse(self.owner.start())
        self.assertTrue(self.owner.close()); self.assertFalse(self.owner.start())
        self.assertEqual(order, ['reader-start', 'listener-start', 'listener-close', 'reader-close'])
        self.assertIsNone(self.hook.source.ssap)
        self.assertTrue(self.peer.connections[0].closed)
        self.hook.start.assert_not_called(); self.hook.close.assert_not_called()

    def test_start_failure_joins_actual_reader_without_holding_its_state_lock(self):
        entered = threading.Event()
        old = self.owner.read_power
        self.owner.power_reader.read_fn = lambda: entered.set() or old()
        self.owner.resident.start = Mock(side_effect=RuntimeError('start'))
        begin = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'start'): self.owner.start()
        self.assertLess(time.monotonic() - begin, 1)
        self.assertTrue(self.owner.power_reader.stopped.is_set())
        self.assertTrue(self.owner.closed)
        self.assertIsNone(self.hook.source.ssap)

    def test_close_cannot_restore_over_a_replaced_power_source(self):
        other = object(); self.hook.source.ssap = other
        self.assertTrue(self.owner.close())
        self.assertIs(self.hook.source.ssap, other)

    def auth_refusal(self, reply):
        self.peer.registration = reply
        with self.assertRaisesRegex(ValueError, 'TV_AUTH_FAILED'): self.owner.read_power()
        self.assertTrue(self.peer.connections[-1].closed)
        self.assertFalse(any(c[0].startswith('ssap:') for c in self.peer.calls))
        self.assertTrue(self.owner.killswitch.paused())
        before = len(self.peer.connections)
        with self.assertRaises(ValueError): self.owner.launch(self.payload(), timeout_s=2)
        with self.assertRaises(ValueError): self.owner.read_power()
        self.assertEqual(len(self.peer.connections), before)

    def test_missing_saved_key_ack_never_requests_and_latches(self):
        self.auth_refusal({'returnValue': True})

    def test_wrong_saved_key_ack_never_requests_and_latches(self):
        self.auth_refusal({'client-key': '0' * 64})

    def test_pairing_prompt_never_requests_or_retries(self):
        self.auth_refusal({'client-key': KEY, 'pairingType': 'PROMPT'})

    def test_certificate_change_trips_shared_agent_and_closes(self):
        self.peer.exception = self.owner._certificate_changed()
        with self.assertRaises(self.owner._certificate_changed): self.owner.read_power()
        self.assertTrue(self.owner.killswitch.paused())
        self.assertTrue(self.owner.controller.killswitch.tv_off())
        self.assertTrue(self.peer.connections[-1].closed)

    def test_launch_rejects_foreign_or_enlarged_payload_before_connection(self):
        base = self.owner.viewer.open_url + '#voice-session=' + SID
        bad = [self.payload(base + '?extra=1'), self.payload(base.replace(MANAGER_KEY, '0' * 64)),
               self.payload(base.replace('192.0.2.10', '192.0.2.11')),
               self.payload('http://192.0.2.10:4177/' + TOKEN + '/viewer#extra'),
               {'id': 'netflix'}, dict(self.payload(), extra=1),
               {'id': 'com.webos.app.tinybrowser', 'params': {'contentTarget': base, 'url': base}}]
        for value in bad:
            self.assertEqual(self.owner.launch(value, timeout_s=2), {'returnValue': False})
        self.assertEqual(self.peer.calls, [])

    def test_unknown_launch_ack_not_success_and_viewer_never_replays(self):
        self.peer.launch_reply = {'returnValue': 1}
        self.assertEqual(self.owner.viewer.open(SID, '3' * 32 + ':start', timeout_s=2), 'unknown')
        self.assertEqual(self.owner.viewer.open(SID, '3' * 32 + ':start', timeout_s=2), 'rejected')
        self.assertEqual(sum(row[0].endswith('/launch') for row in self.peer.calls), 1)

    def test_close_inside_connect_or_register_prevents_next_operation(self):
        self.peer.on_connect = self.owner.close
        with self.assertRaises(ValueError): self.owner.launch(self.payload(), timeout_s=2)
        self.assertTrue(all(t.closed for t in self.peer.connections))
        self.assertFalse(any('register' in t.calls for t in self.peer.connections))

    def test_close_inside_registration_prevents_launch(self):
        self.peer.on_register = self.owner.close
        with self.assertRaises(ValueError): self.owner.launch(self.payload(), timeout_s=2)
        self.assertTrue(all(t.closed for t in self.peer.connections))
        self.assertFalse(any(row[0].startswith('ssap:') for row in self.peer.calls))

    def test_close_or_expiry_after_request_never_reports_launch_ack(self):
        self.peer.on_request = self.owner.close
        with self.assertRaises(ValueError): self.owner.launch(self.payload(), timeout_s=2)
        self.assertTrue(all(t.closed for t in self.peer.connections))
        self.assertEqual(sum(row[0].endswith('/launch') for row in self.peer.calls), 1)

    def test_close_waits_for_concurrent_launch_to_unwind(self):
        entered, release, close_done = threading.Event(), threading.Event(), threading.Event()
        self.peer.on_request = lambda: entered.set() or release.wait(1)
        outcomes = []
        def launch():
            try: self.owner.launch(self.payload(), timeout_s=2)
            except ValueError: outcomes.append('cancelled')
        thread = threading.Thread(target=launch)
        thread.start(); self.assertTrue(entered.wait(1))
        closed = []
        def close():
            closed.append(self.owner.close(timeout_s=1)); close_done.set()
        closer = threading.Thread(target=close)
        closer.start()
        try:
            self.assertFalse(close_done.wait(.02))
        finally:
            release.set(); thread.join(1); closer.join(1)
        self.assertFalse(thread.is_alive()); self.assertFalse(closer.is_alive())
        self.assertEqual(closed, [True]); self.assertEqual(outcomes, ['cancelled'])
        self.assertEqual(self.owner._active, {})

    def test_reentrant_close_reports_unsettled_without_self_join(self):
        closed = []
        self.peer.on_request = lambda: closed.append(self.owner.close(timeout_s=.5))
        begin = time.monotonic()
        with self.assertRaises(ValueError): self.owner.launch(self.payload(), timeout_s=2)
        self.assertLess(time.monotonic() - begin, .25)
        self.assertEqual(closed, [False]); self.assertFalse(self.owner.cleanup_complete)
        self.assertEqual(self.owner._active, {})

    def test_deadline_crossed_by_register_prevents_request(self):
        self.peer.on_register = lambda: setattr(self.clock, 'now', self.clock.now + 3)
        with self.assertRaises(TimeoutError): self.owner.launch(self.payload(), timeout_s=2)
        self.assertTrue(self.peer.connections[-1].closed)
        self.assertFalse(any(row[0].startswith('ssap:') for row in self.peer.calls))

    def test_private_material_refuses_unsafe_file_before_connection(self):
        for body in ('{"client-key":"' + KEY + '","client-key":"' + KEY + '"}',
                     '{"client-key":"' + KEY + '","extra":true}', '{}', 'x' * 4097):
            self.key.write_text(body)
            with self.assertRaises(ValueError): self.owner.read_power()
        self.key.unlink(); os.mkfifo(self.key, 0o600)
        begin = time.monotonic()
        with self.assertRaises(ValueError): self.owner.read_power()
        self.assertLess(time.monotonic() - begin, .25)
        self.key.unlink(); self.key.symlink_to(self.pin)
        with self.assertRaises(ValueError): self.owner.read_power()
        self.assertEqual(self.peer.calls, [])

    def test_held_material_change_is_refused(self):
        original = os.read
        def read(fd, length):
            value = original(fd, length)
            self.key.write_text(json.dumps({'client-key': '0' * 64}))
            return value
        with patch.object(rm.os, 'read', read), self.assertRaises(ValueError): self.owner.read_power()
        self.assertEqual(self.peer.calls, [])

    def test_failed_cleanup_is_sticky_and_others_still_close(self):
        self.owner.resident.close = Mock(return_value=False)
        self.assertFalse(self.owner.close())
        self.assertTrue(self.owner.power_reader.stopping.is_set())
        self.assertTrue(self.peer.connections[0].closed)
        self.assertFalse(self.owner.close())

    def test_constructor_failure_closes_its_gateway_without_replacing_source(self):
        previous = self.hook.source.ssap
        with patch.object(rm, 'AgentController', side_effect=ValueError('MODEL_CONFIG')):
            with self.assertRaisesRegex(ValueError, 'MODEL_CONFIG'):
                rm.ResidentManager(
                    manager=self.manager, spark_ip='192.0.2.10', tv_ip='192.0.2.20', manager_key=MANAGER_KEY,
                    ssap_key_path=self.key, ssap_pin_path=self.pin, hook=self.hook,
                    enrollment=object(), identity_loader=lambda: None, voice_key='f' * 64,
                    transport_factory=self.peer.factory, clock=self.clock)
        self.assertTrue(self.peer.connections[-1].closed)
        self.assertIs(self.hook.source.ssap, previous)
        self.factory.assert_not_called()


class PackagedAssemblyTests(unittest.TestCase):
    def test_actual_rendered_constructor_is_inert_without_source_checkout_imports(self):
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / 'tools'))
        try:
            spec = importlib.util.spec_from_file_location('resident_fixture_build', root / 'tools/build.py')
            build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
        finally:
            sys.path.remove(str(root / 'tools'))
        code = r'''
import pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / 'spark'))
def forbid(event, args):
    if event in ('socket.__new__', 'socket.connect', 'socket.bind', 'subprocess.Popen',
                 'os.system', 'os.exec', 'os.posix_spawn', 'os.kill', 'os.killpg'):
        raise AssertionError('NATIVE_ACTION_FORBIDDEN')
    if event == 'import' and args[0].split('.')[0] in ('torch', 'transformers', 'numpy'):
        raise AssertionError('MODEL_IMPORT_FORBIDDEN')
sys.addaudithook(forbid)
import manager
from voice_control.pool import create_production_pool, create_warm_hook
from voice_control.resident_manager import ResidentManager
def no_spawn(*a, **kw): raise AssertionError('NO_SPAWN')
mgr = manager.Manager(spawn=no_spawn)
pool = create_production_pool('/usr/bin/python3', '/synthetic/asr', '/synthetic/intent')
hook = create_warm_hook(pool)
owner = ResidentManager(manager=mgr, spark_ip=manager.SPARK, tv_ip=manager.TV,
    manager_key='c'*64, ssap_key_path='/nonexistent/synthetic-key',
    ssap_pin_path='/nonexistent/synthetic-pin', hook=hook, enrollment=object(),
    identity_loader=lambda: None, voice_key='d'*64)
assert pool.active is None and pool.prompt_phase == 'P5'
assert owner.controller is owner.resident._controller
assert owner.controller.tv.gateway is owner.gateway
assert owner.power_reader.current() is None
assert owner.resident._listener is None and owner._gateway_transport.sock is None
assert owner.close() and hook.close() and mgr.close() is None
for name, mod in tuple(sys.modules.items()):
    if name == 'manager' or name.startswith('voice_control.'):
        assert pathlib.Path(mod.__file__).resolve().is_relative_to(root)
assert 'controller' not in sys.modules and 'agent' not in sys.modules
'''
        with tempfile.TemporaryDirectory(prefix='resident-package-') as directory:
            out = Path(directory).resolve() / 'build'
            build.build(build.config((root / 'config/example.json').read_bytes()), out)
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(out)],
                                    cwd=directory, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
