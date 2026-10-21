import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import runtime_preload as r
import runtime_cleanup as c

class RuntimeTests(unittest.TestCase):
 def test_imports_before_thread_check_no_model_call(self):
  with tempfile.TemporaryDirectory()as d:
   p=Path(d)/'startup-public-runtime';p.mkdir(mode=0o700);row=r.new()
   with patch.dict(os.environ,{'TMPDIR':str(p)}),patch.object(r.Path,'iterdir',side_effect=[iter([1]),iter([1])]),patch.object(r.importlib,'import_module')as imports:
    self.assertTrue(r.prepare(row)['complete']);self.assertEqual([v.args[0]for v in imports.call_args_list],list(r.IMPORTS))
   self.assertFalse(row['modelInstancesLoaded'])
 def test_extra_threads_or_import_error_refuse_with_public_diagnostic(self):
  with tempfile.TemporaryDirectory()as d:
   p=Path(d)/'startup-public-runtime';p.mkdir(mode=0o700)
   for outcome,side in (([iter([1]),iter([1,2])],None),([iter([1])],ImportError('public fixed module absent'))):
    row=r.new()
    with patch.dict(os.environ,{'TMPDIR':str(p)}),patch.object(r.Path,'iterdir',side_effect=outcome),patch.object(r.importlib,'import_module',side_effect=side):
     with self.assertRaises((ValueError,ImportError)):r.prepare(row)
    self.assertFalse(row['complete']);self.assertIsNotNone(row['diagnostic'])
 def test_cleanup_owns_tree_and_never_reads_contents(self):
  with tempfile.TemporaryDirectory()as d:
   root=Path(d)/'runtime';root.mkdir(mode=0o700);identity=c.identity(root.lstat());nested=root/'tmp123';nested.mkdir();(nested/'public.py').write_bytes(b'fixed code')
   with patch.object(Path,'read_bytes',side_effect=AssertionError('no payload reads')):row=c.remove_public_tree(root,identity)
   self.assertTrue(row['complete']);self.assertEqual(row['entriesRemoved'],2);self.assertFalse(root.exists())
 def test_cleanup_refuses_symlink_foreign_identity_and_oversize_before_any_delete(self):
  with tempfile.TemporaryDirectory()as d:
   outside=Path(d)/'outside';outside.write_text('untouched')
   root=Path(d)/'runtime';root.mkdir(mode=0o700);identity=c.identity(root.lstat());a=root/'a';a.write_text('public');link=root/'link';link.symlink_to(outside)
   with self.assertRaises(ValueError):c.remove_public_tree(root,identity)
   self.assertTrue(a.exists());self.assertEqual(outside.read_text(),'untouched');link.unlink()
   with self.assertRaises(ValueError):c.remove_public_tree(root,(0,0,0))
   big=root/'big';big.write_bytes(b'x'*1048577)
   with self.assertRaises(ValueError):c.remove_public_tree(root,identity)
   self.assertTrue(a.exists())
 def test_false_preload_ready_rejected(self):
  for change in ({'complete':True},{'modelInstancesLoaded':True},{'privateInputAccepted':True},{'diagnostic':{'locals':'secret'}}):
   with self.assertRaises(ValueError):r.validate({**r.new(),**change})

if __name__=='__main__':unittest.main()
