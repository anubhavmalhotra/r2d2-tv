"""Explicit Linux ARM64 own-process inventory for public tensor diagnostics.

Importing performs no native calls. Snapshots export fixed scalar counts only.
Hidden fstat/access-flag fingerprints are stronger than FD numbers, but do not
attest exact open-file-description identity or GPU/broker privacy.
"""
import ctypes
import copy
import fcntl
import os
import platform
from pathlib import Path
import re
import stat

MAX_FDS = 64
MAX_THREADS = 16
SOL_SOCKET_LINUX = 1
SO_DOMAIN_LINUX = 39
O_PATH_LINUX = 0x200000
STATUS_FIELDS = {
    'NoNewPrivs': 'no_new_privs', 'Seccomp': 'seccomp_mode',
    'Seccomp_filters': 'filter_count',
}


class InventoryRefused(ValueError):
    pass


class Snapshot:
    __slots__ = ('_metadata', '_identities', '_kind', '_stdio_scope', '_null_device')

    def __init__(self, metadata, identities, *, kind, stdio_scope=(), null_device=None):
        object.__setattr__(self, '_metadata', copy.deepcopy(metadata))
        object.__setattr__(self, '_identities', frozenset(identities))
        object.__setattr__(self, '_kind', kind)
        object.__setattr__(self, '_stdio_scope', tuple(stdio_scope))
        object.__setattr__(self, '_null_device', null_device)

    def __setattr__(self, name, value):
        raise AttributeError('private inventory snapshot is immutable')

    @property
    def metadata(self):
        return copy.deepcopy(self._metadata)

    def __repr__(self):
        return '<private own-process inventory snapshot>'


