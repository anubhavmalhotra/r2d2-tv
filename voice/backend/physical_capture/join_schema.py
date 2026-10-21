"""Validate the current closed physical-inference composition, never load receipts.

Expected intent is the fixed YouTube-browser command from the pinned physical
inference source. A model-matched boolean from any other producer has no meaning.
"""
from . import adapter_contract as adapter_result
from .capture_contract import qualify_physical

COMPOSITION=set('schema,kind,failure,failurePhase,exceptionClass,server,tv,tvAdapter,owner,compositionQualified,ownedSshExited,privateASRQualified,GPUOrBrokerPrivacyQualified,wholeTvZeroLgTrafficProven,microphoneActivated,commandDispatched'.split(','))
SERVER_FLAGS=set('modelsReady,modelInvoked,asrCompleted,intentCompleted,boundedPcmAdmitted,keywordsMatched,expectedIntentMatched,hmacAuthenticated,releaseObserved,workerCleanupComplete,sourcePeerMatched,tls13,peerCertificateMatched,replySent,socketClosed,listenerClosed,credentialsRemoved'.split(','))
SERVER_KEYS=SERVER_FLAGS|set('schema,kind,pcmBytes,warmMs,asrMs,intentMs,physicalCaptureProven,commandDispatched,failure'.split(','))
OWNER_KEYS=set('schema,kind,cleanupVerified,remainingOwned,childExit,sourcesRemoved,pipesClosed,drainersJoined,failure'.split(','))

def need(value):
 if not value:raise ValueError('CURRENT_CAPTURE_UNQUALIFIED')
def exact(row,keys):need(type(row)is dict and set(row)==keys)
def integer(value,lo,hi):need(type(value)is int and lo<=value<=hi)
def qualified_current(value):
 """Returns only PCM size after strict success validation; no text or row retained."""
 exact(value,COMPOSITION);integer(value['schema'],1,1)
 need(value['kind']=='physical-speech-composition')
 need(all(value[k]is None for k in('failure','failurePhase','exceptionClass')))
 need(all(value[k]is True for k in('compositionQualified','ownedSshExited','microphoneActivated')))
 need(all(value[k]is False for k in('privateASRQualified','GPUOrBrokerPrivacyQualified','wholeTvZeroLgTrafficProven','commandDispatched')))
 server=value['server'];exact(server,SERVER_KEYS);integer(server['schema'],1,1)
 need(server['kind']=='physical-speech-tls-server'and server['failure']is None)
 need(all(server[k]is True for k in SERVER_FLAGS))
 need(server['physicalCaptureProven']is False and server['commandDispatched']is False)
 for k,hi in [('pcmBytes',256000),('warmMs',120000),('asrMs',8000),('intentMs',5000)]:integer(server[k],0,hi)
 need(server['pcmBytes']>=960 and server['pcmBytes']%2==0)
 owner=value['owner'];exact(owner,OWNER_KEYS);integer(owner['schema'],1,1)
 need(owner['kind']=='physical-speech-owner'and owner['failure']is None)
 integer(owner['remainingOwned'],0,0);integer(owner['childExit'],0,0)
 need(all(owner[k]is True for k in('cleanupVerified','sourcesRemoved','pipesClosed','drainersJoined')))
 adapter_result.result(dict(schema=1,kind='physical-tv-adapter-result',tv=value['tv'],diagnostic=value['tvAdapter']))
 need(value['tvAdapter']['error']is None)
 need(qualify_physical(value['tv'],server['pcmBytes'])is True)
 d=value['tv']['nativeDiagnostic']
 need(all(d[k]is None for k in ('parentError','verificationError','probeError','captureError','recordingError','childSignal','helperErrorStage')))
 need(all(d[k]=='complete'for k in ('parentStage','probeStage','capturePhase','recordingPhase')))
 need(all(d[k]is True for k in ('childStarted','childReaped','verificationPresent','probePresent','capturePresent','recordingPresent','recordingReady','deviceOpened','deviceClosed')))
 need(d['childExitCode']==0 and d['identityChangeMask']==0 and not d['deviceDetached'])
 need(all(d[k]is False for k in ('emergencyPresent','emergencyStopInvoked','emergencyStopForwarded')))
 need(d['emergencyBootstrapTransport']is None)
 need(all(d[k]is not None and d[k]['ack']is True for k in ('manifestAddTransport','manifestRemoveTransport')))
 b=d['bootstrapTransport'];need(b is not None and b['exitCode']==0 and b['projectionValid']is True and b['pidObserved']is True)
 return server['pcmBytes']
