"""Disposable, assertion-sensitive reversions; no device or service operations."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
FILES = ('owner_key.js', 'resident_mic.js', 'resident_hid.js', 'resident_main.js',
         'test_owner_key.js', 'test_resident_mic.js', 'test_resident_hid.js', 'test_voice_phase_golden.json')
MUTANTS = (
    ('post-drain-metadata', 'owner_key.js', 'check(false,false,currentBudget);return Object.freeze', 'check(false,true,currentBudget);return Object.freeze', 'test_resident_mic.js'),
    ('capture-deadline', 'resident_mic.js', 'const currentBudget=()=>now()-began<CAPTURE_MS;', 'const currentBudget=()=>true;', 'test_resident_mic.js'),
    ('hid-caller-deadline', 'resident_hid.js', 't-began<8000&&currentBudget()===true', 't-began<8000', 'test_resident_hid.js'),
    ('key-caller-deadline', 'owner_key.js', 'clock()-began<1000&&currentBudget()===true', 'clock()-began<1000', 'test_owner_key.js'),
    ('wrong-key', 'owner_key.js', 'if(type!==1||code!==expected.keyCode)continue;', 'if(type!==1)continue;', 'test_owner_key.js'),
    ('identity-digest', 'owner_key.js', 'if(digest!==expected.identitySha256)continue;', 'if(false)continue;', 'test_owner_key.js'),
    ('repeats', 'owner_key.js', "if(value===1&&!down&&!ignore)pressed=true;", "if(!down&&!ignore)pressed=true;", 'test_owner_key.js'),
    ('busy-backlog', 'owner_key.js', 'const ignore=discard||!synced;', 'const ignore=!synced;', 'test_owner_key.js'),
    ('dropped-input', 'owner_key.js', "need(!(type===0&&code===3),'OWNER_KEY_DROPPED');", "need(true,'OWNER_KEY_DROPPED');", 'test_owner_key.js'),
    ('held-node', 'owner_key.js', 'same(saved.node,fs.fstatSync(fd))&&same(saved.node,fs.lstatSync(saved.path))&&', 'true&&', 'test_owner_key.js'),
    ('saturation', 'owner_key.js', 'if(!empty){synced=false;pressed=false;released=false;}', 'if(!empty){synced=true;}', 'test_owner_key.js'),
    ('pre-start-release', 'resident_mic.js', "if(!initiatingKey.ready||!initiatingKey.down||initiatingKey.released)throw fault('OWNER_KEY_RELEASED');", "if(false)throw fault('OWNER_KEY_RELEASED');", 'test_resident_mic.js'),
    ('key-close-unknown', 'resident_mic.js', 'if(!ownerKey.close())closeFailed=true;', 'ownerKey.close();', 'test_resident_mic.js'),
    ('hold-limit-cancels', 'resident_mic.js', 'if(!(start&&!currentBudget()&&error&&DEADLINE_CODES.has(error.code)))captureError=error;', 'captureError=error;', 'test_resident_mic.js'),
    ('hold-limit-not-passed', 'resident_mic.js', 'start&&!currentBudget()&&error&&DEADLINE_CODES', 'start&&error&&DEADLINE_CODES', 'test_resident_mic.js'),
    ('in-capture-budget', 'resident_mic.js', 'current(false,currentBudget);', 'current(false);', 'test_resident_mic.js'),
    ('early-refusal-lost', 'resident_mic.js', "if(receipt&&receipt.outcome&&receipt.outcome.state==='refused'&&receipt.actionAcknowledged!==true)return receipt;", '', 'test_resident_mic.js'),
    ('receipt-toast-unchecked', 'resident_mic.js', "typeof raw.toast==='string'&&/^[\\x20-\\x7e]{1,60}$/.test(raw.toast)", "typeof raw.toast==='string'", 'test_resident_mic.js'),
    ('receipt-other-press', 'resident_mic.js', 'raw.press!==press.id||', '', 'test_resident_mic.js'),
    ('surface-unchecked', 'resident_mic.js', 'SURFACES.includes(raw.surface)', "typeof raw.surface==='string'", 'test_resident_mic.js'),
    ('surface-ignored', 'resident_mic.js', "if(where==='other')toast(message);", 'toast(message);', 'test_resident_mic.js'),
    ('press-key-kept', 'resident_mic.js', 'if(press.key)press.key.fill(0);', '', 'test_resident_mic.js'),
    ('press-close-doubt-lost', 'resident_mic.js', 'if(press.unsure)closeFailed=true;', '', 'test_resident_mic.js'),
    ('ping-blocks-capture', 'resident_mic.js', "const press=openPress();phase(press,'pressed');", "const press=openPress();await phase(press,'pressed');", 'test_resident_mic.js'),
)

def run(directory, script):
    return subprocess.run(['node', script], cwd=directory, capture_output=True, text=True, timeout=15)

class Reversions(unittest.TestCase):
    def test_all_reversions_are_assertion_killed(self):
        for name, file, before, after, script in MUTANTS:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='r2d2-owner-key-') as directory:
                directory = Path(directory)
                for item in FILES:
                    shutil.copyfile(ROOT/item, directory/item)
                positive = run(directory, script)
                self.assertEqual(positive.returncode, 0, positive.stdout+positive.stderr)
                source = (directory/file).read_text()
                self.assertEqual(source.count(before), 1)
                (directory/file).write_text(source.replace(before, after, 1))
                compiled = subprocess.run(['node', '--check', file], cwd=directory, capture_output=True, text=True, timeout=5)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = run(directory, script)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn('FAIL ', result.stderr)
                self.assertIn('AssertionError', result.stderr)
                self.assertNotIn('SyntaxError', result.stderr)

if __name__ == '__main__':
    unittest.main()
