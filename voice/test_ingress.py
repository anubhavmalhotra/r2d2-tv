import json
import threading
import unittest
from voice.ingress import Ingress, MAX_BYTES, RATE


class Tests(unittest.TestCase):
    def make(self, asr=None, deliver=None):
        self.now = 0.0; self.calls = []; self.cancelled = []
        def transcribe(view, rate, deadline):
            self.view = view
            self.assertEqual((rate, deadline), (RATE, 30.0))
            self.assertTrue(view.readonly)
            return 'open YouTube in browser'
        return Ingress('a'*32, 30.0, asr or transcribe,
                       deliver or self.calls.append, lambda: self.cancelled.append(True), lambda: self.now)

    def test_one_use_delivery_and_wipe(self):
        a = self.make(); self.assertTrue(a.feed(0, b'\1\0'*10))
        self.assertEqual(a.finish()['state'], 'delivered')
        self.assertEqual(len(self.calls), 1); self.assertEqual(bytes(self.view), b'\0'*20)
        a.finish(); self.assertFalse(a.feed(1, b'\1\0')); self.assertEqual(len(self.calls), 1)
        self.assertNotIn('YouTube', json.dumps(a.snapshot()))

    def test_duplicates_and_gaps_reject(self):
        for seq in (0, 2, True):
            a = self.make(); a.feed(0, b'\1\0'); self.assertFalse(a.feed(seq, b'\1\0'))
            a.finish(); self.assertEqual(self.calls, []); self.assertEqual(self.cancelled, [True])

    def test_pcm_shape(self):
        for pcm in (b'', b'1', 'secret', b'0'*8194):
            a = self.make(); self.assertFalse(a.feed(0, pcm)); self.assertEqual(a.finish()['state'], 'rejected')

    def test_duration_and_byte_budget(self):
        a = self.make()
        for seq in range(MAX_BYTES // 8000):
            self.assertTrue(a.feed(seq, b'\0'*8000))
        self.assertFalse(a.feed(MAX_BYTES//8000, b'\0\0'))
        a = self.make(); a.feed(0, b'\0\0'); self.now = 20
        self.assertEqual(a.tick()['state'], 'expired'); self.assertEqual(a.finish()['state'], 'expired')

    def test_cancel_without_inference(self):
        a = self.make(lambda *args: self.fail('ASR called'))
        a.feed(0, b'\1\0'); old = a._audio
        a.cancel(); a.cancel(); a.finish()
        self.assertEqual(old, b'\0\0'); self.assertEqual(self.cancelled, [True])

    def test_cancel_during_asr_discards(self):
        started = threading.Event(); release = threading.Event()
        def asr(view, *args):
            self.view = view; started.set(); release.wait(1); return 'private text'
        a = self.make(asr); a.feed(0, b'\1\0')
        worker = threading.Thread(target=a.finish); worker.start(); self.assertTrue(started.wait(1))
        a.cancel(); release.set(); worker.join(2); self.assertFalse(worker.is_alive())
        self.assertEqual(self.calls, []); self.assertEqual(a.snapshot()['state'], 'cancelled')
        self.assertEqual(bytes(self.view), b'\0\0')

    def test_late_asr_discard(self):
        def asr(*args):
            self.now = 30; return 'late command'
        a = self.make(asr); a.feed(0, b'\0\0')
        self.assertEqual(a.finish()['state'], 'expired'); self.assertEqual(self.calls, [])

    def test_asr_exception_sanitized(self):
        def asr(*args): raise RuntimeError('SECRET URL or transcript')
        a = self.make(asr); a.feed(0, b'\0\0')
        self.assertEqual(a.finish()['reason'], 'ASR_FAILED')
        self.assertNotIn('SECRET', str(a.snapshot()))

    def test_transcript_limits(self):
        for text in ('', ' ', 'x'*1025, '\0private', '\ud800', {'command':'x'}, None):
            a = self.make(lambda *args: text); a.feed(0, b'\0\0')
            self.assertEqual(a.finish()['state'], 'rejected'); self.assertEqual(self.calls, [])

    def test_delivery_unknown_no_retry(self):
        def deliver(text): self.calls.append(text); raise RuntimeError(text)
        a = self.make(deliver=deliver); a.feed(0, b'\0\0')
        self.assertEqual(a.finish()['state'], 'unknown'); a.finish()
        self.assertEqual(len(self.calls), 1); self.assertNotIn('YouTube', str(a.snapshot()))

    def test_owner_binding(self):
        for identifier, expires in [('x', 30), ('a'*32, float('nan')), ('a'*32, 31), ('a'*32, 0)]:
            with self.assertRaises(ValueError):
                Ingress(identifier, expires, lambda: None, lambda: None, lambda: None, lambda: 0)



if __name__ == '__main__': unittest.main()
