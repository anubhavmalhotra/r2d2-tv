"""Local Node subprocesses only; no TV, firmware, service or network actions."""
import json,os,shutil,subprocess,tempfile,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent
class Mutations(unittest.TestCase):
 def run_mutation(self,name,file,old,new,kind=None):
  with tempfile.TemporaryDirectory(prefix='voice-join-mutant-')as temp:
   out=Path(temp)
   for source in ('joined_process.js','manifest_transport.js','capture_child.js'):shutil.copyfile(HERE/source,out/source)
   target=out/file;text=target.read_text();self.assertEqual(text.count(old),1);target.write_text(text.replace(old,new))
   env=dict(os.environ,MODULE_DIR=str(out))
   if kind:env['JOIN_KIND']=kind
   result=subprocess.run(['node',str(HERE/'test_join.js')],env=env,capture_output=True,timeout=15)
   self.assertNotEqual(result.returncode,0,name)
   self.assertNotIn(b'SyntaxError',result.stderr,name)
 def test_old_manifest_child_close_order(self):
  self.run_mutation('manifest-old-close','joined_process.js','        settled();\n      });','        finish();\n      });','manifest')
 def test_old_capture_child_close_order(self):
  self.run_mutation('capture-old-close','joined_process.js','        settled();\n      });','        finish();\n      });','capture')
 def test_manifest_false_response_cannot_ack(self):
  self.run_mutation('false-ack','manifest_transport.js','result.accepted && result.result.returnValue === true','result.accepted')
 def test_callback_cannot_change_projection(self):
  self.run_mutation('mutable-result','capture_child.js','    Object.freeze(projected);','')
 def test_semantic_result_gate(self):
  self.run_mutation('result-gate','capture_child.js','    if (hooks.validateResult && hooks.validateResult(projected) !== true) return null;','')
if __name__=='__main__':unittest.main()
