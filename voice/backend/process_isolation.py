"""Prepared ordinary-user per-process ARM64 isolation; never applied on import.

Socket denial is irreversible for this process and inherited by its children;
exiting the owned trial reverses its scope. It changes no host firewall, swap,
service, sysctl, namespace, capability or administrator setting. CUDA/native model
compatibility and effective memory retention remain separate acceptance checks.
"""
import ctypes
import errno
import os
from pathlib import Path
import platform
import resource
import stat

if __package__:
    from .controller import Refused
else:
    from controller import Refused

# Exact Linux v6.17 asm-generic syscall ABI and audit architecture constants.
# Socket operations, io_uring, external process/FD acquisition, and executable
# launch are refused. clone3 returns ENOSYS so pthread may use guarded clone.
DENIED = {nr: errno.EPERM for nr in (*range(198,213),117,221,241,242,243,269,
                                   270,271,280,281,425,426,427,438)}
DENIED[435] = errno.ENOSYS
ARCH = 0xc00000b7
ALLOW = 0x7fff0000
KILL = 0x80000000
ERROR = 0x00050000


def filter_instructions():
    # struct seccomp_data offsets: nr=0, arch=4, args[0]=16. Reject any ABI switch.
    rows=[(0x20,0,0,4),(0x15,1,0,ARCH),(0x06,0,0,KILL),(0x20,0,0,0)]
    for nr,code in sorted(DENIED.items()):
        rows.extend(((0x15,0,1,nr),(0x06,0,0,ERROR|code)))
    # Only thread clone (CLONE_THREAD) is permitted. Process forks would bypass
    # mlockall inheritance assumptions; no alternate process exec path exists.
    rows.extend(((0x15,0,3,220),(0x20,0,0,16),(0x45,1,0,0x00010000),
                 (0x06,0,0,ERROR|errno.EPERM),(0x06,0,0,ALLOW)))
    return rows


class SockFilter(ctypes.Structure):
    _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),
              ('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(SockFilter))]


def safe_descriptors(allowed):
    if type(allowed) is not tuple or not 3<=len(allowed)<=4 or len(set(allowed))!=len(allowed) or any(type(n)is not int or n<0 for n in allowed):
        raise Refused('ISOLATION_DESCRIPTORS')
    if not set((0,1,2)).issubset(allowed):raise Refused('ISOLATION_DESCRIPTORS')
    null=os.stat('/dev/null')
    observed=set()
    for entry in os.listdir('/proc/self/fd'):
        if not entry.isdigit():raise Refused('ISOLATION_DESCRIPTORS')
        descriptor=int(entry)
        try:info=os.fstat(descriptor)
        except OSError as error:
            # The directory enumeration's own transient fd may already be closed.
            if error.errno==errno.EBADF:continue
            raise Refused('ISOLATION_DESCRIPTORS')from None
        if descriptor not in allowed:raise Refused('ISOLATION_DESCRIPTORS')
        if not (stat.S_ISFIFO(info.st_mode) or
                (stat.S_ISCHR(info.st_mode) and info.st_rdev==null.st_rdev)):
            raise Refused('ISOLATION_DESCRIPTORS')
        observed.add(descriptor)
    if observed!=set(allowed):raise Refused('ISOLATION_DESCRIPTORS')


def apply_socket_denial(allowed_pipe_fds):
    """Call in a dedicated owned worker before ML imports and before threads."""
    if platform.system()!='Linux' or platform.machine()!='aarch64':
        raise Refused('ISOLATION_ARCH')
    if len(os.listdir('/proc/self/task'))!=1:raise Refused('ISOLATION_THREADS')
    safe_descriptors(allowed_pipe_fds)
    library=ctypes.CDLL(None,use_errno=True)
    library.prctl.argtypes=[ctypes.c_int]+[ctypes.c_ulong]*4
    library.prctl.restype=ctypes.c_int
    rows=filter_instructions()
    program=(SockFilter*len(rows))(*(SockFilter(*row)for row in rows))
    descriptor=SockFprog(len(rows),program)
    if library.prctl(38,1,0,0,0)!=0:raise Refused('ISOLATION_NO_NEW_PRIVS')
    if library.prctl(22,2,ctypes.addressof(descriptor),0,0)!=0:raise Refused('ISOLATION_SECCOMP')
    if library.prctl(21,0,0,0,0)!=2:raise Refused('ISOLATION_READBACK')
    return {'schema':1,'seccomp_filter_installed':True,'no_new_privs':True,
            'socket_creation_and_transport_denied':True,'io_uring_denied':True,
            'external_fd_acquisition_denied':True,'process_exec_and_fork_denied':True,
            'thread_clone_retained':True,'host_policy_changed':False,
            'native_cuda_compatible':False}


def memory_lock_metadata():
    soft,hard=resource.getrlimit(resource.RLIMIT_MEMLOCK)
    infinite=resource.RLIM_INFINITY
    return {'schema':1,'soft_bytes':None if soft==infinite else soft,
            'hard_bytes':None if hard==infinite else hard,
            'soft_unlimited':soft==infinite,'hard_unlimited':hard==infinite,
            'host_swap_changed':False}


def lock_current_and_future(*, maximum_locked_bytes=64*1024**3):
    """Prepared process-only strict lock. Refuses if the hard limit is inadequate.

    This does not prove the ingress parent or CUDA/driver/shared pages avoid
    retention; both actual inference and a complete allocation/lock audit remain.
    No automatic privileged limit raise or global swap disabling occurs.
    """
    if platform.system()!='Linux' or platform.machine()!='aarch64':raise Refused('MEMORY_LOCK_ARCH')
    if type(maximum_locked_bytes)is not int or not 16*1024**3<=maximum_locked_bytes<=64*1024**3:
        raise Refused('MEMORY_LOCK_BUDGET')
    soft,hard=resource.getrlimit(resource.RLIMIT_MEMLOCK)
    if hard!=resource.RLIM_INFINITY and hard<maximum_locked_bytes:raise Refused('MEMORY_LOCK_LIMIT')
    # Only the owned process's soft resource ceiling changes, within existing hard.
    resource.setrlimit(resource.RLIMIT_MEMLOCK,(maximum_locked_bytes,hard))
    library=ctypes.CDLL(None,use_errno=True)
    library.mlockall.argtypes=[ctypes.c_int]
    library.mlockall.restype=ctypes.c_int
    if library.mlockall(1|2|4)!=0:raise Refused('MEMORY_LOCK_REFUSED')
    return {'schema':1,'current_future_onfault_requested':True,'maximum_locked_bytes':maximum_locked_bytes,
            'host_swap_changed':False,'forensic_payload_ram_only_qualified':False}
