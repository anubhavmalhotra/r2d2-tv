"""TV gateway: fixed request table, no pointer input, no prompt path."""
import inspect
import os
import unittest

import agent.executors.tv_gateway as tvmod
from agent.errors import Refused
from agent.executors.tv_gateway import FORBIDDEN_PERMISSIONS, P4_MANIFEST, SSAP, FakeSSAPTransport, op_ids
from agent.registry import APP_IDS, BROWSER_ID, HOME_ID
from tests.support import Case

OP = ['%032x' % (0xabcdef00 + i) for i in range(1, 50)]


class Gateway(Case):
    def gw(self, phase='P4', **kw):
        e = self.env(phase)
        for key, value in kw.items():
            setattr(e.transport, key, value)
        return e

    def test_refuses_before_connecting_without_key_or_pin(self):
        for broken in ('missing_key', 'missing_pin', 'key_mode', 'pin_mode', 'symlink', 'bad_json', 'extra_field'):
            e = self.gw()
            if broken == 'missing_key':
                os.unlink(e.key_path)
            elif broken == 'missing_pin':
                os.unlink(e.pin_path)                    # no trust-on-first-use
            elif broken == 'key_mode':
                os.chmod(e.key_path, 0o644)
            elif broken == 'pin_mode':
                os.chmod(e.pin_path, 0o640)
            elif broken == 'symlink':
                real = e.key_path.with_name('real.json')
                os.rename(e.key_path, real)
                os.symlink(real, e.key_path)
            elif broken == 'bad_json':
                e.key_path.write_text('{"client-key": "abc", "client-key": "def"}')
            else:
                self.private_file('client-key.json', {'client-key': 'ab' * 16, 'pairingType': 'PROMPT'},
                                  base=e.base)
            with self.subTest(broken=broken):
                with self.assertRaisesRegex(Refused, '^TV_CHANNEL_UNAVAILABLE$'):
                    e.gateway.home(OP[0])
                self.assertEqual((e.transport.connects, e.transport.registers, e.transport.requests), (0, 0, []))

    def test_pairing_prompt_closes_latches_off_and_never_retries(self):
        e = self.gw(register_mode='prompt')
        key_before = e.key_path.read_bytes()
        with self.assertRaisesRegex(Refused, '^TV_PERMISSION_DENIED$'):
            e.gateway.home(OP[0])
        self.assertEqual((e.transport.registers, e.transport.closed, e.transport.requests), (1, 1, []))
        self.assertEqual((e.kill.dir / 'TV_OFF').read_text(), 'PAIRING_PROMPT\n')
        self.assertEqual((e.kill.dir / 'PAUSED').read_text(), 'PAIRING_PROMPT\n')   # whole agent paused
        self.assertEqual(e.trips, ['PAIRING_PROMPT'])
        for _ in range(3):
            with self.assertRaisesRegex(Refused, '^TV_CHANNEL_UNAVAILABLE$'):
                e.gateway.home(OP[1])
        self.assertEqual((e.transport.connects, e.transport.registers), (1, 1))
        self.assertEqual(e.key_path.read_bytes(), key_before)     # never writes a key

    def test_denied_register_and_changed_certificate(self):
        e = self.gw(register_mode='denied')
        with self.assertRaisesRegex(Refused, '^TV_PERMISSION_DENIED$'):
            e.gateway.foreground()
        self.assertEqual((e.kill.dir / 'TV_OFF').read_text(), 'PERMISSION_DENIED\n')
        self.assertFalse((e.kill.dir / 'PAUSED').exists())      # TV tools only
        e = self.gw(cert_ok=False)
        with self.assertRaisesRegex(Refused, '^TV_CHANNEL_UNAVAILABLE$'):
            e.gateway.foreground()
        self.assertEqual((e.kill.dir / 'TV_OFF').read_text(), 'CERT_CHANGED\n')
        self.assertEqual((e.kill.dir / 'PAUSED').read_text(), 'CERT_CHANGED\n')
        self.assertEqual(e.transport.registers, 0)

    def test_pairing_prompt_mid_session_latches_off(self):
        e = self.gw()
        e.transport.forced['ssap://com.webos.applicationManager/getForegroundAppInfo'] = {'pairingType': 'PROMPT'}
        self.assertIsNone(e.gateway.foreground())
        self.assertTrue(e.kill.tv_off() and e.kill.paused())

    def test_register_interface_has_no_pairing_type(self):
        params = inspect.signature(FakeSSAPTransport.register).parameters
        self.assertEqual([p for p in params if p != 'self'], ['client_key', 'timeout_s'])
        source = inspect.getsource(tvmod)
        self.assertNotIn('getPointerInputSocket', source)
        self.assertNotIn('networkinput', source)
        self.assertNotIn('forcePairing', source.replace('never forcePairing', ''))

    def test_only_table_uris_are_ever_sent(self):
        e = self.gw('P5')
        gw = e.gateway
        gw.foreground(); gw.installed_keys(); gw.volume_read(); gw.mute_read()
        gw.launch_app('netflix', OP[0]); self.clock.wait(1)
        gw.home(OP[1]); self.clock.wait(1)
        gw.open_browser('a' * 32, OP[2]); self.clock.wait(1)
        gw.volume_step('up', OP[3]); gw.volume_step('down', OP[4]); self.clock.wait(1)
        gw.set_mute(True, OP[5]); gw.media('play', OP[6]); self.clock.wait(1)
        gw.media('pause', OP[7]); gw.toast('TV_MUTE_VERIFIED', OP[8])
        sent = {uri for uri, _ in e.transport.requests}
        self.assertEqual(sent, {uri for uri, _, _ in SSAP.values()})
        self.assertTrue(set(e.transport.launched) <= set(APP_IDS.values()) | {HOME_ID, BROWSER_ID})

    def test_manifest_has_no_input_close_settings_or_power(self):
        self.assertFalse(set(P4_MANIFEST) & set(FORBIDDEN_PERMISSIONS))
        for name in P4_MANIFEST:
            for word in ('POINTER', 'MOUSE', 'KEYBOARD', 'JOYSTICK', 'TEXT', 'POWER', 'SETTING', 'CLOSE', 'SCREEN'):
                self.assertNotIn(word, name)

    def test_phase_gating_without_connecting(self):
        e = self.gw('P3')
        for action in (lambda g: g.volume_step('up', OP[0]), lambda g: g.set_mute(True, OP[1]),
                       lambda g: g.media('pause', OP[2]), lambda g: g.toast('TV_MUTE_VERIFIED', OP[3]),
                       lambda g: g.volume_read()):
            with self.assertRaisesRegex(Refused, '^TV_TOOL_UNAVAILABLE$'):
                action(e.gateway)
        self.assertEqual(e.transport.connects, 0)

    def test_op_ids_are_one_use_and_hex(self):
        e = self.gw()
        self.assertEqual(e.gateway.home(OP[0])[0], 'accepted')
        self.clock.wait(1)
        for op in (OP[0], 'XYZ', OP[1].upper(), None, OP[1] + '0'):
            with self.subTest(op=op), self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
                e.gateway.home(op)
        self.assertEqual(e.transport.launched, [HOME_ID])
        self.assertEqual(len(set(op_ids('n:volume', 5))), 5)

    def test_payload_table_is_closed(self):
        e = self.gw('P5')
        bad = [('launch', {'id': 'com.webos.app.settings'}), ('launch', {'id': 'netflix', 'params': {'x': 1}}),
               ('launch', {'id': BROWSER_ID, 'params': {'contentTarget': 'https://evil.example'}}),
               ('launch', {'id': HOME_ID, 'extra': 1}), ('set_mute', {'mute': 'yes'}),
               ('toast', {'message': 'free text the owner said'}), ('volume_up', {'by': 10}),
               ('foreground', {'appId': 'x'})]
        for name, payload in bad:
            with self.subTest(name=name, payload=payload), self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
                e.gateway._send(name, payload, OP[9])
        self.assertEqual(e.transport.requests, [])
        with self.assertRaises(KeyError):
            e.gateway._send('getPointerInputSocket', {}, OP[10])

    def test_launch_requires_live_installed_list(self):
        e = self.gw()
        e.transport.installed = [i for i in e.transport.installed if i != 'hulu']
        with self.assertRaisesRegex(Refused, '^TV_APP_NOT_INSTALLED$'):
            e.gateway.launch_app('hulu', OP[0])
        with self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
            e.gateway.launch_app('com.webos.app.settings', OP[1])
        self.assertEqual(e.transport.launched, [])

    def test_open_browser_builds_target_from_gateway_constant(self):
        e = self.gw()
        self.assertEqual(e.gateway.open_browser('b' * 32, OP[0])[0], 'accepted')
        self.assertEqual(e.transport.launched, [BROWSER_ID])
        for session in ('B' * 32, 'b' * 31, 'https://evil.example', None):
            with self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
                e.gateway.open_browser(session, OP[1])
        self.private_file('open-url.json', {'url': 'http://192.0.2.99:4181/%s/open' % ('a' * 64)}, base=e.base)
        with self.assertRaisesRegex(Refused, '^TV_CHANNEL_UNAVAILABLE$'):
            e.gateway.open_browser('b' * 32, OP[2])

    def test_open_url_follows_the_configured_spark_address(self):
        url = 'http://198.51.100.7:4181/%s/open' % ('a' * 64)
        self.assertIsNotNone(tvmod.open_url_pattern('198.51.100.7').fullmatch(url))
        self.assertIsNone(tvmod.open_url_pattern('198.51.100.7').fullmatch(url.replace('.7:', '.70:')))
        self.assertIsNone(tvmod.OPEN_URL.fullmatch(url))
        for bad in ('198.51.100.256', '198.51.100', 'spark.local', '198.51.100.7:4181', '198.51.100.7\n', None, 1):
            with self.assertRaises(ValueError):
                tvmod.open_url_pattern(bad)
        e = self.gw()
        g = tvmod.TVGateway(e.transport, key_path=e.key_path, pin_path=e.pin_path, open_url_path=e.open_path,
                            killswitch=e.kill, phase='P4', clock=self.clock, spark_ip='198.51.100.7')
        # The environment's open URL names the default address, so this gateway refuses it.
        with self.assertRaisesRegex(Refused, '^TV_CHANNEL_UNAVAILABLE$'):
            g.open_browser('b' * 32, OP[3])
        self.private_file('open-url.json', {'url': url}, base=e.base)
        self.assertEqual(g.open_browser('b' * 32, OP[4])[0], 'accepted')
        self.assertEqual(e.transport.launched, [BROWSER_ID])

    def test_independent_rate_limits_and_single_flight(self):
        e = self.gw()
        e.gateway.home(OP[0]); e.gateway.home(OP[1])
        with self.assertRaisesRegex(Refused, '^RATE_LIMITED$'):
            e.gateway.home(OP[2])                         # 2 requests/s
        self.assertEqual(len(e.transport.launched), 2)
        self.clock.wait(1)
        self.assertTrue(e.gateway.inflight.acquire())
        try:
            with self.assertRaisesRegex(Refused, '^TV_CHANNEL_UNAVAILABLE$'):
                e.gateway.home(OP[3])                     # one request in flight
        finally:
            e.gateway.inflight.release()

    def test_observations_are_enumerated(self):
        e = self.gw()
        e.transport.foreground_id = 'com.lg.app.signage'
        self.assertEqual(e.gateway.foreground(), 'other')
        e.transport.foreground_id = APP_IDS['disney_plus']
        self.assertEqual(e.gateway.foreground(), 'disney_plus')
        self.assertEqual(e.gateway.installed_keys(), frozenset(APP_IDS))
        self.assertEqual(e.tv.surface(), 'app_disney_plus')

    def test_toast_is_template_only(self):
        e = self.gw()
        with self.assertRaisesRegex(Refused, '^TOOL_SCHEMA$'):
            e.gateway.toast('a free-text message', OP[0])
        self.assertTrue(e.tv.toast('TV_MUTE_VERIFIED'))


if __name__ == '__main__':
    unittest.main()
