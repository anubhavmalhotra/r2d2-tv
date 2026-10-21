"""Verify configured local model snapshots against reviewed public hashes.

No installer, remote code, download, fallback path or private host identity.
The internal identity map is consumed by the descriptor-role guard, not logged.
"""
import hashlib
import os
from pathlib import Path
import stat
if __package__:
    from .pinned_models import MODELS
    from .model_descriptor_roles import record
else:
    from pinned_models import MODELS
    from model_descriptor_roles import record

def verify(asr_path,intent_path):
    resources={}
    configured={'asr':Path(asr_path),'intent':Path(intent_path)}
    for model in MODELS:
        folder=configured[model['role']]
        info=folder.lstat()
        if(not folder.is_absolute()or folder.resolve()!=folder or not stat.S_ISDIR(info.st_mode)
           or info.st_uid!=os.getuid()or info.st_mode&0o077):raise ValueError('MODEL_DIRECTORY')
        if {p.name for p in folder.iterdir()}!={entry['file']for entry in model['files']}:
            raise ValueError('MODEL_FILE_SET')
        for entry in model['files']:
            path=folder/entry['file'];fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                before=os.fstat(fd)
                if(not stat.S_ISREG(before.st_mode)or before.st_uid!=os.getuid()
                   or before.st_size!=entry['bytes']):raise ValueError('MODEL_FILE')
                digest=hashlib.sha256();total=0
                while True:
                    block=os.read(fd,min(2097152,entry['bytes']-total+1))
                    if not block:break
                    total+=len(block)
                    if total>entry['bytes']:raise ValueError('MODEL_FILE')
                    digest.update(block)
                identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                if(total!=entry['bytes']or digest.hexdigest()!=entry['sha256']
                   or identity(before)!=identity(os.fstat(fd))or identity(before)!=identity(path.lstat())):
                    raise ValueError('MODEL_FILE')
                record(resources,path,before,model['role'],entry['file'])
            finally:os.close(fd)
    return resources