def number(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')


def _names(raw, maximum, code):
    if type(raw) not in (tuple, list) or not 1 <= len(raw) <= maximum:
        raise InventoryRefused(code)
    if any(type(value) is not str or re.fullmatch(r'[0-9]+', value) is None
           for value in raw):
        raise InventoryRefused(code)
    names = frozenset(int(value) for value in raw)
    if len(names) != len(raw) or any(value > 2**31-1 for value in names):
        raise InventoryRefused(code)
    return names


def thread_status(raw):
    if type(raw) is not str or len(raw) > 65536:
        raise InventoryRefused('BOOTSTRAP_THREAD_STATUS')
    row = {}
    for line in raw.splitlines():
        key, _, value = line.partition(':')
        if key not in STATUS_FIELDS:
            continue
        match = re.fullmatch(r'\s*([0-9]+)\s*', value)
        if not match or key in row:
            raise InventoryRefused('BOOTSTRAP_THREAD_STATUS')
        row[key] = int(match[1])
    if (set(row) != set(STATUS_FIELDS) or row['NoNewPrivs'] not in (0, 1)
            or row['Seccomp'] not in (0, 1, 2)
            or not 0 <= row['Seccomp_filters'] <= 32):
        raise InventoryRefused('BOOTSTRAP_THREAD_STATUS')
    return {STATUS_FIELDS[key]: value for key, value in row.items()}


def thread_start_time(raw, task):
    """Read only stat field 22; names and all raw stat text stay local."""
    if type(raw) is not str or len(raw) > 4096 or type(task) is not int or task <= 0:
        raise InventoryRefused('BOOTSTRAP_THREAD_IDENTITY')
    prefix, separator, _ = raw.partition(' (')
    _, closing, tail = raw.rpartition(') ')
    fields = tail.split()
    if (not separator or not closing or prefix != str(task)
            or not 20 <= len(fields) <= 128
            or fields[0] not in ('R', 'S', 'D', 'Z', 'T', 't', 'X', 'x', 'K', 'W', 'P', 'I')
            or re.fullmatch(r'[0-9]+', fields[19]) is None):
        raise InventoryRefused('BOOTSTRAP_THREAD_IDENTITY')
    result = int(fields[19])
    number(result, 2**64-1)
    return result


def thread_inventory(*, list_tasks=None, read_status=None, read_stat=None):
    list_tasks = list_tasks or (lambda: os.listdir('/proc/self/task'))

    def bounded_status(task):
        with open(Path('/proc/self/task') / str(task) / 'status') as stream:
            return stream.read(65537)

    def bounded_stat(task):
        with open(Path('/proc/self/task') / str(task) / 'stat') as stream:
            return stream.read(4097)

    read_status = read_status or bounded_status
    read_stat = read_stat or bounded_stat
    first = _names(list_tasks(), MAX_THREADS, 'BOOTSTRAP_THREAD_SCOPE')
    if any(task <= 0 for task in first):
        raise InventoryRefused('BOOTSTRAP_THREAD_SCOPE')

    def capture(tasks):
        result = {}
        for task in sorted(tasks):
            try:
                before = thread_start_time(read_stat(task), task)
                row = thread_status(read_status(task))
                after = thread_start_time(read_stat(task), task)
            except OSError:
                raise InventoryRefused('BOOTSTRAP_THREAD_RACE') from None
            if before != after:
                raise InventoryRefused('BOOTSTRAP_THREAD_RACE')
            result[(task, before)] = row
        return result

    rows = capture(first)
    second = _names(list_tasks(), MAX_THREADS, 'BOOTSTRAP_THREAD_SCOPE')
    if first != second or rows != capture(second):
        raise InventoryRefused('BOOTSTRAP_THREAD_RACE')
    final = _names(list_tasks(), MAX_THREADS, 'BOOTSTRAP_THREAD_SCOPE')
    if final != first:
        raise InventoryRefused('BOOTSTRAP_THREAD_RACE')
    filters = {row['filter_count'] for row in rows.values()}
    metadata = {
        'schema': 2, 'thread_count': len(rows),
        'no_new_privs_count': sum(row['no_new_privs'] == 1 for row in rows.values()),
        'seccomp_mode2_count': sum(row['seccomp_mode'] == 2 for row in rows.values()),
        'uniform_filter_count': next(iter(filters)) if len(filters) == 1 else None,
        'names_uids_paths_or_task_ids_exported': False,
        'thread_start_time_identity_held_privately': True,
        'consistent_two_pass_snapshot': True,
        'all_intermediate_thread_activity_proven_absent': False,
    }
    return Snapshot(metadata, rows, kind='threads')


def _descriptor_flags(fd):
    return fcntl.fcntl(fd, fcntl.F_GETFL), fcntl.fcntl(fd, fcntl.F_GETFD)


def _fingerprint(fd, describe, descriptor_flags):
    try:
        before = describe(fd)
        flags = descriptor_flags(fd)
        after = describe(fd)
    except OSError:
        # No arbitrary EBADF exception: even a transient observed FD is refused.
        raise InventoryRefused('BOOTSTRAP_FD_RACE') from None

    def fields(info):
        try:
            values = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_rdev)
        except (AttributeError, TypeError, ValueError):
            raise InventoryRefused('BOOTSTRAP_FD_IDENTITY') from None
        for value in values:
            number(value, 2**64-1)
        return values

    original = fields(before)
    if fields(after) != original:
        raise InventoryRefused('BOOTSTRAP_FD_RACE')
    if type(flags) is not tuple or len(flags) != 2:
        raise InventoryRefused('BOOTSTRAP_FD_IDENTITY')
    number(flags[0], 2**32-1)
    number(flags[1], 1)  # Linux FD_CLOEXEC is the only supported descriptor flag.
    if flags[0] & O_PATH_LINUX:
        raise InventoryRefused('BOOTSTRAP_FD_CLASS_UNSUPPORTED')
    return (fd, *original, *flags)


class _Dirent(ctypes.Structure):
    # Native-only Linux LP64 libc dirent; explicitly restricted to Linux ARM64.
    _fields_ = [
        ('ino', ctypes.c_uint64), ('offset', ctypes.c_int64),
        ('record_length', ctypes.c_ushort), ('kind', ctypes.c_ubyte),
        ('name', ctypes.c_char * 256),
    ]


