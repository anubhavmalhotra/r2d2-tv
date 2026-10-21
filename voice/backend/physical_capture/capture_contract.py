"""Closed scalar physical-capture contract; no device/profile implementation."""
import re
def need(v,c='CAPTURE_RECEIPT'):
 if not v:raise ValueError(c)

def record(x,keys):need(type(x)is dict and set(x)==set(keys))

def count(v,hi):need(type(v)is int and 0<=v<=hi)

def boolean(x,keys):
 for k in keys:need(type(x[k])is bool)

def error(v):need(v is None or type(v)is str and re.fullmatch('[A-Z_]{1,40}',v))

RECORDING_PHASES=('admission','preopen','open','postopen','predrain','ready','wait','start','capture','stop','drain','close','complete')

M='attempted,ack,closed,stdoutClosed,stderrClosed,exitCode,signal,responseJson,returnValue,errorCode,errorCategory,stdoutBytes,stderrBytes,timedOut,transportError'.split(',')

def manifest_transport(x):
 record(x,M);boolean(x,'attempted,ack,closed,stdoutClosed,stderrClosed,responseJson,timedOut'.split(','))
 need(x['exitCode']is None or type(x['exitCode'])is int and 0<=x['exitCode']<=255)
 need(x['signal']in(None,'SIGTERM','SIGKILL','other'));need(x['returnValue']is None or type(x['returnValue'])is bool)
 need(x['errorCode']is None or type(x['errorCode'])is int and abs(x['errorCode'])<=2147483647)
 for k in ('stdoutBytes','stderrBytes'):count(x[k],4097)
 need(x['errorCategory']in(None,'NOT_ALLOWED','ALREADY_LOADED','REASON_UNKNOWN','OTHER'))
 need(x['transportError']in(None,'SPAWN','CHILD','OUTPUT_LIMIT','TIMEOUT','INCOMPLETE_CLOSE'))
 if x['ack']:need(all(x[k]for k in ('attempted','closed','stdoutClosed','stderrClosed','responseJson'))and x['exitCode']==0 and x['signal']is None and x['returnValue']is True and not x['timedOut']and x['transportError']is None)
 return x

B='started,pidObserved,closed,stdoutClosed,stderrClosed,exitCode,signal,stdoutBytes,stderrBytes,stderrTruncated,stdoutJson,projectionValid,transportError,stderrCategory'.split(',')

PROBE_PHASES=('admission','staging','stage-fence','manifest-add','manifest-fence','retain-voice','seal','journal','verify-files','child-fence','child','pid-readback','emergency-fence','emergency-pid','emergency','finish-fence','complete')

def child_transport(x):
 record(x,B);boolean(x,'started,pidObserved,closed,stdoutClosed,stderrClosed,stderrTruncated,stdoutJson,projectionValid'.split(','))
 need(x['exitCode']is None or type(x['exitCode'])is int and 0<=x['exitCode']<=255);need(x['signal']in(None,'SIGTERM','SIGKILL','other'))
 for k in ('stdoutBytes','stderrBytes'):count(x[k],65536)
 need(x['transportError']in(None,'SPAWN','CHILD','STDIN','OUTPUT_LIMIT','TIMEOUT','INCOMPLETE_CLOSE'));need(x['stderrCategory']in(None,'MODULE_NOT_FOUND','SYNTAX_ERROR','ADDON_ABI','BOOTSTRAP_PID','BOOTSTRAP_CONFIG','BOOTSTRAP_ROLE','BOOTSTRAP_MAIN','MULTIPLE','OTHER'))
 if x['projectionValid']:need(x['stdoutJson']and x['closed']and x['stdoutClosed']and x['stderrClosed']and x['signal']is None and x['transportError']is None)
 if x['pidObserved']:need(x['started'])
 if x['stderrBytes']==0:need(x['stderrCategory']is None)
 return x

def qualify_physical(summary,pcm_bytes):
 expected={'schema','kind','complete','error','physicalPressObserved','microphoneCaptured','pcmBytes','pcmSentBytes','startForwarded','stopForwarded','streamStopObserved','quietMs','pcmWiped','rawWiped','transportClosed','inferenceAccepted','fourGuardsRetained','temporaryCleanup','homeRestored','socketClosed','childReaped','credentialsWiped','freshHomeBeforeLaunch','generatedCredentialSourcesRemoved','physicalMicOffProven','commandDispatched','nativeDiagnostic'}
 record(summary,expected);need(summary['schema']==1 and summary['kind']=='physical-tv-summary'and summary['error']is None)
 native_diagnostic(summary['nativeDiagnostic'])
 for k in expected-{'schema','kind','error','pcmBytes','pcmSentBytes','quietMs','physicalMicOffProven','commandDispatched','nativeDiagnostic'}:need(summary[k]is True)
 need(type(pcm_bytes)is int and 960<=pcm_bytes<=192000 and pcm_bytes==summary['pcmBytes']==summary['pcmSentBytes']and summary['quietMs']>=2000 and summary['physicalMicOffProven']is False and summary['commandDispatched']is False)
 return True

D='schema,parentStage,parentError,childStarted,childReaped,childExitCode,childSignal,helperErrorStage,verificationPresent,verificationError,probePresent,probeError,capturePresent,capturePhase,captureError,recordingPresent,recordingPhase,recordingError,recordingReady,deviceOpened,deviceClosed,identityChangeMask,nodePresentAtEnd,sysfsPresentAtEnd,deviceDetached,emergencyPresent,emergencyError,emergencyPhase,emergencyStopInvoked,emergencyStopForwarded,manifestAddTransport,manifestRemoveTransport,probeStage,bootstrapTransport,emergencyBootstrapTransport'.split(',')

def native_diagnostic(x):
 record(x,D);need(type(x['schema'])is int and x['schema']==1);need(x['probeStage']is None or x['probeStage']in PROBE_PHASES)
 for k in ('bootstrapTransport','emergencyBootstrapTransport'):
  if x[k]is not None:child_transport(x[k])
 for k in ('manifestAddTransport','manifestRemoveTransport'):
  if x[k]is not None:manifest_transport(x[k])
 boolean(x,'childStarted,childReaped,verificationPresent,probePresent,capturePresent,recordingPresent,recordingReady,deviceOpened,deviceClosed,deviceDetached,emergencyPresent,emergencyStopInvoked,emergencyStopForwarded'.split(','))
 for k in ('parentError','verificationError','probeError','captureError','recordingError','emergencyError'):error(x[k])
 need(x['parentStage']in(None,'admission','staging','child','readback','complete'))
 for k in ('capturePhase','emergencyPhase'):need(x[k]in(None,'bootstrap','runtime','authority','remote','capture','decode','transport','complete'))
 need(x['recordingPhase']is None or x['recordingPhase']in RECORDING_PHASES)
 need(x['childSignal']in(None,'SIGKILL','OTHER'));need(x['childExitCode']is None or type(x['childExitCode'])is int and 0<=x['childExitCode']<=255)
 need(x['helperErrorStage']is None or type(x['helperErrorStage'])is int and 71<=x['helperErrorStage']<=85);count(x['identityChangeMask'],127)
 for k in ('nodePresentAtEnd','sysfsPresentAtEnd'):need(x[k]is None or type(x[k])is bool)
 return x
