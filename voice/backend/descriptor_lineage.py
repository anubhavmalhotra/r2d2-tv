"""Own-process public-bootstrap observations, never GPU-channel admission.

Raw fingerprints and symlink targets stay local. Output uses fresh ordinal IDs,
fixed descriptor classes, and scalar socket properties. Equal fingerprints are
observations, not exact open-file-description identity or a no-ABA guarantee.
"""
import copy
import ctypes
import os
import stat

if __package__:
    from .gpu_bootstrap_guard import validate_fd_snapshot, validate_thread_snapshot
    from .gpu_bootstrap_readback import fd_inventory, thread_inventory, InventoryRefused
else:
    from gpu_bootstrap_guard import validate_fd_snapshot, validate_thread_snapshot
    from gpu_bootstrap_readback import fd_inventory, thread_inventory, InventoryRefused

MAX_RESULT_ROWS = 128
ANON_CLASSES = {
    'anon_inode:[eventfd]': 'eventfd',
    'anon_inode:[eventpoll]': 'eventpoll',
    'anon_inode:[timerfd]': 'timerfd',
    'anon_inode:[signalfd]': 'signalfd',
    'anon_inode:inotify': 'inotify',
    'anon_inode:[inotify]': 'inotify',
}
SOCKET_FIELDS = ('family', 'type', 'accepting')
KINDS = {'stdio', 'pipe', 'socket', 'character', 'regular', 'directory',
         'unknown', *ANON_CLASSES.values()}
RELATIONS = {'retained-observed', 'added-observed', 'removed-observed',
             'replaced-before', 'replaced-after'}


def refuse():
    raise InventoryRefused('LINEAGE_UNCONFIRMED')


def _socket_properties(fd):
    # No recv, send, peer-name query, endpoint inspection or descriptor duplicate.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getsockopt.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_void_p]
    libc.getsockopt.restype = ctypes.c_int
    result = {}
    for key, option, maximum in (('family', 39, 255), ('type', 3, 15),
                                 ('accepting', 30, 1)):
        value = ctypes.c_int()
        length = ctypes.c_uint(ctypes.sizeof(value))
        if (libc.getsockopt(fd, 1, option, ctypes.byref(value),
                           ctypes.byref(length)) != 0 or length.value != 4
                or not 0 <= value.value <= maximum):
            refuse()
        result[key] = value.value
    return result


def _classify(fp, scope, *, readlink, socket_properties):
    fd, _, _, kind, _, _, _ = fp
    if fd in scope:
        return ('stdio', None)
    kinds = {stat.S_IFIFO: 'pipe', stat.S_IFSOCK: 'socket',
             stat.S_IFCHR: 'character', stat.S_IFREG: 'regular',
             stat.S_IFDIR: 'directory'}
    label = kinds.get(kind, 'unknown')
    details = None
    if label == 'socket':
        details = socket_properties(fd)
        if (type(details) is not dict or set(details) != set(SOCKET_FIELDS)
                or any(type(details[k]) is not int for k in SOCKET_FIELDS)
                or not 0 <= details['family'] <= 255
                or not 0 <= details['type'] <= 15 or details['accepting'] not in (0, 1)):
            refuse()
        details = copy.deepcopy(details)
    elif label == 'unknown':
        # readlink is metadata, not descriptor content. Arbitrary targets never
        # escape; only these exact Linux anonymous-inode tags can be classified.
        target = readlink('/proc/self/fd/' + str(fd))
        if type(target) is not str or len(target) > 4096:
            refuse()
        label = ANON_CLASSES.get(target, 'unknown')
    return label, details


class Sample:
    __slots__ = ('fds', 'tasks', 'details')

    def __init__(self, fds, tasks, details):
        validate_fd_snapshot(fds)
        validate_thread_snapshot(tasks)
        object.__setattr__(self, 'fds', fds)
        object.__setattr__(self, 'tasks', tasks)
        object.__setattr__(self, 'details', tuple(details))

    def __setattr__(self, *_):
        raise AttributeError('immutable lineage observation')

    def __repr__(self):
        return '<private own-process lineage observation>'


def capture(scope, *, inventory=fd_inventory, tasks=thread_inventory,
            readlink=os.readlink, socket_properties=_socket_properties):
    before = inventory(scope, read_domains=True)
    validate_fd_snapshot(before)
    threads = tasks()
    validate_thread_snapshot(threads)
    details = []
    for fp in sorted(before._identities):
        label, properties = _classify(fp, scope, readlink=readlink,
                                     socket_properties=socket_properties)
        details.append((fp, label, None if properties is None else
                        tuple(properties[k] for k in SOCKET_FIELDS)))
    # Refuse deletion/reuse or metadata drift while enrichment was running.
    after = inventory(scope, read_domains=True)
    validate_fd_snapshot(after)
    if before._identities != after._identities or before.metadata != after.metadata:
        refuse()
    return Sample(before, threads, details)


