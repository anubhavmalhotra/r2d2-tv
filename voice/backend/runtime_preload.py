"""Public runtime import preparation before threads/seals/model input.

Transformers imports Torch's RemoteModule helper, which creates a public-code
TemporaryDirectory at import time. Resolve those classes before the write seal.
No model weights/instances, tokenizer requests, audio or inference are invoked.
"""
import importlib
import os
from pathlib import Path
import stat
if __package__:
    from .startup_diagnostic import load_diagnostic, validate_diagnostic
else:
    from startup_diagnostic import load_diagnostic,validate_diagnostic

IMPORTS=('torch.distributed.nn.jit.instantiator','transformers.modeling_utils',
         'transformers.processing_utils','transformers.models.auto.modeling_auto',
         'transformers.models.auto.processing_auto')

def new():
    return {'schema':1,'complete':False,'importsCompleted':0,'oneThreadAfter':False,
            'beforeFilesystemSeal':True,'modelInstancesLoaded':False,'privateInputAccepted':False,
            'diagnostic':None}

def prepare(row):
    try:
        temp=Path(os.environ['TMPDIR']);info=temp.lstat()
        if (temp.name!='startup-public-runtime' or not stat.S_ISDIR(info.st_mode)
                or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700
                or len(list(Path('/proc/self/task').iterdir()))!=1):raise ValueError('PUBLIC_RUNTIME_SCOPE')
        for name in IMPORTS:
            importlib.import_module(name);row['importsCompleted']+=1
        row['oneThreadAfter']=len(list(Path('/proc/self/task').iterdir()))==1
        if not row['oneThreadAfter']:raise ValueError('PUBLIC_RUNTIME_THREADS')
        row['complete']=True
        return validate(row)
    except BaseException as error:
        row['diagnostic']=load_diagnostic(error)
        raise

def validate(row):
    if row is None:return None
    if (type(row)is not dict or set(row)!=set(new())or type(row['schema'])is not int or row['schema']!=1
            or type(row['importsCompleted'])is not int or not 0<=row['importsCompleted']<=len(IMPORTS)
            or any(type(row[k])is not bool for k in ('complete','oneThreadAfter'))
            or row['beforeFilesystemSeal']is not True or row['modelInstancesLoaded']is not False
            or row['privateInputAccepted']is not False):raise ValueError('PUBLIC_RUNTIME_RESULT')
    validate_diagnostic(row['diagnostic'])
    if row['complete']and(row['importsCompleted']!=len(IMPORTS)or not row['oneThreadAfter']or row['diagnostic']is not None):raise ValueError('PUBLIC_RUNTIME_RESULT')
    return row
