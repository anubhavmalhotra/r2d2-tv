"""Explicit no-microphone warm/close check for the actual model-worker API.

Import/default invocation is inert. The --run operation loads already installed
local models in the isolated owned child, checks the bound ready handshake and
then closes/reaps that child and removes public startup-code temporary files.
It creates no listener, sends no audio/text command, and contacts no TV.
"""
import json
import sys
from model_worker import ModelWorker

def check(python,asr,intent,*,factory=ModelWorker):
    result={'schema':1,'kind':'model-worker-warm-close','ready':False,
            'cleanupComplete':False,'isolation':None,'failure':None,'startupFailure':None,
            'microphoneStarted':False,'privateInputAccepted':False,'TVContacted':False,
            'listenerOpened':False,'commandDispatched':False}
    worker=factory(python,asr,intent,qualified=True)
    try:
        worker.start(timeout_s=120)
        result['ready']=worker.sealed is not None and worker.process is not None and worker.process.poll()is None
        result['isolation']=dict(worker.sealed)if result['ready']else None
        if not result['ready']:result['failure']='WORKER_NOT_READY'
    except Exception:
        result['failure']='WORKER_STARTUP_FAILED'
        result['startupFailure']=getattr(worker,'startup_failure',None)
    finally:
        result['cleanupComplete']=worker.close()is True
        if not result['cleanupComplete']:result['failure']='WORKER_CLEANUP_UNCONFIRMED'
    return result

def main(argv=None):
    argv=sys.argv[1:]if argv is None else argv
    if not argv:
        print(json.dumps({'preparedOnly':True,'modelsLoaded':False,'microphoneStarted':False}));return 0
    if len(argv)!=4 or argv[0]!='--run':return 2
    value=check(*argv[1:]);print(json.dumps(value,separators=(',',':')))
    return 0 if value['ready']and value['cleanupComplete']and value['failure']is None else 2

if __name__=='__main__':raise SystemExit(main())
