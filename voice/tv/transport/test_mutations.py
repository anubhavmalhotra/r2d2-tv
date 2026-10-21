"""Mutate production copies only; targeted unit controls must reject each defect."""
from pathlib import Path
import json,shutil,subprocess,tempfile,unittest
ROOT=Path(__file__).resolve().parent
MUTATIONS=[
 ('stop-ack-omitted','pcm_provider_adapter.js','r.nativeStartAccepted===true&&r.nativeStopAccepted===true&&r.nativeStopFailed===false&&',''),
 ('queued-pcm-alias','tls_pcm_client.js','const pcm=Buffer.from(buffer);','const pcm=buffer;'),
 ('sequence-skips-first','tls_pcm_client.js','sequence=0,chunks=0','sequence=1,chunks=0'),
 ('unbounded-pending','tls_pcm_client.js','pending+buffer.length>MAX_PENDING','false'),
 ('short-result-wait','tls_pcm_client.js','RESULT_TIMEOUT_MS=200000','RESULT_TIMEOUT_MS=105000'),
]
class MutationControls(unittest.TestCase):
 def test_each_real_regression_is_detected(self):
  for name,file,before,after in MUTATIONS:
   with self.subTest(name=name),tempfile.TemporaryDirectory(prefix='r2d2-tls-mutation-',dir=None)as tmp:
    dest=Path(tmp)
    for f in ['tls_pcm_client.js','pcm_provider_adapter.js','test_unit.js']:(dest/f).write_bytes((ROOT/f).read_bytes())
    good=subprocess.run([shutil.which('node'),str(dest/'test_unit.js')],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
    self.assertEqual(good.returncode,0,name+' positive failed')
    p=dest/file;s=p.read_text();self.assertEqual(s.count(before),1);p.write_text(s.replace(before,after))
    compiled=subprocess.run([shutil.which('node'),'--check',str(p)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
    self.assertEqual(compiled.returncode,0,name+' mutant did not compile')
    r=subprocess.run([shutil.which('node'),str(dest/'test_unit.js')],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
    self.assertNotEqual(r.returncode,0,name+' escaped executed controls')
    self.assertIn(b'AssertionError',r.stderr,name+' did not fail by assertion')
    self.assertNotIn(b'SyntaxError',r.stderr,name+' failed by syntax')
if __name__=='__main__':unittest.main()
