"""Synthetic owner/Trial integration through real private socketpair and HTTP parsing."""
from collections import deque
import json
import threading
import time
import types
import unittest

from voice.backend import browser_channel as channel
from voice.backend.browser_spawn import ControlledSpawn
from voice.backend.ui_capture_broker import (UiCaptureBroker, UNAVAILABLE, canonical,
                                             derive_key, sign, valid_status)
from test_resident_entry import Fixture, KEY, BROWSER_KEY

UI_KEY, SID, OWNER = 'f'*64, 'c'*32, 'd'*32
FIRST, SECOND = '1'*16, '2'*16


def status(request=None, state='idle', *, started=False, stopped=None, cleanup=True, available=True):
    return dict(available=available, state=state, request=request, started=started,
                stopped=(request is not None) if stopped is None else stopped, cleanupComplete=cleanup)


class Trial:
    def __init__(self):
        self.lock = threading.Lock()
        self.commands, self.calls = deque(), []
    def capture_exchange(self, body):
        # A real non-reentrant lock catches the media/Trial lock-order regression.
        if not self.lock.acquire(False): raise AssertionError('TRIAL_LOCK_HELD')
        self.lock.release()
        self.calls.append(body)
        return {'command': self.commands.popleft() if self.commands else None}


class Joined(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.manager = types.SimpleNamespace(lock=threading.RLock(), state='ready',
            session={'id':SID,'process':types.SimpleNamespace(poll=lambda:None)})
        self.spawn = ControlledSpawn('/synthetic/runtime/trial.py')
        self.spawn.bind_manager(self.manager)
        self.manager.control_spawn = self.spawn
        self.client, sock = channel.channel_pair(SID)
        self.spawn.clients[SID] = self.client
        self.trial = Trial()
        self.host = channel.TrialControlHost(sock,self.trial,clock=lambda:self.now)
        self.thread = threading.Thread(target=self.host.serve,kwargs={'lifetime_s':15},daemon=True)
        self.thread.start()
        self.broker = UiCaptureBroker(self.manager,UI_KEY,clock=lambda:self.now)
        self.seq = 0
    def tearDown(self):
        self.client.close();self.host.close();self.thread.join(1)
        self.assertFalse(self.thread.is_alive())
        self.broker.close()
    def packet(self, observation=None, *, session=SID, owner=OWNER, seq=None, key=UI_KEY):
        if seq is None: self.seq += 1;seq=self.seq
        return sign(dict(schema=1,owner=owner,session=session,seq=seq,nonce='%016x'%seq,
                         status=observation if observation is not None else status()),derive_key(key),'request')
    def poll(self, observation=None, **kw):
        return self.broker.exchange(self.packet(observation,**kw))
    def queue(self, action, identity=FIRST):
        self.trial.commands.append(dict(request=identity,action=action))
    def start(self):
        self.queue('start');self.assertEqual(self.poll()['command'],dict(request=FIRST,action='start'))

    def test_authenticated_session_echo_nonce_and_replay(self):
        packet=self.packet(session=None);reply=self.broker.exchange(packet)
        self.assertEqual(reply['session'],SID);self.assertIsNone(reply['command'])
        self.assertEqual(reply['nonce'],packet['nonce']);self.assertEqual(reply['seq'],packet['seq'])
        unsigned={k:v for k,v in reply.items() if k!='mac'}
        self.assertEqual(reply,sign(unsigned,derive_key(UI_KEY),'response'))
        self.assertEqual(self.trial.calls,[])
        with self.assertRaisesRegex(ValueError,'REPLAY'):self.broker.exchange(packet)
        packet=self.packet();packet['status']['available']=False
        with self.assertRaisesRegex(ValueError,'AUTH'):self.broker.exchange(packet)
        self.assertNotIn(UI_KEY,json.dumps(reply))

    def test_two_jobs_and_terminal_clean_status(self):
        self.start();self.queue('start')
        self.assertIsNone(self.poll(status(FIRST,'listening',started=True,stopped=False,cleanup=False))['command'])
        self.queue('release');self.assertEqual(self.poll(status(FIRST,'listening',started=True,stopped=False,cleanup=False))['command']['action'],'release')
        self.queue('release');self.assertIsNone(self.poll(status(FIRST,'finishing',started=True,stopped=True))['command'])
        terminal=status(FIRST,'accepted',started=True,stopped=True)
        self.assertIsNone(self.poll(terminal)['command']);self.assertIsNone(self.broker.current)
        self.queue('start',SECOND)
        self.assertEqual(self.poll(terminal)['command'],dict(request=SECOND,action='start'))
        self.assertEqual(self.broker.current['request'],SECOND)

    def test_prestart_refusal_without_stop_ack_can_retire(self):
        self.start();terminal=status(FIRST,'refused',started=False,stopped=False,cleanup=True)
        self.poll(terminal);self.assertIsNone(self.broker.current)
        self.queue('start',SECOND);self.assertEqual(self.poll(terminal)['command']['request'],SECOND)

    def test_release_uses_absolute_200s_result_window_not_45s(self):
        self.start();self.queue('release')
        finishing=status(FIRST,'finishing',started=True,stopped=True,cleanup=False)
        self.assertEqual(self.poll(finishing)['command']['action'],'release')
        for _ in range(199):
            self.now+=1
            self.assertIsNone(self.poll(finishing)['command'])
        self.now+=2
        self.assertEqual(self.poll(finishing)['command'],dict(request=FIRST,action='cancel'))
        self.assertIsNotNone(self.broker.current)

    def test_lost_binding_retains_exact_owned_request_and_session(self):
        self.start();self.manager.session=None
        reply=self.poll(status(FIRST,'listening',started=True,stopped=False,cleanup=False))
        self.assertEqual(reply['session'],SID);self.assertEqual(reply['command'],dict(request=FIRST,action='cancel'))
        self.assertFalse(self.broker.close());self.assertIsNotNone(self.broker.current)

    def test_retired_owner_cannot_return_with_higher_sequence(self):
        self.start()
        reply=self.poll(status(FIRST,'accepted',started=True),owner='e'*32,seq=1)
        self.assertEqual(reply['command']['action'],'cancel');self.assertIsNotNone(self.broker.current)
        with self.assertRaisesRegex(ValueError,'RETIRED_OWNER'):
            self.poll(status(FIRST,'accepted',started=True),owner=OWNER,seq=100)

    def test_stale_poll_publishes_unavailable_without_faking_cleanup(self):
        self.start();self.now+=2.01;self.broker.tick()
        self.assertEqual(self.trial.calls[-1],dict(session=SID,status=UNAVAILABLE))
        self.assertFalse(self.trial.calls[-1]['status']['cleanupComplete'])
        self.assertEqual(self.poll(status(FIRST,'listening',started=True,stopped=False,cleanup=False))['command']['action'],'cancel')

    def test_capture_does_not_refresh_page_probe_or_exhaust_normal_budget(self):
        self.poll();self.assertIsNone(self.host.requested_at)
        self.client.seq=self.host.seq=512
        self.poll();self.assertEqual(self.client.seq,513)
        self.assertEqual(self.client.regular_requests,0);self.assertEqual(self.host.regular_requests,0)

    def test_unknown_status_and_command_are_rejected(self):
        row=status();row['transcript']='never sent';self.assertFalse(valid_status(row))
        for invalid in (status(stopped=True),status(cleanup=False),dict(status(),state=[])):
            self.assertFalse(valid_status(invalid))
        with self.assertRaisesRegex(ValueError,'AUTH'):self.poll(row)
        self.trial.commands.append({'action':'shell','request':FIRST})
        self.assertIsNone(self.poll()['command']);self.assertTrue(self.client.channel.closed)

    def test_expired_authenticated_capture_frame_never_calls_trial(self):
        self.poll();calls=len(self.trial.calls)
        packet=dict(schema=1,session=SID,seq=self.client.seq+1,action='capture_exchange',
                    status=status(),deadline=time.monotonic()-.01)
        self.client.channel.send(channel.signed(packet,self.client.key,b'request'),time.monotonic()+.5)
        self.thread.join(1);self.assertFalse(self.thread.is_alive())
        self.assertEqual(len(self.trial.calls),calls)


class Http(Fixture):
    def configured(self):
        self.cfg.update(ui_capture=True,ui_capture_key_path=str(self.write('ui-key',UI_KEY)))
        owner=self.entry();self.addCleanup(owner.close);return owner
    def packet(self, *, key=UI_KEY, seq=1):
        return sign(dict(schema=1,owner=OWNER,session=None,seq=seq,nonce='%016x'%seq,status=status()),derive_key(key),'request')
    def post(self, owner, packet=None, *, extra=None, peer='192.0.2.20', headers=None):
        body=canonical(packet if packet is not None else self.packet())
        headers=headers if headers is not None else [('Host','192.0.2.10:4181'),('Content-Type','application/json'),('Content-Length',str(len(body)))]+(extra or [])
        return self.request(owner,method='POST',path='/'+KEY+'/ui-capture',peer=peer,headers=headers,body=body)

    def test_default_disabled_and_independent_private_key_required(self):
        owner=self.entry();self.addCleanup(owner.close)
        self.assertEqual(self.post(owner)[0],404)
        for secret in (KEY,BROWSER_KEY,'F'*64):
            self.cfg.update(ui_capture=True,ui_capture_key_path=str(self.write('ui-key',secret)))
            with self.assertRaises(ValueError):self.entry()
        self.write('ui-key',UI_KEY).chmod(0o644)
        with self.assertRaisesRegex(ValueError,'PRIVATE_FILE'):self.entry()

    def test_real_http_hmac_and_nonce_response(self):
        owner=self.configured();code,reply,_=self.post(owner)
        self.assertEqual(code,200);self.assertIsNone(reply['session']);self.assertEqual(reply['nonce'],'0000000000000001')
        self.assertEqual(reply,sign({k:v for k,v in reply.items() if k!='mac'},derive_key(UI_KEY),'response'))
        self.assertEqual(self.post(owner,self.packet(key=KEY,seq=2))[0],403)

    def test_direct_peer_origin_host_and_enrollment_gates(self):
        owner=self.configured()
        for kwargs in ({'peer':'192.0.2.21'},{'extra':[('Origin','null')]},
                {'extra':[('Host','192.0.2.10:4181')]},
                {'headers':[('Host','foreign'),('Content-Type','application/json'),('Content-Length','1')]}):
            with self.subTest(kwargs=kwargs):self.assertEqual(self.post(owner,**kwargs)[0],403)
        self.now+=1100;self.assertEqual(self.post(owner)[0],403)

    def test_replay_get_and_body_bound(self):
        owner=self.configured();self.assertEqual(self.post(owner)[0],200);self.assertEqual(self.post(owner)[0],403)
        self.assertEqual(self.request(owner,path='/'+KEY+'/ui-capture')[0],405)
        headers=[('Host','192.0.2.10:4181'),('Content-Type','application/json'),('Content-Length','2049')]
        self.assertEqual(self.post(owner,headers=headers)[0],403)


if __name__=='__main__':unittest.main()
