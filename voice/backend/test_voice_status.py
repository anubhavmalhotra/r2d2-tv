"""VoiceStatus: wire views, ping replay, press binding, long-poll, receipts, echo."""
import itertools
import json
import threading
import time
import unittest

from voice.backend import voice_copy as vc
from voice.backend import voice_status as vs

HOME_KEYS = ['v', 'seq', 'phase', 'ageMs', 'tone', 'word', 'headline', 'detail', 'badge', 'meter',
             'meterMs', 'dwellMs', 'mood', 'chip']
CARD_KEYS = HOME_KEYS[:-1] + ['echo']
A, B, C = 'a' * 16, 'b' * 16, 'c' * 16


class Clock:
    def __init__(self): self.now = 500.0
    def __call__(self): return self.now


def ping(press, phase, seq, *, boot='1' * 16, code=None, deadline=None, cut=False):
    return dict(schema=1, boot=boot, seq=seq, press=press, phase=phase, code=code, deadlineMs=deadline, cut=cut)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.status = vs.VoiceStatus(clock=self.clock)
        self.seq = 0
        self.addCleanup(self.status.close)

    def ping(self, press, phase, **kw):
        self.seq += 1
        return self.status.phase_ping(ping(press, phase, self.seq, **kw))

    def view(self):
        view = self.status.home_view()
        self.assertEqual(list(view), HOME_KEYS)
        raw = json.dumps(view, separators=(',', ':')).encode()
        self.assertLessEqual(len(raw), vs.HOME_MAX)
        self.assertTrue(raw.isascii())
        # Home's cross-field rules: a timed meter carries its duration, idle is empty.
        if view['meter'] is None:
            self.assertIsNone(view['meterMs'])
        elif view['meter'] != 'sweep':
            self.assertIsNotNone(view['meterMs'])
        if view['phase'] == 'idle':
            self.assertEqual((view['word'], view['headline'], view['detail'], view['badge'], view['meter']),
                             ('', '', '', None, None))
        else:
            self.assertTrue(view['headline'])
        return view

    def press(self, press=A):
        """Resident press -> listening -> released, then the upload authenticates."""
        self.ping(press, 'pressed'); self.ping(press, 'listening', deadline=5400); self.ping(press, 'released')
        return self.status.begin_utterance()


