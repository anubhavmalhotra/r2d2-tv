"""Executor failures are never reported as success; kill switch, rate limits, audit."""
import io
import json
import os
import unittest

from agent import agentctl
from agent.audit import AuditLog, AuditUnavailable
from agent.errors import Outcome, Refused
from agent.executors.base import ExecResult
from agent.golden import golden_model
from agent.killswitch import KillSwitch
from agent.policy import RateLimiter
from agent.registry import APP_IDS
from tests.support import SENTINEL, Case, call, scripted
from tests.test_agent import mutations


class ExecutorFailures(Case):
    def browser(self, phase='P2', **session):
        e = self.env(phase, model=golden_model(phase))
        e.channel.open_session(**{'surface': 'browser_youtube_watch', 'video': 1, 'paused': True, **session})
        return e

    def test_navigation_ack_without_page_match_is_unknown(self):
        e = self.browser()
        e.channel.effects['navigate'] = False
        self.assertEqual(self.say(e, 'open duckduckgo'), Outcome('unknown', 'NAVIGATION_UNCONFIRMED'))
        self.assertEqual(len(mutations(e)), 1)

    def test_channel_exception_after_dispatch_is_unknown(self):
        e = self.browser()
        e.channel.raise_on.add('navigate')
        self.assertEqual(self.say(e, 'open duckduckgo'), Outcome('unknown', 'NAVIGATION_UNCONFIRMED'))
        e.channel.raise_on.clear()
        e.channel.raise_on.add('observe')
        self.clock.wait(20)
        self.assertEqual(self.say(e, 'search the web for news today').state, 'refused')  # pre-dispatch read

    def test_garbage_ack_is_unknown_and_rejection_is_not_retried(self):
        e = self.browser()
        e.channel.results['navigate'] = 'ok!'
        self.assertEqual(self.say(e, 'open duckduckgo').state, 'unknown')
        e.channel.results['navigate'] = 'rejected'
        self.clock.wait(20)
        self.assertEqual(self.say(e, 'open duckduckgo'), Outcome('rejected', 'NAVIGATION_REJECTED'))
        self.assertEqual(len(mutations(e)), 2)

    def test_media_key_without_flip_is_unknown_and_no_video_refused(self):
        e = self.browser()
        e.channel.effects['media_key'] = False
        self.assertEqual(self.say(e, 'resume'), Outcome('unknown', 'MEDIA_UNCONFIRMED'))
        self.assertEqual([c[0] for c in mutations(e)], ['media_key'])
        e = self.browser(surface='browser_other', video=0, paused=None)
        self.assertEqual(self.say(e, 'pause'), Outcome('refused', 'BROWSER_MEDIA_NO_VIDEO'))
        self.assertEqual(mutations(e), [])

    def test_end_without_idle_and_history_without_new_generation(self):
        e = self.browser()
        e.channel.effects['end'] = False
        self.assertEqual(self.say(e, 'close the browser'), Outcome('unknown', 'END_UNCONFIRMED'))
        e = self.browser()
        e.channel.effects['history'] = False
        self.assertEqual(self.say(e, 'go back'), Outcome('unknown', 'HISTORY_UNCONFIRMED'))

    def test_inconsistent_or_raising_executor_is_unknown(self):
        bad_results = [ExecResult('verified', 'BROWSER_NAVIGATION_ACCEPTED', True),   # wrong state for code
                       ExecResult('accepted', 'TV_FOREGROUND_VERIFIED', True),        # other executor's code
                       ExecResult('refused', 'BROWSER_NOT_OPENED', True),             # refused after dispatch
                       ExecResult('accepted', 'BROWSER_NAVIGATION_ACCEPTED', 'yes'),
                       ('accepted', 'BROWSER_NAVIGATION_ACCEPTED'), None, RuntimeError('boom')]
        for bad in bad_results:
            e = self.browser()

            def run(plan, *, gate, deadline, bad=bad):
                if isinstance(bad, Exception):
                    raise bad
                return bad
            e.browser.run = run
            with self.subTest(bad=bad):
                self.assertEqual(self.say(e, 'open duckduckgo'), Outcome('unknown', 'EXECUTION_UNCONFIRMED'))

    def test_tv_failures(self):
        e = self.env('P4', model=golden_model('P4'))
        e.transport.launch_effect = False
        self.assertEqual(self.say(e, 'open hulu'), Outcome('unknown', 'TV_UNCONFIRMED'))
        self.assertEqual(e.transport.launched, ['hulu'])                     # exactly once, no retry
        e = self.env('P4', model=golden_model('P4'))
        e.transport.forced['ssap://system.launcher/launch'] = {'returnValue': False, 'errorCode': 403,
                                                               'errorText': 'access denied'}
        self.assertEqual(self.say(e, 'open hulu'), Outcome('rejected', 'TV_LAUNCH_REJECTED'))
        self.assertEqual(e.gateway.last_error_class, 'permission')
        e = self.env('P4', model=golden_model('P4'))
        e.transport.forced['ssap://audio/setMute'] = OSError('reset')
        self.assertEqual(self.say(e, 'mute'), Outcome('unknown', 'TV_UNCONFIRMED'))
        self.assertGreaterEqual(e.transport.closed, 1)            # dropped after the lost reply
        e = self.env('P4', model=golden_model('P4'))
        e.transport.forced['ssap://media.controls/pause'] = {'ok': 1}
        e.transport.foreground_id = APP_IDS['tubi']
        self.assertEqual(self.say(e, 'pause'), Outcome('unknown', 'TV_UNCONFIRMED'))
        e = self.env('P4', model=golden_model('P4'))
        e.transport.forced['ssap://audio/getStatus'] = {'returnValue': True, 'mute': False}
        self.assertEqual(self.say(e, 'mute'), Outcome('unknown', 'TV_UNCONFIRMED'))

    def test_volume_stops_at_first_uncertain_step(self):
        e = self.env('P4', model=golden_model('P4'))
        calls = []
        real = e.transport.request

        def flaky(uri, payload, *, timeout_s):
            if uri.endswith('volumeDown'):
                calls.append(uri)
                if len(calls) == 2:
                    raise OSError('lost')
            return real(uri, payload, timeout_s=timeout_s)
        e.transport.request = flaky
        self.assertEqual(self.say(e, 'lower the volume by 4'), Outcome('unknown', 'TV_UNCONFIRMED'))
        self.assertEqual(len(calls), 2)

    def test_three_unknowns_trip_pause(self):
        e = self.browser()
        e.channel.effects['navigate'] = False
        for _ in range(3):
            self.clock.wait(20)
            self.assertEqual(self.say(e, 'open duckduckgo').state, 'unknown')
        self.assertTrue(e.kill.paused())
        self.assertEqual((e.kill.dir / 'PAUSED').read_text(), 'UNKNOWN_STREAK\n')
        with self.assertRaisesRegex(Refused, '^AGENT_PAUSED$'):
            e.agent.arm()


