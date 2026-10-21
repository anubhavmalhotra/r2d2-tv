import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from browser_channel import (MAX_REQUESTS, ControlClient, FramedSocket, TrialControlHost,
                             allowed_url, channel_pair, signed, valid_card)
from browser_spawn import ControlledSpawn, attach_trial, close_trial
from controller import Refused


# Synthetic receipt; never a real utterance.
CARD = dict(v=1, seq=7, phase='result', ageMs=200, tone='sent', word='SENT',
            headline='Searching YouTube...', detail='', badge='sent', meter=None,
            meterMs=None, dwellMs=4000, mood='verified', echo='lo-fi music')


class Trial:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []
        self.cards = []
        self.accept = True
        self.delivered = {'delivered': None}
    def queue_navigation(self, body):
        self.calls.append(dict(body))
        return self.accept
    def voice_card_exchange(self, body):
        # The real mirror has its own leaf lock; the host must not hold Trial.lock.
        assert not self.lock._is_owned()
        self.cards.append(body)
        return self.delivered


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.client, sock = channel_pair('a'*32)
        self.trial = Trial()
        self.now = 50.0
        self.host = TrialControlHost(sock, self.trial, clock=lambda:self.now)
        self.thread = threading.Thread(target=self.host.serve, kwargs={'lifetime_s':5}, daemon=True)
        self.thread.start()
    def tearDown(self):
        self.client.close(); self.host.close(); self.thread.join(1)
        self.assertFalse(self.thread.is_alive())

    def test_real_socket_auth_navigation_and_separate_presentation_proof(self):
        self.host.publish(lambda _:False, ready=True)
        initial = self.client.observe(timeout_s=1)
        self.assertTrue(initial.ready); self.assertFalse(initial.presented)
        result = self.client.queue_navigation({'url':'https://www.youtube.com'},operation='b'*32+':navigate',timeout_s=1)
        self.assertEqual(result,'accepted')
        row=self.client.observe(timeout_s=1, presentation=lambda *a,**k:True)
        self.assertFalse(row.presented); self.assertIsNone(row.url)
        self.host.publish(lambda wanted:wanted=='https://www.youtube.com',ready=True)
        row=self.client.observe(timeout_s=1)
        self.assertFalse(row.presented); self.assertEqual(row.url,'https://www.youtube.com')
        row=self.client.observe(timeout_s=1,presentation=lambda *a,**k:True)
        self.assertTrue(row.presented)
        self.assertEqual(self.trial.calls,[{'url':'https://www.youtube.com'}])

    def test_stale_proof_and_new_navigation_invalidates_old_page(self):
        self.client.queue_navigation({'url':'https://www.youtube.com'},operation='b'*32+':navigate',timeout_s=1)
        self.host.publish(lambda _:True,ready=True)
        self.now+=1.01
        self.assertFalse(self.client.observe(timeout_s=1).ready)
        self.now-=.5
        self.client.queue_navigation({'url':'https://duckduckgo.com/?q=weather'},operation='c'*32+':navigate',timeout_s=1)
        self.assertFalse(self.client.observe(timeout_s=1).presented)
        self.assertIsNone(self.client.observe(timeout_s=1).url)

    def test_duplicate_mutation_is_consumed_and_never_repeated(self):
        operation='b'*32+':navigate'
        body={'url':'https://www.youtube.com'}
        self.assertEqual(self.client.queue_navigation(body,operation=operation,timeout_s=1),'accepted')
        self.assertEqual(self.client.queue_navigation(body,operation=operation,timeout_s=1),'unknown')
        self.assertEqual(len(self.trial.calls),1)
        self.assertTrue(self.client.channel.closed)

    def test_rejected_trial_queue_does_not_establish_target(self):
        self.trial.accept=False
        self.assertEqual(self.client.queue_navigation({'url':'https://www.youtube.com'},operation='b'*32+':navigate',timeout_s=1),'rejected')
        self.host.publish(lambda _:True,ready=True)
        self.assertIsNone(self.client.observe(timeout_s=1).url)

    def test_foreign_session_closes_endpoint(self):
        self.client.observe(timeout_s=1)
        row={'schema':1,'session':'c'*32,'seq':self.client.seq+1,'action':'observe','deadline':time.monotonic()+1}
        self.client.channel.send(signed(row,self.client.key,b'request'),time.monotonic()+1)
        self.thread.join(1)
        self.assertTrue(self.host.channel.closed)
        with self.assertRaises(Refused):self.client.observe(timeout_s=.1)
        self.assertEqual(self.trial.calls,[])

    def test_valid_mac_with_replayed_sequence_is_rejected(self):
        self.client.observe(timeout_s=1)
        row={'schema':1,'session':self.client.session,'seq':self.client.seq,
             'action':'navigate','operation':'b'*32+':navigate','url':'https://www.youtube.com',
             'deadline':time.monotonic()+1}
        self.client.channel.send(signed(row,self.client.key,b'request'),time.monotonic()+1)
        self.thread.join(1)
        self.assertTrue(self.host.channel.closed)
        self.assertEqual(self.trial.calls,[])

    def test_tampered_message_cannot_dispatch(self):
        self.client.observe(timeout_s=1)
        row=signed({'schema':1,'session':self.client.session,'seq':self.client.seq+1,
                    'action':'navigate','operation':'b'*32+':navigate','url':'https://www.youtube.com',
                    'deadline':time.monotonic()+1},self.client.key,b'request')
        row['url']='https://duckduckgo.com/?q=changed'
        self.client.channel.send(row,time.monotonic()+1)
        self.thread.join(1)
        self.assertTrue(self.host.channel.closed)
        self.assertEqual(self.trial.calls,[])

    def test_lost_ack_after_mutation_never_replays_navigation(self):
        send=self.host.channel.send
        def drop_reply(row,deadline):
            if 'result'in row:self.host.close()
            else:send(row,deadline)
        self.host.channel.send=drop_reply
        body={'url':'https://www.youtube.com'};operation='b'*32+':navigate'
        self.assertEqual(self.client.queue_navigation(body,operation=operation,timeout_s=1),'unknown')
        self.assertEqual(self.trial.calls,[body])
        self.assertEqual(self.client.queue_navigation(body,operation=operation,timeout_s=1),'unknown')
        self.assertEqual(self.trial.calls,[body])

    def test_no_arbitrary_navigation_or_trailing_search_parameters(self):
        for url in ('http://www.youtube.com','https://www.youtube.com/watch?v=secret',
                    'https://duckduckgo.com/?q=a&secret=b','https://user@duckduckgo.com/?q=a',
                    'javascript:alert(1)','file:///etc/passwd'):
            self.assertFalse(allowed_url(url))
        self.assertTrue(allowed_url('https://duckduckgo.com/?q=a%20b'))

    def test_google_search_template_preserves_channel_admission(self):
        prefix='https://www.google.com/search?q='
        url=prefix+'caf%C3%A9%20%26%20cats'
        self.assertTrue(allowed_url(url))
        self.assertTrue(allowed_url(prefix+'a'*1500))
        for tail in ('', 'a'*1501, 'cats&sourceid=chrome', 'cats&oq=cat', 'cats#top', 'cats+dogs', '%', '%2f', '%FF', '%0A', '%2521g'):
            self.assertFalse(allowed_url(prefix+tail),tail[:40])
        for bad in ('http://www.google.com/search?q=cats', 'https://google.com/search?q=cats',
                    'https://www.google.com:443/search?q=cats', 'https://www.google.com/search?source=voice&q=cats'):
            self.assertFalse(allowed_url(bad),bad)
        self.assertEqual(self.client.queue_navigation({'url':url},operation='d'*32+':navigate',timeout_s=1),'accepted')
        self.assertEqual(self.trial.calls,[{'url':url}])
        self.assertTrue(allowed_url('https://duckduckgo.com'))
        self.assertTrue(allowed_url('https://duckduckgo.com/?q=cats'))

    def test_voice_card_push_is_exempt_from_budget_and_page_proof(self):
        self.trial.delivered = {'delivered': 6}
        for _ in range(600):
            self.assertEqual(self.client.voice_card(CARD, timeout_s=.5), {'delivered': 6})
        self.assertEqual(self.client.voice_card(None, timeout_s=.5), {'delivered': 6})
        self.assertEqual(len(self.trial.cards), 601)
        self.assertEqual(self.trial.cards[0], {'card': CARD}); self.assertEqual(self.trial.cards[-1], {'card': None})
        self.assertGreater(self.client.seq, MAX_REQUESTS)
        self.assertEqual((self.client.regular_requests, self.host.regular_requests), (0, 0))
        self.assertIsNone(self.host.requested_at)
        self.assertEqual(self.client.navigate('b'*32+':navigate', 'https://www.youtube.com', timeout_s=1), 'accepted')
        self.assertEqual((self.client.regular_requests, self.host.regular_requests), (1, 1))
        self.assertIsNotNone(self.host.requested_at)

    def test_voice_card_shape_is_closed(self):
        self.assertTrue(valid_card(CARD))
        idle = dict(CARD, phase='idle', tone='ready', word='', headline='', badge=None, mood='idle', dwellMs=None, echo=None)
        self.assertTrue(valid_card(idle))
        self.assertTrue(valid_card(dict(CARD, word='DIDN\u2019T WORK', headline='I\u2019m sorry, I cannot do that.')))
        # Spark now writes three periods; U+2026 copy from an older Spark is still admitted.
        self.assertTrue(valid_card(dict(CARD, headline='Searching YouTube\u2026')))
        missing = dict(CARD); del missing['echo']
        for bad in (None, 'card', missing, dict(CARD, chip='ready'), dict(CARD, v=True), dict(CARD, seq=2**31),
                    dict(CARD, phase='speaking'), dict(CARD, badge='done'), dict(CARD, dwellMs=20001),
                    dict(CARD, headline='x'*41), dict(CARD, detail='caf\u00e9'), dict(CARD, word='Sent'),
                    dict(CARD, echo='a\u202eb'), dict(CARD, echo='x'*49), dict(CARD, echo=''),
                    dict(CARD, phase='listening'), dict(idle, headline='Listening...')):
            self.assertFalse(valid_card(bad), repr(bad)[:120])

    def test_malformed_card_or_busy_lock_never_poisons_navigation(self):
        self.client.observe(timeout_s=1); seq = self.client.seq
        for bad in (dict(CARD, echo='a\u202eb'), dict(CARD, extra=1), 'card', dict(CARD, headline='x'*41)):
            self.assertIsNone(self.client.voice_card(bad, timeout_s=.5))
        self.assertEqual(self.client.seq, seq); self.assertEqual(self.trial.cards, [])
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.client.lock: held.set(); release.wait(2)
        other = threading.Thread(target=hold); other.start(); held.wait(1)
        try:
            started = time.monotonic()
            self.assertIsNone(self.client.voice_card(CARD, timeout_s=.5))
            self.assertLess(time.monotonic() - started, .3)
        finally:
            release.set(); other.join(1)
        self.assertFalse(self.client.channel.closed); self.assertEqual(self.client.seq, seq)
        self.assertIsNone(self.client.voice_card(CARD, timeout_s=.1))
        self.assertFalse(self.client.channel.closed)
        self.assertEqual(self.client.navigate('b'*32+':navigate', 'https://www.youtube.com', timeout_s=1), 'accepted')

    def test_voice_card_does_not_wait_for_trial_lock(self):
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.trial.lock: held.set(); release.wait(2)
        other = threading.Thread(target=hold); other.start(); held.wait(1)
        try:
            started = time.monotonic()
            self.assertEqual(self.client.voice_card(CARD, timeout_s=.5), {'delivered': None})
            self.assertLess(time.monotonic() - started, .3)
        finally:
            release.set(); other.join(1)

    def test_forged_card_or_bad_mirror_reply_fails_closed(self):
        self.client.observe(timeout_s=1)
        row = {'schema': 1, 'session': self.client.session, 'seq': self.client.seq + 1, 'action': 'voice_card',
               'deadline': time.monotonic() + 1, 'card': dict(CARD, echo='a\u202eb')}
        self.client.channel.send(signed(row, self.client.key, b'request'), time.monotonic() + 1)
        self.thread.join(1)
        self.assertTrue(self.host.channel.closed); self.assertEqual(self.trial.cards, [])

    def test_bad_mirror_reply_is_unconfirmed_not_raised(self):
        self.trial.delivered = {'delivered': True}
        self.assertIsNone(self.client.voice_card(CARD, timeout_s=.5))
        self.assertTrue(self.client.channel.closed)

    def test_blocked_trial_lock_has_hard_deadline_no_late_mutation(self):
        held=threading.Event(); release=threading.Event()
        def hold():
            with self.trial.lock:held.set();release.wait(2)
        other=threading.Thread(target=hold);other.start();held.wait(1)
        start=time.monotonic()
        result=self.client.queue_navigation({'url':'https://www.youtube.com'},operation='b'*32+':navigate',timeout_s=.08)
        self.assertIn(result,('rejected','unknown'))
        self.assertLess(time.monotonic()-start,.3)
        release.set();other.join(1);time.sleep(.02)
        self.assertEqual(self.trial.calls,[])


