"""Validating discard sink; persistent diagnostics require explicit opt-in.

The default keeps no history and does not touch files. persistent=True enables
the private hash-chained JSONL format. Its action and power metadata
is usage history even though callers redact free text to character counts.
Both modes reject invalid row structure; the identifier guard is not a proof
that an arbitrary caller's identifier-shaped string contains no private text.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time

ZERO = 'sha256:' + '0' * 64
_NAME = re.compile(r'audit-(\d{8})\.jsonl')
_SAFE_STR = re.compile(r'[A-Za-z0-9_.:+-]{0,80}')
_SAFE_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,31}')
DAY_S = 86400


class AuditUnavailable(Exception):
    pass


def _check_value(value, depth=0):
    if depth > 4:
        raise AuditUnavailable('AUDIT_ROW')
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        if not _SAFE_STR.fullmatch(value):
            raise AuditUnavailable('AUDIT_ROW')
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not _SAFE_KEY.fullmatch(key):
                raise AuditUnavailable('AUDIT_ROW')
            _check_value(item, depth + 1)
        return
    raise AuditUnavailable('AUDIT_ROW')


def audit_args(tool, args):
    """Enumerated args in clear; free text as {'kind':'text','chars':n} only."""
    result = {}
    for name, spec in tool.args:
        value = args[name]
        result[name] = {'kind': 'text', 'chars': len(value)} if spec.free_text else value
    return result


def _line_hash(line):
    return 'sha256:' + hashlib.sha256(line).hexdigest()


class AuditLog:
    def __init__(self, directory, *, wall=time.time, retention_days=30, persistent=False):
        if type(persistent) is not bool:
            raise ValueError('AUDIT_CONFIG')
        self.persistent = persistent
        self.dir = Path(directory)
        self.wall = wall
        self.retention_days = retention_days
        self.lock = threading.Lock()
        self.last = None
        self.seq = None

    # -- checks ---------------------------------------------------------------
    def _prepare(self):
        try:
            self.dir.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise AuditUnavailable('AUDIT_DIR') from None
        st = os.lstat(self.dir)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or
                not os.access(self.dir, os.W_OK | os.X_OK)):
            raise AuditUnavailable('AUDIT_DIR')

    def writable(self):
        if not self.persistent:
            return True
        try:
            with self.lock:
                self._prepare()
                self._load_tail()
            return True
        except Exception:
            return False

    def _files(self):
        return sorted(p for p in self.dir.iterdir() if _NAME.fullmatch(p.name))

    def _load_tail(self):
        if self.last is not None:
            return
        files = self._files()
        if not files:
            self.last, self.seq = ZERO, 0
            return
        raw = self._read_private(files[-1])
        lines = raw.split(b'\n')
        if lines[-1] != b'' or len(lines) < 2:
            raise AuditUnavailable('AUDIT_TAIL')
        row = json.loads(lines[-2])
        self.last, self.seq = _line_hash(lines[-2]), row['seq']

    @staticmethod
    def _read_private(path):
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise AuditUnavailable('AUDIT_FILE')
        with open(path, 'rb') as handle:
            return handle.read()

    # -- writing --------------------------------------------------------------
    def append(self, row):
        try:
            _check_value(row)
            if any(k in row for k in ('schema', 'ts', 'seq', 'prev')):
                raise AuditUnavailable('AUDIT_ROW')
            if not self.persistent:
                return  # no file access, serialization, counters or payload retention
            with self.lock:
                self._prepare()
                self._load_tail()
                now = self.wall()
                full = {'schema': 1, 'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
                        'seq': self.seq + 1, **row, 'prev': self.last}
                line = json.dumps(full, separators=(',', ':'), ensure_ascii=True,
                                  allow_nan=False).encode('ascii')
                path = self.dir / ('audit-%s.jsonl' % time.strftime('%Y%m%d', time.gmtime(now)))
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                        raise AuditUnavailable('AUDIT_FILE')
                    os.write(fd, line + b'\n')
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.last, self.seq = _line_hash(line), full['seq']
                self._prune(now)
        except AuditUnavailable:
            raise
        except Exception:
            raise AuditUnavailable('AUDIT_WRITE') from None

    def _prune(self, now):
        cutoff = time.strftime('%Y%m%d', time.gmtime(now - self.retention_days * DAY_S))
        for path in self._files():
            if _NAME.fullmatch(path.name).group(1) < cutoff:
                os.unlink(path)

    # -- verification ---------------------------------------------------------
    @staticmethod
    def verify(directory):
        """Re-hash the whole chain. Returns the row count; raises ValueError on any
        break. The first remaining row's `prev` is the anchor (older files may have
        been pruned), so truncation is detectable only if daily heads are anchored
        on another host."""
        directory = Path(directory)
        previous, seq, count = None, None, 0
        for path in sorted(p for p in directory.iterdir() if _NAME.fullmatch(p.name)):
            raw = AuditLog._read_private(path)
            if not raw.endswith(b'\n'):
                raise ValueError('AUDIT_CHAIN')
            for line in raw[:-1].split(b'\n'):
                row = json.loads(line)
                if previous is not None and (row['prev'] != previous or row['seq'] != seq + 1):
                    raise ValueError('AUDIT_CHAIN')
                previous, seq = _line_hash(line), row['seq']
                count += 1
        return count
