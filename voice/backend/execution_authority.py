"""Owned-child permit for fixed public tensors only; never private model authority.

The reviewed parent supplies one public attempt identifier to its own child.
No inherited socket, credential, model, microphone or input payload is accepted.
"""
import dataclasses
import fcntl
import os
import re
import stat

_AUTHORITY = object()
_issued = False


class AuthorityRefused(ValueError):
    pass


@dataclasses.dataclass(frozen=True, repr=False)
class ExecutionPermit:
    attempt: str
    response_fd: int
    parent_pid: int
    expected_policy_sha256: str
    response_identity: tuple
    _authority: object = dataclasses.field(repr=False)


def _identity(fd):
    value = os.fstat(fd)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if not stat.S_ISFIFO(value.st_mode) or flags & os.O_ACCMODE != os.O_WRONLY:
        raise AuthorityRefused('RESPONSE_PIPE_UNQUALIFIED')
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode), flags & os.O_ACCMODE


def issue_native_permit(attempt, response_fd, expected_policy_sha256):
    global _issued
    if (_issued or type(attempt) is not str or re.fullmatch('[a-f0-9]{32}', attempt) is None
            or type(response_fd) is not int or response_fd < 3
            or type(expected_policy_sha256) is not str
            or re.fullmatch('[a-f0-9]{64}', expected_policy_sha256) is None
            or os.getppid() <= 1):
        raise AuthorityRefused('PUBLIC_CHILD_AUTHORITY_UNQUALIFIED')
    permit = ExecutionPermit(attempt, response_fd, os.getppid(),
                             expected_policy_sha256, _identity(response_fd), _AUTHORITY)
    _issued = True
    return permit


def require_native_permit(value):
    if (type(value) is not ExecutionPermit or value._authority is not _AUTHORITY
            or value.parent_pid != os.getppid()
            or value.response_identity != _identity(value.response_fd)):
        raise AuthorityRefused('PUBLIC_CHILD_AUTHORITY_UNQUALIFIED')