class TransportLifecycleTests(unittest.TestCase):
    def test_nonreading_real_socket_backpressure_deadline_closes(self):
        parent,child=socket.socketpair()
        parent.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
        io=FramedSocket(parent)
        parent.setblocking(False)
        try:
            while True:parent.send(b'x'*4096)
        except BlockingIOError:pass
        start=time.monotonic()
        with self.assertRaisesRegex(Refused,'CONTROL_TIMEOUT'):
            io.send({'value':'x'*3000},time.monotonic()+.06)
        self.assertLess(time.monotonic()-start,.25)
        io.close();child.close()

    def test_real_subprocess_inherits_only_capability_and_reaps(self):
        parent,child=socket.socketpair()
        code='''import os,socket,sys\ns=socket.socket(fileno=int(sys.argv[1]));s.sendall(b"ready");s.close()\n'''
        proc=subprocess.Popen([sys.executable,'-B','-c',code,str(child.fileno())],pass_fds=(child.fileno(),),
                              stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        child.close();parent.settimeout(1)
        self.assertEqual(parent.recv(5),b'ready');parent.close()
        self.assertEqual(proc.wait(timeout=1),0)

    def test_actual_manager_uses_explicit_spawn_hook_offline(self):
        # Load the actual generic Manager source without rendering device placeholders,
        # exercising real start/work lifecycle through a bounded synthetic subprocess.
        root=Path(__file__).resolve().parents[2]
        path=root/'spark/manager.py.in'
        source=path.read_text()
        module=types.ModuleType('reviewed_manager_fixture');module.__file__=str(path)
        exec(compile(source,str(path),'exec'),module.__dict__)
        runtime=str(path.parent/'runtime/trial.py')
        seen={};child_done=threading.Event()
        class Proc:
            pid=123
            def __init__(self,fd):
                self.sock=socket.socket(fileno=os.dup(fd));self.stdout=open(os.devnull,'rb');self.returncode=None
                def run():
                    host=TrialControlHost(self.sock,Trial());host.serve(lifetime_s=.2)
                    self.returncode=0;child_done.set()
                threading.Thread(target=run,daemon=True).start()
            def poll(self):return self.returncode
            def terminate(self):self.sock.close();self.returncode=0
            def wait(self,timeout):
                child_done.wait(timeout)
                self.stdout.close();return 0
        def spawn(argv,**kwargs):
            seen.update(argv=argv,kwargs=kwargs)
            return Proc(kwargs['pass_fds'][0])
        hook=ControlledSpawn(runtime,spawn=spawn)
        manager=module.Manager(spawn=hook)
        hook.bind_manager(manager)
        status,body=manager.start()
        self.assertEqual(status,202)
        manager.session['worker'].join(1)
        self.assertFalse(manager.session if manager.session else None)
        self.assertEqual(seen['argv'][-2],'--voice-control-fd')
        self.assertNotIn('key',seen['kwargs']['env'])
        hook.close(body['id'])

    def test_child_attach_explicit_and_clean_exit(self):
        client,child=channel_pair('a'*32)
        descriptor=child.detach()
        host,thread=attach_trial(Trial(),descriptor)
        client.observe(timeout_s=1)
        self.assertTrue(close_trial(host,thread))
        client.close()


if __name__=='__main__':unittest.main()
