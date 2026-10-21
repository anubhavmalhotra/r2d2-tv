"""Actual runtime audit sink keeps no usage history without explicit opt-in."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from . import result


class NoHistory(unittest.TestCase):
    def test_default_touches_no_files_or_clock_and_retains_no_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'absent'
            log = result.ChainedAudit(directory, wall=lambda: self.fail('clock read'))
            before = dict(vars(log))
            with mock.patch.object(result.os, 'lstat', side_effect=AssertionError('file read')), \
                 mock.patch.object(result.os, 'open', side_effect=AssertionError('file write')), \
                 mock.patch.object(result.os, 'listdir', side_effect=AssertionError('directory read')):
                for _ in range(3):
                    self.assertIsNone(log.append({'event': 'voice', 'marker': 'PRIVATE_SENTINEL',
                                                  'timings': {'elapsedMs': 12}}))
            self.assertFalse(directory.exists())
            self.assertEqual(vars(log), before)

    def test_existing_owner_files_are_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / 'audit-20000101.jsonl'
            old.write_bytes(b'existing owner file\n')
            log = result.ChainedAudit(folder)
            log.append({'event': 'voice'})
            self.assertEqual(list(Path(folder).iterdir()), [old])
            self.assertEqual(old.read_bytes(), b'existing owner file\n')

    def test_default_still_refuses_invalid_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            for row in ({'seq': 1}, {'ts': 1}, {'url': 'https://example.test'},
                        {'counter': 2**53}, {'value': 1.25}, ['voice']):
                with self.subTest(row=row), self.assertRaises(ValueError):
                    result.ChainedAudit(folder).append(row)
            self.assertEqual(os.listdir(folder), [])

    def test_opt_in_requires_literal_boolean(self):
        for value in (1, 'yes', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^AUDIT_CONFIG$'):
                result.ChainedAudit('/unused', persistent=value)

    def test_explicit_persistent_diagnostics_remain_available(self):
        with tempfile.TemporaryDirectory() as folder:
            log = result.ChainedAudit(folder, persistent=True, wall=lambda: 1700000000)
            self.assertEqual(log.append({'event': 'one'}), 1)
            self.assertEqual(log.append({'event': 'two'}), 2)
            self.assertEqual(len(os.listdir(folder)), 1)
            self.assertTrue(result.verify_chain(folder))


if __name__ == '__main__':
    unittest.main()
