import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import model_assets
from worker_runtime import Runtime

class Tests(unittest.TestCase):
    def test_public_startup_tree_is_owned_and_cleaned_without_reading_payload(self):
        runtime=Runtime('a'*32);root=runtime.root
        self.assertEqual(root.stat().st_mode&0o777,0o700)
        public=runtime.temp/'public_import.py';public.write_text('PUBLIC_STARTUP_CODE')
        self.assertTrue(runtime.cleanup());self.assertFalse(root.exists());self.assertTrue(runtime.cleanup())
    def test_symlink_does_not_clean_foreign_file(self):
        with tempfile.TemporaryDirectory()as d:
            foreign=Path(d)/'keep';foreign.write_text('public fixture')
            runtime=Runtime('b'*32);link=runtime.temp/'link';link.symlink_to(foreign)
            with self.assertRaises(ValueError):runtime.cleanup()
            self.assertTrue(foreign.exists());link.unlink();self.assertTrue(runtime.cleanup())
    def test_model_hashes_bind_descriptors_and_refuse_drift(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d).resolve();folders=[];manifest=[]
            for role in('asr','intent'):
                folder=root/role;folder.mkdir(mode=0o700);folders.append(folder)
                path=folder/'weights';path.write_bytes(role.encode())
                manifest.append({'role':role,'files':[{'file':'weights','bytes':len(role),'sha256':hashlib.sha256(role.encode()).hexdigest()}]})
            with patch.object(model_assets,'MODELS',manifest):
                resources=model_assets.verify(*folders);self.assertEqual(len(resources),2)
                (folders[0]/'weights').write_bytes(b'bad')
                with self.assertRaises(ValueError):model_assets.verify(*folders)

if __name__=='__main__':unittest.main()
