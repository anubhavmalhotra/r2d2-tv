"""Positive-first loader mutations in disposable writable source copies."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


CASES = (
    ('anonymous-links', 'and s.st_nlink == 0', 'and True'),
    ('descriptor-inheritance', 'self._need(not self.io.get_inheritable(fd))', 'pass'),
    ('seal-readback', 'self._need(self.locks.fcntl(fd, self.locks.F_GET_SEALS) == seals)', 'pass'),
    ('descriptor-alias', "self._need(tuple(getattr(s,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_size','st_nlink')) == expected)", 'pass'),
    ('post-load-identity', 'for fd, expected in (cert, private): self._need(self._metadata(fd) == expected)', 'pass'),
    ('client-certificate-required', 'context.verify_mode = self.tls.CERT_REQUIRED', 'context.verify_mode = 0'),
    ('one-close-attempt', 'pending, self.fds = self.fds, []', 'pending = self.fds'),
    ('mutable-buffer-clear', "raw[:] = b'\\0' * len(raw); raw.clear()", 'pass'),
)


def main():
    source = Path(__file__).resolve().parent
    original = (source / 'ram_credentials.py').read_text()
    command = [sys.executable, '-B', '-m', 'unittest',
               'voice.backend.physical_capture.test_ram_credentials', '-q']
    results = []
    for name, before, after in CASES:
        if original.count(before) != 1:
            raise RuntimeError('MUTATION_SOURCE')
        with tempfile.TemporaryDirectory(prefix='ram-credentials-mutation-') as directory:
            root = Path(directory)
            target = root / 'voice/backend/physical_capture'
            target.mkdir(parents=True)
            for filename in ('ram_credentials.py', 'test_ram_credentials.py'):
                shutil.copy2(source / filename, target / filename)
                (target / filename).chmod(0o600)
            positive = subprocess.run(command, cwd=root, capture_output=True, timeout=15)
            if positive.returncode:
                raise RuntimeError('POSITIVE_FAILED')
            (target / 'ram_credentials.py').write_text(original.replace(before, after))
            mutant = subprocess.run(command, cwd=root, capture_output=True, timeout=15)
            if not mutant.returncode:
                raise RuntimeError('MUTATION_SURVIVED')
            results.append(dict(case=name, positivePassed=True, mutantKilled=True))
    print(json.dumps(dict(schema=1, nativeExecuted=False, pairs=results), sort_keys=True))


if __name__ == '__main__':
    main()
