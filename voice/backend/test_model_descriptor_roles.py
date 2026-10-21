import fcntl
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import model_descriptor_roles as m

def fingerprint(fd):
    s=os.fstat(fd)
    return(fd,s.st_dev,s.st_ino,stat.S_IFMT(s.st_mode),s.st_rdev,fcntl.fcntl(fd,fcntl.F_GETFL),fcntl.fcntl(fd,fcntl.F_GETFD))

def snap(fps):return SimpleNamespace(_identities=frozenset(fps),_stdio_scope=(0,1,2,44),_null_device=3)

class Tests(unittest.TestCase):
    def case(self,mode=os.O_RDONLY,known=True):
        with tempfile.TemporaryDirectory()as d:
            p=Path(d)/'model.safetensors';p.write_bytes(b'public-model-fixture')
            resources={}
            if known:m.record(resources,p,p.lstat(),'asr',p.name)
            fd=os.open(p,mode)
            try:
                # A retained fingerprint need not be a live FD: inspect touches only additions.
                before=snap({(123,1,2,stat.S_IFCHR,3,0,1)})
                after=snap(before._identities|{fingerprint(fd)})
                return m.inspect(before,after,resources,readlink=lambda _:str(p))
            finally:os.close(fd)
    def test_only_verified_readonly_model_file_can_qualify(self):
        result=self.case();self.assertTrue(result['qualified'])
        self.assertEqual(result['added'][0]['modelRole'],'asr')
        self.assertEqual(result['added'][0]['pathClass'],'verified-model')
    def test_unknown_or_write_capable_regular_file_refuses(self):
        self.assertFalse(self.case(known=False)['qualified'])
        self.assertFalse(self.case(mode=os.O_RDWR)['qualified'])
    def test_replaced_bootstrap_descriptor_refuses(self):
        before=snap({(123,1,2,stat.S_IFCHR,3,0,1)})
        result=m.inspect(before,snap(set()),{})
        self.assertFalse(result['qualified']);self.assertFalse(result['bootstrapDescriptorsRetained'])
    def test_changed_model_identity_and_fd_flags_refuse(self):
        with tempfile.TemporaryDirectory()as d:
            p=Path(d)/'model.safetensors';p.write_bytes(b'public');resources={};m.record(resources,p,p.lstat(),'intent',p.name)
            fd=os.open(p,os.O_RDONLY)
            try:
                fp=fingerprint(fd);p.write_bytes(b'changed-public')
                self.assertFalse(m.inspect(snap(set()),snap({fp}),resources,readlink=lambda _:str(p))['qualified'])
                with self.assertRaisesRegex(ValueError,'MODEL_RESOURCE_RACE'):
                    m.inspect(snap(set()),snap({fp}),resources,flags=lambda _:(os.O_RDWR,fp[-1]),readlink=lambda _:str(p))
            finally:os.close(fd)
    def test_projection_cannot_inflate_identity_or_export_full_path(self):
        result=self.case();row=result['added'][0]
        self.assertNotIn('/tmp/',str(result));self.assertNotIn('/var/',str(result))
        row['qualified']=False
        with self.assertRaises(ValueError):m.validate(result)

if __name__=='__main__':unittest.main()
