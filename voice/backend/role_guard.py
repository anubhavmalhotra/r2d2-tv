"""Public-only admission of observed CUDA notification resources.

Requires known pre-bootstrap stdio, paired pipes, eventfd tags, unconnected
listening UNIX SEQPACKET, and unchanged fingerprints. No model/private gate is
opened. The original socket-denial TSYNC program is unchanged.
"""
import os
import stat
if __package__:
    from .descriptor_lineage import Sample
    from .gpu_bootstrap_guard import BootstrapGuard, validate_fd_snapshot, validate_thread_snapshot
    from .gpu_bootstrap_readback import Snapshot, InventoryRefused
else:
    from descriptor_lineage import Sample
    from gpu_bootstrap_guard import BootstrapGuard, validate_fd_snapshot, validate_thread_snapshot
    from gpu_bootstrap_readback import Snapshot, InventoryRefused


def roles(initial, bootstrapped, socket_proofs):
    if type(initial) is not Sample or type(bootstrapped) is not Sample or type(socket_proofs) is not dict:
        raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
    first, later = initial.fds, bootstrapped.fds
    for snap in (first, later): validate_fd_snapshot(snap)
    for snap in (initial.tasks, bootstrapped.tasks): validate_thread_snapshot(snap)
    if (len(first._stdio_scope) != 4 or first._stdio_scope != later._stdio_scope
            or first.metadata['descriptor_count'] != 4 or first.metadata['allowed_stdio_count'] != 4
            or first._identities - later._identities
            or initial.tasks.metadata['thread_count'] != 1):
        raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
    if (len(bootstrapped.details) != len(later._identities)
            or {item[0] for item in bootstrapped.details} != later._identities):
        raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
    pipes = {}; sockets = set(); events = characters = 0
    for fp, kind, properties in bootstrapped.details:
        if fp in first._identities:
            if kind != 'stdio': raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
            continue
        if kind == 'pipe':
            if fp[3] != stat.S_IFIFO: raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
            pipes.setdefault(fp[1:4], []).append(fp)
        elif kind == 'eventfd':
            if fp[3] != 0: raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
            events += 1
        elif kind == 'character':
            if fp[3] != stat.S_IFCHR: raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
            # Preserve the existing guard's hardware character-device contract.
            characters += 1
        elif kind == 'socket':
            if fp[3] != stat.S_IFSOCK or properties != (1, 5, 1):  # AF_UNIX, SOCK_SEQPACKET, accepting.
                raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
            proof = socket_proofs.get(fp[0])
            if (type(proof) is not dict or set(proof) != {'notConnected', 'peerErrno', 'localAddressClass'}
                    or proof['notConnected'] is not True or type(proof['peerErrno']) is not int
                    or proof['peerErrno'] != 107 or proof['localAddressClass'] not in ('unnamed', 'abstract', 'pathname')):
                raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
            sockets.add(fp[0])
        else:
            raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
    if set(socket_proofs) != sockets or len(sockets) != 1 or not 1 <= len(pipes) <= 8 or not 1 <= events <= 8:
        raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
    for pair in pipes.values():
        if len(pair) != 2 or sorted(fp[5] & os.O_ACCMODE for fp in pair) != [os.O_RDONLY, os.O_WRONLY]:
            raise InventoryRefused('GPU_ROLES_UNQUALIFIED')
    return {'listenerCount': len(sockets), 'pipePairCount': len(pipes),
            'eventfdCount': events, 'characterCount': characters,
            'listenerNotConnected': True, 'listenerAddressClass': socket_proofs[next(iter(sockets))]['localAddressClass'], 'preBootstrapStdioOnly': True,
            'knownNotificationRolesObserved': True, 'privateInputQualified': False}


class RoleGuard(BootstrapGuard):
    def __init__(self, operations):
        super().__init__()
        self.operations = operations

    def capture_inventory(self, fds, threads):
        if self.stage != 'bootstrapped' or type(fds) is not Snapshot or type(threads) is not Snapshot:
            self.refuse('BOOTSTRAP_SEQUENCE')
        try:
            validate_fd_snapshot(fds); validate_thread_snapshot(threads)
            first, last = self.operations.initial_sample, self.operations.bootstrapped_sample
            value = roles(first, last, self.operations.socket_proofs)
            if fds._identities != last.fds._identities or fds.metadata != last.fds.metadata:
                self.refuse('BOOTSTRAP_DESCRIPTORS_CHANGED')
            if threads._identities != last.tasks._identities or threads.metadata != last.tasks.metadata:
                self.refuse('BOOTSTRAP_THREAD_COVERAGE_UNQUALIFIED')
            row = threads.metadata
            if (row['no_new_privs_count'] != row['thread_count'] or row['seccomp_mode2_count'] != row['thread_count']
                    or type(row['uniform_filter_count']) is not int or row['uniform_filter_count'] < 1):
                self.refuse('BOOTSTRAP_PRESEAL_THREADS_UNQUALIFIED')
            self.before_fds, self.before_threads = fds, threads
            self.operations.role_observation = value
            self.stage = 'inventoried'
        except InventoryRefused:
            self.refuse('GPU_ROLES_UNQUALIFIED')

    def _require_postseal_inventory(self, fds, threads):
        # getsockopt is intentionally denied under the unchanged policy. The
        # retained fingerprints bind immutable family/type; listener state
        # cannot connect/accept/shutdown/listen after the syscall seal.
        if type(fds) is not Snapshot or type(threads) is not Snapshot:
            self.refuse('BOOTSTRAP_INVENTORY_SCHEMA')
        validate_fd_snapshot(fds); validate_thread_snapshot(threads)
        expected = self.before_fds.metadata
        expected['socket_domains'] = []
        if (fds._identities != self.before_fds._identities or fds._stdio_scope != self.before_fds._stdio_scope
                or fds._null_device != self.before_fds._null_device or fds.metadata != expected):
            self.refuse('BOOTSTRAP_DESCRIPTORS_CHANGED')
        row, previous = threads.metadata, self.before_threads.metadata
        if (threads._identities != self.before_threads._identities
                or row['thread_count'] != previous['thread_count']
                or row['no_new_privs_count'] != row['thread_count']
                or row['seccomp_mode2_count'] != row['thread_count']
                or row['uniform_filter_count'] != previous['uniform_filter_count'] + 1):
            self.refuse('BOOTSTRAP_THREAD_COVERAGE_UNQUALIFIED')


def validate_observation(value):
    if value is None: return None
    if (type(value) is not dict or set(value) != {'listenerCount', 'pipePairCount', 'eventfdCount', 'characterCount',
            'listenerNotConnected', 'listenerAddressClass', 'preBootstrapStdioOnly', 'knownNotificationRolesObserved', 'privateInputQualified'}
            or any(type(value[k]) is not int for k in ('listenerCount', 'pipePairCount', 'eventfdCount', 'characterCount'))
            or value['listenerAddressClass'] not in ('unnamed', 'abstract', 'pathname')
            or value['listenerCount'] != 1 or not 1 <= value['pipePairCount'] <= 8
            or not 1 <= value['eventfdCount'] <= 8 or not 0 <= value['characterCount'] <= 64
            or any(value[k] is not True for k in ('listenerNotConnected', 'preBootstrapStdioOnly', 'knownNotificationRolesObserved'))
            or value['privateInputQualified'] is not False):
        raise ValueError('GPU_ROLES_UNQUALIFIED')
    return dict(value)
