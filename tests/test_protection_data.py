"""Rebuild reviewed filter output and verify the generic private build's bundle."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import build
import privacy_check
spec=importlib.util.spec_from_file_location('protection_conversion',ROOT/'protection/convert.py')
conversion=importlib.util.module_from_spec(spec)
spec.loader.exec_module(conversion)


class ProtectionDataTests(unittest.TestCase):
    def test_original_rules_reproduce_every_shipped_byte(self):
        lock=json.loads((ROOT/'protection/provenance.json').read_text())
        self.assertEqual(set(lock['files']),set(conversion.LISTS))
        total=0
        for name,pin in lock['files'].items():
            raw=(ROOT/'protection/upstream'/(name+'.json')).read_bytes()
            self.assertEqual(len(raw),pin['bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),pin['sha256'])
            original=json.loads(raw)
            rules,stats=conversion.convert(original)
            result=conversion.dump(rules)
            self.assertEqual(hashlib.sha256(result).hexdigest(),pin['outputSHA256'])
            self.assertEqual(result,(ROOT/'spark/runtime/extension/rules'/(name+'.json.in')).read_bytes())
            self.assertEqual([r for r in rules if r['action']['type']=='allow'],
                             [r for r in original if r['action']['type']=='allow'])
            total+=len(rules)
        self.assertEqual(total,13185)

    def test_rendered_bundle_identity_matches_startup_verification(self):
        cfg=build.config((ROOT/'config/example.json').read_bytes())
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp).resolve()/'build'
            build.build(cfg,out)
            runtime=out/'spark/runtime'
            manifest=(runtime/'bundle-pins.json').read_bytes()
            self.assertEqual(hashlib.sha256(manifest).hexdigest(),
                             'c9db61de58e4b1cb91caf2ad72bff2487b8995999bcaa511dd62b9208026a94b')
            pins=json.loads(manifest)
            paths={str(p.relative_to(runtime/'extension')) for p in (runtime/'extension').rglob('*') if p.is_file()}
            self.assertEqual(paths,set(pins['files']))
            for name,pin in pins['files'].items():
                raw=(runtime/'extension'/name).read_bytes()
                self.assertEqual(len(raw),pin['bytes'])
                self.assertEqual(hashlib.sha256(raw).hexdigest(),pin['sha256'])

    def test_public_data_exception_requires_both_path_and_exact_bytes(self):
        for name in privacy_check.PUBLIC_FILTER_DATA:
            raw=(ROOT/name).read_bytes()
            self.assertEqual(privacy_check.issues(name,raw),[])
            self.assertIn('private-ipv4',privacy_check.issues('unreviewed.json',raw))
            self.assertIn('private-ipv4',privacy_check.issues(name,raw+b' '))
            self.assertIn('unexpected-large-file',privacy_check.issues(name,raw+b' '))


if __name__=='__main__':unittest.main()
