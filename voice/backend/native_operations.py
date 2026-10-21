"""Public CUDA/TSYNC operations, requiring an explicit owned-child permit.

Importing makes no native call, imports no Torch/model, and inventories no FD.
The worker invokes these before model loading or reading private input.
"""
import ctypes
import json
import os
import platform
import resource
import select
import signal
import stat
import time
if __package__:
    from .execution_authority import require_native_permit
    from .gpu_bootstrap_readback import fd_inventory, thread_inventory
    from .gpu_bootstrap_tsync import apply, policy_hash
    from .process_isolation import lock_current_and_future
    from .lock_seal import seal
    from .memory_status import summary
    from .probe_contract import valid, MAX_RESPONSE_BYTES
else:
    from execution_authority import require_native_permit
    from gpu_bootstrap_readback import fd_inventory, thread_inventory
    from gpu_bootstrap_tsync import apply, policy_hash
    from process_isolation import lock_current_and_future
    from lock_seal import seal
    from memory_status import summary
    from probe_contract import valid, MAX_RESPONSE_BYTES


class NativeOperations:
    def __init__(self, permit):
        require_native_permit(permit)
        self.permit = permit
        self.torch = None
        self.created_socket = None
        self.started = time.monotonic()
        self.deadline = self.started + 35

    def owner_alive(self):
        require_native_permit(self.permit)
        return (time.monotonic() < self.deadline
                and os.getppid() == self.permit.parent_pid)

    def configure_owned_process(self):
        require_native_permit(self.permit)
        if (platform.system() != 'Linux' or platform.machine() != 'aarch64'
                or os.getuid() != 1000 or os.geteuid() != 1000
                or not self.owner_alive()):
            return False
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.umask(0o077)
        library = ctypes.CDLL(None, use_errno=True)
        library.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        library.prctl.restype = ctypes.c_int
        if library.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or not self.owner_alive():
            return False
        # The issuer binds the exact owned response pipe identity.
        info = os.fstat(self.permit.response_fd)
        if not stat.S_ISFIFO(info.st_mode):
            return False
        reader = writer = None
        try:
            reader = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)
            writer = os.open('/dev/null', os.O_WRONLY | os.O_CLOEXEC)
            # Keep the owned input pipe unread until the mandatory sealed-ready
            # handshake. Its exact read-only identity is checked by inventory.
            for fd in (1, 2):
                os.dup2(writer, fd, inheritable=False)
        finally:
            for owned in (reader, writer):
                if owned is not None and owned not in (0, 1, 2):
                    os.close(owned)
        def timeout(*_):
            raise TimeoutError()
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(35)
        return True

    def lock_and_seal(self):
        require_native_permit(self.permit)
        lock = lock_current_and_future()
        sealed = seal()
        sample = summary()
        return (lock.get('current_future_onfault_requested') is True
                and sealed.get('munlock_munlockall_mlockall_denied') is True
                and sample.get('no_new_privs') == 1
                and sample.get('seccomp_mode') == 2
                and sample.get('swap_bytes') == 0)

    def _tensor(self):
        torch = self.torch
        a = b = c = None
        try:
            with torch.inference_mode():
                a = torch.arange(16, dtype=torch.float32, device='cuda:0').reshape(4, 4)
                b = torch.eye(4, dtype=torch.float32, device='cuda:0')
                c = a @ b
                torch.cuda.synchronize()
                return bool(torch.equal(a, c))
        finally:
            a = b = c = None

    def bootstrap_public_tensor(self):
        require_native_permit(self.permit)
        import torch
        self.torch = torch
        versions = torch.__version__ == '2.9.1+cu130', torch.version.cuda == '13.0'
        if versions != (True, True):
            raise ValueError('GPU_UNQUALIFIED')
        driver = ctypes.CDLL('libcuda.so.1')
        driver.cuInit.argtypes = [ctypes.c_uint]
        driver.cuInit.restype = ctypes.c_int
        init = int(driver.cuInit(0))
        if init != 0:
            return {'torchVersionMatches': True, 'cudaVersionMatches': True,
                    'driverInitCode': init, 'deviceCountCode': None,
                    'deviceCount': None, 'cudaInitSucceeded': False,
                    'bootstrapTensorMatched': False, 'maximumLiveTensorElements': 48}
        count = ctypes.c_int()
        driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        driver.cuDeviceGetCount.restype = ctypes.c_int
        code = int(driver.cuDeviceGetCount(ctypes.byref(count)))
        torch.cuda.init()
        return {'torchVersionMatches': True, 'cudaVersionMatches': True,
                'driverInitCode': init, 'deviceCountCode': code,
                'deviceCount': count.value if code == 0 else None,
                'cudaInitSucceeded': bool(torch.cuda.is_initialized()),
                'bootstrapTensorMatched': self._tensor(),
                'maximumLiveTensorElements': 48}

    def inventory(self, *, read_domains):
        require_native_permit(self.permit)
        return (fd_inventory((0, 1, 2, self.permit.response_fd), read_domains=read_domains),
                thread_inventory())

    def synchronize_unchanged_policy(self):
        require_native_permit(self.permit)
        result = apply()
        return {'return': 0 if result.get('tsync_return_zero') is True else -1,
                'policyMatches': result.get('target_linux_arm64_policy_sha256') == self.permit.expected_policy_sha256
                and policy_hash() == self.permit.expected_policy_sha256}

    def postseal_public_tensor(self):
        require_native_permit(self.permit)
        return self._tensor()

    def socket_creation_denial(self, family):
        require_native_permit(self.permit)
        if type(family) is not int or family not in (1, 2, 10, 16):
            raise ValueError('NETWORK_UNQUALIFIED')
        library = ctypes.CDLL(None, use_errno=True)
        library.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        library.socket.restype = ctypes.c_int
        ctypes.set_errno(0)
        fd = library.socket(family, 2 | 0x80000, 0)
        error = ctypes.get_errno()
        if fd >= 0:
            # This is the one exact newly owned FD, never an inspected driver FD.
            os.close(fd)
        return int(fd), int(error)

    def memory_sample(self):
        require_native_permit(self.permit)
        sample = summary()
        return {'swapBytes': sample['swap_bytes'], 'lockedBytes': sample['locked_bytes']}

    def destroy_public_tensor_references(self):
        self.torch = None

    def emit_scalar(self, row):
        require_native_permit(self.permit)
        if not valid(row) or row['attempt'] != self.permit.attempt:
            raise ValueError('RESPONSE_SCOPE')
        raw = json.dumps(row, separators=(',', ':')).encode()
        if len(raw) > MAX_RESPONSE_BYTES or not self.owner_alive():
            raise ValueError('RESPONSE_BOUND')
        fd = self.permit.response_fd
        os.set_blocking(fd, False)
        end = min(self.deadline, time.monotonic() + 2)
        while raw:
            if time.monotonic() >= end or not self.owner_alive():
                raise ValueError('RESPONSE_TIMEOUT')
            if not select.select([], [fd], [], min(.05, end-time.monotonic()))[1]:
                continue
            try:
                count = os.write(fd, raw[:4096])
            except BlockingIOError:
                continue
            if count <= 0:
                raise ValueError('RESPONSE_UNCONFIRMED')
            raw = raw[count:]
        signal.alarm(0)
        os.close(fd)
