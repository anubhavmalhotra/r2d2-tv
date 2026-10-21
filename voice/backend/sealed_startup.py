"""Mandatory CUDA, filesystem, memory and all-thread network worker seal.

Bootstrap processes a fixed public tensor before model loading. Readiness uses
fresh resource/filter/denial readback after model loading and before every
private reply. This owned-worker boundary does not attest the TV microphone,
LG interception, driver internals or forensic erasure of hardware memory.
"""
import re
import signal
if __package__:
    from .execution_authority import issue_native_permit
    from .role_operations import RoleOperations, run as execute
    from .gpu_bootstrap_readback import fd_inventory, thread_inventory
    from .gpu_bootstrap_guard import validate_fd_snapshot, validate_thread_snapshot
    from .probe_contract import valid
    from . import filesystem_seal
    from . import model_descriptor_roles
else:
    from execution_authority import issue_native_permit
    from role_operations import RoleOperations,run as execute
    from gpu_bootstrap_readback import fd_inventory,thread_inventory
    from gpu_bootstrap_guard import validate_fd_snapshot,validate_thread_snapshot
    from probe_contract import valid
    import filesystem_seal
    import model_descriptor_roles

POLICY_SHA256='9f38390e5ea6847aaa3ba3ff2263d32710c186d700b9f271d8cc287f9de436c4'
FALSE=('privateASRQualified','GPUOrBrokerPrivacyQualified','filesystemPrivacyQualified',
       'forensicRetentionQualified','LGBackendBlockedProved')
TRUE=('sealed','publicTensorMatched','TSYNCZero','knownCudaRolesObserved',
      'persistentFileWritesDenied','socketOperationsDenied','resourceIdentitiesVerified')

class StartupRefused(ValueError):
    def __init__(self,row):
        super().__init__('PUBLIC_BOOTSTRAP_SEAL_UNQUALIFIED')
        self.public_row=dict(row)if valid(row)else None

class Context:
    """Process-private state; never serialized, logged or accepted from a client."""
    __slots__=('ops','attempt','original','resources','response_fd')
    def __init__(self,ops,attempt,original,response_fd):
        self.ops,self.attempt,self.original,self.response_fd=ops,attempt,original,response_fd
        self.resources={}

def validate_ready(row,attempt):
    keys={'schema','attempt','policySha256','noNewPrivs','seccompMode','filterCount',
          'threadCount','descriptorCount',*TRUE,*FALSE}
    if(type(row)is not dict or set(row)!=keys or type(row['schema'])is not int or row['schema']!=2
       or type(attempt)is not str or not re.fullmatch('[a-f0-9]{32}',attempt)or row['attempt']!=attempt
       or row['policySha256']!=POLICY_SHA256 or any(row[k]is not True for k in TRUE)
       or any(row[k]is not False for k in FALSE)
       or any(type(row[k])is not int for k in ('noNewPrivs','seccompMode','filterCount','threadCount','descriptorCount'))
       or row['noNewPrivs']!=1 or row['seccompMode']!=2 or row['filterCount']!=2
       or not 1<=row['threadCount']<=16 or not 4<=row['descriptorCount']<=64):
        raise ValueError('SEALED_READY_UNQUALIFIED')
    return dict(row)

def current_seal(context,response_fd,*,descriptors=fd_inventory,threads=thread_inventory):
    if type(context)is not Context or context.response_fd!=response_fd:raise ValueError('SEALED_CONTEXT_UNQUALIFIED')
    ops=context.ops
    fds=descriptors((0,1,2,response_fd),read_domains=False);tasks=threads()
    validate_fd_snapshot(fds);validate_thread_snapshot(tasks)
    roles=model_descriptor_roles.inspect(context.original,fds,context.resources)
    if not roles['qualified']:raise ValueError('SEALED_RESOURCES_UNQUALIFIED')
    task=tasks.metadata
    if(task['no_new_privs_count']!=task['thread_count']or task['seccomp_mode2_count']!=task['thread_count']
       or task['uniform_filter_count']!=2):raise ValueError('SEALED_THREADS_UNQUALIFIED')
    for family in(1,2,10,16):
        if ops.socket_creation_denial(family)!=(-1,1):raise ValueError('SEALED_NETWORK_UNQUALIFIED')
    filesystem_seal.check_denials(ops.denial_probe,ops.filesystem_observation)
    memory=ops.memory_sample()
    if memory['swapBytes']!=0 or memory['lockedBytes']<=0:raise ValueError('SEALED_MEMORY_UNQUALIFIED')
    final=descriptors((0,1,2,response_fd),read_domains=False);final_tasks=threads()
    if final._identities!=fds._identities or final_tasks._identities!=tasks._identities:
        raise ValueError('SEALED_RESOURCES_CHANGED')
    return validate_ready({'schema':2,'attempt':context.attempt,'policySha256':POLICY_SHA256,
                          'noNewPrivs':1,'seccompMode':2,'filterCount':2,
                          'threadCount':task['thread_count'],'descriptorCount':fds.metadata['descriptor_count'],
                          **{name:True for name in TRUE},**{name:False for name in FALSE}},context.attempt)

def bootstrap(response_fd,attempt,*,operations_class=RoleOperations,run=execute,
              descriptors=fd_inventory,threads=thread_inventory,issue=issue_native_permit):
    permit=issue(attempt,response_fd,POLICY_SHA256);ops=operations_class(permit)
    row=run(ops,attempt)
    if not valid(row)or row.get('complete')is not True or row.get('error')is not None:raise StartupRefused(row)
    filesystem_seal.validate(ops.filesystem_observation)
    if ops.filesystem_observation['negativeProbesPassed']is not True:raise ValueError('SEALED_FILESYSTEM_UNQUALIFIED')
    if __package__:
        from .role_guard import validate_observation
    else:
        from role_guard import validate_observation
    if validate_observation(ops.role_observation)is None:raise ValueError('SEALED_ROLES_UNQUALIFIED')
    original=ops.bootstrapped_sample.fds
    context=Context(ops,attempt,original,response_fd)
    current_seal(context,response_fd,descriptors=descriptors,threads=threads)
    signal.alarm(0)  # Parent separately bounds model startup and every request.
    return context,row