class Views(Base):
    def test_idle_view_is_exact_and_chip_bumps_seq_only_on_change(self):
        view = self.view()
        self.assertEqual(view, dict(v=1, seq=1, phase='idle', ageMs=0, tone='idle', word='', headline='',
                                    detail='', badge=None, meter=None, meterMs=None, dwellMs=None,
                                    mood='sleepy', chip='starting'))
        self.status.readiness('ready', 'ok')
        self.assertEqual((self.view()['seq'], self.view()['chip'], self.view()['tone']), (2, 'ready', 'ready'))
        self.status.readiness('grace', 'ok')   # same chip: Home is not woken
        self.assertEqual(self.view()['seq'], 2)
        self.status.readiness('ready', 'paused')
        self.assertEqual((self.view()['seq'], self.view()['chip'], self.view()['mood']), (3, 'paused', 'dim'))

    def test_every_reachable_view_fits_512_ascii_and_card_fits_1024(self):
        tools = sorted(vc.TOOLS) + [None]
        slots = sorted(vc.SLOTS) + [None]
        codes = sorted(set(vc._CODES) | vc.FAULTS | {'TV_APP_NOT_INSTALLED', 'TV_STATUS_SHOWN',
                       'BROWSER_STATUS_SHOWN', 'TV_VOLUME_ACCEPTED', 'BROWSER_NAVIGATION_ACCEPTED'})
        for tool, slot in itertools.product(tools, slots):
            for state, code in itertools.product(vc.STATES, codes):
                self.status._u = dict(token=1, press=None, candidate=None, began=0, armed=True, open=True,
                                      detail=dict(code=code, tool=tool, slot=slot, rule='repeat',
                                                  expires=self.clock() + 15), echo='\U0001f600' * 48, outcome=None)
                self.status.outcome(1, state, code)
                view = self.view()
                card = self.status.viewer_card()
                self.assertEqual(list(card), CARD_KEYS)
                self.assertLessEqual(len(json.dumps(card, separators=(',', ':'))), vs.CARD_MAX)
                self.assertIn(view['phase'], ('result', 'confirm'))

    def test_phases_meter_and_dwell(self):
        self.status.readiness('ready', 'ok')
        self.ping(A, 'pressed')
        view = self.view()
        self.assertEqual((view['phase'], view['word'], view['mood'], view['meter']), ('pressed', 'GETTING READY', 'front', None))
        self.ping(A, 'listening', deadline=5400)
        self.clock.now += .3
        view = self.view()
        self.assertEqual((view['phase'], view['meter'], view['meterMs'], view['ageMs']), ('listening', 'hold', 5400, 300))
        self.ping(A, 'released', cut=True)
        view = self.view()
        self.assertEqual((view['phase'], view['word'], view['meter'], view['detail']),
                         ('released', 'THINKING', 'sweep', 'That was the 9-second limit. Using what I heard.'))
        # A late lower phase for the same press never regresses the display.
        before = self.view()['seq']
        self.ping(A, 'listening', deadline=1000)
        self.assertEqual((self.view()['phase'], self.view()['seq']), ('released', before))

    def test_listening_without_a_deadline_shows_no_hold_meter(self):
        self.ping(A, 'pressed'); self.ping(A, 'listening')
        view = self.view()
        self.assertEqual((view['phase'], view['meter'], view['meterMs']), ('listening', None, None))

    def test_stale_pressed_and_result_reply_idle(self):
        self.ping(A, 'pressed')
        self.clock.now += 13
        self.assertEqual(self.view()['phase'], 'idle')
        token = self.press(B)
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.clock.now += 3.9
        self.assertEqual((self.view()['phase'], self.view()['dwellMs']), ('result', 4000))
        self.clock.now += .2
        view = self.view()
        self.assertEqual((view['phase'], view['headline'], view['badge']), ('idle', '', None))

    def test_a_display_that_ages_into_idle_takes_one_new_seq(self):
        # Home renders each seq once: an aged display must not keep the old seq.
        for phase, wait in (('pressed', 12.1), ('listening', 12.1), ('released', 210.1)):
            with self.subTest(phase=phase):
                self.ping(A if phase == 'pressed' else B, phase)
                before = self.view()['seq']
                self.clock.now += wait - .2
                self.assertEqual((self.view()['phase'], self.view()['seq']), (phase, before))
                self.clock.now += .2
                view = self.view()
                self.assertEqual((view['phase'], view['seq']), ('idle', before + 1))
                self.assertEqual(self.view()['seq'], before + 1)   # once, not per read
                self.assertEqual(self.status.viewer_card()['seq'], before + 1)
        token = self.press(C)
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        seq = self.view()['seq']
        self.clock.now += 4.1
        self.assertEqual((self.view()['phase'], self.view()['seq']), ('idle', seq + 1))
        # A new display starts its own expiry.
        self.ping('d' * 16, 'pressed')
        seq = self.view()['seq']
        self.clock.now += 12.1
        self.assertEqual(self.status.wait_change(seq, 0), seq + 1)

    def test_a_held_home_poll_wakes_when_its_display_ages_out(self):
        self.ping(A, 'pressed'); self.ping(A, 'listening', deadline=5400)
        seq = self.view()['seq']
        out = []
        thread = threading.Thread(target=lambda: out.append(self.status.wait_home(seq, timeout_s=5)))
        thread.start()
        end = time.monotonic() + 2
        while self.status._home_waiter is None and time.monotonic() < end:
            time.sleep(.005)
        self.clock.now += 12.1   # the fault ping was lost: nothing else will change seq
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual((out[0]['seq'], out[0]['phase'], out[0]['headline']), (seq + 1, 'idle', ''))

    def test_tv_faults_and_result_unconfirmed_never_overrides_spark(self):
        self.ping(A, 'pressed'); self.ping(A, 'fault', code='OWNER_KEY_RELEASED')
        view = self.view()
        self.assertEqual((view['phase'], view['headline'], view['badge']), ('result', 'Hold the mic a little longer.', 'nothing'))
        token = self.press(B)
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.ping(B, 'fault', code='RESULT_UNCONFIRMED')
        self.assertEqual(self.view()['badge'], 'confirmed')
        self.ping(C, 'pressed'); self.ping(C, 'listening'); self.ping(C, 'released')
        self.ping(C, 'fault', code='RESULT_UNCONFIRMED')
        self.assertEqual((self.view()['word'], self.view()['badge']), ('NOT SURE', 'unconfirmed'))

    def test_replay_order_and_boot_reset(self):
        self.assertEqual(self.status.phase_ping(ping(A, 'pressed', 5)), 'other')
        self.assertIsNone(self.status.phase_ping(ping(A, 'listening', 5)))
        self.assertIsNone(self.status.phase_ping(ping(A, 'listening', 4)))
        self.assertEqual(self.status.phase_ping(ping(A, 'listening', 6)), 'other')
        self.assertEqual(self.status.phase_ping(ping(B, 'pressed', 1, boot='2' * 16)), 'other')
        # A retired boot can never come back, even with a higher seq.
        self.assertIsNone(self.status.phase_ping(ping(A, 'pressed', 99, boot='1' * 16)))
        self.status.close()
        self.assertIsNone(self.status.phase_ping(ping(A, 'pressed', 7, boot='2' * 16)))

    def test_parse_ping_exact_shape_and_mac(self):
        row = ping(A, 'listening', 3, deadline=5000)
        key = bytearray(b'k' * 32)
        mac = vs.ping_mac(key, row)
        self.assertEqual(vs.parse_ping(dict(row, mac=mac)), (row, mac))
        canonical = json.dumps(row, sort_keys=True, separators=(',', ':')).encode()
        import hashlib, hmac
        self.assertEqual(mac, hmac.new(bytes(key), b'R2D2-voice-phase-v1\0' + canonical, hashlib.sha256).hexdigest())
        bad = [dict(row), dict(row, mac=mac, extra=1), dict(row, mac=mac, schema=2), dict(row, mac=mac, schema=True),
               dict(row, mac=mac, seq=0), dict(row, mac=mac, seq=2 ** 31 + 1), dict(row, mac=mac, seq=1.0),
               dict(row, mac=mac, boot='A' * 16), dict(row, mac=mac, press='a' * 15), dict(row, mac=mac, phase='sending'),
               dict(row, mac=mac, code='BUSY'), dict(row, mac=mac, phase='fault'),
               dict(row, mac=mac, phase='fault', code='VOICE_UNAVAILABLE'), dict(row, mac=mac, deadlineMs=10001),
               dict(row, mac=mac, deadlineMs=-1), dict(row, mac=mac, deadlineMs=10000.0),
               dict(row, mac=mac, deadlineMs=True), dict(row, mac=mac, cut=0), dict(row, mac=mac.upper()),
               dict(row, mac=mac, phase=['pressed']), dict(row, mac=mac, phase='fault', code=['BUSY']), [row]]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                vs.parse_ping(value)
        self.assertEqual(vs.parse_ping(dict(ping(A, 'fault', 4, code='BUSY'), mac=mac))[0]['code'], 'BUSY')

    def test_parse_ping_deadline_covers_the_ten_second_hold(self):
        # The TV counts listening down from its 9.2 s hold limit, clamped to this bound.
        self.assertEqual(vs.DEADLINE_MAX_MS, 10000)
        for deadline in (0, 5200, 6001, 9199, 10000, None):
            row = ping(A, 'listening', 3, deadline=deadline)
            mac = vs.ping_mac(b'k' * 32, row)
            self.assertEqual(vs.parse_ping(dict(row, mac=mac)), (row, mac))
        for deadline in (10001, 20000, -1, 9199.0, True, '9199'):
            row = ping(A, 'listening', 3, deadline=deadline)
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                vs.parse_ping(dict(row, mac=vs.ping_mac(b'k' * 32, row)))
        # A full-length countdown reaches Home and the viewer as the hold meter.
        self.ping(A, 'pressed'); self.ping(A, 'listening', deadline=10000)
        view = self.view()
        self.assertEqual((view['phase'], view['meter'], view['meterMs']), ('listening', 'hold', 10000))
        self.ping(A, 'released', cut=True)
        self.assertEqual(self.view()['detail'], 'That was the 9-second limit. Using what I heard.')

    def test_resident_javascript_mac_is_accepted(self):
        import shutil, subprocess
        node = shutil.which('node')
        if node is None:
            self.skipTest('node absent')
        # The resident's own canonical form: sorted-key replacer over the unsigned row.
        script = ("const crypto=require('crypto');const key=Buffer.alloc(32,7);"
                  "const value={schema:1,boot:'0123456789abcdef',seq:9,press:'fedcba9876543210',phase:'released',"
                  "code:null,deadlineMs:null,cut:true};const names=Object.keys(value).sort();"
                  "value.mac=crypto.createHmac('sha256',key).update('R2D2-voice-phase-v1\\0','ascii')"
                  ".update(JSON.stringify(value,names),'ascii').digest('hex');"
                  "process.stdout.write(JSON.stringify(value,[...names,'mac'].sort()));")
        raw = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=10, check=True).stdout
        row, mac = vs.parse_ping(json.loads(raw))
        self.assertEqual(vs.ping_mac(bytearray([7] * 32), row), mac)
        self.assertEqual(self.status.phase_ping(row), 'other')

    def test_confirmation_pending_then_confirming_listen(self):
        token = self.press(A)
        self.status.agent_outcome('refused', 'CONFIRMATION_PENDING', 'tv.volume', 'volume:up:4', 'ceiling', 15000)
        self.clock.now += .5
        self.status.outcome(token, 'refused', 'CONFIRMATION_PENDING')
        view = self.view()
        self.assertEqual((view['phase'], view['headline'], view['badge'], view['meter'], view['meterMs'], view['dwellMs']),
                         ('confirm', 'Turn the volume up 4?', 'waiting', 'countdown', 13500, 14500))
        self.ping(B, 'pressed'); self.ping(B, 'listening', deadline=5000)
        self.assertEqual((self.view()['word'], self.view()['detail']), ('CONFIRMING', 'Say “yes” or “no”, then let go.'))
        token = self.status.begin_utterance()
        self.status.outcome(token, 'verified', 'TV_VOLUME_VERIFIED')
        self.ping(C, 'pressed'); self.ping(C, 'listening')
        self.assertEqual(self.view()['word'], 'LISTENING')

    def test_expired_confirmation_replies_idle_and_missing_detail_uses_safe_ttl(self):
        token = self.press(A)
        self.status.outcome(token, 'refused', 'CONFIRMATION_PENDING')
        view = self.view()
        self.assertEqual((view['headline'], view['dwellMs'], view['meterMs']), ('Should I go ahead?', 14000, 13000))
        self.clock.now += 14.1
        self.assertEqual(self.view()['phase'], 'idle')


