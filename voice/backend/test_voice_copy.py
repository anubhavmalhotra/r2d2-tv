"""Closed voice copy table: every reachable string, its charset and its limits."""
import itertools
from pathlib import Path
import re
import unittest

from voice.backend import voice_copy as vc

# Codes the agent, controller, ingress, resident and TV can produce, plus junk.
CODES = sorted(set('''
REQUEST_NOT_ARMED REQUEST_EXPIRED AGENT_BUSY AGENT_PAUSED AUDIT_UNAVAILABLE TRANSCRIPT_SCOPE
MODEL_UNAVAILABLE MODEL_CONFIG TOOL_SCHEMA TOOL_DISABLED RATE_LIMITED UNSUPPORTED_COMMAND
NEGATED_COMMAND NOTHING_PENDING PENDING_CANCELLED EXECUTION_UNCONFIRMED CONFIRMATION_PENDING
CONFIRMATION_DECLINED CONFIRMATION_EXPIRED CONFIRMATION_SURFACE_UNAVAILABLE BROWSER_NOT_OPENED
BROWSER_NAVIGATION_ACCEPTED NAVIGATION_REJECTED NAVIGATION_UNCONFIRMED BROWSER_HISTORY_ACCEPTED
HISTORY_REJECTED HISTORY_UNCONFIRMED BROWSER_MEDIA_VERIFIED BROWSER_MEDIA_NO_VIDEO MEDIA_REJECTED
MEDIA_UNCONFIRMED BROWSER_SESSION_ENDED END_REJECTED END_UNCONFIRMED BROWSER_STATUS_SHOWN
TV_CHANNEL_UNAVAILABLE TV_TOOL_UNAVAILABLE TV_PERMISSION_DENIED TV_APP_NOT_INSTALLED
TV_LAUNCH_REJECTED TV_FOREGROUND_VERIFIED TV_VOLUME_ACCEPTED TV_VOLUME_VERIFIED TV_MUTE_VERIFIED
TV_MEDIA_ACCEPTED TV_REQUEST_REJECTED TV_UNCONFIRMED TV_STATUS_SHOWN DEEPLINK_UNQUALIFIED
VOICE_WARMING_UP VOICE_UNAVAILABLE VOICE_BUSY CONTROLLER_BUSY VOICE_CANCELLED INPUT_SCHEMA
INPUT_SIZE INPUT_EXPIRED INPUT_DISCONNECTED INPUT_SEQUENCE DEVICE_NOT_AUTHENTICATED
AUDIO_EMPTY_OR_SHORT ASR_OUTPUT_INVALID ASR_UNAVAILABLE ASR_UNQUALIFIED TRANSPORT_UNQUALIFIED
BROWSER_BRIDGE_UNQUALIFIED SOMETHING_NEW_REJECTED SOMETHING_NEW
'''.split()) | vc.FAULTS)
TOOLS = sorted(vc.TOOLS) + [None, 'tv.unknown']
SLOTS = sorted(vc.SLOTS) + [None, 'app:evil', 'volume:up:9', 'zz', {'kind': 'text'}]
TEXT_CHARS = set(map(chr, range(32, 127))) | set('’‘“”')
ROOT = Path(__file__).resolve().parents[2]
# Every source that puts text on the TV: Home, the TinyBrowser open page, the
# Manager's page, the Chromium viewer, and the Spark side of the voice cards.
UI_SOURCES = ('tv/home/*.in', 'spark/open.js.in', 'spark/manager.py.in', 'spark/runtime/viewer.js.in',
              'spark/runtime/build_viewer.py.in', 'voice/backend/voice_copy.py', 'voice/backend/voice_status.py')
# U+2026 in any spelling. The LG TV's fallback sans-serif draws it mid-line, so
# an ellipsis on screen is always three periods.
ELLIPSIS = re.compile(r'\u2026|\\u2026|\\u\{0*2026\}|\\N\{HORIZONTAL ELLIPSIS\}|&hellip;|&#0*8230;|&#x0*2026;', re.I)
# Receivers' charset allow-lists ([ -~...]) still admit U+2026 from older Spark builds.
VALIDATOR_CLASS = re.compile(r'\[ -~[^\]\n]*\]')