def compare(before, after):
    if type(before) is not Sample or type(after) is not Sample:
        refuse()
    if (before.fds._stdio_scope != after.fds._stdio_scope
            or before.fds._null_device != after.fds._null_device):
        refuse()
    left = {fp[0]: (fp, kind, properties) for fp, kind, properties in before.details}
    right = {fp[0]: (fp, kind, properties) for fp, kind, properties in after.details}
    rows, pipe_groups = [], {}

    def append(item, relation):
        fp, kind, properties = item
        group = None
        if kind == 'pipe':
            # Group only observed same dev/inode/type. This is not a peer-identity
            # assertion, a pipe ownership proof, or open-file-description proof.
            group_key = fp[1:4]
            group = pipe_groups.setdefault(group_key, len(pipe_groups) + 1)
        rows.append({'id': len(rows) + 1, 'kind': kind, 'relation': relation,
                     'pipeGroup': group,
                     'socket': None if properties is None else dict(zip(SOCKET_FIELDS, properties))})

    for fd in sorted(set(left) | set(right)):
        if fd not in left:
            append(right[fd], 'added-observed')
        elif fd not in right:
            append(left[fd], 'removed-observed')
        elif left[fd] == right[fd]:
            append(left[fd], 'retained-observed')
        else:
            append(left[fd], 'replaced-before')
            append(right[fd], 'replaced-after')
    prior_tasks, next_tasks = before.tasks._identities, after.tasks._identities
    result = {
        'schema': 1, 'descriptors': rows,
        'beforeDescriptorCount': len(left), 'afterDescriptorCount': len(right),
        'threads': {'retainedObserved': len(prior_tasks & next_tasks),
                    'addedObserved': len(next_tasks - prior_tasks),
                    'removedObserved': len(prior_tasks - next_tasks)},
        'privateInputQualified': False, 'channelRolesQualified': False,
        'allIntermediateActivityObserved': False, 'exactOpenFileDescriptionsProven': False,
        'pathsEndpointsPayloadOrNativeIdsExported': False,
        'inspectedDescriptorsClosed': False,
    }
    return validate(result)


def validate(result):
    fields = {'schema', 'descriptors', 'beforeDescriptorCount', 'afterDescriptorCount',
              'threads', 'privateInputQualified', 'channelRolesQualified',
              'allIntermediateActivityObserved', 'exactOpenFileDescriptionsProven',
              'pathsEndpointsPayloadOrNativeIdsExported', 'inspectedDescriptorsClosed'}
    flags = fields - {'schema', 'descriptors', 'beforeDescriptorCount',
                     'afterDescriptorCount', 'threads'}
    if (type(result) is not dict or set(result) != fields
            or type(result['schema']) is not int or result['schema'] != 1
            or any(result[k] is not False for k in flags)
            or any(type(result[k]) is not int or not 3 <= result[k] <= 64
                   for k in ('beforeDescriptorCount', 'afterDescriptorCount'))):
        refuse()
    rows = result['descriptors']
    if type(rows) is not list or not 3 <= len(rows) <= MAX_RESULT_ROWS:
        refuse()
    before_count = after_count = 0
    for index, row in enumerate(rows, 1):
        if (type(row) is not dict or set(row) != {'id', 'kind', 'relation', 'pipeGroup', 'socket'}
                or type(row['id']) is not int or row['id'] != index
                or type(row['kind']) is not str or row['kind'] not in KINDS
                or type(row['relation']) is not str or row['relation'] not in RELATIONS):
            refuse()
        if row['kind'] == 'pipe':
            if type(row['pipeGroup']) is not int or not 1 <= row['pipeGroup'] <= 64:
                refuse()
        elif row['pipeGroup'] is not None:
            refuse()
        if row['kind'] == 'socket':
            prop = row['socket']
            if (type(prop) is not dict or set(prop) != set(SOCKET_FIELDS)
                    or any(type(prop[k]) is not int for k in SOCKET_FIELDS)
                    or not 0 <= prop['family'] <= 255 or not 0 <= prop['type'] <= 15
                    or prop['accepting'] not in (0, 1)):
                refuse()
        elif row['socket'] is not None:
            refuse()
        before_count += row['relation'] in ('retained-observed', 'removed-observed', 'replaced-before')
        after_count += row['relation'] in ('retained-observed', 'added-observed', 'replaced-after')
    if before_count != result['beforeDescriptorCount'] or after_count != result['afterDescriptorCount']:
        refuse()
    threads = result['threads']
    if (type(threads) is not dict or set(threads) != {'retainedObserved', 'addedObserved', 'removedObserved'}
            or any(type(v) is not int or not 0 <= v <= 16 for v in threads.values())
            or not 1 <= threads['retainedObserved'] + threads['addedObserved'] <= 16
            or not 1 <= threads['retainedObserved'] + threads['removedObserved'] <= 16):
        refuse()
    return copy.deepcopy(result)