class KillSwitchTests(Case):
    def test_pause_blocks_arm_model_and_dispatch(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session()
        e.kill.pause()
        with self.assertRaisesRegex(Refused, '^AGENT_PAUSED$'):
            e.agent.arm()
        e.kill.resume()
        lease = e.agent.arm()
        e.kill.pause()                                     # after arm, before model
        self.assertEqual(e.agent.handle_utterance(lease, 'go back'), Outcome('refused', 'AGENT_PAUSED'))
        self.assertEqual(e.model.calls, 0)
        e.kill.resume()

        class PausingModel:
            prompt_sha256 = e.model.prompt_sha256
            kill = None

            def interpret(self, transcript, *, timeout_s):
                self.kill.pause()                          # during the model call
                return call('control.back')
        model = PausingModel()
        e2 = self.env('P2', model=model)
        model.kill = e2.kill
        e2.channel.open_session()
        self.assertEqual(self.say(e2, 'go back'), Outcome('refused', 'AGENT_PAUSED'))
        self.assertEqual(mutations(e2), [])

    def test_unreadable_or_unsafe_config_counts_as_paused(self):
        cfg = self.tmp / 'cfg'
        self.assertTrue(KillSwitch(cfg).paused())         # missing directory
        cfg.mkdir(mode=0o700)
        self.assertFalse(KillSwitch(cfg).paused())
        os.chmod(cfg, 0o755)
        self.assertTrue(KillSwitch(cfg).paused())         # group/world readable
        os.chmod(cfg, 0o700)
        (cfg / 'PAUSED').mkdir()                           # odd but present -> paused
        self.assertTrue(KillSwitch(cfg).paused())

    def test_trip_latches_in_memory_when_flag_cannot_be_written(self):
        cfg = self.tmp / 'ro'
        cfg.mkdir(mode=0o700)
        switch = KillSwitch(cfg)
        os.chmod(cfg, 0o500)
        self.addCleanup(os.chmod, cfg, 0o700)
        self.assertFalse(switch.pause('UNKNOWN_STREAK'))
        self.assertTrue(switch.paused())

    def test_tv_off_refuses_only_tv_tools_without_connecting(self):
        e = self.env('P4', model=golden_model('P4'))
        e.channel.open_session(surface='browser_other')
        e.kill.set_tv_off()
        self.assertEqual(self.say(e, 'open netflix'), Outcome('refused', 'TV_CHANNEL_UNAVAILABLE'))
        self.assertEqual(self.say(e, 'go back'), Outcome('accepted', 'BROWSER_HISTORY_ACCEPTED'))
        self.assertEqual(e.transport.connects, 0)

    def test_agentctl(self):
        cfg = str(self.tmp / 'ctl')
        os.mkdir(cfg, 0o700)
        out = io.StringIO()
        self.assertEqual(agentctl.main(['--config-dir', cfg, 'pause'], out), 0)
        self.assertTrue(KillSwitch(cfg).paused())
        agentctl.main(['--config-dir', cfg, 'resume'], out)
        agentctl.main(['--config-dir', cfg, 'tv-off'], out)
        self.assertEqual((KillSwitch(cfg).paused(), KillSwitch(cfg).tv_off()), (False, True))
        agentctl.main(['--config-dir', cfg, 'tv-on'], out)
        self.assertIn('paused=False tv_off=False', out.getvalue().splitlines()[-1])


class RateLimits(Case):
    def test_utterance_limit_at_arm(self):
        e = self.env('P2', model=golden_model('P2'))
        for _ in range(12):
            e.agent.cancel(e.agent.arm())
        with self.assertRaisesRegex(Refused, '^RATE_LIMITED$'):
            e.agent.arm()
        self.clock.wait(60)
        self.assertIsNotNone(e.agent.arm())

    def test_launch_scope_and_trip_after_three_hits(self):
        e = self.env('P4', model=golden_model('P4'))
        e.transport.foreground_id = APP_IDS['netflix']
        for phrase in ('open hulu', 'open tubi', 'open sling', 'go home'):
            self.clock.wait(1)
            self.assertEqual(self.say(e, phrase), Outcome('verified', 'TV_FOREGROUND_VERIFIED'), phrase)
        for _ in range(3):
            self.clock.wait(1)
            self.assertEqual(self.say(e, 'open netflix'), Outcome('refused', 'RATE_LIMITED'))
        self.assertTrue(e.kill.paused())
        self.assertEqual((e.kill.dir / 'PAUSED').read_text(), 'RATE_TRIPS\n')
        self.assertEqual(len(e.transport.launched), 4)

    def test_scopes_unit(self):
        limiter = RateLimiter(self.clock)
        for _ in range(6):
            limiter.admit(('dispatch', 'browser'))
        with self.assertRaisesRegex(Refused, 'RATE_LIMITED'):
            limiter.admit(('dispatch', 'browser'))
        limiter.admit(('dispatch', 'mute'))          # dispatch has 10/min
        for _ in range(6):
            limiter.admit(('volume_steps',), 5)
        with self.assertRaisesRegex(Refused, 'RATE_LIMITED'):
            limiter.admit(('volume_steps',), 1)     # 30 steps/min
        with self.assertRaisesRegex(Refused, 'TOOL_SCHEMA'):
            limiter.admit(('unknown_scope',))


class Audit(Case):
    def test_rows_chain_and_verify(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session()
        for phrase in ('go back', 'tell me a joke', 'search youtube for lo-fi music'):
            self.say(e, phrase)
        rows = self.audit_rows(e)
        self.assertEqual([r['seq'] for r in rows], [1, 2, 3])
        self.assertEqual(AuditLog.verify(e.audit_dir), 3)
        self.assertEqual(rows[2]['selection']['args'], {'query': {'kind': 'text', 'chars': 11}})
        self.assertEqual(rows[1]['execution']['code'], 'UNSUPPORTED_COMMAND')
        path = next(e.audit_dir.iterdir())
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(e.audit_dir.stat().st_mode & 0o777), '0o700')
        raw = path.read_bytes().replace(b'"UNSUPPORTED_COMMAND"', b'"BROWSER_HISTORY_ACCEPTED"')
        path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, 'AUDIT_CHAIN'):
            AuditLog.verify(e.audit_dir)

    def test_no_transcript_query_or_model_text_on_disk(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session(surface='browser_other')
        table_model = scripted(call('browser.youtube_search', query=SENTINEL), 'P2')
        e2 = self.env('P2', model=table_model)
        e2.channel.open_session(surface='browser_other')
        self.assertEqual(self.say(e2, 'search youtube for ' + SENTINEL).state, 'accepted')
        e3 = self.env('P2', model=scripted('not json ' + SENTINEL, 'P2'))
        self.say(e3, 'please ' + SENTINEL)
        self.say(e, SENTINEL)
        blob = b''.join(p.read_bytes() for p in self.tmp.rglob('*') if p.is_file()).lower()
        for needle in (SENTINEL, SENTINEL.replace(' ', '+'), SENTINEL.replace(' ', '%20'), 'zebra', 'marmalade'):
            self.assertNotIn(needle.encode(), blob)
        self.assertIn(('BROWSER_NAVIGATION_ACCEPTED', 'Opening in the R2D2 browser: ' + SENTINEL),
                      e2.overlay.shown)                  # shown on the TV only

    def test_fail_closed_when_audit_unwritable(self):
        e = self.env('P2', model=golden_model('P2'))
        e.channel.open_session()
        e.audit_dir.mkdir(mode=0o700)
        os.chmod(e.audit_dir, 0o500)
        self.addCleanup(os.chmod, e.audit_dir, 0o700)
        self.assertEqual(self.say(e, 'go back'), Outcome('refused', 'AUDIT_UNAVAILABLE'))
        self.assertEqual(mutations(e), [])
        self.assertTrue(e.kill.paused())
        self.assertEqual((e.kill.dir / 'PAUSED').read_text(), 'AUDIT_WRITE\n')

    def test_structural_guard_rejects_free_text(self):
        log = AuditLog(self.tmp / 'a', wall=self.clock.wall)
        for row in ({'note': 'free text here'}, {'q': SENTINEL}, {'x': ['list']}, {'Bad Key': 1},
                    {'seq': 5}, {'u': 'https://example.com/?q=a'}):
            with self.subTest(row=row), self.assertRaises(AuditUnavailable):
                log.append(row)
        log.append({'tool': 'browser.close', 'n': 3, 'ok': True, 'none': None})

    def test_day_rollover_chain_and_retention(self):
        log = AuditLog(self.tmp / 'r', wall=self.clock.wall, persistent=True)
        log.append({'event': 'a'})
        self.clock.wait(86400)
        log.append({'event': 'b'})
        files = sorted((self.tmp / 'r').iterdir())
        self.assertEqual(len(files), 2)
        self.assertEqual(AuditLog.verify(self.tmp / 'r'), 2)
        self.clock.wait(31 * 86400)
        log.append({'event': 'c'})
        self.assertEqual(len(list((self.tmp / 'r').iterdir())), 1)   # >30-day files pruned
        reopened = AuditLog(self.tmp / 'r', wall=self.clock.wall, persistent=True)
        reopened.append({'event': 'd'})
        self.assertEqual(AuditLog.verify(self.tmp / 'r'), 2)
        self.assertEqual(json.loads((sorted((self.tmp / 'r').iterdir())[-1]).read_text().splitlines()[-1])['seq'], 4)


if __name__ == '__main__':
    unittest.main()
