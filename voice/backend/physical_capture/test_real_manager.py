"""Execute the actual generic Manager class, replacing only the worker thread.

Manager.start/status/command/session/locks are real. No process, socket, browser
or TV starts: the selected worker Thread records start without invoking work().
"""
import hashlib
import json
from pathlib import Path
import threading
import types
import unittest
from .test_consumer import Harness, good
from ..channel_adapter import ChannelBrowserAdapter
from ..controller import BrowserObservation
HERE=Path(__file__).resolve().parent

class InertWorker:
 def __init__(self,*args,**kwargs):self.started=False
 def start(self):self.started=True
 def join(self,*args,**kwargs):pass

def actual_manager():
 source=HERE.parents[2]/'spark/manager.py.in'
 raw=source.read_bytes()
 for k,v in {'SPARK_IP':'192.0.2.1','TV_IP':'192.0.2.2','SPARK_USER':'example','SPARK_HOSTNAME':'example'}.items():raw=raw.replace(('{{'+k+'}}').encode(),v.encode())
 assert b'{{' not in raw
 mod=types.ModuleType('actual_manager_fixture');mod.__file__=str(source)
 exec(compile(raw,mod.__file__,'exec'),mod.__dict__)
 # Replace only this module's threading binding; global real Thread remains real.
 mod.threading=types.SimpleNamespace(RLock=threading.RLock,Event=threading.Event,Thread=InertWorker)
 return mod.Manager(spawn=lambda *_a,**_k:(_ for _ in ()).throw(AssertionError('native spawn')))

class ActualHarness(Harness):
 def __init__(self):
  super().__init__();self.manager=actual_manager();self.starts=[]
  self.spawn=types.SimpleNamespace(manager=self.manager,client=self.lookup)
  self.manager.control_spawn=self.spawn
  self.client=types.SimpleNamespace(session=None,observe=self.observe,queue_navigation=self.navigate)
  self.runtime=ChannelBrowserAdapter(self.manager,self.spawn,viewer=self.viewer,qualified=True,require_idle=True)
  self.owner=types.SimpleNamespace(manager=self.manager,runtime=self.runtime,viewer=self.viewer,enabled=True)
 def lookup(self,sid,**kwargs):return self.client if self.client.session==sid else None
 def open(self,session,op,**kwargs):
  self.calls.append(('open',session,op));self.starts.append(session)
  with self.manager.lock:
   self.assert_session(session)
   self.manager.state='ready';self.manager.session['claimed']=True
   self.client.session=session
  if self.after_open:self.after_open()
  return 'accepted'
 def assert_session(self,sid):
  assert self.manager.session['id']==sid and self.manager.session['worker'].started
 def observe(self,**kwargs):return BrowserObservation(self.client.session,True,True,False,self.url)

class RealManagerTests(unittest.TestCase):
 def test_real_rlock_start_session_and_controller_navigation(self):
  h=ActualHarness();r=h.consumer().run()
  self.assertEqual(r['code'],'BROWSER_NAVIGATION_ACCEPTED');self.assertEqual(len(h.starts),1)
  self.assertEqual(h.manager.status(),{'state':'ready'});self.assertEqual(h.runtime.identity,h.manager.session['id'])
  self.assertFalse(h.manager.closed);self.assertEqual([x[0]for x in h.calls],['open','navigate'])
 def test_real_manager_active_session_never_reused(self):
  h=ActualHarness();code,row=h.manager.start();self.assertEqual(code,202)
  h.manager.state='ready';h.manager.session['claimed']=True;old=h.manager.session
  r=h.consumer().run();self.assertEqual(r['code'],'OWNER_UNAVAILABLE')
  self.assertIs(h.manager.session,old);self.assertEqual(h.calls,[]);self.assertEqual(h.captures,0)
 def test_cancel_during_adapter_lock_with_real_manager_no_start(self):
  h=ActualHarness();c=h.consumer();entered=threading.Event();results=[];original=h.runtime.launch_browser
  def wrapped(*a,**k):entered.set();return original(*a,**k)
  h.runtime.launch_browser=wrapped;h.runtime.lock.acquire()
  worker=threading.Thread(target=lambda:results.append(c.run()));worker.start()
  try:
   self.assertTrue(entered.wait(1));done=[]
   canceller=threading.Thread(target=lambda:done.append(c.cancel()));canceller.start();canceller.join(.5)
   self.assertFalse(canceller.is_alive());self.assertEqual(done,[True])
  finally:h.runtime.lock.release()
  worker.join(2);self.assertFalse(worker.is_alive())
  self.assertEqual(h.manager.status(),{'state':'idle'});self.assertIsNone(h.manager.session)
  self.assertEqual(h.calls,[]);self.assertEqual(results[0]['state'],'unknown')
 def test_current_session_replaced_after_launch_no_navigation(self):
  h=ActualHarness()
  def replace():
   with h.manager.lock:h.manager.session=dict(id='b'*32,claimed=True,url='other')
  h.after_open=replace;r=h.consumer().run()
  self.assertEqual(r['code'],'BROWSER_NOT_READY');self.assertEqual([x[0]for x in h.calls],['open'])
  self.assertEqual(h.manager.session['id'],'b'*32)
if __name__=='__main__':unittest.main()
