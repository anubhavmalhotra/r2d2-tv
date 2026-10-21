"""Single-use acceptance guard for a fixed public tensor diagnostic only.

No microphone/model ingress exists. Fingerprint consistency and tensor success
never open private input or attest GPU brokers, filesystems or RAM retention.
"""
import os
import stat
if __package__:
    from .gpu_bootstrap_readback import Snapshot, InventoryRefused, O_PATH_LINUX
else:
    from gpu_bootstrap_readback import Snapshot, InventoryRefused, O_PATH_LINUX

LINUX_ERRNO = {'EPERM': 1, 'ENOSYS': 38, 'EACCES': 13, 'EBADF': 9}


def _integers(values, maximum):
    return all(type(value) is int and 0 <= value <= maximum for value in values)


def validate_thread_snapshot(snapshot):
    row = snapshot.metadata
    fields = {
        'schema', 'thread_count', 'no_new_privs_count', 'seccomp_mode2_count',
        'uniform_filter_count', 'names_uids_paths_or_task_ids_exported',
        'thread_start_time_identity_held_privately', 'consistent_two_pass_snapshot',
        'all_intermediate_thread_activity_proven_absent',
    }
    if (snapshot._kind != 'threads' or snapshot._stdio_scope != ()
            or snapshot._null_device is not None or type(row) is not dict
            or set(row) != fields or type(row['schema']) is not int or row['schema'] != 2
            or row['names_uids_paths_or_task_ids_exported'] is not False
            or row['all_intermediate_thread_activity_proven_absent'] is not False
            or row['thread_start_time_identity_held_privately'] is not True
            or row['consistent_two_pass_snapshot'] is not True
            or not _integers((row[key] for key in
                             ('thread_count', 'no_new_privs_count', 'seccomp_mode2_count')), 16)
            or not 1 <= row['thread_count'] <= 16
            or row['no_new_privs_count'] > row['thread_count']
            or row['seccomp_mode2_count'] > row['thread_count']
            or len(snapshot._identities) != row['thread_count']
            or (row['uniform_filter_count'] is not None and
                (type(row['uniform_filter_count']) is not int or
                 not 0 <= row['uniform_filter_count'] <= 32))):
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    if any(type(value) is not tuple or len(value) != 2
           or not _integers(value, 2**64-1) or not 0 < value[0] <= 2**31-1
           for value in snapshot._identities):
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    if len({value[0] for value in snapshot._identities}) != len(snapshot._identities):
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')


