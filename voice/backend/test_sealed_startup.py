"""Focused pure/mocked controls: no sockets, CUDA, seccomp, models or SSH."""
import ast
import copy
import json
from pathlib import Path
import types
import sys
import unittest
from unittest.mock import Mock, patch
import sealed_startup as start
from controller import Refused
from model_worker import ModelWorker
from offline_fixtures import Context, threads, info
from probe_contract import new, BOOLS, FAMILIES
import stat

ATTEMPT = 'a' * 32


def complete():
    row = new(ATTEMPT)
    row.update({key: True for key in BOOLS})
    row.update(phase='complete', driverInitCode=0, deviceCountCode=0, deviceCount=1,
               sampleSwapBytes=0, sampleLockedBytes=4096, maximumLiveTensorElements=48,
               networkDenials=[{'family': f, 'return': -1, 'errno': 1} for f in FAMILIES])
    return row


def ready(attempt=ATTEMPT):
    return {'schema':2,'attempt':attempt,'policySha256':start.POLICY_SHA256,
            'noNewPrivs':1,'seccompMode':2,'filterCount':2,'threadCount':2,'descriptorCount':4,
            **{key:True for key in start.TRUE},**{key:False for key in start.FALSE}}


def context():
    ctx=Context(scope=(0,1,2,44));fds=ctx.snapshot()
    filesystem={'abi':8,'installedBeforeThreads':True,'writeDeviceRules':6,
                'persistentFileWritesAllowed':False,'ownedTaskMetadataRuleCount':1,'ownedTaskInodeBound':True,
                'tcpAllowed':False,'abstractSocketScope':True,'signalScope':True,'negativeProbesPassed':True}
    role={'listenerCount':1,'pipePairCount':3,'eventfdCount':4,'characterCount':26,
          'listenerNotConnected':True,'listenerAddressClass':'abstract','preBootstrapStdioOnly':True,
          'knownNotificationRolesObserved':True,'privateInputQualified':False}
    ops=types.SimpleNamespace(filesystem_observation=filesystem,role_observation=role,
          bootstrapped_sample=types.SimpleNamespace(fds=fds),denial_probe=Path('/public/attempt.json'),
          socket_creation_denial=lambda family:(-1,1),memory_sample=lambda:{'swapBytes':0,'lockedBytes':4096})
    return ctx,ops


