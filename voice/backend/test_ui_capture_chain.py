"""Packaged Python broker/Trial ↔ actual JS owner, with inert HTTP/hardware/PCM.

Only local anonymous pipes and a socketpair are used. No TLS socket, model,
provider process, device, service, or target is contacted.
"""
import http.client
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / 'voice/tv/local-capture/ui_capture_chain_fixture.js'
SESSION, EPOCH = 'a'*32, 'b'*32
# Synthetic receipt; never a real utterance.
CARD = dict(v=1, seq=7, phase='result', ageMs=200, tone='sent', word='SENT',
            headline='Searching YouTube...', detail='', badge='sent', meter=None,
            meterMs=None, dwellMs=4000, mood='verified', echo='lo-fi music')


class Chain:
    def __init__(self, build):
        # This class runs only in an isolated child, avoiding flat/package import
        # collisions with the rest of the backend tests.
        sys.path[:0] = [str(build/'spark/runtime'), str(build/'spark')]
        import trial
        from voice_control.browser_channel import channel_pair, TrialControlHost
        from voice_control.ui_capture_broker import UiCaptureBroker
        self.trial = trial.Trial(Path('/unused'), 'c'*48, Mock(), 'mse')
        self.trial.page_ready = True
        self.touch()
        media = self.trial.media_stream
        media.epoch, media.generation, media.active, media.ready = EPOCH, 1, True, True
        self.client, server = channel_pair(SESSION)
        self.host = TrialControlHost(server, self.trial)
        self.thread = threading.Thread(target=self.host.serve, daemon=True)
        self.thread.start()
        self.manager = SimpleNamespace(control_spawn=SimpleNamespace(current_client=lambda timeout_s:(SESSION, self.client)))
        self.broker = UiCaptureBroker(self.manager, 'f'*64)
    def touch(self):
        self.trial.owner_verified_at = self.trial.ready_at = self.trial.last_viewer_heartbeat = time.monotonic()
    def exchange(self, packet):
        self.touch()
        return self.broker.exchange(packet)
    def command(self, action, identifier='1'*16):
        self.touch()
        binding = self.trial.voice_binding(dict(epoch=EPOCH, generation=1))
        return self.trial.voice.command(binding, dict(schema=1, action=action, request=identifier))
    def status(self):
        self.touch()
        return self.trial.voice.status(self.trial.voice_binding(dict(epoch=EPOCH, generation=1)))
    def close(self):
        self.trial.voice.close()
        self.trial.voice_adapter.close()
        self.client.close(); self.host.close(); self.thread.join(2)
        assert not self.thread.is_alive(), 'owned channel did not close'


