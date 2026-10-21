import errno
import unittest
from unittest import mock
from controller import Refused
from process_isolation import ARCH,ALLOW,ERROR,KILL,DENIED,filter_instructions,apply_socket_denial,lock_current_and_future


def evaluate(nr,arch=ARCH,arg0=0):
    rows=filter_instructions();index=0;accumulator=0
    while index<len(rows):
        code,jt,jf,k=rows[index]
        if code==0x20:accumulator={0:nr,4:arch,16:arg0}[k]
        elif code==0x15:index+=jt if accumulator==k else jf
        elif code==0x45:index+=jt if accumulator&k else jf
        elif code==0x06:return k
        else:raise AssertionError('UNKNOWN_BPF')
        index+=1
    raise AssertionError('NO_RETURN')


class IsolationContractTests(unittest.TestCase):
    def test_all_denied_network_and_escape_syscalls_exact_errno(self):
        for nr,code in DENIED.items():self.assertEqual(evaluate(nr),ERROR|code)
        for nr in (63,64,29,98,222):self.assertEqual(evaluate(nr),ALLOW)
    def test_architecture_switch_kills_instead_of_reinterpreting_numbers(self):
        for arch in (0,0x40000028,0xc000003e):self.assertEqual(evaluate(63,arch),KILL)
    def test_only_shared_memory_thread_clone_retained(self):
        self.assertEqual(evaluate(220,arg0=0x10000),ALLOW)
        self.assertEqual(evaluate(220,arg0=17),ERROR|errno.EPERM)
    def test_no_activation_on_wrong_host_or_insufficient_lock_limit(self):
        with mock.patch('process_isolation.platform.system',return_value='Darwin'):
            with self.assertRaisesRegex(Refused,'ISOLATION_ARCH'):apply_socket_denial((0,1,2))
        with mock.patch('process_isolation.platform.system',return_value='Linux'),mock.patch('process_isolation.platform.machine',return_value='aarch64'),mock.patch('process_isolation.resource.getrlimit',return_value=(65536,65536)),mock.patch('process_isolation.resource.setrlimit')as setter:
            with self.assertRaisesRegex(Refused,'MEMORY_LOCK_LIMIT'):lock_current_and_future()
            setter.assert_not_called()


if __name__=='__main__':unittest.main()
