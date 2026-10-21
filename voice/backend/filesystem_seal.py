"""Owned worker Landlock boundary installed before CUDA threads exist.

No persistent file writes are permitted. Own-process procfs thread metadata
allows the CUDA thread-name operation, and stays subject to kernel permissions. Read-only model/library
access remains available; exact device inodes permit the existing GPU runtime.
This is not a host policy and does not change any device or kernel setting.
"""
import ctypes
import errno
import os
from pathlib import Path
import platform
import stat

WRITE_FILE = 1 << 1
IOCTL_DEV = 1 << 15
FS_MUTATIONS = WRITE_FILE | sum(1 << bit for bit in range(4, 16))
NET_TCP = 3
SCOPED = 3
# Fresh read-only Spark observation; no device alias or directory grant.
DEVICES = (
    ('/dev/null', 1, 3), ('/dev/nvidia0', 195, 0),
    ('/dev/nvidiactl', 195, 255), ('/dev/nvidia-modeset', 195, 254),
    ('/dev/nvidia-uvm', 499, 0), ('/dev/nvidia-uvm-tools', 499, 1),
)


class Ruleset(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64),
                ('handled_access_net', ctypes.c_uint64), ('scoped', ctypes.c_uint64)]


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def install():
    if (platform.system() != 'Linux' or platform.machine() != 'aarch64'
            or os.getuid() != os.geteuid() or os.getuid() != 1000
            or len(list(Path('/proc/self/task').iterdir())) != 1):
        raise ValueError('FILESYSTEM_SCOPE')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = int(libc.syscall(ctypes.c_long(444), ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1)))
    if abi < 8: raise ValueError('FILESYSTEM_ABI')
    attr = Ruleset(FS_MUTATIONS, NET_TCP, SCOPED)
    rulefd = int(libc.syscall(ctypes.c_long(444), ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint(0)))
    if rulefd < 0: raise ValueError('FILESYSTEM_RULESET')
    try:
        for path, major, minor in DEVICES:
            fd = os.open(path, 0x200000 | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISCHR(info.st_mode) or (os.major(info.st_rdev), os.minor(info.st_rdev)) != (major, minor):
                    raise ValueError('FILESYSTEM_DEVICE_DRIFT')
                rule = PathBeneath(WRITE_FILE | IOCTL_DEV, fd)
                if libc.syscall(ctypes.c_long(445), ctypes.c_int(rulefd), ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint(0)) != 0:
                    raise ValueError('FILESYSTEM_DEVICE_RULE')
            finally: os.close(fd)
        # CUDA opens each newly created thread's comm with O_CREAT|O_TRUNC.
        # Existing procfs entries need no create right. This inode is only our
        # own process's volatile task metadata, never /proc/sys or disk.
        taskfd=os.open('/proc/self/task',0x200000|os.O_CLOEXEC|os.O_NOFOLLOW)
        try:
            info=os.fstat(taskfd);own=os.stat('/proc/'+str(os.getpid())+'/task',follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid()
                    or (info.st_dev,info.st_ino)!=(own.st_dev,own.st_ino)):
                raise ValueError('FILESYSTEM_TASK_SCOPE')
            task_rule=PathBeneath(WRITE_FILE|(1<<14),taskfd)
            if libc.syscall(ctypes.c_long(445),ctypes.c_int(rulefd),ctypes.c_int(1),ctypes.byref(task_rule),ctypes.c_uint(0))!=0:
                raise ValueError('FILESYSTEM_TASK_RULE')
        finally:os.close(taskfd)
        if libc.prctl(38, 1, 0, 0, 0) != 0: raise ValueError('FILESYSTEM_PRIVILEGES')
        if libc.syscall(ctypes.c_long(446), ctypes.c_int(rulefd), ctypes.c_uint(0)) != 0:
            raise ValueError('FILESYSTEM_RESTRICT')
    finally: os.close(rulefd)
    return {'abi': abi, 'installedBeforeThreads': True, 'writeDeviceRules': len(DEVICES),
            'persistentFileWritesAllowed': False, 'ownedTaskMetadataRuleCount': 1, 'ownedTaskInodeBound': True, 'tcpAllowed': False,
            'abstractSocketScope': True, 'signalScope': True, 'negativeProbesPassed': False}


def check_denials(source_file, observation):
    """Attempt no payload writes: only open/mkdir against owned public sources."""
    source = Path(source_file)
    info = source.lstat(); parent = source.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.getuid()):
        raise ValueError('FILESYSTEM_FIXTURE')
    # O_WRONLY does not modify source bytes, even if a defective seal permits it.
    # Child cannot chmod/write because this probe never performs either operation.
    failed = False
    try:
        fd = os.open(source, os.O_WRONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno != errno.EACCES: raise ValueError('FILESYSTEM_DENIAL') from None
    else:
        os.close(fd); failed = True
    # parent stages this fixed file writable 0600 to ensure DAC cannot cause
    # the denial; identity/hash and mode are checked by the parent afterward.
    made = source.parent/'denied-public-directory'
    try: os.mkdir(made, 0o700)
    except OSError as exc:
        if exc.errno != errno.EACCES: raise ValueError('FILESYSTEM_DENIAL') from None
    else:
        os.rmdir(made); failed = True
    if failed: raise ValueError('FILESYSTEM_DENIAL')
    observation['negativeProbesPassed'] = True
    return validate(observation)


def validate(row):
    if row is None: return None
    if (type(row) is not dict or set(row) != {'abi', 'installedBeforeThreads', 'writeDeviceRules',
            'persistentFileWritesAllowed', 'ownedTaskMetadataRuleCount', 'ownedTaskInodeBound', 'tcpAllowed', 'abstractSocketScope', 'signalScope', 'negativeProbesPassed'}
            or type(row['abi']) is not int or not 8 <= row['abi'] <= 64
            or type(row['ownedTaskMetadataRuleCount']) is not int or row['ownedTaskMetadataRuleCount']!=1 or row['ownedTaskInodeBound']is not True
            or type(row['writeDeviceRules']) is not int or row['writeDeviceRules'] != len(DEVICES)
            or any(row[key] is not True for key in ('installedBeforeThreads', 'abstractSocketScope', 'signalScope'))
            or any(row[key] is not False for key in ('persistentFileWritesAllowed', 'tcpAllowed'))
            or type(row['negativeProbesPassed']) is not bool):
        raise ValueError('FILESYSTEM_RESULT_SCOPE')
    return dict(row)
