"""Compile and exercise the actual native handoff on local unnamed sockets."""
from pathlib import Path
import json, os, shutil, subprocess, tempfile, unittest
HERE = Path(__file__).resolve().parent
class NativeHandoff(unittest.TestCase):
    def compile(self, source, binary):
        compiler = os.environ.get('CXX') or shutil.which('c++')
        self.assertIsNotNone(compiler)
        compiled = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
            str(source/'native_handoff.cpp'), str(source/'native_handoff_test.cpp'), '-o', str(binary)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
    def test_actual_core_and_short_write_regression(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / 'handoff-test'
            self.compile(HERE, binary)
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), {'groups':23, 'deviceCalls':False})
    def test_regressions_kill_source_mutants(self):
        mutants = [
            ('native_handoff.cpp', '!first || second || bytes', '!second || first || bytes'),
            ('native_handoff.cpp', 'send(fd, data + offset, bytes - offset, send_flags())', 'send(fd, data + offset, bytes, send_flags())'),
            ('native_handoff.cpp', 'p.result.phase != Phase::Capturing && p.result.phase != Phase::Starting', 'p.result.phase != Phase::Capturing'),
            ('ble_callback.hpp', '!= image.load_bias + 0x5771c', '!= 0x5771c'),
            ('owner_retirement.h', 'state->deadline=now+750', 'state->deadline=now+100'),
        ]
        for filename, original, replacement in mutants:
            with self.subTest(filename=filename, regression=original), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for name in ('native_handoff.cpp', 'native_handoff.hpp', 'ui_authority.hpp', 'native_handoff_test.cpp', 'ble_callback.hpp', 'owner_retirement.h', 'native_retirement.hpp'):
                    shutil.copyfile(HERE/name, root/name)
                path = root/filename
                source = path.read_text()
                self.assertEqual(source.count(original), 1)
                path.write_text(source.replace(original, replacement))
                binary = root/'mutant'
                self.compile(root, binary)
                result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, 'Regression survived: '+original)
if __name__ == '__main__': unittest.main()
