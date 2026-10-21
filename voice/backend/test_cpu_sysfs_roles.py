import copy
import os
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import cpu_sysfs_roles as c
import model_descriptor_roles as m

TARGET='/sys/devices/system/cpu/cpu0/cache/index2/level'

def info():return SimpleNamespace(st_dev=11,st_ino=12,st_mode=stat.S_IFREG|0o444,st_uid=0,st_rdev=0,st_size=4096,st_mtime_ns=1,st_ctime_ns=2)
def snap(ids):return SimpleNamespace(_identities=frozenset(ids),_stdio_scope=(0,1,2,44),_null_device=3)

class Tests(unittest.TestCase):
    def check(self,target=TARGET,metadata=None,flags=os.O_RDONLY,**kwargs):
        row=metadata or info()
        return c.qualify(5,target,row,flags,magic=kwargs.get('magic',lambda _:c.SYSFS_MAGIC),path_info=kwargs.get('path_info',lambda _:info()),canonical=kwargs.get('canonical',str))
    def test_known_readonly_cpu_leaf_requires_sysfs_and_matching_inode(self):
        self.assertTrue(self.check())
        self.assertFalse(self.check(magic=lambda _:0xEF53))
        changed=info();changed.st_ino+=1
        self.assertFalse(self.check(path_info=lambda _:changed))
        self.assertFalse(self.check(canonical=lambda p:'/different'))
    def test_nonroot_writable_symlink_and_unknown_sysfs_do_not_qualify(self):
        changed=info();changed.st_uid=1000
        self.assertFalse(self.check(metadata=changed,path_info=lambda _:changed));self.assertFalse(self.check(flags=os.O_RDWR))
        changed=info();changed.st_mode=stat.S_IFLNK|0o777
        self.assertFalse(self.check(metadata=changed))
        for path in('/sys/kernel/uevent_seqnum','/sys/devices/system/cpu/cpu0/online',TARGET+'/x',TARGET.replace('cpu0','cpu4096'),TARGET.replace('index2','index8')):
            self.assertFalse(self.check(target=path))
    def test_actual_resource_projection_accepts_only_proven_cpu_metadata(self):
        row=info();fp=(5,row.st_dev,row.st_ino,stat.S_IFREG,0,os.O_RDONLY,1)
        # Provide only the OS metadata dependencies; qualification logic remains real.
        real=c.qualify
        with patch.object(m.cpu_sysfs_roles,'qualify',side_effect=lambda fd,target,info,flags:real(fd,target,info,flags,magic=lambda _:c.SYSFS_MAGIC,path_info=lambda _:row,canonical=str)):
            result=m.inspect(snap(set()),snap({fp}),{},describe=lambda _:row,flags=lambda _:(os.O_RDONLY,1),readlink=lambda _:TARGET)
        self.assertTrue(result['qualified']);self.assertTrue(result['added'][0]['cpuSysfsMetadataMatched'])
        self.assertEqual(result['added'][0]['technicalPath'],TARGET)
        mutated=copy.deepcopy(result);mutated['added'][0]['cpuSysfsMetadataMatched']=False
        with self.assertRaises(ValueError):m.validate(mutated)

if __name__=='__main__':unittest.main()
