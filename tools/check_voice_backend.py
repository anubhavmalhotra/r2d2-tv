#!/usr/bin/env python3
"""Run flat backend tests and package-based capture tests in their own namespaces.

The backend serves standalone modules, while physical_capture uses parent-relative
imports. Discovering both with voice/backend as the package root misimports the
capture tests and skips their coverage with five loader errors.
"""
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'voice' / 'backend'


def main():
    sys.path[:0] = [str(BACKEND), str(ROOT)]
    flat = [path.stem for path in sorted(BACKEND.glob('test_*.py'))]
    packages = [
        '.'.join(path.relative_to(ROOT).with_suffix('').parts)
        for path in sorted(BACKEND.rglob('test_*.py'))
        if path.parent != BACKEND
    ]
    if not flat or not packages:
        raise RuntimeError('Both backend test families are required')
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(flat + packages)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if not loader.errors and result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