class JavaScript:
    def __init__(self, chain, options):
        self.chain, self.number, self.requests = chain, 0, []
        self.child = subprocess.Popen(['node', str(DRIVER)], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.buffer = bytearray()
        try: self.run('init', options=options)
        except BaseException:
            self.close(force=True)
            raise
    def send(self, value):
        self.child.stdin.write(json.dumps(value).encode()+b'\n')
        self.child.stdin.flush()
    def line(self, deadline):
        while b'\n' not in self.buffer:
            remain = deadline-time.monotonic()
            assert remain > 0 and select.select([self.child.stdout], [], [], remain)[0], 'fixture timeout'
            raw = os.read(self.child.stdout.fileno(), 4096)
            assert raw and len(self.buffer)+len(raw) <= 16384, 'fixture frame refused'
            self.buffer.extend(raw)
        raw, _, rest = self.buffer.partition(b'\n'); self.buffer[:] = rest
        return json.loads(raw)
    def run(self, operation, **fields):
        self.number += 1
        self.send(dict(id=self.number, op=operation, **fields))
        deadline = time.monotonic()+4
        while True:
            value = self.line(deadline)
            if value['kind'] == 'http':
                self.requests.append(value['packet'])
                self.send(dict(kind='reply', id=value['id'], packet=self.chain.exchange(value['packet'])))
            else:
                assert value['kind'] == 'result' and value['id'] == self.number, 'fixture result refused'
                return value['value']
    def close(self, force=False):
        try:
            if not force: self.run('close')
        finally:
            self.child.stdin.close()
            try: self.child.wait(2)
            except subprocess.TimeoutExpired:
                self.child.terminate()
                try: self.child.wait(2)
                except subprocess.TimeoutExpired: self.child.kill(); self.child.wait(2)
            self.child.stdout.close(); self.child.stderr.close()
            assert force or self.child.returncode == 0, 'owned JS fixture failed'


def admitted(chain, js):
    assert js.run('poll')['ok'] and js.run('poll')['ok']
    assert chain.status()['state'] == 'idle'


def start(chain, js, identifier='1'*16):
    assert chain.command('start', identifier)[0] == 202
    assert js.run('poll')['ok'] and js.run('poll')['ok']
    assert chain.status()['state'] == 'listening'
    js.run('pcm')


class Status:
    """VoiceStatus stand-in exposing only its viewer interface."""
    def __init__(self): self.card, self.delivered = dict(CARD), []
    def viewer_card(self): return self.card
    def note_viewer_delivered(self, seq): self.delivered.append(seq)


def viewer_server(chain):
    import protocol, trial
    parent = trial.handler_for(chain.trial)
    class Handler(parent):
        def setup(this):
            super().setup(); this.client_address = (protocol.TV, 1234)
    class Server(trial.BoundedServer):
        def verify_request(this, request, address): return address[0] == '127.0.0.1'
    server = Server(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True).start()
    return server


def heartbeat(chain, server):
    import protocol
    from test_trial import metrics
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    conn.request('POST', '/'+chain.trial.token+'/metrics', body=json.dumps(dict(metrics(), token=chain.trial.token)),
                 headers={'Host': '%s:%d' % (protocol.SPARK, protocol.PORT), 'Content-Type': 'application/json'})
    response = conn.getresponse(); data = response.read(); conn.close()
    assert response.status == 200 and len(data) <= 1024, 'heartbeat refused'
    value = json.loads(data)
    # The handler counts delivery just after its reply write returns.
    deadline = time.monotonic() + 1
    while value['voice'] is not None and chain.trial.voice_card.delivered is None and time.monotonic() < deadline:
        time.sleep(.005)
    return value


def voice_scenario(name, build):
    chain, server = Chain(build), None
    try:
        from voice_control.voice_card_push import push_voice_card
        from voice_control.browser_channel import MAX_REQUESTS
        status, server = Status(), viewer_server(chain)
        if name == 'voice_card_delivery':
            assert heartbeat(chain, server) == {'ok': True, 'voice': None}
            assert push_voice_card(chain.manager, status) and status.delivered == []
            reply = heartbeat(chain, server)
            assert set(reply) == {'ok', 'voice'} and reply['voice']['seq'] == 7 and reply['voice']['echo'] == 'lo-fi music'
            assert dict(reply['voice'], ageMs=200) == CARD and reply['voice']['ageMs'] >= 200
            assert push_voice_card(chain.manager, status) and status.delivered == [7]
            assert push_voice_card(chain.manager, status) and status.delivered == [7], 'delivery reported once'
            assert 'lo-fi' not in json.dumps(chain.trial.metrics)
            chain.trial.voice_card.at -= 3
            assert heartbeat(chain, server) == {'ok': True, 'voice': None}, 'mirror outlived its pushes'
        elif name == 'voice_card_lifetime':
            for _ in range(600):
                assert push_voice_card(chain.manager, status)
            assert chain.client.seq > MAX_REQUESTS and chain.host.requested_at is None
            assert chain.client.regular_requests == chain.host.regular_requests == 0
            chain.touch()
            assert chain.client.navigate('3'*32+':navigate', 'https://www.youtube.com', timeout_s=.5) == 'accepted'
            assert chain.client.regular_requests == chain.host.regular_requests == 1
        elif name == 'voice_card_refusal':
            # The Manager channel and the Trial mirror validate independently; they must agree.
            import voice_card
            from voice_control.browser_channel import valid_card
            idle = dict(CARD, phase='idle', tone='ready', word='', headline='', badge=None, mood='idle', dwellMs=None, echo=None)
            for vector in (CARD, idle, dict(CARD, word='DIDN\u2019T WORK'), dict(CARD, echo='\U0001f408'*48), dict(CARD, chip='ready'),
                           dict(CARD, v=True), dict(CARD, seq=2**31), dict(CARD, ageMs=600001), dict(CARD, phase='speaking'),
                           dict(CARD, badge='done'), dict(CARD, meterMs=20001), dict(CARD, headline='x'*41), dict(CARD, detail='caf\u00e9'),
                           dict(CARD, echo='a\u202eb'), dict(CARD, echo='x'*49), dict(CARD, phase='listening'), dict(idle, headline='Got it.'),
                           None, 'card', [CARD]):
                assert valid_card(vector) is voice_card.valid_card(vector), repr(vector)[:80]
            seq = chain.client.seq
            for bad in (dict(CARD, echo='a\u202eb'), dict(CARD, chip='ready'), 'card', dict(CARD, headline='x'*41)):
                status.card = bad
                assert not push_voice_card(chain.manager, status)
            assert chain.client.seq == seq and chain.trial.voice_card.card is None
            held, release = threading.Event(), threading.Event()
            def hold():
                with chain.trial.lock:
                    held.set(); assert release.wait(2)
            thread = threading.Thread(target=hold); thread.start(); assert held.wait(1)
            try:
                status.card, then = dict(CARD), time.monotonic()
                assert push_voice_card(chain.manager, status) and time.monotonic()-then < .5
            finally:
                release.set(); thread.join(1)
            chain.touch()
            assert chain.client.navigate('3'*32+':navigate', 'https://www.youtube.com', timeout_s=.5) == 'accepted'
        else: raise AssertionError('unknown test case')
        assert not chain.client.channel.closed
    finally:
        try:
            if server is not None: server.shutdown(); server.server_close()
        finally: chain.close()


def scenario(name, build):
    if name.startswith('voice_card'):
        return voice_scenario(name, build)
    chain, js = Chain(build), None
    try:
        js = JavaScript(chain, {'foreignReply': name == 'prestart_refusal',
                                'nativeStartFailures': 1 if name == 'protective_stop' else 0})
        admitted(chain, js)
        if name == 'two_commands':
            for identifier in ('1'*16, '2'*16):
                start(chain, js, identifier)
                assert chain.command('release', identifier)[0] == 202
                assert js.run('poll')['ok'] and js.run('poll')['ok']
                assert chain.status()['state'] == 'accepted'
            vector = js.run('vector', value={'z':False, 'a':{'request':None,'state':'idle'}})
            assert vector['canonical'] == '{"a":{"request":null,"state":"idle"},"z":false}'
            assert len({packet['owner'] for packet in js.requests}) == 1
            assert js.run('poll')['stats'] == dict(starts=2, stops=2, releases=2, launched=2)
        elif name == 'prestart_refusal':
            assert chain.command('start')[0] == 202
            assert js.run('poll')['ok'] and js.run('poll')['ok']
            assert chain.status()['state'] == 'refused' and chain.broker.current is None
            assert js.run('poll')['stats']['starts'] == 0
        elif name == 'protective_stop':
            assert chain.command('start')[0] == 202
            assert js.run('poll')['ok']
            refused = js.run('poll')
            assert refused['ok']
            assert refused['status'] == dict(available=True, state='refused', request='1'*16,
                                             started=False, stopped=True, cleanupComplete=True)
            assert refused['stats'] == dict(starts=1, stops=1, releases=0, launched=1)
            assert chain.status()['state'] == 'refused'
            assert chain.status()['available'] and not chain.trial.voice.poisoned
            assert chain.broker.current is None
            start(chain, js, '2'*16)
            assert chain.command('release', '2'*16)[0] == 202
            assert js.run('poll')['ok'] and js.run('poll')['ok']
            assert chain.status()['state'] == 'accepted'
            assert js.run('poll')['stats'] == dict(starts=2, stops=2, releases=1, launched=2)
        elif name in ('own_navigation', 'cancel_late_result'):
            start(chain, js); js.run('hold', value=True)
            assert chain.command('release')[0] == 202
            js.run('poll'); js.run('poll')
            assert chain.status()['state'] == 'finishing'
            if name == 'own_navigation':
                assert chain.client.navigate('3'*32+':navigate', 'https://www.youtube.com', timeout_s=.5) == 'accepted'
                assert chain.trial.input_epoch == 1 and chain.trial.navigation_pending is not None
                assert chain.status()['state'] == 'finishing'
            else:
                assert chain.command('cancel')[0] == 202
                js.run('poll')
            js.run('resolve'); js.run('poll')
            assert chain.status()['state'] == ('accepted' if name == 'own_navigation' else 'unknown')
        elif name in ('trial_lock', 'active_rtc_lock'):
            if name == 'active_rtc_lock':
                from display_transport import RTCDisplay
                media = RTCDisplay(chain.trial, Mock())
                media.epoch, media.generation, media.active, media.ready = EPOCH, 1, True, True
                sender = Mock(answer_taken=True, connection_ready=True, video_count=1, error=None)
                sender.stop = threading.Event(); sender.media_ended = threading.Event(); media.sender = sender
                chain.trial.media_stream = media; chain.trial.display_transport = 'webrtc'
                start(chain, js)
            held, release = threading.Event(), threading.Event()
            def hold():
                with chain.trial.lock:
                    held.set(); assert release.wait(2)
            thread = threading.Thread(target=hold); thread.start(); assert held.wait(1)
            try:
                then = time.monotonic()
                assert js.run('poll')['ok'] and time.monotonic()-then < .5
            finally:
                release.set(); thread.join(1)
            assert not thread.is_alive()
        else: raise AssertionError('unknown test case')
        assert not chain.client.channel.closed
    finally:
        try:
            if js is not None: js.close()
        finally: chain.close()


class PackagedUiCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='r2d2-ui-chain-')
        cls.build = Path(cls.temporary.name).resolve()/'build'
        result = subprocess.run([sys.executable, '-B', str(ROOT/'tools/build.py'), '--config',
                                 str(ROOT/'config/example.json'), '--output', str(cls.build), '--include-tests'],
                                capture_output=True, timeout=30)
        if result.returncode != 0:
            cls.temporary.cleanup()
            raise AssertionError('packaged source build failed')
    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()
    def run_case(self, name):
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                 '--case', name, str(self.build)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout, b'CHAIN_PASS\n')
    def test_two_actual_owner_commands_and_signing(self): self.run_case('two_commands')
    def test_prestart_refusal_retires_without_hardware(self): self.run_case('prestart_refusal')
    def test_failed_native_start_protective_stop_allows_second_attempt(self): self.run_case('protective_stop')
    def test_actual_released_navigation_retains_result(self): self.run_case('own_navigation')
    def test_cancel_late_ack_is_unknown(self): self.run_case('cancel_late_result')
    def test_trial_lock_does_not_poison_channel(self): self.run_case('trial_lock')
    def test_active_rtc_poll_does_not_wait_trial_lock(self): self.run_case('active_rtc_lock')
    def test_voice_card_push_reaches_heartbeat_and_reports_delivery(self): self.run_case('voice_card_delivery')
    def test_voice_card_pushes_do_not_spend_channel_lifetime(self): self.run_case('voice_card_lifetime')
    def test_bad_voice_card_never_poisons_channel(self): self.run_case('voice_card_refusal')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--case':
        scenario(sys.argv[2], Path(sys.argv[3])); print('CHAIN_PASS')
    else: unittest.main()
