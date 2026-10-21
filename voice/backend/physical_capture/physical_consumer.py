"""One current capture -> one fixed verified intent -> existing shared browser.

No listener, model, process, TV client, receipt-file loader or Manager is created.
A trusted future source-pinned live runner delivers its result synchronously to
an invocation-bound callback; a serialized row by itself is not an authority.
Native assembly remains unconditionally disabled in native_activation.py.
"""
import threading
import time
from ..controller import Controller, Outcome, Refused
from ..channel_adapter import ChannelBrowserAdapter
from .join_schema import qualified_current

EXPECTED='{"action":"browser_open","destination":"youtube"}'
CODES=frozenset(('CONSUMER_UNARMED','CONSUMER_REPLAY','OWNER_UNAVAILABLE','CAPTURE_UNCONFIRMED',
 'CAPTURE_EXPIRED','CAPTURE_CANCELLED','CAPTURE_REPLAY','CURRENT_CAPTURE_UNQUALIFIED',
 'BROWSER_NAVIGATION_ACCEPTED','BROWSER_TARGET_PRESENTED','BROWSER_START_REJECTED',
 'BROWSER_START_UNCONFIRMED','BROWSER_NOT_READY','NAVIGATION_REJECTED',
 'NAVIGATION_UNCONFIRMED','EXECUTION_UNCONFIRMED','REQUEST_EXPIRED',
 'REQUEST_NOT_ARMED','BROWSER_BRIDGE_UNQUALIFIED','CAPABILITY_UNAVAILABLE'))

class _VerifiedIntent:
 """Reuses the already-matched fixed intent; does not call an LLM twice."""
 def __init__(self,live):self.live=live;self.used=False
 def interpret(self,_text,*,timeout_s):
  if self.used or not self.live():raise Refused('REQUEST_EXPIRED')
  self.used=True;return EXPECTED

class _RuntimeFence:
 """Recheck the same owner before/after every existing runtime operation."""
 apple_title_enabled=False
 def __init__(self,consumer):self.consumer=consumer
 @property
 def browser_enabled(self):return self.consumer._live_dispatch()
 def _call(self,name,*args,**kwargs):
  if not self.consumer._live_dispatch():raise Refused('REQUEST_EXPIRED')
  if name in ('launch_browser','navigate'):kwargs['is_current']=self.consumer._live_dispatch
  value=getattr(self.consumer.runtime,name)(*args,**kwargs)
  if not self.consumer._live_dispatch():raise Refused('REQUEST_EXPIRED')
  return value
 def launch_browser(self,*args,**kwargs):return self._call('launch_browser',*args,**kwargs)
 def browser_observation(self,*args,**kwargs):return self._call('browser_observation',*args,**kwargs)
 def navigate(self,*args,**kwargs):return self._call('navigate',*args,**kwargs)