class Pipeline(Base):
    def test_binding_latest_released_press_within_window(self):
        token = self.press(A)
        self.status.armed(token)
        self.assertEqual(self.view()['phase'], 'released')
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.assertTrue(self.status.receipt(A, timeout_s=0)['done'])
        # Same press never binds twice; an old release (over 15 s) never binds.
        token = self.status.begin_utterance()
        self.status.outcome(token, 'refused', 'NEGATED_COMMAND')
        self.assertEqual(self.status.receipt(A, timeout_s=0)['toast'], 'Done')
        self.ping(B, 'pressed'); self.ping(B, 'listening'); self.ping(B, 'released')
        self.clock.now += 15.1
        token = self.status.begin_utterance()
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.assertFalse(self.status.receipt(B, timeout_s=0)['done'])
        # A newer press that has not released yet is not bound to this upload.
        self.ping(C, 'pressed')
        token = self.status.begin_utterance()
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.assertFalse(self.status.receipt(C, timeout_s=0)['done'])

    def test_faults_never_take_or_keep_the_upload_binding(self):
        # A busy or tap hint is its own press: it never displaces the press in flight.
        self.ping(A, 'pressed'); self.ping(A, 'listening'); self.ping(A, 'released')
        self.ping(B, 'fault', code='BUSY')
        token = self.status.begin_utterance()
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.assertEqual(self.status.receipt(A, timeout_s=0)['done'], True)
        self.assertEqual(self.status.receipt(B, timeout_s=0)['toast'], 'Voice is busy, try again in a moment')
        # A press that faulted (its upload failed) never binds a later upload.
        self.ping(C, 'pressed'); self.ping(C, 'listening'); self.ping(C, 'released')
        self.ping(C, 'fault', code='TRANSPORT_FAILED')
        token = self.status.begin_utterance()
        self.status.outcome(token, 'refused', 'RATE_LIMITED')
        self.assertEqual(self.status.receipt(C, timeout_s=0)['toast'], "Can't reach Spark. Nothing was sent")

    def test_late_release_ping_binds_the_pending_press_without_regress(self):
        self.ping(A, 'pressed'); self.ping(A, 'listening', deadline=5000)
        token = self.status.begin_utterance()
        self.status.armed(token)
        self.status.doing('tv.mute', 'mute:on')
        self.ping(A, 'released')
        view = self.view()
        self.assertEqual((view['phase'], view['headline']), ('doing', 'Muting...'))
        self.status.agent_outcome('verified', 'TV_MUTE_VERIFIED', 'tv.mute', 'mute:on')
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.assertEqual(self.status.receipt(A, timeout_s=0)['toast'], 'Muted')

    def test_release_ping_after_a_fast_outcome_still_records_the_receipt(self):
        self.ping(A, 'pressed'); self.ping(A, 'listening')
        token = self.status.begin_utterance()
        self.status.outcome(token, 'refused', 'VOICE_WARMING_UP')
        self.ping(A, 'released')
        self.assertEqual(self.view()['word'], 'WARMING UP')
        self.assertEqual(self.status.receipt(A, timeout_s=0)['toast'],
                         'Voice model warming up - try again in a moment')

    def test_viewer_cards_pass_the_channel_validator(self):
        try:
            from voice.backend.browser_channel import valid_card
        except ImportError:
            self.skipTest('viewer channel validator absent')
        cards = [self.status.viewer_card()]
        token = self.press(A)
        cards.append(self.status.viewer_card())
        self.status.doing('browser.web_search', None)
        cards.append(self.status.viewer_card())
        self.status.stage_echo('cat videos \U0001f600')
        self.status.agent_outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED', 'browser.web_search', None)
        self.status.outcome(token, 'accepted', 'BROWSER_NAVIGATION_ACCEPTED')
        cards.append(self.status.viewer_card())
        token = self.press(B)
        self.status.agent_outcome('refused', 'CONFIRMATION_PENDING', 'control.back', None, 'repeat', 15000)
        self.status.outcome(token, 'refused', 'CONFIRMATION_PENDING')
        cards.append(self.status.viewer_card())
        self.ping(C, 'pressed'); self.ping(C, 'listening', deadline=5000)
        cards.append(self.status.viewer_card())
        self.ping(C, 'fault', code='CAPTURE_LOST')
        cards.append(self.status.viewer_card())
        self.assertEqual(cards[3]['echo'], 'cat videos \U0001f600')
        for card in cards:
            self.assertTrue(valid_card(card), card)

    def test_merge_agent_detail_only_when_codes_match(self):
        token = self.press(A)
        self.status.agent_outcome('verified', 'TV_FOREGROUND_VERIFIED', 'tv.open_app', 'app:netflix')
        self.status.outcome(token, 'refused', 'REQUEST_EXPIRED')   # controller override at E10
        view = self.view()
        self.assertEqual((view['headline'], view['badge']), ('Not sure that worked.', 'unconfirmed'))
        token = self.press(B)
        self.status.agent_outcome('verified', 'TV_FOREGROUND_VERIFIED', 'tv.open_app', 'app:netflix')
        self.status.outcome(token, 'verified', 'TV_FOREGROUND_VERIFIED')
        self.assertEqual(self.view()['headline'], 'Netflix is open.')

    def test_invalid_pipeline_values_are_ignored_or_sanitized(self):
        token = self.press(A)
        self.status.doing('rm -rf', 'app:netflix; drop')
        self.assertEqual(self.view()['headline'], 'Working on it...')
        self.status.agent_outcome('verified', 'TV_FOREGROUND_VERIFIED', {'x': 1}, ['slot'], 'never', 99999)
        self.status.outcome(token, 'verified', 'lowercase code')
        self.assertEqual(self.view()['headline'], 'Voice isn’t available right now.')
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')   # the utterance is already closed
        self.assertEqual(self.view()['headline'], 'Voice isn’t available right now.')
        self.status.armed(12345); self.status.thinking(None); self.status.failed('x')
        self.status.note_viewer_delivered('3'); self.status.readiness(['ready'], None)

    def test_failed_utterance_is_unknown_only_after_arm(self):
        token = self.press(A)
        self.status.failed(token)
        self.assertEqual(self.view()['headline'], 'Voice isn’t available right now.')
        token = self.press(B)
        self.status.armed(token)
        self.status.failed(token)
        self.assertEqual(self.view()['badge'], 'unconfirmed')
        token = self.press(C)
        self.status.outcome(token, 'verified', 'TV_MUTE_VERIFIED')
        self.status.failed(token)   # a send failure after the outcome changes nothing
        self.assertEqual(self.view()['badge'], 'confirmed')

    def test_echo_reaches_only_the_viewer_card_for_searches_with_ttl(self):
        token = self.press(A)
        self.status.stage_echo('lo-fi   music')
        self.status.agent_outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED', 'browser.youtube_search', None)
        self.status.outcome(token, 'accepted', 'BROWSER_NAVIGATION_ACCEPTED')
        self.assertEqual(self.status.viewer_card()['echo'], 'lo-fi music')
        self.assertNotIn('lo-fi', json.dumps(self.status.home_view()))
        self.clock.now += 3.9
        self.assertEqual(self.status.viewer_card()['echo'], 'lo-fi music')
        self.clock.now += .2   # past the 4 s dwell: idle cards carry no echo
        self.assertIsNone(self.status.viewer_card()['echo'])
        token = self.press(B)
        self.status.stage_echo('x' * 60)
        self.status.agent_outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED', 'browser.web_search', None)
        self.status.outcome(token, 'accepted', 'BROWSER_NAVIGATION_ACCEPTED')
        self.assertEqual(self.status.viewer_card()['echo'], 'x' * 45 + '...')
        for text, tool in (('kept', 'browser.open_site'), ('a​b', 'browser.web_search'), ('a\nb', 'browser.web_search')):
            token = self.press(C)
            self.status.stage_echo(text)
            self.status.agent_outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED', tool, None)
            self.status.outcome(token, 'accepted', 'BROWSER_NAVIGATION_ACCEPTED')
            self.assertIsNone(self.status.viewer_card()['echo'], text)
        self.status.close()
        self.assertIsNone(self.status.viewer_card())

    def test_status_feedback_contract(self):
        feedback = vs.StatusFeedback(self.status)
        self.assertIs(feedback.show('CONFIRMATION_PENDING', 'r2d2_home', echo='tv.volume'), True)
        self.assertIs(feedback.can_confirm('unknown', free_text=False), True)
        self.assertIs(feedback.can_confirm('browser_other', free_text=True), False)
        self.assertIs(vs.StatusFeedback(None, phase='P1b').can_confirm('unknown', free_text=False), False)
        class Broken:
            def stage_echo(self, text): raise RuntimeError('boom')
        self.assertIs(vs.StatusFeedback(Broken()).show('BROWSER_NAVIGATION_ACCEPTED', 'x', echo='q'), True)
        self.assertIs(vs.StatusFeedback(None).show('X', 'y'), True)
        # Showing a pending card publishes nothing: only the final outcome does.
        before = self.view()['seq']
        feedback.show('TV_FOREGROUND_VERIFIED', 'r2d2_home')
        self.assertEqual(self.view()['seq'], before)


