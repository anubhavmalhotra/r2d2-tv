"""Mocked syscall and exact public scratch controls; no native policy installed."""
import ctypes
import errno
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import filesystem_seal as fs


class Call:
    def __init__(self, function): self.function = function
    def __call__(self, *args): return self.function(*args)


class Library:
    def __init__(self, fail=None, abi=8):
        self.calls = []; self.fail = fail; self.abi = abi; self.rule_count=0
        self.syscall = Call(self.call); self.prctl = Call(lambda *a: 0)
    def call(self, number, *args):
        number = number.value
        self.calls.append(number)
        if number == self.fail: return -1
        if number == 444:
            if args[-1].value == 1: return self.abi
            a = ctypes.cast(args[0], ctypes.POINTER(fs.Ruleset)).contents
            assert (a.handled_access_fs, a.handled_access_net, a.scoped) == (fs.FS_MUTATIONS, 3, 3)
            assert args[1].value == 24
            return 50
        if number == 445:
            a = ctypes.cast(args[2], ctypes.POINTER(fs.PathBeneath)).contents
            assert args[0].value == 50 and args[1].value == 1
            self.rule_count+=1
            assert a.allowed_access == (fs.WRITE_FILE|fs.IOCTL_DEV if self.rule_count<=6 else fs.WRITE_FILE|(1<<14))
            assert a.parent_fd == 51
            assert ctypes.sizeof(a) == 12
            return 0
        if number == 446: return 0
        raise AssertionError(number)


class FilesystemTests(unittest.TestCase):
    def invoke(self, library, threads=1, wrong_device=False, wrong_task=False):
        current = [None]; opened=[]; closed=[]
        def opening(path, flags): current[0]=path;opened.append(path);return 51
        def fstat(fd):
            assert fd == 51
            if current[0]=='/proc/self/task':return SimpleNamespace(st_mode=stat.S_IFDIR,st_uid=1000,st_dev=123,st_ino=456)
            match = next(v for v in fs.DEVICES if v[0] == current[0])
            return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=(match[1],match[2]+int(wrong_device)))
        with patch.object(fs.platform,'system',return_value='Linux'), patch.object(fs.platform,'machine',return_value='aarch64'), patch.object(fs.os,'getuid',return_value=1000), patch.object(fs.os,'geteuid',return_value=1000), patch.object(fs.Path,'iterdir',return_value=iter(range(threads))), patch.object(fs.ctypes,'CDLL',return_value=library), patch.object(fs.os,'stat',return_value=SimpleNamespace(st_dev=123,st_ino=456+int(wrong_task))), patch.object(fs.os,'open',side_effect=opening), patch.object(fs.os,'fstat',side_effect=fstat), patch.object(fs.os,'major',side_effect=lambda v:v[0]), patch.object(fs.os,'minor',side_effect=lambda v:v[1]), patch.object(fs.os,'close',side_effect=closed.append):
            try: result=fs.install()
            finally: self.opened=opened;self.closed=closed
        return result

    def test_exact_device_only_rules_closed_before_return(self):
        lib=Library();row=self.invoke(lib)
        self.assertEqual(lib.calls,[444,444]+[445]*7+[446])
        self.assertEqual(self.opened,[p for p,_,_ in fs.DEVICES]+['/proc/self/task'])
        self.assertEqual(self.closed,[51]*7+[50]);self.assertEqual(fs.validate(row),row)
        self.assertFalse(row['negativeProbesPassed'])

    def test_multithread_or_old_abi_never_restrict(self):
        for args in ({'threads':2},{'library':Library(abi=7)}):
            lib=args.pop('library',Library())
            with self.assertRaises(ValueError):self.invoke(lib,**args)
            self.assertNotIn(446,lib.calls)

    def test_device_drift_or_add_failure_closes_owned_descriptors(self):
        for lib,drift in ((Library(),True),(Library(fail=445),False)):
            with self.assertRaises(ValueError):self.invoke(lib,wrong_device=drift)
            self.assertEqual(self.closed,[51,50]);self.assertNotIn(446,lib.calls)

    def test_foreign_task_inode_refuses_before_restriction(self):
        lib=Library()
        with self.assertRaisesRegex(ValueError,'FILESYSTEM_TASK_SCOPE'):self.invoke(lib,wrong_task=True)
        self.assertNotIn(446,lib.calls);self.assertEqual(self.closed[-2:],[51,50])

    def test_restrict_failure_closes_rulefd(self):
        with self.assertRaisesRegex(ValueError,'FILESYSTEM_RESTRICT'):self.invoke(Library(fail=446))
        self.assertEqual(self.closed[-1],50)

    def test_negative_probe_requires_landlock_not_dac_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d,0o700);p=Path(d)/'attempt.json';p.write_text('public');p.chmod(0o600)
            row=self.invoke(Library())
            denied=PermissionError(errno.EACCES,'fixed')
            with patch.object(fs.os,'open',side_effect=denied),patch.object(fs.os,'mkdir',side_effect=denied):
                self.assertTrue(fs.check_denials(p,row)['negativeProbesPassed'])
            self.assertEqual(p.read_text(),'public')
            p.chmod(0o400)
            with self.assertRaisesRegex(ValueError,'FILESYSTEM_FIXTURE'):fs.check_denials(p,row)

    def test_unsealed_public_probe_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d,0o700);p=Path(d)/'attempt.json';p.write_text('public');p.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'FILESYSTEM_DENIAL'):fs.check_denials(p,self.invoke(Library()))
            self.assertEqual(p.read_text(),'public');self.assertEqual([v.name for v in Path(d).iterdir()],['attempt.json'])

    def test_projection_cannot_claim_regular_writes_or_export_path(self):
        row=self.invoke(Library())
        for change in ({'persistentFileWritesAllowed':True},{'devicePath':'/private'}, {'writeDeviceRules':True}):
            with self.assertRaises(ValueError):fs.validate({**row,**change})


if __name__=='__main__':unittest.main()
