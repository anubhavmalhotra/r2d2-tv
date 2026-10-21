"""Borrowed socket metadata only; never receive/send/accept or duplicate an FD."""
import ctypes
import os


def classify_address(raw, size):
    if type(raw) is not bytes or type(size) is not int or not 2 <= size <= 110 or len(raw) != size:
        raise ValueError('SOCKET_SCOPE_UNQUALIFIED')
    if int.from_bytes(raw[:2], 'little') != 1:
        raise ValueError('SOCKET_SCOPE_UNQUALIFIED')
    return 'unnamed' if size == 2 else 'abstract' if raw[2] == 0 else 'pathname'


def query(fd, *, library=None):
    if type(fd) is not int or not 3 <= fd <= 2**31 - 1:
        raise ValueError('SOCKET_SCOPE_UNQUALIFIED')
    libc = library or ctypes.CDLL(None, use_errno=True)
    for name in ('getsockname', 'getpeername'):
        function = getattr(libc, name)
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        function.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(128)
    size = ctypes.c_uint(128)
    try:
        ctypes.set_errno(0)
        code = libc.getpeername(fd, buffer, ctypes.byref(size))
        peer_errno = ctypes.get_errno() if code != 0 else 0
        if code != -1 or peer_errno != 107:  # Linux ENOTCONN, never host errno.
            raise ValueError('SOCKET_SCOPE_UNQUALIFIED')
        size.value = 128
        if libc.getsockname(fd, buffer, ctypes.byref(size)) != 0:
            raise ValueError('SOCKET_SCOPE_UNQUALIFIED')
        local_class = classify_address(bytes(buffer[:size.value]), size.value)
        return {'notConnected': True, 'peerErrno': 107, 'localAddressClass': local_class}
    finally:
        ctypes.memset(buffer, 0, 128)
