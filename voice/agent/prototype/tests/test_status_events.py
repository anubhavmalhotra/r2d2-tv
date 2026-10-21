"""Agent status events for the voice UI: enum-only, never raising, after the rewrite."""
from agent.errors import Outcome
from agent.golden import golden_model
from agent.registry import APP_IDS
from tests.support import Case, SENTINEL, call, scripted
from tests.test_agent import mutations


class Recorder:
    def __init__(self):
        self.rows = []

    def __call__(self, lease, kind, fields):
        self.rows.append((lease, kind, dict(fields)))

    def kinds(self):
        return [(kind, fields['tool'], fields['slot']) for _, kind, fields in self.rows]

    def outcome(self):
        return [fields for _, kind, fields in self.rows if kind == 'outcome'][-1]


class Enumerated:
    """The resident StatusFeedback contract: no surface, enum-only confirmations."""
    def __init__(self, shown=True):
        self.shown, self.calls = shown, []

    def can_confirm(self, surface, *, free_text):
        return not free_text

    def show(self, code, surface, *, echo=None):
        self.calls.append(code)
        return self.shown


class StatusEvents(Case):
    def env_with(self, phase='P5', **kw):
        self.events = Recorder()
        kw.setdefault('model', golden_model(phase))
        return self.env(phase, events=self.events, **kw)

    def test_doing_precedes_outcome_with_closed_slot_and_the_armed_lease(self):
        e = self.env_with()
        e.transport.foreground_id = APP_IDS['hulu']
        lease = e.agent.arm()
        self.assertEqual(e.agent.handle_utterance(lease, 'open netflix'),
                         Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual([(kind, fields) for _, kind, fields in self.events.rows], [
            ('doing', {'tool': 'tv.open_app', 'slot': 'app:netflix'}),
            ('outcome', {'state': 'verified', 'code': 'TV_FOREGROUND_VERIFIED', 'rule': None,
                         'ttl': None, 'tool': 'tv.open_app', 'slot': 'app:netflix'})])
        self.assertTrue(all(row[0] is lease for row in self.events.rows))

    def test_slots_for_every_enumerated_tool(self):
        e = self.env_with()
        e.transport.foreground_id = APP_IDS['netflix']
        for phrase, slot in (('lower the volume by 4', 'volume:down:4'), ('mute', 'mute:on'),
                             ('unmute', 'mute:off'), ('go home', 'via:tv'), ('pause', 'media:pause')):
            with self.subTest(phrase=phrase):
                self.clock.wait(61)
                self.say(e, phrase)
                self.assertEqual(self.events.outcome()['slot'], slot)
        e.transport.foreground_id = APP_IDS['tubi']
        self.say(e, "what's playing")
        self.assertEqual(self.events.outcome(), {'state': 'accepted', 'code': 'TV_STATUS_SHOWN', 'rule': None,
                                                 'ttl': None, 'tool': 'meta.whats_playing', 'slot': 'on:tubi'})
        self.assertEqual(self.events.kinds()[-2], ('doing', 'meta.whats_playing', None))

    def test_browser_routes_and_status_display_map_to_closed_slots(self):
        e = self.env_with()
        e.channel.open_session(surface='browser_youtube_watch', video=1, paused=True)
        self.say(e, "what's playing")
        self.assertEqual(self.events.outcome()['slot'], 'page:yt_watch:paused')
        self.say(e, 'go home')
        self.assertEqual(self.events.outcome()['slot'], 'via:browser')
        e.channel.open_session(surface='browser_other')
        self.say(e, 'open youtube in the browser')
        self.assertEqual(self.events.outcome()['slot'], 'site:youtube')

    def test_free_text_never_reaches_an_event(self):
        e = self.env_with(model=scripted(call('browser.web_search', query=SENTINEL)))
        e.channel.open_session(surface='browser_other')
        self.assertEqual(self.say(e, 'search the web for ' + SENTINEL).code, 'BROWSER_NAVIGATION_ACCEPTED')
        self.assertEqual(self.events.kinds(), [('doing', 'browser.web_search', None),
                                               ('outcome', 'browser.web_search', None)])
        for word in SENTINEL.split():
            self.assertNotIn(word, repr(self.events.rows))
        e = self.env_with(model=scripted(call('tv.open_content', app='apple_tv', kind='title', text=SENTINEL)),
                          feedback=Enumerated())
        self.assertEqual(self.say(e, 'open ' + SENTINEL), Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))
        self.assertEqual(self.events.outcome()['slot'], 'content:apple_tv:title')
        self.assertNotIn('zebra', repr(self.events.rows))

    def test_refusal_before_dispatch_emits_no_doing(self):
        e = self.env_with()
        lease = e.agent.arm()
        e.kill.pause('OWNER')
        self.assertEqual(e.agent.handle_utterance(lease, 'open netflix'), Outcome('refused', 'AGENT_PAUSED'))
        self.assertEqual(self.events.kinds(), [('outcome', None, None)])
        e = self.env_with()
        e.transport.foreground_id = APP_IDS['netflix']
        self.assertEqual(self.say(e, 'go back'), Outcome('refused', 'TV_TOOL_UNAVAILABLE'))
        self.assertEqual(self.events.kinds(), [('outcome', 'control.back', None)])

    def test_pending_outcome_carries_rule_and_ttl_after_the_rewrite(self):
        e = self.env_with('P4', feedback=Enumerated())
        e.transport.foreground_id, e.transport.volume = APP_IDS['netflix'], 34
        self.assertEqual(self.say(e, 'turn the volume up by 3'), Outcome('refused', 'CONFIRMATION_PENDING'))
        self.assertEqual(self.events.outcome(), {'state': 'refused', 'code': 'CONFIRMATION_PENDING',
                                                 'rule': 'ceiling', 'ttl': 15000, 'tool': 'tv.volume',
                                                 'slot': 'volume:up:3'})
        self.assertNotIn('doing', [kind for _, kind, _ in self.events.rows])
        # A pending the surface could not show is reported as what it became.
        e = self.env_with('P4', feedback=Enumerated(shown=False))
        e.transport.foreground_id, e.transport.volume = APP_IDS['netflix'], 34
        self.assertEqual(self.say(e, 'turn the volume up by 3'),
                         Outcome('refused', 'CONFIRMATION_SURFACE_UNAVAILABLE'))
        self.assertEqual(self.events.outcome()['code'], 'CONFIRMATION_SURFACE_UNAVAILABLE')
        self.assertIsNone(self.events.outcome()['ttl'])

    def test_answers_carry_the_pending_tool_and_slot(self):
        for answer, wait, code in (('yes', 2, 'TV_VOLUME_VERIFIED'), ('no', 2, 'CONFIRMATION_DECLINED'),
                                   ('yes', 16, 'CONFIRMATION_EXPIRED')):
            with self.subTest(answer=answer, wait=wait):
                e = self.env_with('P4', feedback=Enumerated())
                e.transport.foreground_id, e.transport.volume = APP_IDS['netflix'], 34
                self.say(e, 'turn the volume up by 3')
                self.clock.wait(wait)
                self.assertEqual(self.say(e, answer).code, code)
                self.assertEqual((self.events.outcome()['tool'], self.events.outcome()['slot']),
                                 ('tv.volume', 'volume:up:3'))
                self.assertEqual(self.events.outcome()['rule'], 'ceiling')

    def test_observer_failure_changes_no_outcome_audit_or_streak(self):
        baseline = self.env('P5', model=golden_model('P5'))
        baseline.transport.foreground_id = APP_IDS['hulu']
        def broken(lease, kind, fields):
            raise RuntimeError('observer')
        e = self.env('P5', model=golden_model('P5'), events=broken)
        e.transport.foreground_id = APP_IDS['hulu']
        for env in (baseline, e):
            self.assertEqual(self.say(env, 'open netflix'), Outcome('verified', 'TV_FOREGROUND_VERIFIED'))
        self.assertEqual(e.agent.unknown_streak, 0)
        self.assertEqual(len(mutations(e)), len(mutations(baseline)))
        strip = lambda rows: [{k: v for k, v in row.items() if k not in ('utterance', 'ts', 'prev', 'hash')}
                              for row in rows]
        self.assertEqual(strip(self.audit_rows(e)), strip(self.audit_rows(baseline)))
        self.assertFalse(e.kill.paused())
