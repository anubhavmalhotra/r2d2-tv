"""No-history runtime default, distinct from explicit disk-audit fixtures."""
import unittest

from agent.audit import AuditLog, AuditUnavailable
from agent.errors import Outcome
from tests.support import Case, call


class AuditDefault(Case):
    def test_default_creates_no_files_and_retains_no_record(self):
        directory = self.tmp / 'absent'
        log = AuditLog(directory, wall=self.clock.wall)
        before = dict(vars(log))
        self.assertTrue(log.writable())
        row = {'event': 'test', 'marker': 'PRIVATE_IDENTIFIER_SENTINEL', 'length': 23}
        log.append(row)
        self.assertFalse(directory.exists())
        self.assertEqual(vars(log), before)
        self.assertNotIn(row, vars(log).values())

    def test_default_leaves_existing_files_untouched(self):
        directory = self.tmp / 'existing'
        directory.mkdir(mode=0o700)
        old = directory / 'audit-20000101.jsonl'
        old.write_bytes(b'existing owner data\n')
        log = AuditLog(directory, wall=self.clock.wall)
        self.assertTrue(log.writable())
        log.append({'event': 'test'})
        self.assertEqual(list(directory.iterdir()), [old])
        self.assertEqual(old.read_bytes(), b'existing owner data\n')

    def test_default_still_rejects_invalid_rows(self):
        log = AuditLog(self.tmp / 'absent')
        for row in ({'seq': 1}, {'query': 'free text here'}, {'url': 'https://example.com'},
                    {'items': []}, {'bad key': True}):
            with self.subTest(row=row), self.assertRaises(AuditUnavailable):
                log.append(row)
        self.assertFalse((self.tmp / 'absent').exists())

    def test_persistence_requires_literal_boolean_opt_in(self):
        for value in (1, 'yes', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^AUDIT_CONFIG$'):
                AuditLog(self.tmp / 'absent', persistent=value)

    def test_actual_agent_default_sink_records_no_usage_history(self):
        e = self.env(table={'close': call('browser.close')}, surface=lambda: 'browser_other')
        e.channel.open_session()
        e.audit = AuditLog(e.audit_dir, wall=self.clock.wall)
        e.agent.audit = e.audit
        self.assertEqual(self.say(e, 'close'), Outcome('verified', 'BROWSER_SESSION_ENDED'))
        self.assertFalse(e.audit_dir.exists())
        self.assertIsNone(e.audit.last)
        self.assertIsNone(e.audit.seq)


if __name__ == '__main__':
    unittest.main()
