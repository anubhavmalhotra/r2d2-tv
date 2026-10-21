#!/usr/bin/env python3
"""Offline controlled-copy tests; never modifies the supplied public archives."""
import argparse, hashlib, importlib.util, json, pathlib, shutil, tarfile, tempfile, unittest, zipfile
from unittest.mock import patch

a=argparse.ArgumentParser();a.add_argument('--builder-dir',type=pathlib.Path,required=True);a.add_argument('--upstream-zip',type=pathlib.Path,required=True);a.add_argument('--acorn-tar',type=pathlib.Path,required=True);a.add_argument('--report',type=pathlib.Path,required=True);args=a.parse_args()
spec=importlib.util.spec_from_file_location('snapshot_builder',args.builder_dir/'build.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
originals={p:hashlib.sha256(p.read_bytes()).hexdigest()for p in(args.upstream_zip,args.acorn_tar)}
class SnapshotTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='ubol-snapshot-');self.root=pathlib.Path(self.tmp.name);self.z=self.root/'upstream.zip';self.p=self.root/'acorn.tgz';self.out=self.root/'output';shutil.copyfile(args.upstream_zip,self.z);shutil.copyfile(args.acorn_tar,self.p)
 def tearDown(self):self.tmp.cleanup()
 def test_verified_snapshots_survive_both_source_path_swaps(self):
  extract=m.extract_zip;opens={self.z:0,self.p:0};old_open=pathlib.Path.open;swapped=[]
  def counted(path,*a,**kw):
   mode=a[0]if a else kw.get('mode','r')
   if path in opens and 'r'in mode:opens[path]+=1
   return old_open(path,*a,**kw)
  def swap(raw,target):
   self.assertIs(type(raw),bytes);self.z.write_bytes(b'changed after verification ZIP');self.p.write_bytes(b'changed after verification parser');swapped.append(True);return extract(raw,target)
  with patch.object(m,'extract_zip',swap),patch.object(pathlib.Path,'open',counted):result=m.build(self.z,self.p,self.out)
  self.assertEqual(opens,{self.z:1,self.p:1});self.assertEqual(swapped,[True]);self.assertEqual(result,{'built':True,'files':930,'checksPassed':8,'installed':False,'nativeBrowserVerified':False})
  expected=json.loads((args.builder_dir/'expected-files.json').read_text());actual={str(p.relative_to(self.out/'extension')):hashlib.sha256(p.read_bytes()).hexdigest()for p in(self.out/'extension').rglob('*')if p.is_file()}
  self.assertEqual(actual,expected);self.assertEqual(len(actual),930)
 def test_corrupt_zip_rejected_before_extraction(self):
  self.z.write_bytes(b'corrupt ZIP')
  with patch.object(m,'extract_zip',side_effect=AssertionError('must not extract')):
   with self.assertRaisesRegex(ValueError,'^ZIP_DIGEST$'):m.build(self.z,self.p,self.out)
  self.assertFalse(self.out.exists())
 def test_corrupt_parser_rejected_before_extraction(self):
  self.p.write_bytes(b'corrupt parser')
  with patch.object(m,'extract_zip',side_effect=AssertionError('must not extract')):
   with self.assertRaisesRegex(ValueError,'^PARSER_DIGEST$'):m.build(self.z,self.p,self.out)
  self.assertFalse(self.out.exists())
 def test_zip_read_bound(self):
  with self.z.open('wb')as f:f.truncate(16*1024*1024+1)
  with self.assertRaisesRegex(ValueError,'^INPUT_SIZE$'):m.build(self.z,self.p,self.out)
  self.assertFalse(self.out.exists())
 def test_parser_read_bound(self):
  with self.p.open('wb')as f:f.truncate(512*1024+1)
  with self.assertRaisesRegex(ValueError,'^INPUT_SIZE$'):m.build(self.z,self.p,self.out)
  self.assertFalse(self.out.exists())
 def test_regression_control_reopening_zip_breaks_swap_guarantee(self):
  extract=m.extract_zip
  def reopen(raw,target):
   self.z.write_bytes(b'changed ZIP');return extract(self.z.read_bytes(),target)
  with patch.object(m,'extract_zip',reopen):
   with self.assertRaises(zipfile.BadZipFile):m.build(self.z,self.p,self.out)
  self.assertFalse(self.out.exists())
 def test_regression_control_reopening_parser_breaks_swap_guarantee(self):
  extract=m.extract_parser
  def reopen(raw,target):
   self.p.write_bytes(b'changed parser');return extract(self.p.read_bytes(),target)
  with patch.object(m,'extract_parser',reopen):
   with self.assertRaises(tarfile.ReadError):m.build(self.z,self.p,self.out)
  self.assertFalse(self.out.exists())
result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SnapshotTests))
unchanged=all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in originals.items());assert unchanged
report={'schema':1,'scope':'portable uBOL immutable input snapshot repair','builderSha256':hashlib.sha256((args.builder_dir/'build.py').read_bytes()).hexdigest(),'testSha256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'testsRun':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'regressionControls':2,'fullReproductionFiles':930,'nestedHardeningChecks':8,'originalArchivesUnchanged':unchanged,'networkCalls':0,'nativeDeviceCalls':0,'sourceEdits':0,'passed':result.wasSuccessful()}
args.report.write_text(json.dumps(report,indent=2)+'\n');raise SystemExit(0 if result.wasSuccessful()else 1)
