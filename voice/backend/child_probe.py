"""Actual qualification sequence; native operations must receive an issued permit.

Tests inject operations and do not construct NativeOperations or call device APIs.
There is no standalone command-line native entry point in this module.
"""
import time
if __package__:
    from .gpu_bootstrap_guard import BootstrapGuard
    from .gpu_bootstrap_readback import InventoryRefused
    from .probe_contract import FAMILIES, new, valid
else:
    from gpu_bootstrap_guard import BootstrapGuard
    from gpu_bootstrap_readback import InventoryRefused
    from probe_contract import FAMILIES, new, valid


class ProbeRefused(ValueError):
    pass


def need(value, code):
    if not value:
        raise ProbeRefused(code)


def execute(ops, attempt, *, clock=time.monotonic, guard_factory=BootstrapGuard):
    started = clock()
    deadline = started + 35
    row = new(attempt)
    guard = guard_factory()
    def checkpoint():
        need(clock() < deadline and ops.owner_alive() is True,
             'DEADLINE_OR_OWNER_DRIFT')
    try:
        checkpoint()
        row['phase'] = 'memory'
        need(ops.configure_owned_process() is True, 'CHILD_AUTHORITY_MISSING')
        need(ops.lock_and_seal() is True, 'MEMORY_UNQUALIFIED')
        checkpoint()
        row['phase'] = 'bootstrap'
        boot = ops.bootstrap_public_tensor()
        expected = {'torchVersionMatches', 'cudaVersionMatches', 'driverInitCode',
                    'deviceCountCode', 'deviceCount', 'cudaInitSucceeded',
                    'bootstrapTensorMatched', 'maximumLiveTensorElements'}
        need(type(boot) is dict and set(boot) == expected, 'GPU_UNQUALIFIED')
        row.update(boot)
        need(row['deviceCountCode'] == 0 and type(row['deviceCountCode']) is int
             and type(row['deviceCount']) is int and row['deviceCount'] >= 1
             and row['cudaInitSucceeded'] is True
             and type(row['maximumLiveTensorElements']) is int
             and row['maximumLiveTensorElements'] == 48, 'GPU_UNQUALIFIED')
        guard.bootstrap_complete(torch_version_matches=row['torchVersionMatches'],
                                 cuda_version_matches=row['cudaVersionMatches'],
                                 driver_init_code=row['driverInitCode'],
                                 tensor_matched=row['bootstrapTensorMatched'])
        checkpoint()
        row['phase'] = 'inventory-before'
        fds, threads = ops.inventory(read_domains=True)
        guard.capture_inventory(fds, threads)
        row['inventoryBeforeVerified'] = True
        checkpoint()
        row['phase'] = 'tsync'
        seal = ops.synchronize_unchanged_policy()
        need(type(seal) is dict and set(seal) == {'return', 'policyMatches'}, 'TSYNC_UNQUALIFIED')
        guard.seal_complete(tsync_return=seal['return'], filter_sha256_matches=seal['policyMatches'])
        row['TSYNCZero'], row['policyHashMatches'] = True, True
        checkpoint()
        row['phase'] = 'inventory-after'
        fds, threads = ops.inventory(read_domains=False)
        guard.after_inventory(fds, threads)
        row['inventoryAfterVerified'] = True
        checkpoint()
        row['phase'] = 'tensor-after'
        row['postTensorMatched'] = ops.postseal_public_tensor()
        need(row['postTensorMatched'] is True, 'POSTSEAL_UNQUALIFIED')
        checkpoint()
        row['phase'] = 'network'
        for family in FAMILIES:
            result = ops.socket_creation_denial(family)
            need(type(result) is tuple and len(result) == 2
                 and type(result[0]) is int and result[0] == -1
                 and type(result[1]) is int and result[1] == 1,
                 'NETWORK_UNQUALIFIED')
            row['networkDenials'].append({'family': family, 'return': -1, 'errno': 1})
            checkpoint()
        row['networkDenialsVerified'] = True
        row['phase'] = 'inventory-final'
        mem = ops.memory_sample()
        need(type(mem) is dict and set(mem) == {'swapBytes', 'lockedBytes'}, 'MEMORY_UNQUALIFIED')
        row['sampleSwapBytes'], row['sampleLockedBytes'] = mem['swapBytes'], mem['lockedBytes']
        fds, threads = ops.inventory(read_domains=False)
        guard.fixed_tensor_completed(matched=row['postTensorMatched'],
                                     network_denials_verified=True,
                                     swap_bytes=row['sampleSwapBytes'],
                                     locked_bytes=row['sampleLockedBytes'],
                                     fds=fds, threads=threads)
        row['inventoryFinalVerified'] = True
        checkpoint()
        need(guard.private_input_allowed() is False, 'POSTSEAL_UNQUALIFIED')
        row['phase'], row['complete'] = 'complete', True
    except InventoryRefused:
        row['error'] = ('TSYNC_UNQUALIFIED' if row['phase'] == 'tsync' else
                        'POSTSEAL_UNQUALIFIED' if row['phase'] == 'inventory-final' else
                        'INVENTORY_UNQUALIFIED')
    except ProbeRefused as exc:
        row['error'] = str(exc)
    except BaseException:
        row['error'] = 'PROBE_UNCONFIRMED'
    finally:
        try:
            ops.destroy_public_tensor_references()
        except BaseException:
            row['complete'], row['error'] = False, 'PROBE_UNCONFIRMED'
        elapsed = round((clock()-started)*1000)
        if not 0 <= elapsed <= 35000:
            row['complete'], row['error'] = False, 'DEADLINE_OR_OWNER_DRIFT'
        row['wallMs'] = max(0, min(35000, elapsed))
        if row['error'] is not None:
            row['complete'] = False
    if not valid(row):
        fallback = new(attempt)
        fallback.update(error='PROBE_UNCONFIRMED', wallMs=row['wallMs'])
        return fallback
    return row