class Waiters(Base):
    def run_wait(self, since, **kw):
        out = []
        thread = threading.Thread(target=lambda: out.append(self.status.wait_home(since, **kw)))
        thread.start()
        return thread, out

    def settle(self, predicate):
        end = time.monotonic() + 2
        while not predicate() and time.monotonic() < end:
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_immediate_replies_for_zero_and_mismatch(self):
        begin = time.monotonic()
        self.assertEqual(self.status.wait_home(0)['seq'], 1)
        self.assertEqual(self.status.wait_home(77)['seq'], 1)
        self.status.readiness('ready', 'ok')
        self.assertEqual(self.status.wait_home(1)['seq'], 2)
        self.assertLess(time.monotonic() - begin, .2)

    def test_waiter_wakes_on_change_and_records_home_ack(self):
        thread, out = self.run_wait(1)
        self.settle(lambda: self.status._home_waiter is not None)
        self.assertEqual(self.status.surface(), 'home')
        self.ping(A, 'pressed')
        thread.join(1)
        self.assertEqual((out[0]['seq'], out[0]['phase']), (2, 'pressed'))
        self.assertEqual(self.status._home_seq, 1)

    def test_one_waiter_newer_request_releases_the_older(self):
        first, out1 = self.run_wait(1)
        self.settle(lambda: self.status._home_waiter is not None)
        second, out2 = self.run_wait(1)
        first.join(1)
        self.assertFalse(first.is_alive()); self.assertEqual(out1[0]['seq'], 1)
        self.assertTrue(second.is_alive())
        self.status.close()
        second.join(1)
        self.assertFalse(second.is_alive()); self.assertEqual(out2[0]['seq'], 1)

    def test_gone_client_returns_none_and_hold_is_bounded(self):
        calls = []
        thread, out = self.run_wait(1, gone=lambda: calls.append(1) or len(calls) > 1)
        thread.join(3)
        self.assertEqual(out, [None]); self.assertIsNone(self.status._home_waiter)
        begin = time.monotonic()
        self.assertEqual(self.status.wait_home(1, timeout_s=.3)['seq'], 1)
        self.assertGreaterEqual(time.monotonic() - begin, .25)
        self.assertIs(self.status.wait_home(1, timeout_s=.1, gone=lambda: 1 / 0), None)

    def test_wait_change_paces_the_serve_loop(self):
        begin = time.monotonic()
        self.assertEqual(self.status.wait_change(1, .1), 1)
        self.assertGreaterEqual(time.monotonic() - begin, .08)
        threading.Timer(.05, lambda: self.ping(A, 'pressed')).start()
        self.assertEqual(self.status.wait_change(1, 2), 2)
        self.assertEqual(self.status.wait_change(None, 2), 2)