class OneUsePhysicalConsumer:
 def __init__(self,shared_owner,capture_current,*,qualified=False,clock=time.monotonic,wait=time.sleep):
  self.owner=shared_owner;self.capture_current=capture_current;self.qualified=qualified is True
  self.clock,self.wait=clock,wait;self.lock=threading.Lock();self.state='new';self.cancelled=False
  self.controller=None;self.invocation=None;self.thread=None
  self.manager=getattr(shared_owner,'manager',None);self.runtime=getattr(shared_owner,'runtime',None)
  self.spawn=getattr(self.manager,'control_spawn',None)
  self.viewer=getattr(self.runtime,'viewer',None)
  self.last=None
 def _bound(self):
  r=self.runtime;m=self.manager
  return (self.owner.manager is m and self.owner.runtime is r and
    type(r)is ChannelBrowserAdapter and r.manager is m and r.control_spawn is self.spawn and
    m.control_spawn is self.spawn and self.spawn is not None and self.spawn.manager is m and
    r.viewer is self.viewer and self.owner.viewer is self.viewer and self.owner.enabled is True and
    self.viewer is not None and self.viewer.enabled is True and r.browser_enabled is True and
    r.require_idle is True and m.closed is False)
 def _idle(self):
  if not self._bound():return False
  if not self.manager.lock.acquire(timeout=.2):return False
  try:return self._bound()and self.manager.status()=={'state':'idle'}and self.manager.session is None
  finally:self.manager.lock.release()
 def _live_dispatch(self):
  with self.lock:return not self.cancelled and self.state=='dispatch'and self._bound()
 def cancel(self):
  with self.lock:
   if self.state=='done':return False
   self.cancelled=True;controller=self.controller
  if controller is not None:controller.cancel()
  return True
 def _outcome(self,state,code,*,capture=False,attempted=False):
  if code not in CODES:state,code='unknown','EXECUTION_UNCONFIRMED'
  return dict(schema=1,kind='physical-command-outcome',state=state,code=code,
    captureQualified=capture,commandAttempted=attempted,
    presentationProven=state=='verified'and code=='BROWSER_TARGET_PRESENTED',
    privateASRQualified=False,wholeTvZeroLgTrafficProven=False)
 def run(self):
  """One attempt, including failure/cancel. No method accepts an imported receipt."""
  with self.lock:
   if self.state!='new':return self._outcome('refused','CONSUMER_REPLAY')
   self.state='capture';self.invocation=object();token=self.invocation;self.thread=threading.get_ident()
   if not self.qualified:
    self.state='done';return self._outcome('refused','CONSUMER_UNARMED')
  delivered=0;valid=False;failure=None;capture=False;attempted=False;result=None
  end=self.clock()+250
  def deliver(row):
   nonlocal delivered,valid,failure
   with self.lock:
    if (self.invocation is not token or self.state!='capture' or self.cancelled or
      threading.get_ident()!=self.thread or self.clock()>=end):
     failure='CAPTURE_CANCELLED'if self.cancelled else 'CAPTURE_EXPIRED';valid=False;return False
    delivered+=1
    if delivered!=1:failure='CAPTURE_REPLAY';valid=False;return False
   try:
    qualified_current(row)
    # Validate while still in the same owned capture invocation. Do not retain
    # raw rows, PCM, transcripts, credentials, URLs or diagnostic objects.
    valid=True
   except Exception:
    failure='CURRENT_CAPTURE_UNQUALIFIED';valid=False
   return valid
  try:
   if not self._idle()or not callable(self.capture_current):
    result=self._outcome('refused','OWNER_UNAVAILABLE');return result
   # The native assembler must bind this exact callback to a fresh live host run
   # with its own READY/physical prerequisite admission and ephemeral TLS keys.
   returned=self.capture_current(deliver,timeout_s=max(.001,end-self.clock()))
   if returned is not None:valid=False;failure='CAPTURE_UNCONFIRMED'
   with self.lock:
    if self.cancelled:valid=False;failure='CAPTURE_CANCELLED'
    if self.clock()>=end:valid=False;failure='CAPTURE_EXPIRED'
    if delivered!=1:valid=False;failure=failure or 'CAPTURE_UNCONFIRMED'
   if not valid:
    result=self._outcome('refused',failure or 'CAPTURE_UNCONFIRMED');return result
   capture=True
   if not self._idle():
    result=self._outcome('refused','OWNER_UNAVAILABLE',capture=True);return result
   with self.lock:
    if self.cancelled:
     result=self._outcome('refused','CAPTURE_CANCELLED',capture=True);return result
    if self.clock()>=end:
     result=self._outcome('refused','CAPTURE_EXPIRED',capture=True);return result
    self.state='dispatch'
    self.controller=Controller(_VerifiedIntent(self._live_dispatch),_RuntimeFence(self),clock=self.clock,wait=self.wait)
   lease=self.controller.arm()
   if not self._live_dispatch():self.controller.cancel(lease)
   outcome=self.controller.execute(lease,'verified physical command')
   attempted=self.controller.dispatched
   result=self._outcome(outcome.state,outcome.code,capture=True,attempted=attempted)
   return result
  except Exception:
   attempted=bool(self.controller and self.controller.dispatched)
   result=self._outcome('unknown'if attempted else 'refused',
      'EXECUTION_UNCONFIRMED'if attempted else 'CAPTURE_UNCONFIRMED',capture=capture,attempted=attempted)
   return result
  finally:
   with self.lock:
    self.state='done';self.invocation=None;self.thread=None
    if self.controller is not None:self.controller.cancel()
    self.last=result
   deliver=None
