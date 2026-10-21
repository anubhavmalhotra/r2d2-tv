import threading
import time
import unittest
from types import SimpleNamespace

from browser_channel import TrialControlHost, channel_pair
from voice_card_push import EXCHANGE_S, push_voice_card

# Synthetic receipt; never a real utterance.
CARD = dict(v=1, seq=7, phase='result', ageMs=200, tone='sent', word='SENT',
            headline='Searching YouTube...', detail='', badge='sent', meter=None,
            meterMs=None, dwellMs=4000, mood='verified', echo='lo-fi music')
SESSION = 'a' * 32


class Status:
    def __init__(self, card=CARD):
        self.card, self.delivered, self.reads = card, [], 0
    def viewer_card(self):
        self.reads += 1
        if isinstance(self.card, Exception): raise self.card
        return self.card
    def note_viewer_delivered(self, seq):
        self.delivered.append(seq)


class Client:
    def __init__(self, result, session=SESSION):
        self.session, self.result, self.cards = session, result, []
    def voice_card(self, card, *, timeout_s):
        self.cards.append((card, timeout_s))
        if isinstance(self.result, Exception): raise self.result
        return self.result


def manager(binding):
    def current_client(*, timeout_s):
        if isinstance(binding, Exception): raise binding
        return binding
    return SimpleNamespace(control_spawn=SimpleNamespace(current_client=current_client))


class PushTests(unittest.TestCase):
    def test_no_browser_session_reads_nothing(self):
        for value in (None, SimpleNamespace(), SimpleNamespace(control_spawn=None), manager(None),
                      manager(RuntimeError('lock')), manager((SESSION,)), manager((SESSION, object()))):
            status = Status()
            self.assertFalse(push_voice_card(value, status))
            self.assertEqual((status.reads, status.delivered), (0, []))

    def test_validated_card_is_pushed_and_delivery_noted(self):
        client = Client({'delivered': 7}); status = Status()
        self.assertTrue(push_voice_card(manager((SESSION, client)), status))
        self.assertEqual(client.cards, [(CARD, EXCHANGE_S)]); self.assertEqual(status.delivered, [7])
        client.result = {'delivered': None}; status.card = None
        self.assertTrue(push_voice_card(manager((SESSION, client)), status))
        self.assertEqual(client.cards[-1], (None, EXCHANGE_S)); self.assertEqual(status.delivered, [7])

    def test_malformed_card_is_never_sent(self):
        for card in (dict(CARD, echo='a\u202eb'), dict(CARD, chip='ready'), 'card', [CARD], RuntimeError('x')):
            client = Client({'delivered': 7}); status = Status(card)
            self.assertFalse(push_voice_card(manager((SESSION, client)), status))
            self.assertEqual((client.cards, status.delivered), ([], []))

    def test_refusals_never_raise_or_note_delivery(self):
        for result in (None, {}, {'delivered': 7, 'extra': 1}, RuntimeError('channel')):
            status = Status()
            self.assertFalse(push_voice_card(manager((SESSION, Client(result))), status))
            self.assertEqual(status.delivered, [])
        status = Status()
        self.assertFalse(push_voice_card(manager((SESSION, Client({'delivered': 7}, session='b' * 32))), status))
        self.assertEqual(status.reads, 0)

    def test_real_channel_round_trip(self):
        client, sock = channel_pair(SESSION)
        trial = SimpleNamespace(lock=threading.RLock(), seen=[],
                                voice_card_exchange=lambda body: trial.seen.append(body) or {'delivered': 5})
        host = TrialControlHost(sock, trial)
        thread = threading.Thread(target=host.serve, kwargs={'lifetime_s': 5}, daemon=True); thread.start()
        try:
            status = Status()
            started = time.monotonic()
            self.assertTrue(push_voice_card(manager((SESSION, client)), status))
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual((trial.seen, status.delivered), ([{'card': CARD}], [5]))
            self.assertEqual((client.regular_requests, host.regular_requests, host.requested_at), (0, 0, None))
        finally:
            client.close(); host.close(); thread.join(1)


if __name__ == '__main__': unittest.main()
