"""Exercise the actual composition without device operations or a model."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import role_operations
import model_worker
from startup_diagnostic import load_diagnostic

class Tests(unittest.TestCase):
    def test_role_executor_accepts_guard_factory_and_reaches_owned_configuration(self):
        calls=[]
        ops=SimpleNamespace(owner_alive=lambda:True,
             configure_owned_process=lambda:calls.append('configured')or False,
             destroy_public_tensor_references=lambda:None)
        row=role_operations.run(ops,'a'*32)
        self.assertEqual(calls,['configured']);self.assertFalse(row['complete'])
        self.assertIsNotNone(row['error'])
    def test_startup_failure_is_bounded_to_pre_input_stages(self):
        row={'schema':2,'state':'refused','stage':'bootstrap','diagnostic':load_diagnostic(TypeError('public configuration'))}
        self.assertEqual(model_worker.validate_startup_failure(row),row)
        for delta in({'stage':'asr'},{'raw_audio':b'private'},{'schema':True}):
            with self.assertRaises(ValueError):model_worker.validate_startup_failure({**row,**delta})

if __name__=='__main__':unittest.main()
