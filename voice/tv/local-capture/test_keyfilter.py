import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patch_keyfilter as p


class KeyfilterWiringTests(unittest.TestCase):
    def source(self):
        # Authored fixture. It is not redistributed proprietary firmware.
        q = ('import "base"\nItem {\n' + p._base.SIGNAL +
             '    property string ownerHome: "preserve-owner-home"\n' +
             '    property var callbackMap: ({})\n' +
             '    property string caller: \"com.webos.starfish.keyfilters\"\n' +
             '    property var service: Service { appId: "com.webos.starfish.keyfilters" }\n' +
             '        onKeyFiltersChanged: {\n            preserveOtherFilters();\n        }\n' +
             p._base.GATE + '}\n').encode()
        a = ('function handleHotkeys() {\n' + p._base.VOICE_GUARD +
             '    case Other_Key: preserveOriginalControls();\n}\n').encode()
        return dict(zip(p.NAMES, (q, a)))

    def test_wiring_preserves_every_unrelated_byte(self):
        original = self.source()
        pins = {n: hashlib.sha256(b).hexdigest() for n, b in original.items()}
        result = p.wire_prepared(original, pins, '/fixture/owner/local-capture')
        self.assertEqual(result['appLaunch.js'], original['appLaunch.js'])
        text = result['StarfishKeyFilter.qml'].decode()
        self.assertIn('onOwnerVoiceKeyEvent:', text)
        self.assertIn('root.ownerLocalCapture.key(key, pressed, autoRepeat, deviceId)', text)
        self.assertIn('root.ownerLocalCapture.tick()', text)
        self.assertIn('root.ownerLocalCapture.cancel()', text)
        self.assertIn('root.ownerLocalCapture.close()', text)
        self.assertIn('file:///fixture/owner/local-capture/controller.js', text)
        restored = text.replace(p.WIRE, '').replace(p.CANCEL, '')
        restored = '\n'.join(line for line in restored.split('\n')
                             if not line.startswith('import "file://'))
        self.assertEqual(restored.encode(), original['StarfishKeyFilter.qml'])
        dotted = p.wire_prepared(original, pins, '/var/lib/local.owner.r2d2mic/local-capture')
        self.assertIn(b'file:///var/lib/local.owner.r2d2mic/local-capture/controller.js', dotted['StarfishKeyFilter.qml'])

    def test_drift_reapplication_and_injected_import_refuse(self):
        original = self.source()
        pins = {n: hashlib.sha256(b).hexdigest() for n, b in original.items()}
        for path in ('https://example.invalid/scripts', '/fixture/../owner', '/fixture/owner";run()', '/fixture/owner/'):
            with self.assertRaises(ValueError):
                p.wire_prepared(original, pins, path)
        bad = dict(original); bad['appLaunch.js'] += b'changed'
        with self.assertRaisesRegex(ValueError, 'PREIMAGE_DRIFT'):
            p.wire_prepared(bad, pins, '/fixture/owner')
        done = p.wire_prepared(original, pins, '/fixture/owner')
        with self.assertRaisesRegex(ValueError, 'OWNER_GATE_SCOPE'):
            p.wire_prepared(done, {n: hashlib.sha256(b).hexdigest() for n,b in done.items()}, '/fixture/owner')

    def test_in_place_bridge_wires_app_launch_without_compositor_restart(self):
        app = (p.APP_LAUNCH_INIT + '\nfunction handleHotkeys() {\n' + p._base.VOICE_GUARD +
               '    case Other_Key: preserveOriginalControls();\n}\n').encode()
        sha = hashlib.sha256(app).hexdigest()
        bridged = p.wire_in_place_bridge(app, sha, '/var/lib/local.owner.r2d2mic/local-capture')
        text = bridged.decode()
        self.assertIn('function initOwnerVoiceBridge()', text)
        self.assertIn('function handleVoiceOwner(key, pressed, autoRepeat, deviceId)', text)
        self.assertIn('initOwnerVoiceBridge();', text)
        self.assertIn('file:///var/lib/local.owner.r2d2mic/local-capture/controller.js', text)
        self.assertIn('file:///var/lib/local.owner.r2d2mic/local-capture/service_port.js', text)
        self.assertIn('ownerVoiceBridge.ctrl.key(key, pressed, autoRepeat, deviceId)', text)
        self.assertIn('return handleVoiceOwner(key, pressed, autoRepeat, deviceId);', text)
        # appLaunch.js hands its lunaCall to the QML bridge through setup(); the QML text
        # itself never names lunaCall (Qt 5 does not marshal it into createQmlObject).
        self.assertIn('ownerVoiceBridge.setup(lunaCall);', text)
        self.assertIn('lc.send(svc, method, payload, onReply);', text)
        self.assertIn('lc.subscribe(svc, method, payload, onReply, true);', text)
        self.assertIn('lc.unsubscribe(realToken);', text)
        qml = text[text.index('var qml = '):text.index('Qt.createQmlObject(')]
        self.assertNotIn('lunaCall', qml)
        with self.assertRaisesRegex(ValueError, 'OWNER_GATE_SCOPE'):
            p.wire_in_place_bridge(bridged, hashlib.sha256(bridged).hexdigest(),
                                   '/var/lib/local.owner.r2d2mic/local-capture')


if __name__ == '__main__':
    unittest.main()
