"""Actual lifecycle/consumer/controller/Manager, inert capture and worker only."""
import copy
import json
import threading
import time
import types
import unittest
from unittest import mock
from . import listener_activation as native_activation
from .current_host import CurrentHostCapture
from .remote_lifecycle import RemoteMicLifecycle, CaptureSignals
from .test_consumer import good
from .test_real_manager import ActualHarness

KEY = 777


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now


class Harness:
    def __init__(self, *, mode='good', qualified=True, max_attempts=256, lifetime_s=1800):
        self.browser = ActualHarness()
        self.clock = Clock()
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.signal = None
        self.mode = mode
        self.calls = 0
        self.sequence = 0
        self.hosts = []
        self.session = RemoteMicLifecycle(self.browser.owner, self.factory, [KEY], qualified=qualified,
            max_attempts=max_attempts, lifetime_s=lifetime_s, clock=self.clock,
            consumer_clock=self.browser.clock, consumer_wait=self.browser.clock.wait)

    def factory(self, signal):
        self.signal = signal
        host, tv = types.ModuleType('source_pinned_host_fixture'), types.ModuleType('source_pinned_tv_fixture')
        tv.run = lambda *_a: None
        tv.result = lambda *_a: None
        tv.qualify = lambda *_a: None
        def run(*args, **kwargs):
            self.calls += 1
            self.entered.set()
            if self.mode == 'wait_gate':
                if not self.gate.wait(3): raise ValueError('FIXTURE_TIMEOUT')
            elif self.mode != 'immediate':
                signal.wait_stop(2)
            if self.mode == 'error': raise ValueError('PRIVATE_ERROR_SENTINEL')
            row = good()
            if self.mode == 'bad': row['tv']['streamStopObserved'] = False
            if self.mode == 'unknown-intent':
                row.update(compositionQualified=False, failure='PHYSICAL_INFERENCE_UNCONFIRMED',
                           failurePhase='qualification', exceptionClass='ValueError')
                row['server'].update(expectedIntentMatched=False, keywordsMatched=False,
                                     failure='INFERENCE', replySent=False)
                row['owner'].update(childExit=2, failure='OWNED_CHILD_UNCONFIRMED')
                row['tv'].update(complete=False, error='INFERENCE_UNCONFIRMED', inferenceAccepted=False)
                row['tv']['nativeDiagnostic'].update(childExitCode=1, captureError='INFERENCE_UNCONFIRMED',
                                                      capturePhase='transport', parentError='CHILD_FAILED')
            return row
        host.run = run
        self.hosts.append(host)
        return CurrentHostCapture(host, tv, {'fixture':'0'*64}, qualified=True)

    def key(self, down, repeat=False, sequence=None):
        self.sequence += 1
        return self.session.handle(KEY, down, repeat, sequence=self.sequence if sequence is None else sequence)

    def begin(self):
        assert self.session.start()
        assert self.key(True)
        assert self.entered.wait(1)

    def done(self):
        self.assert_reaped = self.session.reap(2)
        assert self.assert_reaped

    def close(self):
        self.gate.set()
        self.session.close(3)


