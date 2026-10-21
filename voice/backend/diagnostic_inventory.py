"""Fixed scalar inventory reason/count diagnostic for public tensors only.

No FD/task IDs, paths, identities, endpoints, payload or arbitrary error text.
The original guard and complete TSYNC policy remain mandatory and unchanged.
"""
if __package__:
    from .native_operations import NativeOperations
    from .gpu_bootstrap_readback import fd_inventory, thread_inventory, InventoryRefused
    from .gpu_bootstrap_guard import BootstrapGuard
    from .child_probe import execute
else:
    from native_operations import NativeOperations
    from gpu_bootstrap_readback import fd_inventory, thread_inventory, InventoryRefused
    from gpu_bootstrap_guard import BootstrapGuard
    from child_probe import execute

DESCRIPTORS = ('descriptor_count', 'allowed_stdio_count', 'stdio_mismatch_count',
               'socket_count', 'extra_pipe_count', 'extra_regular_count',
               'extra_directory_count', 'extra_character_count', 'extra_unknown_count')
THREADS = ('thread_count', 'no_new_privs_count', 'seccomp_mode2_count', 'uniform_filter_count')
CODES = {
    'GPU_ROLES_UNQUALIFIED', 'INVENTORY_UNCONFIRMED', 'BOOTSTRAP_STDIO_SCOPE', 'BOOTSTRAP_INVENTORY_SCHEMA',
    'BOOTSTRAP_FD_SCOPE', 'BOOTSTRAP_FD_RACE', 'BOOTSTRAP_FD_IDENTITY',
    'BOOTSTRAP_FD_CLASS_UNSUPPORTED', 'BOOTSTRAP_ENUMERATOR_ARCH',
    'BOOTSTRAP_ENUMERATOR_OPEN', 'BOOTSTRAP_ENUMERATOR_IDENTITY',
    'BOOTSTRAP_ENUMERATOR_SEQUENCE', 'BOOTSTRAP_ENUMERATOR_SCAN',
    'BOOTSTRAP_ENUMERATOR_CLOSE', 'BOOTSTRAP_SOCKET_DOMAIN',
    'BOOTSTRAP_THREAD_STATUS', 'BOOTSTRAP_THREAD_IDENTITY',
    'BOOTSTRAP_THREAD_SCOPE', 'BOOTSTRAP_THREAD_RACE',
    'BOOTSTRAP_INHERITED_CHANNEL_UNQUALIFIED', 'BOOTSTRAP_PRESEAL_THREADS_UNQUALIFIED',
}


def new():
    return {'stage': None, 'code': None, 'descriptorCounts': None,
            'socketFamilies': None, 'threadCounts': None}


def validate(value):
    if (type(value) is not dict or set(value) != set(new())
            or value['stage'] not in (None, 'descriptors', 'threads', 'guard')
            or value['code'] is not None and value['code'] not in CODES):
        raise ValueError('INVENTORY_RESULT_SCOPE')
    fds, tasks, families = value['descriptorCounts'], value['threadCounts'], value['socketFamilies']
    if fds is not None:
        if (type(fds) is not dict or set(fds) != set(DESCRIPTORS)
                or any(type(v) is not int or not 0 <= v <= 64 for v in fds.values())
                or fds['descriptor_count'] != sum(fds[k] for k in DESCRIPTORS[1:])):
            raise ValueError('INVENTORY_RESULT_SCOPE')
    if tasks is not None:
        if (type(tasks) is not dict or set(tasks) != set(THREADS)
                or any(type(tasks[k]) is not int or not 0 <= tasks[k] <= 16 for k in THREADS[:3])
                or tasks['uniform_filter_count'] is not None and
                    (type(tasks['uniform_filter_count']) is not int or not 0 <= tasks['uniform_filter_count'] <= 32)
                or tasks['no_new_privs_count'] > tasks['thread_count']
                or tasks['seccomp_mode2_count'] > tasks['thread_count']):
            raise ValueError('INVENTORY_RESULT_SCOPE')
    if families is not None:
        if type(families) is not list or len(families) > 16 or fds is None:
            raise ValueError('INVENTORY_RESULT_SCOPE')
        previous = -1
        for row in families:
            if (type(row) is not dict or set(row) != {'family', 'count'}
                    or type(row['family']) is not int or not previous < row['family'] <= 255
                    or type(row['count']) is not int or not 1 <= row['count'] <= 64):
                raise ValueError('INVENTORY_RESULT_SCOPE')
            previous = row['family']
        if sum(row['count'] for row in families) > fds['socket_count']:
            raise ValueError('INVENTORY_RESULT_SCOPE')
    return {key: value[key] for key in new()}


def reason(value, error):
    code = str(error) if type(error) is InventoryRefused else 'INVENTORY_UNCONFIRMED'
    value['code'] = code if code in CODES else 'INVENTORY_UNCONFIRMED'


class DiagnosticOperations(NativeOperations):
    def __init__(self, permit):
        super().__init__(permit)
        self.inventory_observation = new()

    def inventory(self, *, read_domains):
        # Same strict inventory calls and ordering; record only selected scalars.
        value = self.inventory_observation
        value['stage'] = 'descriptors'
        try:
            fds = fd_inventory((0, 1, 2, self.permit.response_fd), read_domains=read_domains)
            counts = fds.metadata
            value['descriptorCounts'] = {key: counts[key] for key in DESCRIPTORS}
            value['socketFamilies'] = counts['socket_domains']
            value['stage'] = 'threads'
            tasks = thread_inventory()
            value['threadCounts'] = {key: tasks.metadata[key] for key in THREADS}
            validate(value)
            return fds, tasks
        except BaseException as error:
            reason(value, error)
            raise


def diagnostic_execute(ops, attempt):
    class DiagnosticGuard(BootstrapGuard):
        def capture_inventory(self, fds, threads):
            ops.inventory_observation['stage'] = 'guard'
            try:
                return super().capture_inventory(fds, threads)
            except BaseException as error:
                reason(ops.inventory_observation, error)
                raise
    return execute(ops, attempt, guard_factory=DiagnosticGuard)
