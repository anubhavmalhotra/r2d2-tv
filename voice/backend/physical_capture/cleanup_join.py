"""Current capture cleanup only; never authorizes a command or imports a file.

The present contract can prove cleanup after a fully captured utterance rejected
by inference. It cannot prove a no-speech/early-cancel native path had no start,
so those remain quarantined until the real capture contract gains that evidence.
"""
from . import adapter_contract as adapter_result
from .join_schema import COMPOSITION, SERVER_KEYS, SERVER_FLAGS, OWNER_KEYS, exact, integer, need


def cleaned_current(value):
    exact(value, COMPOSITION); integer(value['schema'], 1, 1)
    need(value['kind'] == 'physical-speech-composition')
    need(value['failure'] in (None, 'PHYSICAL_INFERENCE_UNCONFIRMED'))
    need(value['failurePhase'] in (None, 'admission', 'model-warm', 'tv-adapter',
         'tv-projection', 'server-result', 'owner-result', 'qualification'))
    need(value['exceptionClass'] in adapter_result.CLASSES | {None})
    need(type(value['compositionQualified']) is bool)
    need(value['ownedSshExited'] is True and value['microphoneActivated'] is True)
    need(all(value[k] is False for k in ('privateASRQualified', 'GPUOrBrokerPrivacyQualified',
             'wholeTvZeroLgTrafficProven', 'commandDispatched')))
    server = value['server']; exact(server, SERVER_KEYS); integer(server['schema'], 1, 1)
    need(server['kind'] == 'physical-speech-tls-server')
    need(server['failure'] in (None, 'INFERENCE'))
    need(all(type(server[k]) is bool for k in SERVER_FLAGS))
    need(server['physicalCaptureProven'] is False and server['commandDispatched'] is False)
    for key, high in [('pcmBytes',256000), ('warmMs',120000), ('asrMs',8000), ('intentMs',5000)]:
        integer(server[key], 0, high)
    need(all(server[k] is True for k in ('boundedPcmAdmitted', 'hmacAuthenticated', 'releaseObserved',
        'sourcePeerMatched', 'tls13', 'peerCertificateMatched', 'workerCleanupComplete',
        'socketClosed', 'listenerClosed', 'credentialsRemoved')))
    owner = value['owner']; exact(owner, OWNER_KEYS); integer(owner['schema'], 1, 1)
    need(owner['kind'] == 'physical-speech-owner')
    need(owner['failure'] in (None, 'OWNED_CHILD_UNCONFIRMED'))
    integer(owner['remainingOwned'], 0, 0); integer(owner['childExit'], 0, 255)
    need(all(owner[k] is True for k in ('cleanupVerified','sourcesRemoved','pipesClosed','drainersJoined')))
    adapter_result.result(dict(schema=1, kind='physical-tv-adapter-result',
                               tv=value['tv'], diagnostic=value['tvAdapter']))
    need(value['tvAdapter']['error'] is None)
    tv = value['tv']
    need(all(tv[k] is True for k in ('physicalPressObserved', 'microphoneCaptured',
        'startForwarded', 'stopForwarded', 'streamStopObserved', 'pcmWiped', 'rawWiped',
        'transportClosed', 'fourGuardsRetained', 'temporaryCleanup', 'homeRestored',
        'socketClosed', 'childReaped', 'credentialsWiped', 'freshHomeBeforeLaunch',
        'generatedCredentialSourcesRemoved')))
    need(960 <= tv['pcmBytes'] == tv['pcmSentBytes'] == server['pcmBytes'] <= 192000
         and tv['quietMs'] >= 2000)
    d = tv['nativeDiagnostic']
    need(all(d[k] is True for k in ('childStarted','childReaped','verificationPresent','probePresent',
        'capturePresent','recordingPresent','recordingReady','deviceOpened','deviceClosed')))
    need(d['childSignal'] is None and type(d['childExitCode']) is int
         and d['identityChangeMask'] == 0 and d['deviceDetached'] is False)
    need(d['recordingError'] is None and d['recordingPhase'] == 'complete')
    need(all(d[k] is not None and d[k]['ack'] is True
             for k in ('manifestAddTransport','manifestRemoveTransport')))
    b = d['bootstrapTransport']
    need(b is not None and b['projectionValid'] is True and b['pidObserved'] is True)
    if d['emergencyPresent']:
        e = d['emergencyBootstrapTransport']
        need(d['emergencyStopInvoked'] is True and d['emergencyStopForwarded'] is True
             and e is not None and e['projectionValid'] is True and e['pidObserved'] is True
             and e['exitCode'] == 0 and e['signal'] is None)
    else:
        need(d['emergencyStopInvoked'] is False and d['emergencyStopForwarded'] is False
             and d['emergencyBootstrapTransport'] is None)
    return True