def validate_fd_snapshot(snapshot):
    row = snapshot.metadata
    counts = {
        'descriptor_count', 'allowed_stdio_count', 'stdio_mismatch_count',
        'socket_count', 'extra_pipe_count', 'extra_regular_count',
        'extra_directory_count', 'extra_character_count', 'extra_unknown_count',
    }
    false_flags = {
        'endpoints_paths_descriptor_ids_or_payload_exported',
        'inspected_descriptors_closed', 'gpu_or_broker_character_devices_qualified',
        'exact_open_file_description_qualified',
        'all_intermediate_descriptor_activity_proven_absent',
    }
    true_flags = {
        'descriptor_fingerprints_include_inode_device_type_and_access_flags',
        'enumeration_descriptor_excluded_only_by_owned_identity',
        'consistent_two_pass_snapshot',
    }
    fields = {'schema', *counts, *false_flags, *true_flags, 'socket_domains', 'character_devices'}
    scope = snapshot._stdio_scope
    if (snapshot._kind != 'fds' or type(row) is not dict or set(row) != fields
            or type(row['schema']) is not int or row['schema'] != 2
            or any(row[key] is not False for key in false_flags)
            or any(row[key] is not True for key in true_flags)
            or not _integers((row[key] for key in counts), 64)
            or len(snapshot._identities) != row['descriptor_count']
            or type(scope) is not tuple or not 3 <= len(scope) <= 4
            or len(set(scope)) != len(scope) or not {0, 1, 2} <= set(scope)
            or not _integers(scope, 2**31-1)
            or type(snapshot._null_device) is not int
            or not 0 <= snapshot._null_device <= 2**64-1):
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    for fp in snapshot._identities:
        if (type(fp) is not tuple or len(fp) != 7 or not _integers(fp, 2**64-1)
                or fp[0] > 2**31-1 or fp[5] > 2**32-1 or fp[6] > 1
                or fp[5] & O_PATH_LINUX):
            raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    ids = {value[0] for value in snapshot._identities}
    if len(ids) != len(snapshot._identities) or not set(scope) <= ids:
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')

    # Derive classifications again from the hidden records. A fabricated
    # scalar histogram cannot hide a socket/pipe/file or wrong stdio role.
    expected = {key: 0 for key in counts}
    expected['descriptor_count'] = len(snapshot._identities)
    characters = {}
    for fd, _, _, mode, rdev, access, _ in snapshot._identities:
        fifo = mode == stat.S_IFIFO
        null = mode == stat.S_IFCHR and rdev == snapshot._null_device
        if fd in scope:
            role = os.O_RDONLY if fd == 0 else os.O_WRONLY
            key = ('allowed_stdio_count' if (fifo or null) and
                   access & os.O_ACCMODE == role else 'stdio_mismatch_count')
        elif mode == stat.S_IFSOCK:
            key = 'socket_count'
        elif fifo:
            key = 'extra_pipe_count'
        elif mode == stat.S_IFREG:
            key = 'extra_regular_count'
        elif mode == stat.S_IFDIR:
            key = 'extra_directory_count'
        elif mode == stat.S_IFCHR:
            key = 'extra_character_count'
            identity = (os.major(rdev), os.minor(rdev))
            characters[identity] = characters.get(identity, 0) + 1
        else:
            key = 'extra_unknown_count'
        expected[key] += 1
    if any(row[key] != expected[key] for key in counts):
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    if type(row['socket_domains']) is not list or len(row['socket_domains']) > 64:
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    previous = -1
    for value in row['socket_domains']:
        if (type(value) is not dict or set(value) != {'family', 'count'}
                or type(value['family']) is not int or not previous < value['family'] <= 255
                or type(value['count']) is not int or not 1 <= value['count'] <= 64):
            raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
        previous = value['family']
    if sum(value['count'] for value in row['socket_domains']) > row['socket_count']:
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    expected_characters = [
        {'major': major, 'minor': minor, 'count': count}
        for (major, minor), count in sorted(characters.items())
    ]
    if type(row['character_devices']) is not list or len(row['character_devices']) > 64:
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    for value in row['character_devices']:
        if (type(value) is not dict or set(value) != {'major', 'minor', 'count'}
                or not _integers((value['major'], value['minor']), 2**32-1)
                or type(value['count']) is not int or not 1 <= value['count'] <= 64):
            raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')
    if row['character_devices'] != expected_characters:
        raise InventoryRefused('BOOTSTRAP_INVENTORY_SCHEMA')