class CopyTable(unittest.TestCase):
    def every_card(self):
        for tool, slot in itertools.product(TOOLS, SLOTS):
            for state, code in itertools.product(vc.STATES, CODES):
                yield 'result', dict(state=state, code=code, tool=tool, slot=slot)
            for rule in vc.RULES + (None,):
                yield 'confirm', dict(state='refused', code='CONFIRMATION_PENDING', tool=tool, slot=slot, rule=rule)
            yield 'doing', dict(tool=tool, slot=slot)
        for chip in vc.CHIPS:
            yield 'idle', dict(chip=chip)
        for phase in ('pressed', 'listening', 'released'):
            for cut, confirming in itertools.product((False, True), repeat=2):
                yield phase, dict(cut=cut, confirming=confirming)

    def test_every_string_is_closed_charset_and_within_limits(self):
        seen = set()
        for phase, fields in self.every_card():
            card = vc.card(phase, **fields)
            with self.subTest(phase=phase, fields=fields):
                self.assertEqual(set(card), {'word', 'headline', 'detail', 'badge', 'tone', 'mood', 'meter',
                                             'toast', 'dwell'})
                self.assertTrue(vc.WORD.fullmatch(card['word']))
                self.assertLessEqual(len(card['headline']), vc.HEADLINE_MAX)
                self.assertLessEqual(len(card['detail']), vc.DETAIL_MAX)
                for key in ('headline', 'detail'):
                    self.assertLessEqual(set(card[key]), TEXT_CHARS)
                    self.assertTrue(vc.DISPLAY.fullmatch(card[key]), card[key])
                self.assertIn(card['tone'], vc.TONES); self.assertIn(card['mood'], vc.MOODS)
                self.assertIn(card['badge'], vc.BADGES + (None,)); self.assertIn(card['meter'], vc.METERS + (None,))
                if card['toast'] is not None:
                    self.assertTrue(vc.TOAST.fullmatch(card['toast']), card['toast'])
                    self.assertTrue(card['toast'].isascii()); self.assertLessEqual(len(card['toast']), 60)
                if phase == 'result':
                    self.assertIn(card['dwell'], (4000, 5000, 6000)); self.assertIsNotNone(card['badge'])
                seen.update((card['headline'], card['detail'], card['toast']))
        # A closed set: no caller value is ever formatted into copy.
        self.assertFalse(any('evil' in str(text) or 'kind' in str(text) for text in seen))
        self.assertLess(len(seen), 400)

    def test_no_unicode_ellipsis_in_user_visible_text(self):
        # Every card value plus every string constant in the copy table.
        values = [text for phase, fields in self.every_card() for text in vc.card(phase, **fields).values()]
        values += [value for name, value in vars(vc).items() if not name.startswith('__')]
        strings = []
        while values:
            value = values.pop()
            if isinstance(value, dict):
                values.extend(value.values())
            elif isinstance(value, (tuple, list, set, frozenset)):
                values.extend(value)
            elif isinstance(value, str):
                strings.append(value)
        self.assertIn('Listening...', strings)
        self.assertEqual(sorted({text for text in strings if ELLIPSIS.search(text)}), [])
        # Every UI source line, ignoring receivers' charset allow-lists.
        for pattern in UI_SOURCES:
            self.assertTrue(list(ROOT.glob(pattern)), pattern)
        found = [(str(path.relative_to(ROOT)), number, line.strip()[:160])
                 for path in sorted({path for pattern in UI_SOURCES for path in ROOT.glob(pattern)})
                 for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1)
                 if ELLIPSIS.search(VALIDATOR_CLASS.sub('', line))]
        self.assertEqual(found, [])
        self.assertIsNone(ELLIPSIS.search(VALIDATOR_CLASS.sub('', 'ok = /^[ -~\u2018\u2026]*$/.test(v)')))
        for spelling in ('["Deleting\u2026"]', r'"Deleting\u2026"', 'Opening &hellip;', 'Loading&#8230;'):
            self.assertTrue(ELLIPSIS.search(VALIDATOR_CLASS.sub('', spelling)), spelling)

    def test_idle_is_empty_and_chip_derived(self):
        for chip, tone, mood in (('ready', 'ready', 'idle'), ('warming', 'idle', 'sleepy'),
                                 ('starting', 'idle', 'sleepy'), ('unavailable', 'idle', 'dim'),
                                 ('paused', 'idle', 'dim')):
            card = vc.card('idle', chip=chip)
            self.assertEqual((card['word'], card['headline'], card['detail'], card['badge'], card['meter']),
                             ('', '', '', None, None))
            self.assertEqual((card['tone'], card['mood']), (tone, mood))

    def test_chip_from_warm_and_agent(self):
        cases = {('ready', 'ok'): 'ready', ('grace', 'ok'): 'ready', ('warming', 'ok'): 'warming',
                 ('unloaded', 'ok'): 'starting', (None, 'ok'): 'starting', ('degraded', 'ok'): 'unavailable',
                 ('unloading', 'ok'): 'unavailable', ('unavailable', 'ok'): 'unavailable',
                 ('junk', 'ok'): 'unavailable', ('ready', 'paused'): 'paused'}
        for (warm, agent), chip in cases.items():
            self.assertEqual(vc.chip(warm, agent), chip)

    def test_phase_copy_matches_contract(self):
        self.assertEqual(vc.card('pressed'), dict(word='GETTING READY', headline='One moment...',
            detail='Keep holding the mic button.', badge=None, tone='work', mood='front', meter=None,
            toast=None, dwell=None))
        listening = vc.card('listening')
        self.assertEqual((listening['word'], listening['headline'], listening['detail'], listening['meter'],
                          listening['mood'], listening['tone']),
                         ('LISTENING', 'Listening...', 'Speak, then let go.', 'hold', 'listening', 'listen'))
        confirming = vc.card('listening', confirming=True)
        self.assertEqual((confirming['word'], confirming['detail']), ('CONFIRMING', 'Say “yes” or “no”, then let go.'))
        self.assertEqual(vc.card('released')['detail'], 'Working on it...')
        self.assertEqual(vc.card('released', cut=True)['detail'], 'That was the 9-second limit. Using what I heard.')
        self.assertEqual((vc.card('released')['word'], vc.card('released')['meter']), ('THINKING', 'sweep'))
        doing = vc.card('doing', tool='tv.open_app', slot='app:disney_plus')
        self.assertEqual((doing['word'], doing['headline'], doing['meter'], doing['mood']),
                         ('ON IT', 'Opening Disney+...', 'sweep', 'thinking'))
        self.assertEqual(vc.card('doing', tool='tv.home', slot='via:browser')['headline'], 'Closing the browser...')
        self.assertEqual(vc.card('doing', tool='tv.volume', slot='volume:down:4')['headline'],
                         'Turning the volume down 4...')

    def test_confirm_copy_by_rule(self):
        ceiling = vc.card('confirm', tool='tv.volume', slot='volume:up:5', rule='ceiling')
        self.assertEqual((ceiling['word'], ceiling['headline'], ceiling['detail'], ceiling['badge'],
                          ceiling['meter'], ceiling['mood'], ceiling['tone']),
                         ('NEEDS YOUR OK', 'Turn the volume up 5?', 'Hold the mic button and say “yes” or “no”.',
                          'waiting', 'countdown', 'asking', 'ask'))
        self.assertEqual(vc.card('confirm', tool='control.back', rule='repeat')['headline'], 'Go back again?')
        self.assertEqual(vc.card('confirm', tool='browser.open_site', slot='site:duckduckgo', rule='repeat')['toast'],
                         'Open DuckDuckGo again? Hold mic, say yes or no')
        self.assertEqual(vc.card('confirm', tool='tv.volume', rule='ceiling')['headline'], 'Should I go ahead?')

    def test_result_copy_matches_contract(self):
        def r(state, code, tool=None, slot=None):
            card = vc.card('result', state=state, code=code, tool=tool, slot=slot)
            return card['headline'], card['detail'], card['badge'], card['toast']
        cannot = ('I’m sorry, I cannot do that.', '', 'nothing', "I'm sorry, I cannot do that")
        for code in ('UNSUPPORTED_COMMAND', 'TOOL_SCHEMA', 'ASR_OUTPUT_INVALID', 'TOOL_DISABLED',
                     'TV_TOOL_UNAVAILABLE', 'DEEPLINK_UNQUALIFIED', 'CONFIRMATION_SURFACE_UNAVAILABLE'):
            self.assertEqual(r('refused', code, 'tv.open_content', 'content:apple_tv:title'), cannot)
        self.assertEqual(r('refused', 'NEGATED_COMMAND'), ('OK, I won’t.', '', 'nothing', 'OK, nothing done'))
        self.assertEqual(r('verified', 'TV_FOREGROUND_VERIFIED', 'tv.open_app', 'app:hbo_max'),
                         ('HBO Max is open.', '', 'confirmed', None))
        self.assertEqual(r('verified', 'TV_FOREGROUND_VERIFIED', 'tv.home', 'via:tv'),
                         ('You’re home.', '', 'confirmed', 'Back to R2D2 Home'))
        self.assertEqual(r('accepted', 'BROWSER_SESSION_ENDED', 'tv.home', 'via:browser'),
                         ('Closing the browser...', '', 'sent', 'Closing the R2D2 browser'))
        self.assertEqual(r('verified', 'TV_VOLUME_VERIFIED', 'tv.volume', 'volume:up:2'),
                         ('Turned the volume up 2.', '', 'confirmed', 'Volume up 2'))
        self.assertEqual(r('accepted', 'TV_VOLUME_ACCEPTED', 'tv.volume', 'volume:down:1'),
                         ('Turning the volume down 1...', 'Watch the TV’s volume bar.', 'sent', 'Sent: volume down 1'))
        self.assertEqual(r('verified', 'TV_MUTE_VERIFIED', 'tv.mute', 'mute:off'),
                         ('Sound is back on.', '', 'confirmed', 'Sound on'))
        self.assertEqual(r('accepted', 'TV_MEDIA_ACCEPTED', 'control.play_pause', 'media:pause'),
                         ('Sent “pause” to the app.', 'Apps don’t report back.', 'sent', 'Sent pause to the app'))
        self.assertEqual(r('verified', 'BROWSER_MEDIA_VERIFIED', 'control.play_pause', 'media:play')[0], 'Playing.')
        self.assertEqual(r('accepted', 'BROWSER_NAVIGATION_ACCEPTED', 'browser.youtube_search'),
                         ('Searching YouTube...', '', 'sent', 'Searching YouTube in the R2D2 browser'))
        self.assertEqual(r('accepted', 'BROWSER_NAVIGATION_ACCEPTED', 'browser.open_site', 'site:youtube')[3],
                         'Opening YouTube in the R2D2 browser')
        self.assertEqual(r('verified', 'BROWSER_SESSION_ENDED', 'browser.close'),
                         ('Browser closed.', '', 'confirmed', 'Browser closed'))
        self.assertEqual(r('accepted', 'TV_STATUS_SHOWN', 'meta.whats_playing', 'on:apple_tv'),
                         ('You’re watching Apple TV.', 'I can’t see titles yet.', 'sent', "You're in Apple TV"))
        self.assertEqual(r('accepted', 'BROWSER_STATUS_SHOWN', 'meta.whats_playing', 'page:yt_watch:paused')[0],
                         'A YouTube video is paused.')
        self.assertEqual(r('refused', 'TV_APP_NOT_INSTALLED', 'tv.open_app', 'app:sling'),
                         ('Sling TV isn’t installed.', 'Install it on the TV, then try again.', 'nothing',
                          "Sling TV isn't installed on this TV"))
        self.assertEqual(r('refused', 'PENDING_CANCELLED')[2], 'changed')
        self.assertEqual(r('verified', 'PENDING_CANCELLED'), ('Cancelled.', 'Nothing was changed.', 'changed',
                                                              'Cancelled. Nothing changed'))
        self.assertEqual(r('refused', 'CONFIRMATION_EXPIRED')[3], 'Request expired. Nothing changed')
        self.assertEqual(r('rejected', 'TV_LAUNCH_REJECTED'), ('The TV refused that.', 'Nothing changed.', 'failed',
                                                               'The TV refused that request'))
        self.assertEqual(r('rejected', 'NAVIGATION_REJECTED')[0], 'The browser didn’t accept that.')
        self.assertEqual(r('refused', 'START_UNCONFIRMED')[2], 'failed')
        self.assertEqual(r('refused', 'TRANSPORT_FAILED')[:3], ('Can’t reach Spark.',
                                                              'Nothing was sent. Check that Spark is on.', 'failed'))

    def test_unknown_outcomes_never_read_as_success(self):
        for state, code in (('unknown', 'TV_UNCONFIRMED'), ('unknown', 'EXECUTION_UNCONFIRMED'),
                            ('refused', 'REQUEST_EXPIRED'), ('unknown', 'RESULT_UNCONFIRMED'),
                            ('unknown', 'TV_FOREGROUND_VERIFIED')):
            card = vc.card('result', state=state, code=code, tool='tv.open_app', slot='app:netflix')
            self.assertEqual((card['word'], card['headline'], card['badge'], card['mood'], card['dwell']),
                             ('NOT SURE', 'Not sure that worked.', 'unconfirmed', 'asking', 6000))
            self.assertEqual(card['toast'], 'Sent, but not confirmed. Check the TV')

    def test_past_tense_only_when_verified(self):
        for code in ('TV_FOREGROUND_VERIFIED', 'TV_VOLUME_VERIFIED', 'TV_MUTE_VERIFIED', 'BROWSER_MEDIA_VERIFIED',
                     'BROWSER_SESSION_ENDED'):
            for tool, slot in itertools.product(sorted(vc.TOOLS), sorted(vc.SLOTS)):
                card = vc.card('result', state='accepted', code=code, tool=tool, slot=slot)
                self.assertNotIn(card['headline'], ('Muted.', 'Paused.', 'Playing.', 'Browser closed.', 'You’re home.'))
                self.assertFalse(card['headline'].startswith('Turned'))
                self.assertFalse(card['headline'].endswith(' is open.'))
                self.assertIn(card['badge'], ('sent',))

    def test_warming_and_unavailable_moods(self):
        warming = vc.card('result', state='refused', code='VOICE_WARMING_UP')
        self.assertEqual((warming['word'], warming['mood'], warming['toast']),
                         ('WARMING UP', 'sleepy', 'Voice model warming up - try again in a moment'))
        for code in ('VOICE_UNAVAILABLE', 'MODEL_UNAVAILABLE', 'ASR_UNAVAILABLE', 'ASR_UNQUALIFIED', 'AGENT_PAUSED'):
            self.assertEqual(vc.card('result', state='refused', code=code)['mood'], 'dim')

    def test_junk_inputs_fall_back_without_raising(self):
        for state, code in ((None, None), ('weird', 'lower'), ('verified', 'X' * 80), ('accepted', 3)):
            card = vc.card('result', state=state, code=code, tool=['x'], slot=b'app:netflix')
            self.assertIn(card['badge'], vc.BADGES)
        self.assertEqual(vc.card('nonsense', chip='other')['headline'], '')
        self.assertTrue(all(vc.SLOT.fullmatch(slot) for slot in vc.SLOTS))


if __name__ == '__main__':
    unittest.main()