class Tests(unittest.TestCase):
    def harness(self, **kwargs):
        h = Harness(**kwargs)
        self.addCleanup(h.close)
        return h

    def test_release_drives_existing_current_capture_controller_and_real_manager(self):
        h = self.harness(); h.begin()
        self.assertEqual(h.browser.calls, [])
        h.key(False); h.done()
        self.assertEqual(h.session.last['code'], 'BROWSER_NAVIGATION_ACCEPTED')
        self.assertEqual([v[0] for v in h.browser.calls], ['open','navigate'])
        self.assertEqual(h.browser.manager.status(), {'state':'ready'})
        self.assertIs(h.browser.runtime.manager, h.browser.manager)
        self.assertTrue(h.signal.snapshot()['closed'])
        self.assertFalse(h.session.status()['physicalSpeechProven'])

    def test_default_unarmed_and_protected_keys_never_fall_through(self):
        h = self.harness(qualified=False)
        self.assertFalse(h.session.start())
        for down, repeat, seq in [(True,False,1),(False,False,2),(None,False,3),(True,None,4),(True,False,True)]:
            self.assertTrue(h.session.handle(KEY,down,repeat,sequence=seq))
        self.assertFalse(h.session.handle(3,True,False,sequence=5))
        self.assertEqual(h.calls,0); self.assertIsNone(h.session.watchdog)
        with self.assertRaisesRegex(RuntimeError,'UNARMED'):
            native_activation.assemble(lambda: self.fail('native called'))

    def test_repeats_and_out_of_order_events_do_not_release_or_queue(self):
        h = self.harness(); h.begin()
        h.key(True,True); h.key(True,False)
        h.key(False,sequence=1)
        self.assertFalse(h.signal.snapshot()['released'])
        self.assertEqual(h.calls,1)
        h.key(False); h.done()
        self.assertEqual(h.session.attempts,1)

    def test_completion_without_release_never_dispatches(self):
        h = self.harness(mode='immediate'); h.begin(); h.done()
        self.assertEqual(h.browser.calls,[])
        self.assertEqual(h.session.last['code'],'CAPTURE_CANCELLED')
        self.assertFalse(h.session.quarantined)  # Full current cleanup still proved.
        h.key(True); self.assertEqual(h.session.attempts,1)  # Held key remains latched.

    def test_cancel_before_result_invalidates_consumer_then_requests_stop(self):
        h = self.harness(); h.begin(); h.session.cancel(); h.done()
        self.assertTrue(h.signal.snapshot()['cancelled'])
        self.assertEqual(h.signal.snapshot()['reason'],'cancel')
        self.assertEqual(h.browser.calls,[])
        self.assertEqual(h.session.last['code'],'CAPTURE_CANCELLED')
        self.assertFalse(h.session.quarantined)

    def test_suspend_refuses_new_capture_until_old_worker_reaped(self):
        h = self.harness(mode='wait_gate'); h.begin(); h.session.suspend()
        self.assertFalse(h.session.resume())
        h.key(True); self.assertEqual(h.calls,1)
        self.assertEqual(h.signal.snapshot()['reason'],'suspend')
        h.gate.set(); h.done()
        self.assertEqual(h.browser.calls,[])
        self.assertTrue(h.session.resume())
        self.assertEqual(h.session.status()['state'],'ready')

    def test_owner_loss_quarantines_and_never_uses_vendor_fallback(self):
        h = self.harness(); h.begin()
        h.browser.owner.runtime = None
        h.session.tick(); h.done()
        self.assertTrue(h.session.quarantined)
        self.assertFalse(h.session.resume())
        self.assertEqual(h.browser.calls,[])
        self.assertEqual(h.signal.snapshot()['reason'],'owner-lost')
        h.key(False); h.key(True); self.assertEqual(h.calls,1)

    def test_bad_stop_proof_or_failure_remains_quarantined_after_thread_exit(self):
        for mode in ('bad','error'):
            h = self.harness(mode=mode); h.begin(); h.key(False); h.done()
            self.assertTrue(h.session.quarantined)
            self.assertFalse(h.session.resume())
            self.assertFalse(h.session.close(1))
            self.assertEqual(h.browser.calls,[])
            self.assertNotIn('PRIVATE', json.dumps(h.session.status()))

    def test_hold_timeout_cancels_and_caps_event_lifetime(self):
        h = self.harness(); h.begin(); h.clock.now = 10; h.session.tick(); h.done()
        self.assertEqual(h.signal.snapshot()['reason'],'hold-timeout')
        self.assertEqual(h.browser.calls,[])
        self.assertEqual(h.session.last['code'],'CAPTURE_CANCELLED')

    def test_capture_deadline_and_stuck_worker_cannot_be_rearmed(self):
        h = self.harness(mode='wait_gate'); h.begin(); h.key(False)
        h.clock.now = 250; h.session.tick()
        self.assertTrue(h.session.quarantined)
        self.assertFalse(h.session.close(0))
        self.assertTrue(h.session.status()['active'])
        h.key(True); self.assertEqual(h.calls,1)
        h.gate.set(); h.done()
        self.assertEqual(h.browser.calls,[])
        self.assertFalse(h.session.close(1))

    def test_lifetime_clock_regression_and_exhaustion_stop_renewal(self):
        for end in (3,-1,float('nan')):
            h = self.harness(lifetime_s=3); h.begin(); h.clock.now = end; h.session.tick(); h.done()
            self.assertTrue(h.session.closing); self.assertEqual(h.browser.calls,[])
            self.assertFalse(h.session.resume())
        h = self.harness(max_attempts=1); h.begin(); h.session.cancel(); h.done()
        h.key(False); h.key(True)
        self.assertEqual(h.session.state,'exhausted'); self.assertEqual(h.calls,1)

    def test_no_second_capture_before_actual_thread_reap(self):
        h = self.harness(mode='wait_gate'); h.begin(); h.key(False); h.key(True)
        self.assertEqual(h.calls,1); self.assertEqual(h.session.attempts,1)
        h.session.cancel(); h.gate.set(); h.done()
        self.assertEqual(h.browser.calls,[])

    def test_cancel_during_real_adapter_lock_wait_prevents_manager_start(self):
        h = self.harness(); entered = threading.Event()
        original = h.browser.runtime.launch_browser
        def marked(*args,**kwargs):
            entered.set(); return original(*args,**kwargs)
        h.browser.runtime.launch_browser = marked
        h.browser.runtime.lock.acquire()
        try:
            h.begin(); h.key(False); self.assertTrue(entered.wait(1))
            end = threading.Event()
            canceller = threading.Thread(target=lambda:(h.session.suspend(),end.set()))
            canceller.start(); self.assertTrue(end.wait(.5)); canceller.join(1)
        finally: h.browser.runtime.lock.release()
        h.done()
        self.assertEqual(h.browser.manager.status(),{'state':'idle'})
        self.assertEqual(h.browser.calls,[])

    def test_current_capture_object_reuse_is_refused(self):
        h = self.harness()
        old_factory = h.session.factory
        reused = []
        def factory(signal):
            if not reused: reused.append(old_factory(signal))
            return reused[0]
        h.session.factory = factory
        h.begin(); h.session.cancel(); h.done(); h.key(False)
        h.key(True); h.done()
        self.assertTrue(h.session.quarantined); self.assertEqual(h.calls,1)

    def test_normal_current_cleanup_allows_new_independent_attempt(self):
        h = self.harness(); h.begin(); old = h.signal
        h.session.cancel(); h.done(); h.key(False)
        h.entered.clear(); h.key(True); self.assertTrue(h.entered.wait(1))
        self.assertIsNot(old,h.signal)
        self.assertFalse(old._request('release'))
        self.assertFalse(h.signal.snapshot()['released'])
        h.key(False); h.done(); self.assertEqual(h.calls,2)
        self.assertEqual([v[0] for v in h.browser.calls],['open','navigate'])

    def test_watchdog_reaps_completed_worker_without_next_key(self):
        h = self.harness(); h.begin(); h.session.cancel()
        limit = time.monotonic()+2
        while h.session.status()['active'] and time.monotonic()<limit: time.sleep(.01)
        self.assertFalse(h.session.status()['active'])
        self.assertEqual(h.session.status()['state'],'ready')

    def test_two_completed_attempts_without_manual_reap(self):
        h = self.harness()
        for _ in range(2):
            h.entered.clear()
            if not h.session.started and h.session.state == 'new': self.assertTrue(h.session.start())
            h.key(True); self.assertTrue(h.entered.wait(1)); h.session.cancel(); h.key(False)
            limit = time.monotonic()+2
            while h.session.status()['active'] and time.monotonic()<limit: time.sleep(.01)
            self.assertFalse(h.session.status()['active'])
        self.assertEqual(h.calls,2); self.assertEqual(h.browser.calls,[])

    def test_unknown_intent_with_current_full_cleanup_can_renew_without_command(self):
        h = self.harness(mode='unknown-intent'); h.begin(); h.key(False); h.done()
        self.assertFalse(h.session.quarantined)
        self.assertEqual(h.browser.calls,[])
        self.assertFalse(h.session.last['captureQualified'])
        h.mode='good'; h.entered.clear(); h.key(True); self.assertTrue(h.entered.wait(1))
        h.key(False); h.done()
        self.assertEqual(h.calls,2); self.assertEqual(len(h.browser.calls),2)

    def test_early_cancel_without_native_cleanup_evidence_is_deliberately_quarantined(self):
        h = self.harness(mode='error'); h.begin(); h.session.cancel(); h.done()
        self.assertTrue(h.session.status()['cleanupUnresolved'])
        self.assertFalse(h.session.resume()); self.assertEqual(h.browser.calls,[])

    def test_no_threads_created_by_constructor_or_invalid_configuration(self):
        h = self.harness()
        with mock.patch('voice.backend.physical_capture.remote_lifecycle.threading.Thread',side_effect=AssertionError('spawn')):
            x = RemoteMicLifecycle(h.browser.owner,h.factory,[KEY])
            self.assertFalse(x.start())
        for args in ([KEY,KEY],[True],[],list(range(9))):
            with self.assertRaises(ValueError): RemoteMicLifecycle(h.browser.owner,h.factory,args)


if __name__ == '__main__': unittest.main()