class StartupTests(unittest.TestCase):
    def test_success_uses_strict_current_inventory_after_complete_public_seal(self):
        ctx,ops=context()
        with patch.object(start.signal,'alarm')as alarm,patch.object(start.filesystem_seal,'check_denials'):
            state,row=start.bootstrap(44,ATTEMPT,issue=lambda *a:None,operations_class=lambda _:ops,
                    run=lambda *a:complete(),descriptors=lambda *a,**k:ctx.snapshot(),threads=lambda:threads(2))
            actual=start.current_seal(state,44,descriptors=lambda *a,**k:ctx.snapshot(),threads=lambda:threads(2))
        self.assertEqual(actual,ready());alarm.assert_called_once_with(0)
        self.assertTrue(row['complete']);self.assertFalse(actual['privateASRQualified'])

    def test_partial_public_seal_never_enables_ready_or_cancels_watchdog(self):
        partial = new(ATTEMPT);partial['error'] = 'GPU_UNQUALIFIED'
        with patch.object(start.signal, 'alarm') as alarm:
            with self.assertRaises(start.StartupRefused):
                start.bootstrap(44, ATTEMPT, issue=lambda *a: None,
                    operations_class=lambda _: None, run=lambda *a: partial,
                    descriptors=lambda *a, **k: self.fail('must not inventory as success'))
            alarm.assert_not_called()

    def test_legacy_partial_promoted_and_foreign_ready_refused(self):
        for key, value in [('schema', True), ('sealed', False), ('TSYNCZero', False),
                           ('attempt', 'b' * 32), ('policySha256', '0' * 64),
                           ('persistentFileWritesDenied', False), ('filterCount', 1),
                           ('privateASRQualified', True)]:
            row = ready();row[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):start.validate_ready(row, ATTEMPT)
        row = ready();row['privateSpeech'] = 'synthetic-only'
        with self.assertRaises(ValueError):start.validate_ready(row, ATTEMPT)

    def test_replaced_or_new_unknown_descriptor_cannot_pass_ready(self):
        base,ops=context();state=start.Context(ops,ATTEMPT,base.snapshot(),44)
        for mode in(stat.S_IFSOCK,stat.S_IFREG,stat.S_IFIFO):
            changed=Context(scope=(0,1,2,44),extra={55:info(mode)})
            with patch.object(start.model_descriptor_roles,'inspect',return_value={'qualified':False}),self.assertRaises(ValueError):
                start.current_seal(state,44,descriptors=lambda *a,**k:changed.snapshot(),threads=lambda:threads(2))

    def test_post_model_filter_network_and_memory_fail_closed(self):
        ctx,ops=context();state=start.Context(ops,ATTEMPT,ctx.snapshot(),44)
        with patch.object(start.filesystem_seal,'check_denials'):
            self.assertEqual(start.current_seal(state,44,descriptors=lambda *a,**k:ctx.snapshot(),threads=lambda:threads(2)),ready())
            with self.assertRaises(ValueError):start.current_seal(state,44,descriptors=lambda *a,**k:ctx.snapshot(),threads=lambda:threads(1))
            ops.socket_creation_denial=lambda f:(5,0)
            with self.assertRaises(ValueError):start.current_seal(state,44,descriptors=lambda *a,**k:ctx.snapshot(),threads=lambda:threads(2))
            ops.socket_creation_denial=lambda f:(-1,1);ops.memory_sample=lambda:{'swapBytes':1,'lockedBytes':4096}
            with self.assertRaises(ValueError):start.current_seal(state,44,descriptors=lambda *a,**k:ctx.snapshot(),threads=lambda:threads(2))

    def test_missing_filesystem_denial_proof_cannot_emit_ready(self):
        ctx,ops=context();state=start.Context(ops,ATTEMPT,ctx.snapshot(),44)
        with patch.object(start.filesystem_seal,'check_denials',side_effect=ValueError('FILESYSTEM_DENIAL_UNCONFIRMED')):
            with self.assertRaises(ValueError):start.current_seal(state,44,descriptors=lambda *a,**k:ctx.snapshot(),threads=lambda:threads(2))

    def test_source_seal_precedes_models_and_input_read_and_refreshes_before_ready(self):
        tree = ast.parse(Path(__file__).with_name('worker_child.py').read_bytes())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        calls = [(n.lineno, n.func.id if isinstance(n.func, ast.Name) else
                  n.func.attr if isinstance(n.func, ast.Attribute) else '')
                 for n in ast.walk(main) if isinstance(n, ast.Call)]
        seal = next(line for line, name in calls if name == 'bootstrap')
        load = min(line for line, name in calls if name == 'load')
        refreshed = next(line for line, name in calls if name == 'current_seal')
        reads = [line for line, name in calls if name == 'exact']
        ready_reply = min(line for line, name in calls if name == 'reply' and line > load)
        self.assertLess(seal, load);self.assertLess(load, refreshed);self.assertLess(refreshed, ready_reply)
        self.assertTrue(all(seal < line for line in reads))

    def test_parent_rejects_legacy_ready_and_accepts_only_bound_sealed_ready(self):
        for legacy in (True, False):
            worker = ModelWorker('/fixed/python', '/fixed/asr', '/fixed/intent', spawn=lambda *a, **k: Mock())
            worker._read_frame = lambda *a: json.dumps({'schema': 1, 'state': 'ready'} if legacy else
                    {'schema': 2, 'state': 'ready', 'isolation': ready(worker.attempt)}).encode()
            def close():
                worker.sealed=None;worker.process=None;return worker._cleanup_runtime()
            worker.close=close
            with patch('model_worker.os.set_blocking'):
                if legacy:
                    with self.assertRaises(Refused):worker.start()
                    self.assertIsNone(worker.sealed)
                else:
                    worker.start();self.assertEqual(worker.sealed['attempt'], worker.attempt);worker.close()

    def test_parent_never_writes_unsealed_private_request(self):
        worker = ModelWorker('/fixed/python', '/fixed/asr', '/fixed/intent')
        worker.process = Mock();worker._io = Mock()
        with self.assertRaises(Refused):worker.call('asr', b'\0' * 640, timeout_s=1)
        worker._io.assert_not_called()

    def test_worker_bootstrap_failure_never_constructs_provider(self):
        import worker_child
        asr, intent = Mock(), Mock()
        quiet = Mock();quiet.__enter__ = Mock(return_value=quiet);quiet.__exit__ = Mock(return_value=False)
        provider = types.SimpleNamespace(NativeQwenASR=asr, NativeQwenIntent=intent)
        with patch.object(worker_child.sys, 'platform', 'offline-mock'), \
             patch.object(worker_child.sys, 'argv', ['worker','/asr','/intent',ATTEMPT]), \
             patch.object(worker_child.os, 'dup', return_value=44), \
             patch.object(worker_child.os, 'dup2'), patch.object(worker_child.os, 'umask'), \
             patch.object(worker_child.resource, 'setrlimit'), patch('builtins.open', return_value=quiet), \
             patch.dict(sys.modules, {'providers':provider}), \
             patch.object(start, 'bootstrap', side_effect=ValueError('PUBLIC_BOOTSTRAP_SEAL_UNQUALIFIED')):
            with patch.object(worker_child.os,'write',side_effect=lambda fd,data:len(data)):
                self.assertEqual(worker_child.main(),1)
        asr.assert_not_called();intent.assert_not_called()

if __name__ == '__main__':unittest.main()
