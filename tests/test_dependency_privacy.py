import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('dependency_privacy',ROOT/'tools/privacy_check.py')
privacy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class ReviewedPublicVersionTests(unittest.TestCase):
    def setUp(self):
        self.name='voice/backend/dependencies.json'
        self.raw=(ROOT/self.name).read_bytes()

    def test_exact_public_manifest_and_two_version_literals(self):
        self.assertTrue(privacy.reviewed_version_literals(self.name,self.raw))
        self.assertEqual(privacy.issues(self.name,self.raw),[])

    def test_other_path_changed_bytes_or_added_address_refused(self):
        for name,raw in (("other/dependencies.json",self.raw),
                         (self.name,self.raw+b' '),
                         (self.name,self.raw+b'\n'+b'.'.join((b'192',b'168',b'12',b'17')))):
            self.assertFalse(privacy.reviewed_version_literals(name,raw))
            self.assertIn('private-ipv4',privacy.issues(name,raw))

    def test_exception_never_suppresses_other_secret_checks(self):
        raw=self.raw+b'\n'+b'ghp_'+b'a'*40
        findings=privacy.issues(self.name,raw)
        self.assertIn('github-token',findings)
        self.assertIn('private-ipv4',findings)


if __name__=='__main__':unittest.main()
