"""Closed local-child diagnostics. No exception text, credentials or raw TV data."""
import re
from .capture_contract import native_diagnostic

SUMMARY_KEYS=set('schema,kind,complete,error,physicalPressObserved,microphoneCaptured,pcmBytes,pcmSentBytes,startForwarded,stopForwarded,streamStopObserved,quietMs,pcmWiped,rawWiped,transportClosed,inferenceAccepted,fourGuardsRetained,temporaryCleanup,homeRestored,socketClosed,childReaped,credentialsWiped,freshHomeBeforeLaunch,generatedCredentialSourcesRemoved,physicalMicOffProven,commandDispatched,nativeDiagnostic'.split(','))
PHASES={'admission','destination','sources','config','imports','tv-run','projection','complete','launch','input','output','wait'}
CLASSES={'AssertionError','ValueError','TypeError','AttributeError','KeyError','ImportError','ModuleNotFoundError','OSError','BlockingIOError','BrokenPipeError','TimeoutError','KeyboardInterrupt','Other'}
ERRORS={'TV_ADAPTER_UNARMED','TV_ADAPTER_SOURCE','TV_ADAPTER_CHILD_FAILED','TV_INPUT_BOUND','TV_ADAPTER_TIMEOUT','TV_OUTPUT_BOUND','TV_ADAPTER_EXIT','TV_OUTPUT_SCHEMA','TV_ADAPTER_IO','TV_ADAPTER_REAP'}

def summary(value):
 if type(value)is not dict or set(value)!=SUMMARY_KEYS or type(value['schema'])is not int or value['schema']!=1 or value['kind']!='physical-tv-summary':raise ValueError('TV_SUMMARY_SCHEMA')
 if value['error']is not None and(type(value['error'])is not str or not re.fullmatch('[A-Z_]{1,40}',value['error'])):raise ValueError('TV_SUMMARY_SCHEMA')
 for key,high in [('pcmBytes',192000),('pcmSentBytes',192000),('quietMs',10000)]:
  if type(value[key])is not int or not 0<=value[key]<=high:raise ValueError('TV_SUMMARY_SCHEMA')
 native_diagnostic(value['nativeDiagnostic'])
 for key in SUMMARY_KEYS-{'schema','kind','error','pcmBytes','pcmSentBytes','quietMs','nativeDiagnostic'}:
  if type(value[key])is not bool:raise ValueError('TV_SUMMARY_SCHEMA')
 if value['physicalMicOffProven']or value['commandDispatched']:raise ValueError('TV_SUMMARY_SCOPE')
 if value['pcmBytes']%2 or value['pcmSentBytes']%2:raise ValueError('TV_SUMMARY_SCHEMA')
 return value

def exception_class(error):
 name=type(error).__name__
 return name if name in CLASSES else 'Other'

def child(value):
 keys={'schema','kind','phase','error','exceptionClass','runEntered','tv'}
 if(type(value)is not dict or set(value)!=keys or type(value['schema'])is not int or value['schema']!=1 or value['kind']!='physical-tv-adapter-child'
    or value['phase']not in PHASES or value['error']not in(None,'TV_ADAPTER_CHILD_FAILED')or value['exceptionClass']not in CLASSES|{None}or type(value['runEntered'])is not bool):raise ValueError('TV_OUTPUT_SCHEMA')
 if value['tv']is not None:summary(value['tv'])
 if value['error']is None and not(value['phase']=='complete'and value['exceptionClass']is None and value['runEntered']and value['tv']is not None):raise ValueError('TV_OUTPUT_SCHEMA')
 if value['error']is not None and value['exceptionClass']is None:raise ValueError('TV_OUTPUT_SCHEMA')
 return value

def empty():
 return dict(schema=1,kind='physical-tv-adapter-result',tv=None,diagnostic=dict(phase='admission',error=None,exceptionClass=None,runEntered=False,childStarted=False,childReaped=False,childExitCode=None,childSignal=None,forcedTermination=False))

def result(value):
 if type(value)is not dict or set(value)!={'schema','kind','tv','diagnostic'}or type(value['schema'])is not int or value['schema']!=1 or value['kind']!='physical-tv-adapter-result':raise ValueError('TV_OUTPUT_SCHEMA')
 d=value['diagnostic'];keys={'phase','error','exceptionClass','runEntered','childStarted','childReaped','childExitCode','childSignal','forcedTermination'}
 if type(d)is not dict or set(d)!=keys or d['phase']not in PHASES or d['error']not in ERRORS|{None}or d['exceptionClass']not in CLASSES|{None}:raise ValueError('TV_OUTPUT_SCHEMA')
 if any(type(d[k])is not bool for k in('childStarted','childReaped','forcedTermination'))or d['runEntered']is not None and type(d['runEntered'])is not bool:raise ValueError('TV_OUTPUT_SCHEMA')
 if d['childExitCode']is not None and(type(d['childExitCode'])is not int or not 0<=d['childExitCode']<=255)or d['childSignal']not in(None,'SIGTERM','SIGKILL','OTHER'):raise ValueError('TV_OUTPUT_SCHEMA')
 if d['childReaped']and not d['childStarted']or d['childExitCode']is not None and d['childSignal']is not None:raise ValueError('TV_OUTPUT_SCHEMA')
 if not d['childStarted']and any(d[k]is not None for k in('childExitCode','childSignal')):raise ValueError('TV_OUTPUT_SCHEMA')
 if value['tv']is not None:
  summary(value['tv'])
  if d['runEntered']is not True:raise ValueError('TV_OUTPUT_SCHEMA')
 if d['error']is None and not(d['phase']=='complete'and d['exceptionClass']is None and d['runEntered']and d['childStarted']and d['childReaped']and d['childExitCode']==0 and d['childSignal']is None and not d['forcedTermination']and value['tv']is not None):raise ValueError('TV_OUTPUT_SCHEMA')
 return value

def qualified_adapter(value):
 result(value)
 return value['diagnostic']['error']is None
