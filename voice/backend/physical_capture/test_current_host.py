import threading
import types
import unittest
from .current_host import CurrentHostCapture
from .test_consumer import Harness,good

class CurrentHostTests(unittest.TestCase):
 def binding(self,run=None,qualified=True):
  host=types.ModuleType('fixture_current_host');host.run=run or (lambda *_a,**_k:good())
  tv=types.ModuleType('fixture_current_tv');tv.run=lambda:None;tv.result=lambda x:x;tv.qualify=lambda *_:True
  return CurrentHostCapture(host,tv,{'fixture-source':'a'*64},qualified=qualified)
 def test_actual_run_signature_delivers_current_only_once(self):
  calls=[]
  def run(pins,tvr,tvp,tvq,status=None):calls.append((pins,tvr,tvp,tvq,status));return good()
  bound=self.binding(run);h=Harness();r=h.consumer(bound).run()
  self.assertEqual(r['code'],'BROWSER_NAVIGATION_ACCEPTED');self.assertEqual(len(calls),1);self.assertIsNone(calls[0][-1])
  with self.assertRaisesRegex(ValueError,'REPLAY'):bound(lambda _:True,timeout_s=250)
  self.assertFalse(any(isinstance(x,dict)and 'tv'in x for x in vars(bound).values()))
 def test_default_unarmed_before_host_and_consumes(self):
  calls=[];bound=self.binding(lambda *_a,**_k:calls.append(1),qualified=False)
  with self.assertRaisesRegex(ValueError,'UNARMED'):bound(lambda _:True,timeout_s=250)
  self.assertEqual(calls,[])
  with self.assertRaisesRegex(ValueError,'REPLAY'):bound(lambda _:True,timeout_s=250)
 def test_replaced_source_or_bad_result_refuses_delivery(self):
  for mode in('before','after','bad'):
   received=[];bound=None
   def run(*_a,**_k):
    if mode=='after':bound.tv.run=lambda:None
    row=good()
    if mode=='bad':row['owner']['sourcesRemoved']=False
    return row
   bound=self.binding(run)
   if mode=='before':bound.host.run=lambda:good()
   with self.assertRaises(ValueError):bound(lambda x:received.append(x),timeout_s=250)
   self.assertEqual(received,[])
 def test_host_exception_and_callback_refusal_never_retry(self):
  calls=[]
  def run(*_a,**_k):calls.append(1);raise RuntimeError('PRIVATE-SENTINEL')
  bound=self.binding(run)
  with self.assertRaises(RuntimeError):bound(lambda _:True,timeout_s=250)
  with self.assertRaisesRegex(ValueError,'REPLAY'):bound(lambda _:True,timeout_s=250)
  self.assertEqual(calls,[1])
  bound=self.binding()
  with self.assertRaisesRegex(ValueError,'REFUSED'):bound(lambda _:False,timeout_s=250)
 def test_concurrent_second_invocation_is_consumed(self):
  entered=threading.Event();release=threading.Event();results=[]
  def run(*_a,**_k):entered.set();release.wait(2);return good()
  bound=self.binding(run)
  t=threading.Thread(target=lambda:results.append(bound(lambda _:True,timeout_s=250)));t.start();self.assertTrue(entered.wait(1))
  try:
   with self.assertRaisesRegex(ValueError,'REPLAY'):bound(lambda _:True,timeout_s=250)
  finally:release.set()
  t.join(2);self.assertFalse(t.is_alive());self.assertEqual(results,[None])
 def test_preserves_independent_host_cleanup_after_consumer_cancel(self):
  h=Harness();c=None;clean=[]
  def run(*_a,**_k):c.cancel();clean.append('host-cleanup-finished');return good()
  c=h.consumer(self.binding(run));r=c.run()
  self.assertEqual(clean,['host-cleanup-finished']);self.assertEqual(h.calls,[])
  # Host result reaches a cancelled invocation; no command authorization.
  self.assertEqual(r['state'],'refused')
if __name__=='__main__':unittest.main()
