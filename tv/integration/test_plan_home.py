"""Synthetic-only offline controls. No firmware or device data is required."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import plan_home as p


def fixture():
    return {
        'schema': 1,
        'firmware': {'release': 'SYNTHETIC', 'build': 'test-build', 'architecture': 'test'},
        'profile': {
            'defaultApps': {'categories': {'home': 'org.example.stockhome',
                                         'aod': 'org.example.aod'},
                            'metadata': {'unknown': [False, None, 7]}},
            'idleApp': {'appId': 'org.example.stockhome', 'support': True,
                        'metadata': {'unknown': ['keep']}},
            'unrelated': {'keep': ['whole profile', 1]},
        },
        'configIdleApp': {'appId': 'org.example.stockhome', 'unknown': {'keep': 1}},
        'configProfileList': ['idleApp', 'unrelated'],
        'lastAppHandlerPolicy': 'idleApp',
        'factoryIdleApp': {'present': False, 'value': None},
    }


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.originals = self.root / 'originals'; self.originals.mkdir()
        self.snapshot = fixture()
        self.raw = p.encode(self.snapshot)
        self.contract = {'schema': 1, 'firmware': copy.deepcopy(self.snapshot['firmware']),
                         'snapshotSha256': p.digest(self.raw),
                         'expectedHome': 'org.example.stockhome',
                         'expectedIdle': 'org.example.stockhome', 'artifacts': {}}
        for role in sorted(p.ARTIFACT_ROLES):
            raw = ('synthetic test artifact: ' + role).encode()
            (self.originals / role).write_bytes(raw)
            self.contract['artifacts'][role] = {'path': role, 'sha256': p.digest(raw)}

    def plan(self):
        return p.plan(self.raw, self.contract, 'org.example.r2d2', self.originals)

    def rejects(self, code, call):
        with self.assertRaisesRegex(p.Refusal, '^' + code + '$'):
            call()

    def test_only_three_selectors_change_and_inputs_unchanged(self):
        before = copy.deepcopy((self.snapshot, self.contract))
        result = p.candidates(self.snapshot, self.contract, 'org.example.r2d2')
        home = result['home_profile.candidate.json']['defaultApps']
        idle = result['idle_profile.candidate.json']['idleApp']
        config = result['config-set.candidate.json']['configs']['profile.idleApp']
        self.assertEqual(home['categories']['home'], 'org.example.r2d2')
        self.assertEqual(idle['appId'], 'org.example.r2d2')
        self.assertEqual(config['appId'], 'org.example.r2d2')
        home['categories']['home'] = before[0]['profile']['defaultApps']['categories']['home']
        idle['appId'] = before[0]['profile']['idleApp']['appId']
        config['appId'] = before[0]['configIdleApp']['appId']
        self.assertEqual(home, before[0]['profile']['defaultApps'])
        self.assertEqual(idle, before[0]['profile']['idleApp'])
        self.assertEqual(config, before[0]['configIdleApp'])
        self.assertEqual((self.snapshot, self.contract), before)

    def test_factory_absent_restores_null_with_nonvolatile_scope(self):
        result = p.candidates(self.snapshot, self.contract, 'org.example.r2d2')
        self.assertEqual(result['config-restore.candidate.json'],
                         {'configs': {'profile.idleApp': None}, 'volatile': False})
        self.assertIs(result['config-set.candidate.json']['volatile'], False)

    def test_already_selected_fields_are_not_claimed_changed(self):
        self.contract['expectedHome'] = 'org.example.r2d2'
        self.snapshot['profile']['defaultApps']['categories']['home'] = 'org.example.r2d2'
        self.raw = p.encode(self.snapshot)
        self.contract['snapshotSha256'] = p.digest(self.raw)
        summary = p.decode(self.plan()['plan.json'])
        self.assertEqual(summary['changedFields'], ['idleApp.appId', 'profile.idleApp.appId'])

    def test_factory_present_preserves_complete_selected_object(self):
        prior = {'unknown': {'member': [1, False]}, 'appId': 'org.example.prior'}
        self.snapshot['factoryIdleApp'] = {'present': True, 'value': prior}
        result = p.candidates(self.snapshot, self.contract, 'org.example.r2d2')
        value = result['config-restore.candidate.json']['configs']['profile.idleApp']
        self.assertEqual(value, prior)
        value['unknown']['member'].append(2)
        self.assertEqual(prior['unknown']['member'], [1, False])

    def test_snapshot_digest_and_firmware_are_independent_gates(self):
        self.raw += b' '
        self.rejects('SNAPSHOT_HASH', self.plan)
        self.contract['snapshotSha256'] = p.digest(self.raw)
        self.contract['firmware']['build'] = 'different'
        self.rejects('FIRMWARE_MISMATCH', self.plan)

    def test_original_digest_role_and_structure_refusal(self):
        role = sorted(p.ARTIFACT_ROLES)[0]
        (self.originals / role).write_bytes(b'changed original')
        self.rejects('ARTIFACT_HASH', self.plan)
        del self.contract['artifacts'][role]
        self.rejects('ARTIFACT_ROLES', self.plan)

    def test_artifact_path_cannot_escape_or_follow_symlink(self):
        role = sorted(p.ARTIFACT_ROLES)[0]
        self.contract['artifacts'][role]['path'] = '../outside'
        self.rejects('ARTIFACT_PATH', self.plan)
        target = self.originals / role
        link = self.originals / 'link'; link.symlink_to(target)
        self.contract['artifacts'][role]['path'] = 'link'
        self.rejects('NONCANONICAL_INPUT', self.plan)

    def test_shared_hardlink_and_oversized_input_refused(self):
        target = self.originals / sorted(p.ARTIFACT_ROLES)[0]
        os.link(target, self.originals / 'second-name')
        self.rejects('INPUT_FILE', self.plan)
        separate = self.root / 'separate'; separate.write_bytes(b'1234')
        self.rejects('INPUT_FILE', lambda: p.file_hash(separate, 3))

    def test_json_duplicate_nonfinite_depth_and_type_bounds(self):
        self.rejects('DUPLICATE_JSON_KEY', lambda: p.decode(b'{"a":1,"a":2}'))
        self.rejects('NONFINITE_JSON', lambda: p.decode(b'{"a":NaN}'))
        self.rejects('JSON_DEPTH', lambda: p.decode(b'[' * 26 + b'0' + b']' * 26))
        self.rejects('NUMBER_BOUND', lambda: p.decode(b'1e999'))
        self.rejects('JSON_BOUND', lambda: p.decode(b' ' * (p.MAX_JSON + 1)))

    def test_idle_support_policy_list_and_baselines_refuse_unknown_states(self):
        for field, value, code in (
            ('lastAppHandlerPolicy', 'unknown', 'IDLE_POLICY'),
            ('configProfileList', [], 'CONFIG_IDLE_GATE'),
            ('configProfileList', ['idleApp', []], 'CONFIG_IDLE_GATE'),
            ('configProfileList', ['idleApp', 'idleApp'], 'CONFIG_IDLE_GATE'),
            ('configIdleApp', {'appId': 'org.example.other'}, 'CONFIG_IDLE_BASELINE'),
        ):
            value_snapshot = copy.deepcopy(self.snapshot); value_snapshot[field] = value
            self.rejects(code, lambda: p.candidates(value_snapshot, self.contract, 'org.example.r2d2'))
        self.snapshot['profile']['idleApp']['support'] = False
        self.rejects('IDLE_BASELINE', lambda: p.candidates(self.snapshot, self.contract, 'org.example.r2d2'))

    def test_factory_presence_has_no_implicit_default(self):
        for prior in ({'present': True, 'value': []}, {'present': False, 'value': {}},
                      {'present': 1, 'value': {}}, {'present': True, 'value': 'unknown'}):
            self.snapshot['factoryIdleApp'] = prior
            self.rejects('FACTORY_PRIOR', lambda: p.candidates(self.snapshot, self.contract, 'org.example.r2d2'))

    def test_owner_cannot_impersonate_vendor_or_be_arbitrary_text(self):
        for owner in ('com.webos.newhome', 'com.lge.newhome', 'com.palm.newhome'):
            self.rejects('OWNER_NAMESPACE', lambda: p.candidates(self.snapshot, self.contract, owner))
        self.rejects('APP_ID', lambda: p.candidates(self.snapshot, self.contract, 'invalid/../id'))

    def test_publication_private_exclusive_and_manifest_exact(self):
        outputs = self.plan(); output = self.root / 'candidate'
        p.publish(output, outputs)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for name, raw in outputs.items():
            self.assertEqual((output / name).read_bytes(), raw)
            self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
        manifest = p.decode(outputs['plan.json'])
        self.assertIs(manifest['deploymentImplemented'], False)
        self.assertEqual(manifest['liveCalls'], 0)
        for name, record in manifest['outputs'].items():
            self.assertEqual(record, {'sha256': p.digest(outputs[name]), 'bytes': len(outputs[name])})
        with self.assertRaises(FileExistsError):
            p.publish(output, outputs)

    def test_cli_success_and_error_do_not_export_input_values_or_paths(self):
        self.snapshot['profile']['privateExample'] = 'SYNTHETIC_PRIVATE_SENTINEL'
        raw = p.encode(self.snapshot); self.contract['snapshotSha256'] = p.digest(raw)
        snapshot_path = self.root / 'snapshot.json'; snapshot_path.write_bytes(raw)
        contract_path = self.root / 'contract.json'; contract_path.write_bytes(p.encode(self.contract))
        args = [sys.executable, '-B', str(Path(p.__file__).resolve()), '--contract', str(contract_path),
                '--snapshot', str(snapshot_path), '--original-root', str(self.originals),
                '--owner-app-id', 'org.example.r2d2', '--output', str(self.root / 'out')]
        for expected_return in (0, 1):
            result = subprocess.run(args, capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, expected_return)
            self.assertNotIn(b'SYNTHETIC_PRIVATE_SENTINEL', result.stdout + result.stderr)
            self.assertNotIn(str(self.root).encode(), result.stdout + result.stderr)
            self.assertNotIn(b'Traceback', result.stderr)

    def test_pure_planner_needs_no_socket_or_subprocess(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen', side_effect=AssertionError('command forbidden')):
            self.assertEqual(len(self.plan()), 6)


if __name__ == '__main__':
    unittest.main()
