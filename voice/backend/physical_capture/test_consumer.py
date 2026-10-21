import copy
import json
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import patch
from . import physical_consumer as p
from . import join_schema as j
from . import native_activation as activation
from ..channel_adapter import ChannelBrowserAdapter
from ..controller import BrowserObservation
HERE=Path(__file__).resolve().parent
SESSION='a'*32

def good():
 server=dict.fromkeys(j.SERVER_FLAGS,True)
 server.update(schema=1,kind='physical-speech-tls-server',pcmBytes=960,warmMs=1,asrMs=1,intentMs=1,
  physicalCaptureProven=False,commandDispatched=False,failure=None)
 owner=dict(schema=1,kind='physical-speech-owner',cleanupVerified=True,remainingOwned=0,childExit=0,
  sourcesRemoved=True,pipesClosed=True,drainersJoined=True,failure=None)
 adapter=dict(phase='complete',error=None,exceptionClass=None,runEntered=True,childStarted=True,
  childReaped=True,childExitCode=0,childSignal=None,forcedTermination=False)
 return dict(schema=1,kind='physical-speech-composition',failure=None,failurePhase=None,exceptionClass=None,
  server=server,tv=json.loads((HERE/'synthetic_capture.json').read_text()),tvAdapter=adapter,owner=owner,
  compositionQualified=True,ownedSshExited=True,privateASRQualified=False,GPUOrBrokerPrivacyQualified=False,
  wholeTvZeroLgTrafficProven=False,microphoneActivated=True,commandDispatched=False)
class Clock:
 def __init__(self):self.now=0
 def __call__(self):return self.now
 def wait(self,n):self.now+=n
class Manager:
 def __init__(self):
  self.lock=threading.RLock();self.closed=False;self.state='idle';self.session=None
  self.starts=0;self.statuses=0;self.race=None
 def status(self):
  self.statuses+=1
  if self.race:self.race(self.statuses)
  return {'state':self.state}
 def start(self):
  self.starts+=1;self.state='starting';self.session={'id':SESSION,'claimed':False,'url':'viewer'}
  return 202,{'id':SESSION}
 def command(self,*a):raise AssertionError('unexpected manager mutation')
class Harness:
 def __init__(self):
  self.clock=Clock();self.manager=Manager();self.calls=[];self.url=None;self.after_open=None;self.nav_result='accepted'
  self.spawn=types.SimpleNamespace(manager=self.manager,client=lambda sid,**kw:self.client if sid==SESSION else None)
  self.manager.control_spawn=self.spawn
  self.client=types.SimpleNamespace(session=SESSION,observe=self.observe,queue_navigation=self.navigate)
  self.viewer=types.SimpleNamespace(enabled=True,open=self.open)
  self.runtime=ChannelBrowserAdapter(self.manager,self.spawn,viewer=self.viewer,qualified=True,require_idle=True)
  self.owner=types.SimpleNamespace(manager=self.manager,runtime=self.runtime,viewer=self.viewer,enabled=True)
  self.captures=0
 def open(self,session,op,**kwargs):
  self.calls.append(('open',session,op));self.manager.state='ready';self.manager.session['claimed']=True
  if self.after_open:self.after_open()
  return 'accepted'
 def observe(self,**kwargs):return BrowserObservation(SESSION,True,True,False,self.url)
 def navigate(self,body,**kwargs):
  self.calls.append(('navigate',body['url'],kwargs['operation']))
  if self.nav_result=='exception':raise RuntimeError('PRIVATE-SENTINEL')
  if self.nav_result=='accepted':self.url=body['url']
  return self.nav_result
 def runner(self,deliver,**kwargs):self.captures+=1;deliver(good())
 def consumer(self,runner=None,qualified=True):
  return p.OneUsePhysicalConsumer(self.owner,runner or self.runner,qualified=qualified,clock=self.clock,wait=self.clock.wait)
