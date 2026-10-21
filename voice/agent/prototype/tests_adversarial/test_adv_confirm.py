"""Confirmation bypass attempts."""
import unittest

from agent.errors import Outcome
from agent.golden import golden_model
from agent.registry import APP_IDS
from tests_adversarial.adv_support import AppleBuilder, Case, call, mutations, scripted


class RecordingBuilder(AppleBuilder):
    def __init__(self):
        self.texts = []

    def build(self, kind, text):
        self.texts.append(text)
        return super().build(kind, text)


class Switch:
    """A model whose raw output can be changed between utterances."""
    def __init__(self, phase='P5'):
        self.raw = '{"tool":"meta.unsupported","args":{}}'
        self.calls = 0
        self.prompt_sha256 = scripted('x', phase).prompt_sha256

    def interpret(self, transcript, *, timeout_s):
        self.calls += 1
        return self.raw


def content(title):
    return call('tv.open_content', app='apple_tv', kind='title', text=title)


class ConfirmationBypass(Case):
    def p5(self):
        self.builder = RecordingBuilder()
        model = Switch()
        e = self.env('P5', model=model, deeplinks={'apple_tv': self.builder})
        e.channel.open_session(surface='browser_other')
        return e, model

    def test_newer_request_replaces_older_and_yes_runs_only_the_newest_once(self):
        e, model = self.p5()
        model.raw = content('First Title')
        self.assertEqual(self.say(e, 'open first title on apple tv'), Outcome('refused', 'CONFIRMATION_PENDING'))
        model.raw = content('Second Title')
        self.assertEqual(self.say(e, 'open second title on apple tv'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(self.say(e, 'yes'), Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(self.builder.texts, ['Second Title'])
        self.assertEqual(e.transport.launched, [APP_IDS['apple_tv']])

    def test_model_cannot_answer_and_a_failed_parse_still_declines(self):
        e, model = self.p5()
        model.raw = content('Severance')
        self.say(e, 'open severance on apple tv')
        model.raw = '{"tool":"meta.confirm","args":{"answer":"yes"}}'
        self.assertEqual(self.say(e, 'sure go ahead'), Outcome('refused', 'TOOL_SCHEMA'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [])

    def test_confusable_yes_goes_to_the_model_and_declines(self):
        e, model = self.p5()
        model.raw = content('Severance')
        self.say(e, 'open severance on apple tv')
        model.raw = '{"tool":"meta.unsupported","args":{}}'
        self.assertEqual(self.say(e, 'yеs'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [])

    def test_negated_utterance_while_pending_declines(self):
        e, model = self.p5()
        model.raw = content('Severance')
        self.say(e, 'open severance on apple tv')
        self.assertEqual(self.say(e, "don't open it"), Outcome('refused', 'NEGATED_COMMAND'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [])

    def test_kill_switch_between_pending_and_yes(self):
        e, model = self.p5()
        model.raw = content('Severance')
        self.say(e, 'open severance on apple tv')
        e.kill.set_tv_off('OWNER')
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'TV_CHANNEL_UNAVAILABLE'))
        e.kill.set_tv_on()
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [])

    def test_pause_drops_the_pending_confirmation(self):
        """Pausing is level 2 of the kill switch: a press while paused must not leave a
        plan that a later 'yes' (after resume, still inside 15 s) can execute."""
        e, model = self.p5()
        model.raw = content('Severance')
        self.say(e, 'open severance on apple tv')
        e.kill.pause('OWNER')
        with self.assertRaises(Exception):
            self.say(e, 'yes')                                # arm() refuses AGENT_PAUSED
        e.kill.resume()
        self.clock.wait(2)
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'UNSUPPORTED_COMMAND'))
        self.assertEqual(e.transport.launched, [])

    def test_yes_after_expiry_boundary(self):
        e, model = self.p5()
        model.raw = content('Severance')
        self.say(e, 'open severance on apple tv')
        self.clock.wait(14.999)
        self.assertEqual(self.say(e, 'yes').state, 'verified')
        e, model = self.p5()
        model.raw = content('Severance')
        self.say(e, 'open severance on apple tv')
        self.clock.wait(15.0)
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'CONFIRMATION_EXPIRED'))

    def test_volume_ceiling_cannot_be_stepped_around(self):
        e = self.env('P4', model=golden_model('P4'), volume_ceiling=35)
        e.transport.foreground_id, e.transport.volume = APP_IDS['netflix'], 30
        self.assertEqual(self.say(e, 'volume up 5').state, 'verified')     # 35, not above the ceiling
        self.clock.wait(61)
        self.assertEqual(self.say(e, 'volume up 1'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(e.transport.volume, 35)
        self.clock.wait(61)
        # A non-int volume read (bool) fails safe to confirmation.
        e.transport.forced['ssap://audio/getVolume'] = {'returnValue': True, 'volume': True}
        self.assertEqual(self.say(e, 'volume up 1'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(e.transport.volume, 35)

    def test_confirmation_refused_without_tv_for_tv_plan(self):
        e = self.env('P5', model=scripted(content('Severance')), with_tv=False,
                     deeplinks={'apple_tv': AppleBuilder()})
        e.channel.open_session(surface='browser_other')
        self.assertEqual(self.say(e, 'open severance on apple tv'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(self.say(e, 'yes'), Outcome('refused', 'TV_CHANNEL_UNAVAILABLE'))
        self.assertEqual(mutations(e), [])


if __name__ == '__main__':
    unittest.main()
