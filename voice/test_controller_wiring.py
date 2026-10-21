"""Actual offline ingress/controller integration; all model/runtime calls fake."""
import json
import unittest

from voice.controller import Controller, BrowserObservation, Outcome
from voice.ingress import Ingress


class Model:
    def __init__(self): self.calls = 0
    def interpret(self, transcript, *, timeout_s):
        self.calls += 1
        return json.dumps({'action': 'browser_open', 'destination': 'youtube'})


class Runtime:
    browser_enabled = True
    def __init__(self): self.calls = []; self.url = None
    def launch_browser(self, op, *, timeout_s): self.calls.append('start'); return 'accepted'
    def navigate(self, session, url, op, *, timeout_s):
        self.calls.append(('navigate', url)); self.url = url; return 'accepted'
    def browser_observation(self, *, timeout_s):
        return BrowserObservation('b'*32, True, True, True, self.url)


class Integration(unittest.TestCase):
    def setup_request(self, runtime):
        model = Model(); controller = Controller(model, runtime, clock=lambda: 0)
        lease = controller.arm(); outcomes = []
        capture = Ingress(lease.nonce, lease.expires,
            lambda *_: 'open YouTube in browser',
            lambda text: outcomes.append(controller.execute(lease, text)),
            lambda: controller.cancel(lease), clock=lambda: 0)
        capture.feed(0, b'\0\0')
        return capture, controller, lease, outcomes, model

    def test_exact_owned_youtube_mapping_once(self):
        runtime = Runtime(); capture, c, lease, outcomes, model = self.setup_request(runtime)
        capture.finish(); capture.finish()
        self.assertEqual(outcomes, [Outcome('verified', 'BROWSER_TARGET_PRESENTED')])
        self.assertEqual(runtime.calls, ['start', ('navigate', 'https://www.youtube.com')])
        self.assertEqual(model.calls, 1); self.assertTrue(lease.consumed)

    def test_no_live_bridge_is_explicitly_refused(self):
        capture, _, _, outcomes, _ = self.setup_request(None); capture.finish()
        self.assertEqual(outcomes, [Outcome('refused', 'BROWSER_BRIDGE_UNQUALIFIED')])
        # Ingress delivery is not command success; controller outcome is separate.
        self.assertEqual(capture.snapshot()['state'], 'delivered')

    def test_old_capture_cannot_cancel_new_lease(self):
        capture, c, old, _, _ = self.setup_request(Runtime()); new = c.arm()
        capture.cancel(); self.assertIs(c.active, new); self.assertTrue(old.consumed)


if __name__ == '__main__': unittest.main()
