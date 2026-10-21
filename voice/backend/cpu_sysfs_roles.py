"""Read-only kernel CPU topology leaves retained by the installed model runtime.

These descriptors expose CPU identification/cache metadata. They cannot write
audio or transcripts. A path name alone never qualifies a descriptor: require
root ownership, regular type, readonly flags, kernel sysfs filesystem and the
same device/inode through the open descriptor and its canonical sysfs path.
"""
import ctypes
import os
from pathlib import Path
import re
import stat

SYSFS_MAGIC=0x62656572
PATH_RE=re.compile(r'/sys/devices/system/cpu/cpu(0|[1-9][0-9]{0,3})/(?:regs/identification/midr_el1|cache/index[0-7]/(?:level|size|type|shared_cpu_list))')

def valid_path(value):
    match=PATH_RE.fullmatch(value)if type(value)is str else None
    return match is not None and int(match[1])<=4095

def filesystem_magic(fd):
    # glibc fstatfs writes a Linux struct statfs; a 256-byte aligned buffer is
    # larger than its ARM64 layout, and f_type is the first native long.
    libc=ctypes.CDLL(None,use_errno=True)
    libc.fstatfs.argtypes=[ctypes.c_int,ctypes.c_void_p];libc.fstatfs.restype=ctypes.c_int
    buffer=(ctypes.c_long*32)()
    if libc.fstatfs(fd,ctypes.byref(buffer))!=0:raise ValueError('CPU_SYSFS_FILESYSTEM')
    return int(buffer[0])

def qualify(fd,target,info,access,*,magic=filesystem_magic,path_info=None,canonical=None):
    if not valid_path(target)or not stat.S_ISREG(info.st_mode)or info.st_uid!=0 or access&os.O_ACCMODE!=os.O_RDONLY:
        return False
    path=Path(target)
    if canonical is None:canonical=lambda p:str(p.resolve())
    if path_info is None:path_info=lambda p:p.lstat()
    observed=path_info(path)
    fields=lambda s:(s.st_dev,s.st_ino,stat.S_IFMT(s.st_mode),s.st_uid)
    return (canonical(path)==target and fields(observed)==fields(info)
            and magic(fd)==SYSFS_MAGIC)
