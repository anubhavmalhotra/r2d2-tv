"""Pinned public data, conservative exceptions and owner policy; no network."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cosmetics_data', ROOT/'protection/cosmetics.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

def fixture(*, low=None, high='[data-ad-slot]', exceptions=None, hosts=None, entities=False):
    values = {'lowlyGeneric': [[1,'.ad-container']] if low is None else low,
              'highlyGeneric': high, 'exceptions': [] if exceptions is None else exceptions,
              'hostnames': [] if hosts is None else hosts, 'hasEntities': entities}
    return '\n'.join('const '+key+' = '+('new Map('+json.dumps(value)+');' if key=='lowlyGeneric'
                    else json.dumps(value)+';') for key,value in values.items())

class GenericDataTests(unittest.TestCase):
    def test_fixed_json_literal_is_data_and_dynamic_values_are_refused(self):
        self.assertEqual(b.json_literal('const values = /* 2 */["#ad", ".banner"];\nthrow Error("inert");', 'values'), ['#ad','.banner'])
        for source in ('const value = 1;\nconst value = 2;',
                       'const value = ["#ad"] + execute();', 'const value = JSON.parse("[]");'):
            with self.assertRaises(ValueError): b.json_literal(source,'value')

    def test_selector_escape_and_unbalanced_structure_fail_closed(self):
        for source in ('.ad}body{display:none}', '.ad;@import "bad"', '.ad/*comment*/',
                       'url(http://invalid.example)', '.ad\\', '[x="unterminated]',
                       '[name="bad\n};body{display:none}"]', '[data-x="x"', '[data-x="x"]):is(.ad'):
            with self.subTest(source=source), self.assertRaises(ValueError): b.selector(source)

    def test_quoted_and_escaped_selector_content_remains_data(self):
        for source in ('[style="width:100%;"]', ':is(.ad:not(:empty),[href="https://example.com/;"])',
                       '#id\\{', r'#id\7b', '[name="continued\\\nstring"]'):
            self.assertEqual(b.selector(source),source)

    def test_exception_scope_is_conservative_and_deduplicated(self):
        css, excluded, count, rows = b.compile_generic(fixture(exceptions=['#ad','[data-ad-slot]'], hosts=['example.com','example.com']))
        self.assertEqual(excluded,['*://*.example.com/*'])
        self.assertEqual((count,rows),(2,2))
        self.assertIn('.ad-container {display:none!important;}',css)

    def test_exceptions_and_entities_cannot_bypass_validation(self):
        for source in (fixture(exceptions=['#ad']),fixture(hosts=['example.com']),
                       fixture(entities=True),fixture(entities=0),
                       fixture(exceptions=['#ad}body{display:none}'],hosts=['example.com'])):
            with self.assertRaises(ValueError): b.compile_generic(source)

    def test_selector_identifiers_are_exact_bounded_unique_integers(self):
        for rows in ([[True,'#ad']],[[0,'#ad'],[0,'.banner']],[[65536,'#ad']],
                     [[-1,'#ad']],[[1,'#ad','extra']]):
            with self.assertRaisesRegex(ValueError,'LOW_ROW'): b.compile_generic(fixture(low=rows))

    def test_exception_hosts_cannot_inject_match_patterns(self):
        for host in ('https://example.com','example.com/path','*.example.com','example.com:80',
                     'example.com@else.invalid','example..com'):
            with self.assertRaisesRegex(ValueError,'EXCEPTION_HOST'):
                b.compile_generic(fixture(exceptions=['#ad'],hosts=[host]))

    def test_every_public_input_has_exact_provenance_and_hash(self):
        lock=json.loads((ROOT/'protection/provenance.json').read_bytes())
        self.assertEqual(set(lock['cosmeticInputs']),set(b.PINS))
        self.assertEqual(lock['commit'],b.COMMIT)
        for name,pin in lock['cosmeticInputs'].items():
            raw=(ROOT/'protection/upstream'/name).read_bytes()
            self.assertEqual(len(raw),pin['bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),pin['sha256'])
            self.assertEqual(pin['sha256'],b.PINS[name])
            self.assertEqual(b.read_data(name),raw.decode())
        with self.assertRaisesRegex(ValueError,'SOURCE_NAME'): b.read_data('../outside')

    def test_generated_css_and_manifest_exception_scope_reproduce_exact_bytes(self):
        css, excluded, count, rows=b.compile_generic(b.read_data('scripting__generic__easylist.js'))
        lock=json.loads((ROOT/'protection/provenance.json').read_bytes())['cosmeticOutput']
        self.assertEqual((count,rows,len(excluded)),(11408,494,494))
        self.assertEqual(css.encode(),(ROOT/'spark/runtime/extension/generic.css.in').read_bytes())
        self.assertEqual(b.digest(css.encode()),lock['outputSHA256'])
        manifest=json.loads((ROOT/'spark/runtime/extension/manifest.json.in').read_bytes())
        scripts=[x for x in manifest['content_scripts'] if x.get('css')==['generic.css']]
        self.assertEqual(scripts,[{'matches':['http://*/*','https://*/*'],'exclude_matches':excluded,
                                  'css':['generic.css'],'all_frames':True,'run_at':'document_start'}])

    def test_owner_rules_are_exact_high_priority_and_include_navigation(self):
        raw=(ROOT/'spark/runtime/extension/rules/lg-policy.json.in').read_bytes()
        self.assertEqual(raw,b.dump(b.owner_rules()))
        self.assertEqual(b.digest(raw),json.loads((ROOT/'protection/provenance.json').read_bytes())['cosmeticOutput']['ownerPolicySHA256'])
        rules=json.loads(raw)
        allow_priorities=[r.get('priority',1) for name in ('easylist','easyprivacy','ublock-badware')
                          for r in json.loads((ROOT/'spark/runtime/extension/rules'/(name+'.json.in')).read_bytes())
                          if r['action']['type']=='allow']
        self.assertEqual(len(rules),5)
        self.assertEqual(len({r['id'] for r in rules}),5)
        for rule in rules:
            self.assertGreater(rule['priority'],max(allow_priorities))
            self.assertEqual(rule['action'],{'type':'block'})
            self.assertEqual(set(rule['condition']),{'requestDomains','resourceTypes'})
            self.assertEqual(rule['condition']['resourceTypes'],b.RESOURCE_TYPES)
            self.assertIn('main_frame',rule['condition']['resourceTypes'])

    def test_manifest_permissions_and_executable_scope_remain_bounded(self):
        root=ROOT/'spark/runtime'
        pins=json.loads((root/'bundle-pins.json.in').read_bytes())
        self.assertEqual(len(pins['files']),12)
        manifest=json.loads((root/'extension/manifest.json.in').read_bytes())
        self.assertEqual(manifest['permissions'],['declarativeNetRequest'])
        self.assertNotIn('background',manifest)
        self.assertEqual({r['id'] for r in manifest['declarative_net_request']['rule_resources']},
                         {'easylist','easyprivacy','ublock_badware','owner_lg'})
        self.assertTrue(all(r['enabled'] is True for r in manifest['declarative_net_request']['rule_resources']))
        self.assertEqual({n for n in pins['files'] if n.endswith('.js')},{'proof.js','popup_guard.js'})

    def test_authored_proof_vm_has_positive_and_negative_controls(self):
        run=subprocess.run(['node',str(ROOT/'tests/test_protection_proof.js')],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        self.assertEqual(run.returncode,0,'Fixed extension proof fixture failed')
        self.assertEqual(run.stdout,b'7 fixed extension proof cases passed\n')

if __name__=='__main__': unittest.main()