class _OwnedFdEnumerator:
    """Hold the exact enumeration FD until both passes finish.

    fdopendir takes ownership without creating a transient duplicate. Only this
    object's known directory descriptor is excluded from the inspected set.
    """
    def __init__(self):
        if platform.system() != 'Linux' or platform.machine() != 'aarch64':
            raise InventoryRefused('BOOTSTRAP_ENUMERATOR_ARCH')
        self.closed = True
        self._directory = None
        library = ctypes.CDLL(None, use_errno=True)
        library.fdopendir.argtypes = [ctypes.c_int]
        library.fdopendir.restype = ctypes.c_void_p
        library.readdir.argtypes = [ctypes.c_void_p]
        library.readdir.restype = ctypes.POINTER(_Dirent)
        library.rewinddir.argtypes = [ctypes.c_void_p]
        library.rewinddir.restype = None
        library.closedir.argtypes = [ctypes.c_void_p]
        library.closedir.restype = ctypes.c_int
        self._library = library
        fd = os.open('/proc/self/fd', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        pointer = library.fdopendir(fd)
        if not pointer:
            os.close(fd)
            raise InventoryRefused('BOOTSTRAP_ENUMERATOR_OPEN')
        self.fd = fd
        self._directory = pointer
        self.closed = False
        try:
            self.identity = _fingerprint(fd, os.fstat, _descriptor_flags)
            if self.identity[3] != stat.S_IFDIR:
                raise InventoryRefused('BOOTSTRAP_ENUMERATOR_IDENTITY')
        except BaseException:
            self.close()
            raise

    def scan(self):
        if self.closed:
            raise InventoryRefused('BOOTSTRAP_ENUMERATOR_SEQUENCE')
        self._library.rewinddir(self._directory)
        names = []
        while True:
            ctypes.set_errno(0)
            pointer = self._library.readdir(self._directory)
            if not pointer:
                if ctypes.get_errno() != 0:
                    raise InventoryRefused('BOOTSTRAP_ENUMERATOR_SCAN')
                return tuple(names)
            row = pointer.contents
            raw = bytes(row.name)
            if raw in (b'.', b'..'):
                continue
            if (not 20 <= row.record_length <= ctypes.sizeof(_Dirent)
                    or len(raw) > 255 or re.fullmatch(rb'[0-9]+', raw) is None):
                raise InventoryRefused('BOOTSTRAP_ENUMERATOR_SCAN')
            names.append(raw.decode('ascii'))
            if len(names) > MAX_FDS + 1:
                raise InventoryRefused('BOOTSTRAP_FD_SCOPE')

    def close(self):
        if self.closed:
            return
        self.closed = True
        pointer, self._directory = self._directory, None
        if self._library.closedir(pointer) != 0:
            raise InventoryRefused('BOOTSTRAP_ENUMERATOR_CLOSE')


def domain(fd):
    library = ctypes.CDLL(None, use_errno=True)
    library.getsockopt.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    ]
    library.getsockopt.restype = ctypes.c_int
    result = ctypes.c_int()
    length = ctypes.c_uint(ctypes.sizeof(result))
    if (library.getsockopt(fd, SOL_SOCKET_LINUX, SO_DOMAIN_LINUX,
                          ctypes.byref(result), ctypes.byref(length)) != 0
            or length.value != 4 or not 0 <= result.value <= 255):
        raise InventoryRefused('BOOTSTRAP_SOCKET_DOMAIN')
    return result.value


