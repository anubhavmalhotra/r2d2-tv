"""Validating discard sink by default; disk diagnostics need explicit opt-in.

Session, enrollment and warm-policy rows contain usage history even when they
contain only enums and counters. The service therefore validates and discards
them without reading a clock, touching files or retaining row metadata.

Format: `<auditDir>/audit-YYYYMMDD.jsonl` (UTC day), file 0600, directory
0700, O_APPEND, fsync per row; every row carries `seq` and `prev` = "sha256:" + SHA-256 of
the previous row's bytes (without the newline); the first row of a day chains to the last row
of the newest earlier file. Files older than RETAIN_DAYS are deleted. Never written: audio,
transcripts, model output, URLs, certificates, keys, request bodies (`scrub()` refuses any
string that is not a short enum-like token, and any float).
"""
import datetime as dt
import hashlib
import json
import os
import re
import stat
import threading
import time

TOKEN = re.compile(r'[A-Za-z0-9_.:-]{0,64}')
MAX_DEPTH = 6
RETAIN_DAYS = 30
NAME = re.compile(r'audit-(\d{8})\.jsonl')
RESERVED = ('ts', 'seq', 'prev')


def scrub(value, depth=0):
    """Return value if it holds only None/bool/int/short tokens/lists/dicts of them."""
    if depth > MAX_DEPTH:
        raise ValueError('RESULT_DEPTH')
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -2**53 < value < 2**53:
            raise ValueError('RESULT_INT')
        return value
    if type(value) is str:
        if not TOKEN.fullmatch(value):
            raise ValueError('RESULT_TEXT')
        return value
    if type(value) is list:
        if len(value) > 64:
            raise ValueError('RESULT_SIZE')
        return [scrub(v, depth + 1) for v in value]
    if type(value) is dict:
        if len(value) > 64:
            raise ValueError('RESULT_SIZE')
        return {scrub(k, depth + 1): scrub(v, depth + 1) for k, v in value.items()}
    raise ValueError('RESULT_TYPE')


def _private_dir(path):
    s = os.lstat(path)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
        raise ValueError('AUDIT_DIR')


class ChainedAudit:
    """Validate/discard rows, or explicitly opt in to hash-chained diagnostics.

    The persistent implementation raises on any write problem so the caller
    can pause. Token validation is structural; it cannot establish that an
    arbitrary token-shaped string contains no private information.
    """

    def __init__(self, directory, *, wall=time.time, retain_days=RETAIN_DAYS, persistent=False):
        if type(persistent) is not bool:
            raise ValueError('AUDIT_CONFIG')
        self.persistent = persistent
        self.directory, self.wall, self.retain_days = str(directory), wall, retain_days
        self.lock = threading.Lock()
        self.prev = self.seq = None
        self.torn = False   # the newest file ends in a partial row (power cut mid-append)

    def _day(self):
        return dt.datetime.fromtimestamp(int(self.wall()), dt.timezone.utc)

    def _files(self):
        return sorted(n for n in os.listdir(self.directory) if NAME.fullmatch(n))

    def _resume(self):
        """Chain to the last COMPLETE row of the newest existing file (bounded tail read).
        A torn final row (no newline, or not JSON) is left in place as evidence; the next
        append first terminates it with a newline and chains from the last good row."""
        prev, seq = 'sha256:' + '0' * 64, 0
        files = self._files()
        if not files:
            self.prev, self.seq = prev, seq
            return
        path = os.path.join(self.directory, files[-1])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
                raise ValueError('AUDIT_FILE')
            size = s.st_size
            os.lseek(fd, max(0, size - 65536), os.SEEK_SET)
            tail = os.read(fd, 65536)
        finally:
            os.close(fd)
        self.torn = bool(tail) and not tail.endswith(b'\n')
        for line in reversed([line for line in tail.split(b'\n') if line]):
            try:
                row = json.loads(line)
                if type(row) is dict and type(row.get('seq')) is int:
                    prev, seq = 'sha256:' + hashlib.sha256(line).hexdigest(), row['seq']
                    break
            except ValueError:
                continue
        self.prev, self.seq = prev, seq

    def _prune(self, today):
        cutoff = (today - dt.timedelta(days=self.retain_days)).strftime('%Y%m%d')
        for name in self._files():
            if NAME.fullmatch(name).group(1) < cutoff:
                try:
                    os.unlink(os.path.join(self.directory, name))
                except OSError:
                    pass

    def append(self, row):
        row = scrub(row)
        if type(row) is not dict or any(k in row for k in RESERVED):
            raise ValueError('AUDIT_ROW')
        if not self.persistent:
            return None
        with self.lock:
            _private_dir(self.directory)
            if self.prev is None:
                self._resume()
            day = self._day()
            out = {'schema': row.get('schema', 1), 'ts': day.strftime('%Y-%m-%dT%H:%M:%SZ'), 'seq': self.seq + 1}
            out.update((k, v) for k, v in row.items() if k != 'schema')
            out['prev'] = self.prev
            line = json.dumps(out, separators=(',', ':')).encode('ascii')
            path = os.path.join(self.directory, 'audit-%s.jsonl' % day.strftime('%Y%m%d'))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                s = os.fstat(fd)
                if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
                    raise ValueError('AUDIT_FILE')
                if self.torn and s.st_size > 0:
                    os.write(fd, b'\n')       # close the torn row; it stays as evidence
                self.torn = False
                os.write(fd, line + b'\n')
                os.fsync(fd)
            finally:
                os.close(fd)
            self.prev, self.seq = 'sha256:' + hashlib.sha256(line).hexdigest(), out['seq']
            self._prune(day)
            return out['seq']


def verify_chain(directory):
    """Offline check: seq strictly +1 and prev == hash of the previous COMPLETE row across all
    files. A torn row (not JSON) is skipped; the chain must continue from the row before it."""
    previous = seq = None
    for name in sorted(n for n in os.listdir(directory) if NAME.fullmatch(n)):
        with open(os.path.join(directory, name), 'rb') as f:
            for line in f.read().split(b'\n'):
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if previous is not None and (row['prev'] != previous or row['seq'] != seq + 1):
                    return False
                previous, seq = 'sha256:' + hashlib.sha256(line).hexdigest(), row['seq']
    return True