class ConsumerTests(unittest.TestCase):
 def test_real_controller_and_channel_navigate_once_after_current_join(self):
  h=Harness();c=h.consumer();r=c.run()
  self.assertEqual((r['state'],r['code']),('accepted','BROWSER_NAVIGATION_ACCEPTED'))
  self.assertTrue(r['captureQualified']);self.assertTrue(r['commandAttempted']);self.assertFalse(r['presentationProven'])
  self.assertEqual([v[0]for v in h.calls],['open','navigate']);self.assertEqual(h.calls[1][1],'https://www.youtube.com')
  self.assertIs(h.runtime.manager,h.manager);self.assertFalse(h.manager.closed);self.assertEqual(h.captures,1)
  self.assertEqual(c.run()['code'],'CONSUMER_REPLAY');self.assertEqual(h.manager.starts,1)
 def test_default_and_native_entry_refuse_before_runner(self):
  h=Harness();r=h.consumer(qualified=False).run();self.assertEqual(r['code'],'CONSUMER_UNARMED');self.assertEqual(h.captures,0)
  with self.assertRaisesRegex(RuntimeError,'UNARMED'):activation.assemble(lambda:self.fail('called'))
  self.assertIsNone(activation.READY_RECEIPT_SHA);self.assertIsNone(activation.FIRST_PHYSICAL_RECEIPT_SHA)
 def test_no_imported_receipt_api(self):
  h=Harness();c=h.consumer(lambda *_a,**_kw:good());r=c.run()
  self.assertEqual(r['code'],'CAPTURE_UNCONFIRMED');self.assertEqual(h.calls,[])
  with self.assertRaises(TypeError):h.consumer().run(good())
 def test_bad_or_duplicate_delivery_consumes_without_action(self):
  for mutate in [lambda x:x.update(compositionQualified=False),lambda x:x['server'].update(expectedIntentMatched=False),
                 lambda x:x['tv'].update(streamStopObserved=False),lambda x:x['tv'].update(quietMs=1999),
                 lambda x:x['tv'].update(temporaryCleanup=False),lambda x:x['owner'].update(sourcesRemoved=False),
                 lambda x:x['server'].update(pcmBytes=962),lambda x:x['tvAdapter'].update(forcedTermination=True)]:
   h=Harness()
   def run(deliver,**kw):v=good();mutate(v);deliver(v)
   c=h.consumer(run);self.assertEqual(c.run()['code'],'CURRENT_CAPTURE_UNQUALIFIED');self.assertEqual(h.calls,[])
   self.assertEqual(c.run()['code'],'CONSUMER_REPLAY')
  h=Harness()
  def double(deliver,**kw):deliver(good());deliver(good())
  self.assertEqual(h.consumer(double).run()['code'],'CAPTURE_REPLAY');self.assertEqual(h.calls,[])
 def test_all_server_success_and_cleanup_flags_required(self):
  for key in j.SERVER_FLAGS:
   v=good();v['server'][key]=False
   with self.assertRaises(ValueError):j.qualified_current(v)
  for key in ('cleanupVerified','sourcesRemoved','pipesClosed','drainersJoined'):
   v=good();v['owner'][key]=False
   with self.assertRaises(ValueError):j.qualified_current(v)
 def test_all_physical_success_flags_and_diagnostics_required(self):
  for key,value in good()['tv'].items():
   if value is True:
    v=good();v['tv'][key]=False
    with self.assertRaises(ValueError,msg=key):j.qualified_current(v)
  for key,value in [('recordingError','PROCESS_RACE'),('childExitCode',1),('recordingReady',False),('identityChangeMask',1)]:
   v=good();v['tv']['nativeDiagnostic'][key]=value
   with self.assertRaises(ValueError):j.qualified_current(v)
 def test_private_fields_and_contradictions_refused(self):
  for target,key,value in [('server','transcript','PRIVATE-SENTINEL'),('owner','remainingOwned',True),('tv','pcmBytes',True)]:
   v=good();v[target][key]=value
   with self.assertRaises(ValueError):j.qualified_current(v)
 def test_capture_deadline_and_cancel_have_no_command(self):
  h=Harness()
  def late(deliver,**kw):h.clock.now=251;deliver(good())
  self.assertEqual(h.consumer(late).run()['code'],'CAPTURE_EXPIRED');self.assertEqual(h.calls,[])
  h=Harness();c=None
  def cancelled(deliver,**kw):deliver(good());c.cancel()
  c=h.consumer(cancelled);self.assertEqual(c.run()['code'],'CAPTURE_CANCELLED');self.assertEqual(h.calls,[])
 def test_foreign_thread_cannot_deliver_current_result(self):
  h=Harness()
  def foreign(deliver,**kw):
   t=threading.Thread(target=lambda:deliver(good()));t.start();t.join()
  self.assertEqual(h.consumer(foreign).run()['code'],'CAPTURE_EXPIRED');self.assertEqual(h.calls,[])
 def test_concurrent_reentry_cannot_capture_or_mutate_twice(self):
  h=Harness();entered=threading.Event();release=threading.Event();results=[]
  def blocked(deliver,**kw):entered.set();release.wait(2);deliver(good())
  c=h.consumer(blocked);t=threading.Thread(target=lambda:results.append(c.run()));t.start();self.assertTrue(entered.wait(1))
  self.assertEqual(c.run()['code'],'CONSUMER_REPLAY');release.set();t.join(2);self.assertFalse(t.is_alive())
  self.assertEqual(results[0]['code'],'BROWSER_NAVIGATION_ACCEPTED');self.assertEqual(h.manager.starts,1)
 def test_owner_swapped_during_capture_refuses(self):
  h=Harness()
  def swap(deliver,**kw):deliver(good());h.owner.manager=Manager()
  self.assertEqual(h.consumer(swap).run()['code'],'OWNER_UNAVAILABLE');self.assertEqual(h.calls,[])
 def test_atomic_idle_gate_refuses_racing_browser_session(self):
  h=Harness()
  def race(n):
   if n==3:h.manager.state='ready';h.manager.session={'id':SESSION,'claimed':True,'url':'other'}
  h.manager.race=race
  r=h.consumer().run();self.assertEqual(r['code'],'BROWSER_START_REJECTED')
  self.assertEqual(h.manager.starts,0);self.assertEqual(h.calls,[])
 def test_active_browser_or_wrong_runtime_policy_refuses_before_capture(self):
  h=Harness();h.manager.state='ready';self.assertEqual(h.consumer().run()['code'],'OWNER_UNAVAILABLE');self.assertEqual(h.captures,0)
  h=Harness();h.runtime.require_idle=False;self.assertEqual(h.consumer().run()['code'],'OWNER_UNAVAILABLE');self.assertEqual(h.captures,0)
 def test_owner_loss_after_launch_prevents_navigation(self):
  h=Harness();h.after_open=lambda:setattr(h.owner,'runtime',None)
  r=h.consumer().run();self.assertEqual(r['code'],'EXECUTION_UNCONFIRMED');self.assertTrue(r['commandAttempted'])
  self.assertEqual([x[0]for x in h.calls],['open'])
 def test_cancel_after_mutation_is_unknown_and_not_replayed(self):
  h=Harness();c=h.consumer();h.after_open=c.cancel
  r=c.run();self.assertEqual(r['state'],'unknown');self.assertEqual([x[0]for x in h.calls],['open'])
  self.assertEqual(c.run()['code'],'CONSUMER_REPLAY')
 def test_navigation_unknown_does_not_retry_or_leak(self):
  h=Harness();h.nav_result='exception';r=h.consumer().run()
  self.assertEqual(r['code'],'EXECUTION_UNCONFIRMED');self.assertEqual(len(h.calls),2);self.assertNotIn('PRIVATE-SENTINEL',json.dumps(r))
 def test_current_invocation_callback_expires_on_return(self):
  h=Harness();saved=[]
  def runner(deliver,**kw):saved.append(deliver);deliver(good())
  c=h.consumer(runner);self.assertEqual(c.run()['code'],'BROWSER_NAVIGATION_ACCEPTED')
  self.assertFalse(saved[0](good()));self.assertEqual(h.manager.starts,1)
 def test_no_saved_payload_or_outcome_private_data(self):
  h=Harness();c=h.consumer();r=c.run()
  self.assertIsNone(c.invocation);self.assertIsNone(c.thread)
  self.assertNotIn('nativeDiagnostic',vars(c));self.assertNotIn('https',json.dumps(r));self.assertNotIn('pcm',json.dumps(r))
 def test_dispatch_deadline_rechecked_after_final_idle_lock(self):
  h=Harness()
  def delayed(n):
   if n==2:h.clock.now=250
  h.manager.race=delayed
  self.assertEqual(h.consumer().run()['code'],'CAPTURE_EXPIRED');self.assertEqual(h.calls,[])
 def test_cancel_while_adapter_lock_waiting_prevents_start_and_does_not_deadlock(self):
  h=Harness();c=h.consumer();results=[];entered=threading.Event()
  original=h.runtime.launch_browser
  def marked(*args,**kwargs):entered.set();return original(*args,**kwargs)
  h.runtime.launch_browser=marked
  h.runtime.lock.acquire()
  t=threading.Thread(target=lambda:results.append(c.run()));t.start()
  try:
   self.assertTrue(entered.wait(1))
   cancelled=[];x=threading.Thread(target=lambda:cancelled.append(c.cancel()));x.start();x.join(.5)
   self.assertFalse(x.is_alive());self.assertEqual(cancelled,[True])
  finally:h.runtime.lock.release()
  t.join(2);self.assertFalse(t.is_alive());self.assertEqual(h.manager.starts,0);self.assertEqual(h.calls,[])
  self.assertEqual(results[0]['state'],'unknown');self.assertEqual(c.run()['code'],'CONSUMER_REPLAY')
 def test_cancel_while_manager_lock_waiting_prevents_start(self):
  h=Harness();c=h.consumer();results=[];entered=threading.Event();release=threading.Event()
  original=h.runtime.launch_browser
  def marked(*args,**kwargs):entered.set();release.wait(1);return original(*args,**kwargs)
  h.runtime.launch_browser=marked
  t=threading.Thread(target=lambda:results.append(c.run()));t.start();self.assertTrue(entered.wait(1))
  with h.manager.lock:
   release.set();self.assertTrue(c.cancel())
  t.join(2);self.assertFalse(t.is_alive());self.assertEqual(h.manager.starts,0);self.assertEqual(h.calls,[])
 def test_cancel_during_navigation_client_lookup_prevents_queue(self):
  h=Harness();c=h.consumer();lookups=0;original=h.spawn.client
  def client(*args,**kwargs):
   nonlocal lookups
   lookups+=1
   if lookups==2:c.cancel()
   return original(*args,**kwargs)
  h.spawn.client=client
  r=c.run();self.assertEqual(r['state'],'unknown');self.assertEqual([x[0] for x in h.calls],['open'])
if __name__=='__main__':unittest.main()
