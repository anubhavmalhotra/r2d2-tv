"""Prepared process-only lock-state seal, applied explicitly after mlockall.

Prevents later model imports from clearing MCL_FUTURE/current locks through
munlock, munlockall or another mlockall. No host policy change; process exit ends
this scope. This is not a filesystem/GPU/forensic-erasure guarantee.
"""
import ctypes
import errno
import platform
if __package__:
    from .controller import Refused
    from .process_isolation import ARCH, ALLOW, KILL, ERROR, SockFilter, SockFprog
else:
    from controller import Refused
    from process_isolation import ARCH,ALLOW,KILL,ERROR,SockFilter,SockFprog


def instructions():
    rows=[(0x20,0,0,4),(0x15,1,0,ARCH),(0x06,0,0,KILL),(0x20,0,0,0)]
    for syscall in (229,230,231):rows.extend(((0x15,0,1,syscall),(0x06,0,0,ERROR|errno.EPERM)))
    rows.append((0x06,0,0,ALLOW))
    return rows


def seal():
    if platform.system()!='Linux'or platform.machine()!='aarch64':raise Refused('LOCK_SEAL_ARCH')
    library=ctypes.CDLL(None,use_errno=True)
    library.prctl.argtypes=[ctypes.c_int]+[ctypes.c_ulong]*4;library.prctl.restype=ctypes.c_int
    rows=instructions();program=(SockFilter*len(rows))(*(SockFilter(*row)for row in rows))
    descriptor=SockFprog(len(rows),program)
    if library.prctl(38,1,0,0,0)!=0 or library.prctl(22,2,ctypes.addressof(descriptor),0,0)!=0:
        raise Refused('LOCK_SEAL_REFUSED')
    if library.prctl(21,0,0,0,0)!=2:raise Refused('LOCK_SEAL_READBACK')
    return {'schema':1,'munlock_munlockall_mlockall_denied':True,'host_policy_changed':False,
            'forensic_payload_ram_only_qualified':False}
