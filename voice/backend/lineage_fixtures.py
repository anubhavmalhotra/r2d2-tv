"""Copied injected inventory fixtures; no native calls."""
import os
import stat
import types
import gpu_bootstrap_readback as readback
from gpu_bootstrap_readback import fd_inventory, thread_inventory, InventoryRefused
NULL_DEVICE = os.makedev(1, 3)

def info(mode=stat.S_IFIFO, *, inode=100, device=7, rdev=0):
    return types.SimpleNamespace(st_mode=mode, st_dev=device, st_ino=inode, st_rdev=rdev)

class InjectedEnumerator:
    def __init__(self, entries, directory, flags, *, scans=None):
        self.fd = 77
        self.identity = readback._fingerprint(
            77, lambda fd: directory, lambda fd: flags)
        self.entries = tuple(str(value) for value in entries) + ('77',)
        self.scans = iter(scans or ())
        self.closed = False
        self.close_calls = 0

    def scan(self):
        if self.closed:
            raise InventoryRefused('BOOTSTRAP_ENUMERATOR_SEQUENCE')
        return next(self.scans, self.entries)

    def close(self):
        self.close_calls += 1
        self.closed = True

class Context:
    def __init__(self, *, extra=None, scope=(0, 1, 2), inode_offset=0):
        self.scope = scope
        self.rows = {fd: info(inode=100+fd+inode_offset) for fd in scope}
        self.flags = {fd: (os.O_RDONLY if fd == 0 else os.O_WRONLY, 0) for fd in scope}
        self.rows.update(extra or {})
        for fd in extra or {}:
            self.flags[fd] = (os.O_RDWR, 1)
        self.rows[77] = info(stat.S_IFDIR, inode=700)
        self.flags[77] = (os.O_RDONLY, 1)
        self.enumerators = []

    def snapshot(self, *, describe=None, flags=None, scans=None,
                 read_domains=False, socket_domain=None):
        directory = self.rows[77]
        enumerator = InjectedEnumerator(
            [fd for fd in self.rows if fd != 77], directory, self.flags[77], scans=scans)
        self.enumerators.append(enumerator)
        return fd_inventory(self.scope, enumerator=enumerator,
                            describe=describe or self.rows.__getitem__,
                            descriptor_flags=flags or self.flags.__getitem__,
                            null_device=NULL_DEVICE, read_domains=read_domains,
                            socket_domain=socket_domain)

def task_stat(task, start=200):
    # comm includes a close parenthesis; only field 22 is retained privately.
    return f'{task} (PRIVATE ) name) R ' + ' '.join(['0']*18 + [str(start)])

def threads(filters=1, *, ids=(10, 11), starts=None, **overrides):
    starts = starts or {task: 200+task for task in ids}
    options = {
        'list_tasks': lambda: [str(value) for value in ids],
        'read_status': lambda task: f'Name: PRIVATE\nUid: 987654321\nNoNewPrivs: 1\nSeccomp: 2\nSeccomp_filters: {filters}\n',
        'read_stat': lambda task: task_stat(task, starts[task]),
    }
    options.update(overrides)
    return thread_inventory(**options)
