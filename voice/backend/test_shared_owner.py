"""Real rendered manager and owned model pipes; synthetic inference/TV/service.

No native model, CUDA, seccomp, microphone, listener or remote action is invoked.
"""
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import re
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from model_worker import ModelWorker
from shared_owner import SharedVoiceOwner, AssemblyRefused
from service import VoiceService
from ingress import DOMAIN
from test_sealed_startup import ready

ROOT = Path(__file__).resolve().parents[2]
KEY = 'c'*64
DEVICE_KEY, CHALLENGE = b'k'*32, b'q'*32
STUB = r'''
import json,os,struct,sys,time
mode,ready=json.loads(sys.argv[1])
def exact(n):
 b=b''
 while len(b)<n:
  part=os.read(0,n-len(b))
  if not part:raise EOFError()
  b+=part
 return b
def send(row):
 raw=json.dumps(row,separators=(',',':')).encode();raw=struct.pack('>I',len(raw))+raw
 while raw:
  n=os.write(1,raw)
  if n<=0:raise EOFError()
  raw=raw[n:]
if mode=='slow':time.sleep(.2)
if mode=='foreign':ready['attempt']='0'*32
send({'schema':2,'state':'ready','isolation':ready})
try:
 for index in range(1,257):
  size=struct.unpack('>I',exact(4))[0]
  if not 1<=size<=8192:raise ValueError()
  row=json.loads(exact(size))
  if row['id']!=index:raise ValueError()
  if row['action']=='asr':
   exact(row['bytes'])
   if mode=='asr-failure':raise ValueError()
   text='open YouTube in browser'
  else:text='{"action":"browser_open","destination":"youtube"}'
  send({'schema':1,'id':index,'action':row['action'],'text':text})
except EOFError:pass
except Exception:raise SystemExit(3)
'''


class VerifiedSocket:
    def __init__(self):
        hello={'schema':1,'mode':'command','rate':16000,'channels':1,'format':'s16le',
               'tag':hmac.digest(DEVICE_KEY,DOMAIN+CHALLENGE,'sha256').hex()}
        raw=json.dumps(hello).encode()
        self.data=bytearray(struct.pack('>I',len(raw))+raw+struct.pack('>II',0,640)+b'\0'*640+struct.pack('>II',1,0))
        self.sent=b''
    def settimeout(self,_):pass
    def sendall(self,data):self.sent+=data
    def recv(self,n):
        data=bytes(self.data[:n]);del self.data[:n];return data


class SharedOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.out=Path(cls.temp.name).resolve()/'built'
        sys.path.insert(0,str(ROOT/'tools'))
        import build
        build.build(build.config((ROOT/'config/example.json').read_bytes()),cls.out)
        sys.path.insert(0,str(cls.out/'spark'))
        cls.saved={k:v for k,v in sys.modules.items() if k=='manager' or k=='voice_control' or k.startswith('voice_control.')}
        for k in cls.saved:sys.modules.pop(k)
        import manager
        cls.rendered=manager
        from voice_control.viewer_handoff import TVViewerHandoff
        from voice_control.controller import BrowserObservation
        cls.Handoff,cls.Observation=TVViewerHandoff,BrowserObservation

    @classmethod
    def tearDownClass(cls):
        for k in list(sys.modules):
            if k=='manager' or k=='voice_control' or k.startswith('voice_control.'):sys.modules.pop(k)
        sys.modules.update(cls.saved)
        sys.path.remove(str(cls.out/'spark'));sys.path.remove(str(ROOT/'tools'));cls.temp.cleanup()

    def setUp(self):
        self.temp_run=tempfile.TemporaryDirectory();self.stub=Path(self.temp_run.name)/'child.py';self.stub.write_text(STUB)
        self.processes=[];self.owners=[];self.calls=[];self.managers=[]

    def tearDown(self):
        for owner in self.owners:self.assertTrue(owner.close())
        for process in self.processes:self.assertIsNotNone(process.poll())
        for manager in self.managers:manager.close()
        self.temp_run.cleanup()

    def assemble(self,mode='ordinary',enabled=True,qualified=True):
        def spawn(argv,**kwargs):
            process=subprocess.Popen([sys.executable,'-B',str(self.stub),json.dumps([mode,ready(argv[-1])])],**kwargs)
            self.processes.append(process);return process
        worker=ModelWorker(str(Path(sys.executable).resolve()),'/synthetic/asr','/synthetic/intent',qualified=qualified,spawn=spawn)
        manager=self.rendered.Manager(voice_control=True);self.managers.append(manager)
        selected={'url':None}
        def launch(payload,**kwargs):
            self.calls.append(payload)
            self.assertEqual(payload['id'],'com.webos.app.tinybrowser')
            identity=re.fullmatch('http://192.0.2.10:4181/'+KEY+'/open#voice-session=([a-f0-9]{32})',payload['params']['contentTarget'])[1]
            self.assertEqual(manager.session['id'],identity)
            manager.session['process']=types.SimpleNamespace(poll=lambda:None)
            manager.session['url']='http://192.0.2.10:4177/'+'d'*48+'/viewer';manager.state='ready'
            self.assertEqual(manager.command('claim',{'id':identity})[0],200)
            def queue(body,**kwargs):selected['url']=body['url'];return 'accepted'
            manager.control_spawn.clients[identity]=types.SimpleNamespace(session=identity,channel=types.SimpleNamespace(closed=False),
                observe=lambda **kwargs:self.Observation(identity,True,True,False,selected['url']),queue_navigation=queue)
            return {'returnValue':True}
        viewer=self.Handoff('192.0.2.10',KEY,launch,qualified=True)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=context.maximum_version=ssl.TLSVersion.TLSv1_3;context.verify_mode=ssl.CERT_REQUIRED
        owner=SharedVoiceOwner(manager,worker,viewer,KEY,'192.0.2.10',4443,context,'a'*64,DEVICE_KEY,enabled=enabled)
        owner.ingress.challenge=lambda _:CHALLENGE
        self.owners.append(owner)
        return owner

    def test_actual_pipe_to_rendered_controller_and_same_http_manager(self):
        owner=self.assemble();owner.warm(timeout_s=2)
        bound=[]
        for value in vars(owner.http_handler).values():
            bound.extend(cell.cell_contents for cell in getattr(value,'__closure__',()) or ())
        self.assertTrue(any(value is owner.manager for value in bound))
        self.assertIs(owner.runtime.manager,owner.manager)
        observed=[]
        def serve(service,**kwargs):
            self.assertIs(service,owner.service);self.assertIs(service.ingress,owner.ingress)
            self.assertEqual(kwargs['max_connections'],128);self.assertLessEqual(kwargs['lifetime_s'],1500)
            # Verified framing fixture, not a substitute for the TLS socket gate.
            with patch.object(self.rendered.threading,'Thread'):
                observed.append(service.ingress._verified_connection(VerifiedSocket()))
        with patch.object(VoiceService,'serve',serve):owner.serve()
        self.assertEqual((observed[0].state,observed[0].code),('accepted','BROWSER_NAVIGATION_ACCEPTED'))
        self.assertEqual(owner.worker.counter,2);self.assertEqual(len(self.calls),1)
        self.assertEqual(owner.manager.state,'ready');self.assertFalse(owner.manager.closed)
        self.assertTrue(owner.cleanup_complete);self.assertEqual(owner.state,'closed')
        self.assertIsNone(owner.worker.process)

    def test_booleans_without_handshake_or_disabled_owner_never_serve(self):
        for enabled,qualified in ((False,True),(True,False),(True,True)):
            owner=self.assemble(enabled=enabled,qualified=qualified)
            self.assertFalse(owner.ingress.asr.qualified)
            with patch.object(VoiceService,'serve',side_effect=AssertionError('must not listen')):
                with self.assertRaises(AssemblyRefused):owner.serve()
            if not enabled or not qualified:
                with self.assertRaises(AssemblyRefused):owner.warm(timeout_s=2)
        self.assertEqual(self.processes,[]);self.assertEqual(self.calls,[])

    def test_foreign_handshake_is_cleaned_without_listener_or_tv_action(self):
        owner=self.assemble('foreign')
        with self.assertRaises(AssemblyRefused):owner.warm(timeout_s=2)
        self.assertFalse(owner.ingress.asr.qualified);self.assertTrue(owner.cleanup_complete)
        self.assertIsNone(owner.worker.process);self.assertEqual(self.calls,[])

    def test_source_asr_failure_maps_to_rendered_refusal_without_navigation(self):
        owner=self.assemble('asr-failure');owner.warm(timeout_s=2)
        result=owner.ingress._verified_connection(VerifiedSocket())
        self.assertEqual((result.state,result.code),('refused','ASR_UNAVAILABLE'))
        self.assertEqual(self.calls,[]);self.assertIsNone(owner.worker.process)

    def test_live_worker_exit_budget_and_age_invalidate_admission(self):
        owner=self.assemble();owner.warm(timeout_s=2)
        self.assertTrue(owner.ingress.asr.qualified)
        owner.worker.counter=255;self.assertFalse(owner.ingress.asr.qualified)
        owner.worker.counter=0;owner.worker.created-=1800;self.assertFalse(owner.ingress.asr.qualified)
        owner.worker.created+=1800
        owner.worker.process.terminate();owner.worker.process.wait(timeout=1)
        self.assertFalse(owner.ingress.asr.qualified)
        with self.assertRaises(AssemblyRefused):owner.serve()

    def test_service_lifetime_reserves_final_command_and_cleanup(self):
        owner=self.assemble();owner.warm(timeout_s=2)
        owner.worker.created-=1600
        observed=[]
        def serve(service,**kwargs):observed.append(kwargs)
        with patch.object(VoiceService,'serve',serve):owner.serve()
        self.assertEqual(len(observed),1)
        self.assertGreater(observed[0]['lifetime_s'],75)
        self.assertLessEqual(observed[0]['lifetime_s'],80)
        self.assertTrue(owner.cleanup_complete)

    def test_close_does_not_take_an_externally_started_worker(self):
        owner=self.assemble()
        owner.worker.start(timeout_s=2)
        process=owner.worker.process
        self.assertFalse(owner.ingress.asr.qualified)
        self.assertFalse(owner.close())
        self.assertIsNone(process.poll())
        self.assertTrue(owner.worker.close())

    def test_owner_does_not_report_cleanup_while_power_reader_is_unclosed(self):
        owner=self.assemble()
        outcome=[False]
        owner.tv_power_reader=types.SimpleNamespace(close=lambda **kwargs:outcome[0])
        self.assertFalse(owner.close())
        self.assertFalse(owner.cleanup_complete)
        self.assertEqual(owner.state,'cleanup-pending')
        outcome[0]=True
        self.assertTrue(owner.close())
        self.assertTrue(owner.close())

    def test_interruption_after_real_worker_ready_finishes_owner_cleanup(self):
        for interruption in (KeyboardInterrupt(), SystemExit(29)):
            with self.subTest(kind=type(interruption)):
                owner=self.assemble()
                def interrupt(*args,**kwargs):
                    self.assertIsNotNone(owner.worker.process)
                    self.assertIsNone(owner.worker.process.poll())
                    self.assertIsNotNone(owner.worker.sealed)
                    raise interruption
                with patch.object(owner,'_worker_ready',side_effect=interrupt):
                    with self.assertRaises(type(interruption)) as caught:
                        owner.warm(timeout_s=2)
                self.assertIs(caught.exception,interruption)
                process=self.processes[-1]
                self.assertIsNotNone(process.poll())
                self.assertTrue(process.stdin.closed);self.assertTrue(process.stdout.closed)
                self.assertTrue(owner.cleanup_complete);self.assertTrue(owner.quiet.is_set())
                self.assertEqual(owner.state,'closed');self.assertIsNone(owner.owner_thread)
                self.assertIsNone(owner.worker.process);self.assertIsNone(owner.worker.sealed)
                self.assertFalse(owner.manager.closed);self.assertEqual(self.calls,[])

    def test_close_during_startup_reports_pending_until_child_cleanup_finishes(self):
        owner=self.assemble('slow');errors=[]
        def warm():
            try:owner.warm(timeout_s=2)
            except AssemblyRefused:errors.append('closed')
        thread=threading.Thread(target=warm);thread.start()
        deadline=time.monotonic()+1
        while owner.worker.process is None and time.monotonic()<deadline:time.sleep(.005)
        self.assertIsNotNone(owner.worker.process)
        self.assertFalse(owner.close(timeout_s=.01))
        thread.join(3);self.assertFalse(thread.is_alive());self.assertEqual(errors,['closed'])
        self.assertTrue(owner.close());self.assertTrue(owner.cleanup_complete)
        self.assertIsNone(owner.worker.process);self.assertFalse(owner.manager.closed)

    def test_pooled_shared_owner_serves_commands_and_cleans_up(self):
        from voice_control import pool as pool_mod
        def spawn(argv,**kwargs):
            process=subprocess.Popen([sys.executable,'-B',str(self.stub),json.dumps(['ordinary',ready(argv[-1])])],**kwargs)
            self.processes.append(process);return process
        factory=pool_mod.model_factory(
            str(Path(sys.executable).resolve()),'/synthetic/asr','/synthetic/intent',
            qualified=True,spawn=spawn)
        worker_pool=pool_mod.WorkerPool(factory,rotate_after_calls=2)
        readings=[{'returnValue':True,'state':'Active'}]
        power_reader=pool_mod.AuthenticatedTvPowerReader(read_fn=lambda:readings[-1],poll_interval_s=5.0)
        manager=self.rendered.Manager();self.managers.append(manager)
        selected={'url':None}
        def launch(payload,**kwargs):
            self.calls.append(payload)
            identity=re.fullmatch('http://192.0.2.10:4181/'+KEY+'/open#voice-session=([a-f0-9]{32})',payload['params']['contentTarget'])[1]
            manager.session['process']=types.SimpleNamespace(poll=lambda:None)
            manager.session['url']='http://192.0.2.10:4177/'+'d'*48+'/viewer';manager.state='ready'
            self.assertEqual(manager.command('claim',{'id':identity})[0],200)
            def queue(body,**kwargs):selected['url']=body['url'];return 'accepted'
            manager.control_spawn.clients[identity]=types.SimpleNamespace(
                session=identity,channel=types.SimpleNamespace(closed=False),
                observe=lambda **kwargs:self.Observation(identity,True,True,False,selected['url']),
                queue_navigation=queue)
            return {'returnValue':True}
        viewer=self.Handoff('192.0.2.10',KEY,launch,qualified=True)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=context.maximum_version=ssl.TLSVersion.TLSv1_3;context.verify_mode=ssl.CERT_REQUIRED
        owner=SharedVoiceOwner(
            manager,worker_pool,viewer,KEY,'192.0.2.10',4443,context,'a'*64,DEVICE_KEY,
            enabled=True,tv_power_reader=power_reader)
        owner.ingress.challenge=lambda _:CHALLENGE
        self.owners.append(owner)
        owner.warm(timeout_s=2)
        self.assertTrue(owner.ingress.asr.qualified)
        self.assertEqual(worker_pool.summary()['state'],'ready')
        with patch.object(self.rendered.threading,'Thread'):
            first=owner.ingress._verified_connection(VerifiedSocket())
        self.assertEqual((first.state,first.code),('accepted','BROWSER_NAVIGATION_ACCEPTED'))
        self.assertIsNone(worker_pool.leased)
        self.assertEqual(worker_pool.poll(),'ready')
        deadline=time.monotonic()+2
        while worker_pool.rotations<1 and time.monotonic()<deadline:
            worker_pool.poll();time.sleep(.01)
        self.assertEqual(worker_pool.rotations,1)
        with patch.object(self.rendered.threading,'Thread'):
            second=owner.ingress._verified_connection(VerifiedSocket())
        self.assertEqual((second.state,second.code),('accepted','BROWSER_NAVIGATION_ACCEPTED'))
        self.assertTrue(owner.close())
        self.assertTrue(owner.cleanup_complete)
        self.assertEqual(worker_pool.summary()['state'],'empty')


if __name__=='__main__':unittest.main()
