"""Kill switch levels 2 and 3. Flags are re-read on every check.

Fail closed: if the config directory or a flag cannot be read safely, the agent
counts as paused (level 2) and TV tools count as off (level 3).

Residual: any process running as the same Spark user can remove
these flags. They protect against accidents and the agent's own faults, not
against a same-UID adversary.
"""
import os
from pathlib import Path
import stat

PAUSED, TV_OFF = 'PAUSED', 'TV_OFF'
REASONS = frozenset(('OWNER', 'UNKNOWN_STREAK', 'PARSE_MISMATCH', 'RATE_TRIPS', 'AUDIT_WRITE',
                     'PAIRING_PROMPT', 'CERT_CHANGED', 'PERMISSION_DENIED'))


class KillSwitch:
    def __init__(self, config_dir):
        self.dir = Path(config_dir)
        self.latched = False  # set only when a trip could not be written to disk

    def _dir_ok(self):
        try:
            st = os.lstat(self.dir)
        except OSError:
            return False
        return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and
                st.st_mode & 0o077 == 0)

    def _flag(self, name):
        if not self._dir_ok():
            return True
        try:
            os.lstat(self.dir / name)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def paused(self):
        return self.latched or self._flag(PAUSED)

    def tv_off(self):
        return self.paused() or self._flag(TV_OFF)

    def _write(self, name, reason):
        if reason not in REASONS:
            raise ValueError('KILL_REASON')
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(reason + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except OSError:
            if name == PAUSED:
                self.latched = True  # in-process pause survives until restart
            return False

    def pause(self, reason='OWNER'):
        return self._write(PAUSED, reason)

    def set_tv_off(self, reason='OWNER'):
        return self._write(TV_OFF, reason)

    def _clear(self, name):
        try:
            os.unlink(self.dir / name)
        except FileNotFoundError:
            pass

    def resume(self):
        self._clear(PAUSED)

    def set_tv_on(self):
        self._clear(TV_OFF)
