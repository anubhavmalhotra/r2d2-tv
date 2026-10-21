"""Prepared explicit ARM64 TSYNC operation; never applied on import.

Same target-Linux policy as the consumed full filter, with fixed Linux errno38.
Only a reviewed fresh public-tensor diagnostic may call this. Existing sockets,
files, GPU brokers, logging and model privacy require separate qualification.
"""
import ctypes
import hashlib
import platform
import struct
if __package__:
    from .process_isolation import SockFilter, SockFprog
else:
    from process_isolation import SockFilter,SockFprog

ARCH=0xc00000b7;ALLOW=0x7fff0000;KILL=0x80000000;ERROR=0x00050000
SECCOMP_NR_ARM64=277;SECCOMP_SET_MODE_FILTER=1;SECCOMP_FILTER_FLAG_TSYNC=1
TARGET_DENIED={nr:1 for nr in (*range(198,213),117,221,241,242,243,269,270,271,280,281,425,426,427,438)}
TARGET_DENIED[435]=38


class SyncRefused(ValueError):pass


def instructions():
    rows=[(0x20,0,0,4),(0x15,1,0,ARCH),(0x06,0,0,KILL),(0x20,0,0,0)]
    for nr,code in sorted(TARGET_DENIED.items()):rows.extend(((0x15,0,1,nr),(0x06,0,0,ERROR|code)))
    rows.extend(((0x15,0,3,220),(0x20,0,0,16),(0x45,1,0,0x00010000),(0x06,0,0,ERROR|1),(0x06,0,0,ALLOW)))
    return rows


def policy_hash():return hashlib.sha256(b''.join(struct.pack('<HBBI',*row)for row in instructions())).hexdigest()


def apply(*,library=None):
    if platform.system()!='Linux'or platform.machine()!='aarch64':raise SyncRefused('BOOTSTRAP_TSYNC_ARCH')
    library=library or ctypes.CDLL(None,use_errno=True)
    library.prctl.argtypes=[ctypes.c_int]+[ctypes.c_ulong]*4;library.prctl.restype=ctypes.c_int
    library.syscall.argtypes=[ctypes.c_long,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p];library.syscall.restype=ctypes.c_long
    if library.prctl(38,1,0,0,0)!=0:raise SyncRefused('BOOTSTRAP_TSYNC_NO_NEW_PRIVS')
    rows=instructions();program=(SockFilter*len(rows))(*(SockFilter(*row)for row in rows));descriptor=SockFprog(len(rows),program)
    value=library.syscall(SECCOMP_NR_ARM64,SECCOMP_SET_MODE_FILTER,SECCOMP_FILTER_FLAG_TSYNC,ctypes.byref(descriptor))
    if value!=0:raise SyncRefused('BOOTSTRAP_TSYNC_FAILED')
    return {'schema':1,'tsync_return_zero':True,'target_linux_arm64_policy_sha256':policy_hash(),
      'thread_ids_or_error_text_exported':False,'model_or_private_input_qualified':False}
