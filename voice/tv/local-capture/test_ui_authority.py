"""Compile actual native authority and handoff; no vendor runtime or device."""
from pathlib import Path
import json, os, shutil, subprocess, tempfile, unittest
HERE = Path(__file__).resolve().parent
class UIAuthority(unittest.TestCase):
    def run_source(self, source, binary):
        compiler = os.environ.get('CXX') or shutil.which('c++')
        self.assertIsNotNone(compiler)
        built = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
            str(source/'native_handoff.cpp'), str(source/'ui_authority_test.cpp'), '-o', str(binary)], capture_output=True, text=True, timeout=30)
        self.assertEqual(built.returncode, 0, built.stderr)
        return subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    def test_authority_and_original_physical_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            run = self.run_source(HERE, Path(temp)/'test')
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), {'groups':6, 'deviceCalls':False})
    def test_positive_first_authority_mutations(self):
        mutants = [
            ('ui_authority.hpp', '!std::strcmp(sender, kUiCaller)', 'true'),
            ('ui_authority.hpp', '!std::strcmp(category, kUiCategory)', 'true'),
            ('native_handoff.cpp', '(p.profile.ui_capture ? kUiCaller : kCaller)', 'kCaller'),
        ]
        for name, before, after in mutants:
            with self.subTest(name=name, mutation=before), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for source in ('native_handoff.hpp','native_handoff.cpp','ui_authority.hpp','ui_authority_test.cpp'):
                    shutil.copyfile(HERE/source, root/source)
                self.assertEqual(self.run_source(root, root/'control').returncode, 0)
                path=root/name; text=path.read_text(); self.assertEqual(text.count(before),1); path.write_text(text.replace(before,after))
                self.assertEqual(self.run_source(root, root/'mutant').returncode, 1)
if __name__ == '__main__': unittest.main()
