"""Positive-first source-sensitive UI controls; only disposable source copies."""
from pathlib import Path
import shutil, subprocess, tempfile, unittest
HERE = Path(__file__).resolve().parent
class UIMutations(unittest.TestCase):
    def test_behavioral_controls_kill_mutants(self):
        pairs = [
            ('ui_capture.js', "message.senderServiceName()!==SERVICE", 'false', 'test_ui_capture.js'),
            ('ui_capture.js', "if(!ending&&!ended)void cancel('CAPTURE_DEADLINE');", 'if(!ending&&!ended)void release();', 'test_ui_capture.js'),
            ('ui_capture.js', 'try{if(!stopNative(()=>{', 'try{job.release();if(!stopNative(()=>{', 'test_ui_capture.js'),
            ('ui_capture.js', 'for(const retire of [...pending])retire();\n   if(localCleanupFailed)', 'if(localCleanupFailed)', 'test_ui_capture.js'),
            ('owner_service.js', 'current.outcomeUnknown=!!current.actionReleased;', 'current.outcomeUnknown=false;', 'test_ui_capture.js'),
            ('owner_service.js', '(!receipt.uiSubmitted||knownOutcome)', 'true', 'test_ui_capture.js'),
            ('ui_heartbeat.js', 'if(!crypto.timingSafeEqual(expected,provided))throw Error();', 'if(false)throw Error();', 'test_ui_heartbeat.js'),
            ('ui_heartbeat.js', 'value.nonce!==nonce||', '', 'test_ui_heartbeat.js'),
            ('ui_qualification.js', 'value[key]===want', "key==='moduleSha256'||value[key]===want", 'test_ui_qualification.js'),
            ('ui_qualification.js', 'need(text.trim()!==voiceKey);', 'need(true);', 'test_ui_qualification.js'),
        ]
        for source, before, after, test in pairs:
            with self.subTest(source=source, mutation=before), tempfile.TemporaryDirectory() as temp:
                root=Path(temp)/'voice'/'tv';root.mkdir(parents=True)
                shutil.copytree(HERE,root/'local-capture',ignore=shutil.ignore_patterns('__pycache__'))
                shutil.copytree(HERE.parent/'transport',root/'transport',ignore=shutil.ignore_patterns('__pycache__'))
                work=root/'local-capture'
                control=subprocess.run(['node',test],cwd=work,capture_output=True,text=True,timeout=10)
                self.assertEqual(control.returncode,0,control.stderr)
                path=work/source;text=path.read_text();self.assertEqual(text.count(before),1);path.write_text(text.replace(before,after))
                syntax=subprocess.run(['node','--check',source],cwd=work,capture_output=True,text=True,timeout=10)
                self.assertEqual(syntax.returncode,0,syntax.stderr)
                mutant=subprocess.run(['node',test],cwd=work,capture_output=True,text=True,timeout=10)
                self.assertNotEqual(mutant.returncode,0)
                self.assertIn('AssertionError',mutant.stderr)
if __name__ == '__main__':unittest.main()