def fd_inventory(allowed_stdio, *, enumerator=None, describe=None,
                 descriptor_flags=None, null_device=None, socket_domain=None,
                 read_domains=False):
    if (type(allowed_stdio) is not tuple or not 3 <= len(allowed_stdio) <= 4
            or not {0, 1, 2} <= set(allowed_stdio)
            or len(set(allowed_stdio)) != len(allowed_stdio)
            or any(type(value) is not int or not 0 <= value <= 2**31-1
                   for value in allowed_stdio)):
        raise InventoryRefused('BOOTSTRAP_STDIO_SCOPE')
    if type(read_domains) is not bool:
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    describe = describe or os.fstat
    descriptor_flags = descriptor_flags or _descriptor_flags
    null_device = os.stat('/dev/null').st_rdev if null_device is None else null_device
    number(null_device, 2**64-1)
    socket_domain = socket_domain or domain
    owned = enumerator or _OwnedFdEnumerator()
    try:
        if (type(owned.fd) is not int or owned.fd in allowed_stdio
                or owned.identity != _fingerprint(owned.fd, describe, descriptor_flags)
                or owned.identity[3] != stat.S_IFDIR):
            raise InventoryRefused('BOOTSTRAP_ENUMERATOR_IDENTITY')
        first = _names(owned.scan(), MAX_FDS+1, 'BOOTSTRAP_FD_SCOPE')
        if owned.fd not in first:
            raise InventoryRefused('BOOTSTRAP_ENUMERATOR_IDENTITY')
        observed = first - {owned.fd}
        if not set(allowed_stdio) <= observed:
            raise InventoryRefused('BOOTSTRAP_STDIO_SCOPE')
        identities = {_fingerprint(fd, describe, descriptor_flags)
                      for fd in sorted(observed)}
        second = _names(owned.scan(), MAX_FDS+1, 'BOOTSTRAP_FD_SCOPE')
        if (first != second
                or owned.identity != _fingerprint(owned.fd, describe, descriptor_flags)
                or identities != {_fingerprint(fd, describe, descriptor_flags)
                                  for fd in sorted(observed)}):
            raise InventoryRefused('BOOTSTRAP_FD_RACE')
        final = _names(owned.scan(), MAX_FDS+1, 'BOOTSTRAP_FD_SCOPE')
        if first != final:
            raise InventoryRefused('BOOTSTRAP_FD_RACE')
        row = {
            'schema': 2, 'descriptor_count': len(identities),
            'allowed_stdio_count': 0, 'stdio_mismatch_count': 0,
            'socket_count': 0, 'extra_pipe_count': 0, 'extra_regular_count': 0,
            'extra_directory_count': 0, 'extra_character_count': 0,
            'extra_unknown_count': 0, 'socket_domains': [], 'character_devices': [],
            'endpoints_paths_descriptor_ids_or_payload_exported': False,
            'inspected_descriptors_closed': False,
            'gpu_or_broker_character_devices_qualified': False,
            'descriptor_fingerprints_include_inode_device_type_and_access_flags': True,
            'exact_open_file_description_qualified': False,
            'enumeration_descriptor_excluded_only_by_owned_identity': True,
            'consistent_two_pass_snapshot': True,
            'all_intermediate_descriptor_activity_proven_absent': False,
        }
        histogram, domains = {}, {}
        for fp in identities:
            fd, _, _, mode, rdev, access, _ = fp
            fifo = mode == stat.S_IFIFO
            null = mode == stat.S_IFCHR and rdev == null_device
            if fd in allowed_stdio:
                expected_access = os.O_RDONLY if fd == 0 else os.O_WRONLY
                key = ('allowed_stdio_count' if (fifo or null) and
                       access & os.O_ACCMODE == expected_access
                       else 'stdio_mismatch_count')
                row[key] += 1
            elif mode == stat.S_IFSOCK:
                row['socket_count'] += 1
                if read_domains:
                    family = socket_domain(fd)
                    number(family, 255)
                    domains[family] = domains.get(family, 0) + 1
            elif fifo:
                row['extra_pipe_count'] += 1
            elif mode == stat.S_IFREG:
                row['extra_regular_count'] += 1
            elif mode == stat.S_IFDIR:
                row['extra_directory_count'] += 1
            elif mode == stat.S_IFCHR:
                row['extra_character_count'] += 1
                key = (os.major(rdev), os.minor(rdev))
                histogram[key] = histogram.get(key, 0) + 1
            else:
                row['extra_unknown_count'] += 1
        # Domain probing can itself race; inspect again before returning.
        if (identities != {_fingerprint(fd, describe, descriptor_flags)
                          for fd in sorted(observed)}
                or first != _names(owned.scan(), MAX_FDS+1, 'BOOTSTRAP_FD_SCOPE')
                or owned.identity != _fingerprint(owned.fd, describe, descriptor_flags)):
            raise InventoryRefused('BOOTSTRAP_FD_RACE')
        row['socket_domains'] = [{'family': family, 'count': count}
                                 for family, count in sorted(domains.items())]
        row['character_devices'] = [{'major': major, 'minor': minor, 'count': count}
                                    for (major, minor), count in sorted(histogram.items())]
        return Snapshot(row, identities, kind='fds', stdio_scope=allowed_stdio,
                        null_device=null_device)
    finally:
        # Close only the explicitly owned directory, never an inspected FD.
        owned.close()
