"""Read-only model resources may supplement the already sealed CUDA resources.

Only freshly hash-verified model assets or verified read-only CPU sysfs metadata
can supplement the bootstrap resources. No new
device, pipe, socket, writable file, or replaced bootstrap FD is admitted.
Diagnostic path classes/basenames are for the public fixture, not private use.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
if __package__:
    from . import cpu_sysfs_roles
else:
    import cpu_sysfs_roles

def identity(info):
    return (info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode),info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def record(resources,path,info,role,name):
    key=(info.st_dev,info.st_ino)
    if key in resources:raise ValueError('MODEL_RESOURCE_COLLISION')
    resources[key]=(identity(info),Path(path),role,name)

def path_class(target):
    if target.startswith(('/memfd:','memfd:')):return 'memfd'
    if target.startswith('/dev/shm/'):return 'shared-memory'
    if target.startswith('/proc/'):return 'proc'
    if target.startswith(('/tmp/','/run/')):return 'runtime-temporary'
    if '/site-packages/' in target:return 'runtime-package'
    if target.startswith(('/lib/','/lib64/','/usr/lib/')):return 'system-library'
    return 'other'

def inspect(original,current,resources,*,describe=os.fstat,flags=None,readlink=os.readlink):
    flags=flags or (lambda fd:(fcntl.fcntl(fd,fcntl.F_GETFL),fcntl.fcntl(fd,fcntl.F_GETFD)))
    retained=(original._identities<=current._identities and original._stdio_scope==current._stdio_scope
              and original._null_device==current._null_device)
    added=sorted(current._identities-original._identities)
    rows=[]
    for index,fp in enumerate(added):
        fd=fp[0];before=describe(fd);access,fdflags=flags(fd)
        if (fd,before.st_dev,before.st_ino,stat.S_IFMT(before.st_mode),before.st_rdev,access,fdflags)!=fp:
            raise ValueError('MODEL_RESOURCE_RACE')
        target=readlink('/proc/self/fd/'+str(fd))
        if type(target)is not str or len(target)>4096:raise ValueError('MODEL_RESOURCE_PATH')
        known=resources.get((before.st_dev,before.st_ino));matched=False;role=None;name=None
        if known:
            wanted,path,role,name=known
            matched=(identity(before)==wanted and identity(path.lstat())==wanted and str(path)==target)
        regular=stat.S_ISREG(before.st_mode);readonly=(access&os.O_ACCMODE)==os.O_RDONLY
        cpu=cpu_sysfs_roles.qualify(fd,target,before,access)
        qualified=regular and readonly and(matched or cpu)
        basename=Path(target).name[:120]
        if not re.fullmatch('[A-Za-z0-9_. ():-]{1,120}',basename):basename='other'
        rows.append({'id':index+1,'kind':'regular' if regular else 'other','readOnly':readonly,
                     'pathClass':'verified-model' if matched else 'cpu-sysfs' if cpu else path_class(target),
                     'cpuSysfsMetadataMatched':cpu,'technicalPath':target if cpu_sysfs_roles.valid_path(target)else None,
                     'basename':basename,'pathSha256':hashlib.sha256(target.encode()).hexdigest(),
                     'modelRole':role if matched else None,'modelFile':name if matched else None,
                     'identityMatched':matched,'qualified':qualified})
        if identity(describe(fd))!=identity(before) or flags(fd)!=(access,fdflags):
            raise ValueError('MODEL_RESOURCE_RACE')
    return validate({'bootstrapDescriptorsRetained':retained,'added':rows,
                     'qualified':retained and all(r['qualified']for r in rows)})

def validate(value):
    if value is None:return None
    if (type(value)is not dict or set(value)!={'bootstrapDescriptorsRetained','added','qualified'}
            or any(type(value[k])is not bool for k in ('bootstrapDescriptorsRetained','qualified'))
            or type(value['added'])is not list or len(value['added'])>32):raise ValueError('MODEL_RESOURCE_RESULT')
    for index,row in enumerate(value['added']):
        if(type(row)is not dict or set(row)!={'id','kind','readOnly','pathClass','basename','pathSha256','modelRole','modelFile','identityMatched','qualified','cpuSysfsMetadataMatched','technicalPath'}
           or type(row['id'])is not int or row['id']!=index+1 or row['kind']not in('regular','other')
           or row['pathClass']not in('verified-model','cpu-sysfs','memfd','shared-memory','proc','runtime-temporary','runtime-package','system-library','other')
           or any(type(row[k])is not bool for k in ('readOnly','identityMatched','qualified','cpuSysfsMetadataMatched'))
           or type(row['basename'])is not str or not re.fullmatch('[A-Za-z0-9_. ():-]{1,120}',row['basename'])
           or type(row['pathSha256'])is not str or not re.fullmatch('[a-f0-9]{64}',row['pathSha256'])
           or row['modelRole']not in(None,'asr','intent')
           or row['modelFile']is not None and(type(row['modelFile'])is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,120}',row['modelFile']))):raise ValueError('MODEL_RESOURCE_RESULT')
        if row['technicalPath']is not None and not cpu_sysfs_roles.valid_path(row['technicalPath']):raise ValueError('MODEL_RESOURCE_RESULT')
        if row['cpuSysfsMetadataMatched']!=(row['pathClass']=='cpu-sysfs'and row['technicalPath']is not None):raise ValueError('MODEL_RESOURCE_RESULT')
        if row['qualified']!=(row['kind']=='regular'and row['readOnly']and(row['identityMatched']or row['cpuSysfsMetadataMatched'])):raise ValueError('MODEL_RESOURCE_RESULT')
        if row['identityMatched']!=(row['pathClass']=='verified-model'and row['modelRole']is not None and row['modelFile']is not None):raise ValueError('MODEL_RESOURCE_RESULT')
    if value['qualified']!=(value['bootstrapDescriptorsRetained']and all(r['qualified']for r in value['added'])):raise ValueError('MODEL_RESOURCE_RESULT')
    return value
