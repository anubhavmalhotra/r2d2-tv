"""Model adapter interface: the real voice/backend/local_qwen.py drops in
unchanged (P1a legacy contract); registry-contract models must carry the pinned
per-phase prompt hash. No sockets: LocalQwen gets an injected fake connection."""
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import unittest

from agent.errors import Outcome, Refused
from agent.models import FakeModel, LegacyIntentAdapter, ScriptedModel, check_pinned
from agent.registry import prompt_sha256
from tests.support import Case, call, scripted
from tests.test_agent import mutations

CONTROLLER_DIR = Path(os.environ.get('R2D2_CONTROLLER_DIR') or
                      Path(__file__).resolve().parents[3] / 'backend')
# Pinned: a changed local_qwen.py must be re-checked against the adapter contract.
LOCAL_QWEN_SHA256 = '26fb934d5c753e3c38212d06e875f99a8001d33e832e5c7db8cc927a0524dac5'


class FakeSock:
    def shutdown(self, how):
        pass


class Connection:
    """Stands in for http.client.HTTPConnection (as in voice/test_controller.py)."""
    def __init__(self, content):
        self.content, self.body, self.closed, self.sock = content, None, False, FakeSock()

    def connect(self):
        pass

    def request(self, method, path, *, body, headers):
        self.body = json.loads(body)

    def getresponse(self):
        return self

    status = 200

    def read(self, cap):
        return json.dumps({'choices': [{'finish_reason': 'stop',
                                        'message': {'role': 'assistant', 'content': self.content}}]}).encode()[:cap]

    def close(self):
        self.closed = True


def load_local_qwen():
    """Import the shipped, SHA-pinned local_qwen.py read-only, then unload it."""
    path = CONTROLLER_DIR / 'local_qwen.py'
    if not path.exists():
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() != LOCAL_QWEN_SHA256:
        raise AssertionError('local_qwen.py changed; re-review the adapter contract')
    saved = {name: sys.modules.pop(name, None) for name in ('controller', 'local_qwen')}
    sys.path.insert(0, str(CONTROLLER_DIR))
    try:
        sys.dont_write_bytecode = True
        return importlib.import_module('local_qwen')
    finally:
        sys.path.remove(str(CONTROLLER_DIR))
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module


class LocalQwenDropIn(Case):
    def setUp(self):
        super().setUp()
        self.module = load_local_qwen()
        if self.module is None:
            self.skipTest('voice/backend not present')

    def test_interface_signature_matches(self):
        params = inspect.signature(self.module.LocalQwen.interpret).parameters
        self.assertEqual([(n, p.kind.name) for n, p in params.items()],
                         [('self', 'POSITIONAL_OR_KEYWORD'), ('transcript', 'POSITIONAL_OR_KEYWORD'),
                          ('timeout_s', 'KEYWORD_ONLY')])
        for cls in (FakeModel, ScriptedModel, LegacyIntentAdapter):
            self.assertEqual(list(inspect.signature(cls.interpret).parameters), list(params))

    def test_unchanged_local_qwen_through_legacy_adapter_in_p1a(self):
        config = {'endpoint': 'http://127.0.0.1:8123/v1/chat/completions', 'model': 'configured-local-alias'}
        connection = Connection(json.dumps({'action': 'web_search', 'query': 'space news'}))
        qwen = self.module.LocalQwen(config, connection=lambda *a, **k: connection)
        e = self.env('P1a', model=LegacyIntentAdapter(qwen))
        e.channel.open_session()
        self.assertEqual(self.say(e, 'search the web for space news'),
                         Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED'))
        self.assertEqual(mutations(e)[0][2], 'https://www.google.com/search?q=space%20news')
        self.assertTrue(connection.closed)
        self.assertEqual(connection.body['messages'][0]['content'], self.module.SYSTEM)  # prompt untouched

    def test_legacy_title_intent_stays_disabled_and_failures_are_model_unavailable(self):
        config = {'endpoint': 'http://127.0.0.1:8123/v1/chat/completions', 'model': 'alias'}
        title = Connection(json.dumps({'action': 'tv_content_open', 'provider': 'apple_tv', 'title': 'Ted Lasso'}))
        e = self.env('P1a', model=LegacyIntentAdapter(self.module.LocalQwen(config, connection=lambda *a, **k: title)))
        self.assertEqual(self.say(e, 'open ted lasso on apple tv'), Outcome('refused', 'TOOL_DISABLED'))
        broken = Connection('{"action":"shell","command":"ls"}')
        e = self.env('P1a', model=LegacyIntentAdapter(self.module.LocalQwen(config, connection=lambda *a, **k: broken)))
        self.assertEqual(self.say(e, 'run ls'), Outcome('refused', 'MODEL_UNAVAILABLE'))
        e = self.env('P1a', model=LegacyIntentAdapter(self.module.LocalQwen(config)))   # would need a socket
        self.assertEqual(self.say(e, 'open youtube in the browser'), Outcome('refused', 'MODEL_UNAVAILABLE'))


class Pinning(Case):
    def test_registry_models_must_carry_the_phase_prompt_hash(self):
        with self.assertRaisesRegex(Refused, 'MODEL_CONFIG'):
            self.env('P2', model=ScriptedModel(call('control.back')))            # no declared prompt
        with self.assertRaisesRegex(Refused, 'MODEL_CONFIG'):
            self.env('P3', model=ScriptedModel(call('control.back'), prompt_sha=prompt_sha256('P2')))
        with self.assertRaisesRegex(Refused, 'MODEL_CONFIG'):
            check_pinned(LegacyIntentAdapter(FakeModel()), 'P1b', legacy_allowed=False)
        self.env('P2', model=scripted(call('control.back'), 'P2'))

    def test_model_failures_never_dispatch(self):
        for model in (scripted(None, 'P2', exc=TimeoutError()), scripted(None, 'P2'), scripted(b'{}', 'P2'),
                      scripted({'tool': 'control.back', 'args': {}}, 'P2')):
            e = self.env('P2', model=model)
            e.channel.open_session()
            with self.subTest(model=model.raw):
                self.assertIn(self.say(e, 'go back'), (Outcome('refused', 'MODEL_UNAVAILABLE'),
                                                       Outcome('refused', 'TOOL_SCHEMA')))
                self.assertEqual(mutations(e), [])


if __name__ == '__main__':
    unittest.main()
