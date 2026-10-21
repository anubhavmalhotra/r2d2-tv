"""One-use in-memory binding to a reviewed current physical host run.

The future native assembler must supply verified module objects and immutable
source pins, including live READY/physical admission. Injection is a trusted
source boundary, not validation of arbitrary code or a saved receipt. This
module has no import/discovery/receipt-loader path and does no I/O itself.
"""
import math
import threading
import types
from .join_schema import qualified_current

class CurrentHostCapture:
 def __init__(self,host,tv,pins,*,qualified=False):
  self.host,self.tv=host,tv;self.qualified=qualified is True
  self.run_host=getattr(host,'run',None)
  self.tv_run,self.tv_result,self.tv_qualify=(getattr(tv,n,None)for n in('run','result','qualify'))
  # Pins are one private immutable flat source map. Host cannot rewrite authority.
  if type(pins)is not dict or not pins or any(type(k)is not str or type(v)is not str for k,v in pins.items()):
   raise ValueError('CURRENT_HOST_SOURCE')
  self.pins=types.MappingProxyType(dict(pins));self.lock=threading.Lock();self.used=False
  self.cleanup_observer=None
 def bind_cleanup(self,observer):
  """One current-run observer; it grants no inference/command authority."""
  with self.lock:
   if self.used or self.cleanup_observer is not None or not callable(observer):
    raise ValueError('CURRENT_HOST_REPLAY')
   self.cleanup_observer=observer
 def _bound(self):
  return (type(self.host)is types.ModuleType and type(self.tv)is types.ModuleType and
   self.host.run is self.run_host and self.tv.run is self.tv_run and self.tv.result is self.tv_result and
   self.tv.qualify is self.tv_qualify and all(callable(x)for x in(self.run_host,self.tv_run,self.tv_result,self.tv_qualify)))
 def __call__(self,deliver,*,timeout_s):
  # This is a logical finite invocation. The existing physical host retains its
  # independent bounded remote cleanup after errors; do not kill it on a caller
  # deadline or mistake a Python return for remote stream cessation.
  with self.lock:
   if self.used:raise ValueError('CURRENT_HOST_REPLAY')
   self.used=True
  if not self.qualified:raise ValueError('CURRENT_HOST_UNARMED')
  if (not callable(deliver)or not self._bound()or type(timeout_s)not in(int,float)or
      not math.isfinite(timeout_s)or not 0<timeout_s<=250):raise ValueError('CURRENT_HOST_SOURCE')
  row=None
  try:
   # Actual existing native_host.run API; no status text, URL or transcript log.
   row=self.run_host(dict(self.pins),self.tv_run,self.tv_result,self.tv_qualify,status=None)
   if not self._bound():raise ValueError('CURRENT_HOST_CHANGED')
   if self.cleanup_observer is not None:self.cleanup_observer(row)
   qualified_current(row)
   if deliver(row)is not True:raise ValueError('CURRENT_HOST_REFUSED')
  finally:
   # Only references are dropped here. The source-specific closed join above
   # separately requires actual native PCM/key/child/socket cleanup evidence.
   row=None
   self.cleanup_observer=None