class BootstrapGuard:
    def __init__(self):
        self.stage = 'new'
        self.before_fds = self.before_threads = None
        self.refusal = None

    def refuse(self, code):
        self.stage = 'refused'
        self.refusal = code
        raise InventoryRefused(code)

    def bootstrap_complete(self, *, torch_version_matches, cuda_version_matches,
                           driver_init_code, tensor_matched):
        if self.stage != 'new':
            self.refuse('BOOTSTRAP_SEQUENCE')
        if (torch_version_matches is not True or cuda_version_matches is not True
                or type(driver_init_code) is not int or driver_init_code != 0
                or tensor_matched is not True):
            self.refuse('BOOTSTRAP_GPU_UNQUALIFIED')
        self.stage = 'bootstrapped'

    def capture_inventory(self, fds, threads):
        if self.stage != 'bootstrapped' or type(fds) is not Snapshot or type(threads) is not Snapshot:
            self.refuse('BOOTSTRAP_SEQUENCE')
        try:
            validate_fd_snapshot(fds)
            validate_thread_snapshot(threads)
        except InventoryRefused:
            self.refuse('BOOTSTRAP_INVENTORY_SCHEMA')
        self.before_fds, self.before_threads = fds, threads
        if (any(fds.metadata[key] != 0 for key in (
                'stdio_mismatch_count', 'socket_count', 'extra_pipe_count',
                'extra_regular_count', 'extra_directory_count', 'extra_unknown_count'))
                or fds.metadata['allowed_stdio_count'] != len(fds._stdio_scope)):
            self.refuse('BOOTSTRAP_INHERITED_CHANNEL_UNQUALIFIED')
        row = threads.metadata
        if (row['no_new_privs_count'] != row['thread_count']
                or row['seccomp_mode2_count'] != row['thread_count']
                or type(row['uniform_filter_count']) is not int
                or row['uniform_filter_count'] < 1):
            self.refuse('BOOTSTRAP_PRESEAL_THREADS_UNQUALIFIED')
        self.stage = 'inventoried'

    def seal_complete(self, *, tsync_return, filter_sha256_matches):
        if self.stage != 'inventoried':
            self.refuse('BOOTSTRAP_SEQUENCE')
        if type(tsync_return) is not int or tsync_return != 0 or filter_sha256_matches is not True:
            self.refuse('BOOTSTRAP_TSYNC_UNQUALIFIED')
        self.stage = 'sealed'

    def _require_postseal_inventory(self, fds, threads):
        if type(fds) is not Snapshot or type(threads) is not Snapshot:
            self.refuse('BOOTSTRAP_INVENTORY_SCHEMA')
        try:
            validate_fd_snapshot(fds)
            validate_thread_snapshot(threads)
        except InventoryRefused:
            self.refuse('BOOTSTRAP_INVENTORY_SCHEMA')
        if (fds._identities != self.before_fds._identities
                or fds._stdio_scope != self.before_fds._stdio_scope
                or fds._null_device != self.before_fds._null_device
                or fds.metadata != self.before_fds.metadata):
            self.refuse('BOOTSTRAP_DESCRIPTORS_CHANGED')
        row, previous = threads.metadata, self.before_threads.metadata
        if (threads._identities != self.before_threads._identities
                or row['thread_count'] != previous['thread_count']
                or row['no_new_privs_count'] != row['thread_count']
                or row['seccomp_mode2_count'] != row['thread_count']
                or row['uniform_filter_count'] != previous['uniform_filter_count'] + 1):
            self.refuse('BOOTSTRAP_THREAD_COVERAGE_UNQUALIFIED')

    def after_inventory(self, fds, threads):
        if self.stage != 'sealed':
            self.refuse('BOOTSTRAP_SEQUENCE')
        self._require_postseal_inventory(fds, threads)
        self.stage = 'fixed-tensor-test'

    def fixed_tensor_completed(self, *, matched, network_denials_verified,
                               swap_bytes, locked_bytes, fds, threads):
        if self.stage != 'fixed-tensor-test':
            self.refuse('BOOTSTRAP_SEQUENCE')
        # Tensor work and denial tests may themselves create or replace resources.
        self._require_postseal_inventory(fds, threads)
        if (matched is not True or network_denials_verified is not True
                or type(swap_bytes) is not int or swap_bytes != 0
                or type(locked_bytes) is not int or locked_bytes <= 0):
            self.refuse('BOOTSTRAP_POSTSEAL_UNQUALIFIED')
        self.stage = 'functional-diagnostic-complete'

    def private_input_allowed(self):
        return False

    def status(self):
        return {
            'schema': 2, 'stage': self.stage, 'refusal': self.refusal,
            'private_input_allowed': False, 'native_model_qualified': False,
            'broker_or_inherited_gpu_channels_qualified': False,
            'filesystem_payload_retention_qualified': False,
            'forensic_cpu_gpu_ram_only_qualified': False,
            'exact_open_file_description_qualified': False,
            'fixed_tensor_functional_probe_complete': self.stage == 'functional-diagnostic-complete',
        }