class Receipts(Base):
    def test_shape_and_not_done(self):
        receipt = self.status.receipt(A, timeout_s=0)
        self.assertEqual(receipt, dict(schema=1, press=A, done=False, toast=None, shown=False))
        self.assertLessEqual(len(json.dumps(receipt, separators=(',', ':'))), vs.RECEIPT_MAX)
        with self.assertRaises(ValueError):
            self.status.receipt('A' * 16)

    def test_toast_for_third_party_without_waiting(self):
        token = self.press(A)
        self.status.agent_outcome('verified', 'TV_VOLUME_VERIFIED', 'tv.volume', 'volume:up:2')
        self.status.outcome(token, 'verified', 'TV_VOLUME_VERIFIED')
        begin = time.monotonic()
        self.assertEqual(self.status.receipt(A), dict(schema=1, press=A, done=True, toast='Volume up 2', shown=False))
        self.assertLess(time.monotonic() - begin, .2)

    def test_open_app_verified_has_no_toast(self):
        token = self.press(A)
        self.status.agent_outcome('verified', 'TV_FOREGROUND_VERIFIED', 'tv.open_app', 'app:tubi')
        self.status.outcome(token, 'verified', 'TV_FOREGROUND_VERIFIED')
        self.assertEqual(self.status.receipt(A), dict(schema=1, press=A, done=True, toast=None, shown=False))

    def test_home_ack_suppresses_the_toast(self):
        self.status.wait_home(0)
        token = self.press(A)
        self.status.outcome(token, 'refused', 'NEGATED_COMMAND')
        seq = self.status.home_view()['seq']
        out = []
        thread = threading.Thread(target=lambda: out.append(self.status.receipt(A)))
        thread.start()
        time.sleep(.05)
        self.assertTrue(thread.is_alive())   # Home was seen: wait for its next poll
        self.status.wait_home(seq, timeout_s=0)
        thread.join(1)
        self.assertEqual(out, [dict(schema=1, press=A, done=True, toast=None, shown=True)])

    def test_viewer_ack_and_hold_limit(self):
        self.status.note_viewer_delivered(1)
        self.assertEqual(self.status.surface(), 'viewer')
        token = self.press(A)
        self.status.outcome(token, 'rejected', 'NAVIGATION_REJECTED')
        begin = time.monotonic()
        self.assertEqual(self.status.receipt(A)['toast'], "The browser didn't accept that")
        self.assertGreaterEqual(time.monotonic() - begin, 1.4)
        self.assertLess(time.monotonic() - begin, 1.8)
        threading.Timer(.1, lambda: self.status.note_viewer_delivered(self.status.home_view()['seq'])).start()
        self.assertEqual(self.status.receipt(A), dict(schema=1, press=A, done=True, toast=None, shown=True))
        self.status.note_viewer_delivered(10 ** 6)   # a seq not yet issued is no ack
        self.assertLess(self.status._viewer_seq, 10 ** 6)

    def test_waits_for_outcome_then_returns(self):
        self.press(A)
        token = self.status._u['token']
        threading.Timer(.1, lambda: self.status.outcome(token, 'refused', 'RATE_LIMITED')).start()
        self.assertEqual(self.status.receipt(A)['toast'], 'Too many requests, wait a moment')

    def test_surface_ages_out_after_three_seconds(self):
        self.status.wait_home(0)
        self.assertEqual(self.status.surface(), 'home')
        self.clock.now += 3.1
        self.assertEqual(self.status.surface(), 'other')
        self.status.note_viewer_delivered(1)
        self.assertEqual(self.ping(A, 'pressed'), 'viewer')


if __name__ == '__main__':
    unittest.main()
