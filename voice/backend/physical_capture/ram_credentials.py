"""TLS 1.3 server credentials loaded through sealed anonymous Linux memfds.

The SSL API reopens held regular descriptors through /proc/self/fd. Missing
kernel or TLS capabilities refuse; this module has no named-file fallback.
"""
import fcntl
import os
import ssl
import stat


class Credentials:
    def __init__(self, *, io=os, locks=fcntl, tls=ssl):
        self.io, self.locks, self.tls = io, locks, tls
        self.fds = []
        self.row = dict(schema=1, anonymousOnly=True, created=0, sealed=0,
                        closed=0, mutableBuffersCleared=True, closeConfirmed=True)

    def _need(self, ok):
        if not ok: raise ValueError('RAM_CREDENTIALS')

    def _metadata(self, fd):
        s = self.io.fstat(fd)
        self._need(stat.S_ISREG(s.st_mode) and s.st_nlink == 0
                   and stat.S_IMODE(s.st_mode) == 0o600 and s.st_uid == self.io.getuid())
        return tuple(getattr(s,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_size','st_nlink'))

    def _create(self, name, text):
        self._need(type(text) is str and text.isascii() and 1 <= len(text) <= 8192)
        raw = bytearray(text, 'ascii')
        self.row['mutableBuffersCleared'] = False
        try:
            flags = self.io.MFD_CLOEXEC | self.io.MFD_ALLOW_SEALING
            fd = self.io.memfd_create(name, flags)
            self.fds.append(fd); self.row['created'] += 1
            self.io.fchmod(fd, 0o600)
            self._need(not self.io.get_inheritable(fd))
            self._need(self._metadata(fd)[5] == 0)
            offset = 0
            for _ in range(64):
                if offset == len(raw): break
                view = memoryview(raw)[offset:]
                try: n = self.io.write(fd, view)
                finally: view.release()
                self._need(type(n) is int and 0 < n <= len(raw) - offset)
                offset += n
            self._need(offset == len(raw))
            seals = (self.locks.F_SEAL_SEAL | self.locks.F_SEAL_SHRINK |
                     self.locks.F_SEAL_GROW | self.locks.F_SEAL_WRITE)
            self.locks.fcntl(fd, self.locks.F_ADD_SEALS, seals)
            self._need(self.locks.fcntl(fd, self.locks.F_GET_SEALS) == seals)
            self.row['sealed'] += 1
            self.io.lseek(fd, 0, self.io.SEEK_SET)
            identity = self._metadata(fd)
            self._need(identity[5] == len(raw))
            return fd, identity
        finally:
            raw[:] = b'\0' * len(raw); raw.clear()
            self.row['mutableBuffersCleared'] = True

    def context(self, certificate, key, ca):
        self._need(not self.fds and self.row['created'] == 0)
        try:
            self._need(self.io.uname().sysname == 'Linux'
                       and callable(getattr(self.io, 'memfd_create', None)))
            cert = self._create('r2d2-voice-cert', certificate)
            private = self._create('r2d2-voice-key', key)
            context = self.tls.SSLContext(self.tls.PROTOCOL_TLS_SERVER)
            context.minimum_version = context.maximum_version = self.tls.TLSVersion.TLSv1_3
            context.verify_mode = self.tls.CERT_REQUIRED
            for fd, expected in (cert, private):
                path = '/proc/self/fd/' + str(fd)
                self._need(self._metadata(fd) == expected)
                s = self.io.stat(path)
                self._need(tuple(getattr(s,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_size','st_nlink')) == expected)
            context.load_cert_chain('/proc/self/fd/'+str(cert[0]), '/proc/self/fd/'+str(private[0]))
            for fd, expected in (cert, private): self._need(self._metadata(fd) == expected)
            self._need(type(ca) is str and ca.isascii() and 1 <= len(ca) <= 8192)
            context.load_verify_locations(cadata=ca)
            context.options |= self.tls.OP_NO_COMPRESSION
            context.num_tickets = 0
            self._need(self.close())
            return context
        except BaseException:
            self.close()
            raise ValueError('RAM_CREDENTIALS') from None

    def close(self):
        pending, self.fds = self.fds, []
        # Numeric descriptors are relinquished before the one close attempt.
        # A close error remains sticky; retrying could close a reused number.
        for fd in pending:
            try: self.io.close(fd); self.row['closed'] += 1
            except OSError: self.row['closeConfirmed'] = False
        return self.row['closeConfirmed'] and self.row['closed'] == self.row['created']

    def summary(self): return dict(self.row)
