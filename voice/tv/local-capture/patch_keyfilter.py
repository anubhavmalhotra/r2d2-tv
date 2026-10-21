"""Offline wiring for an already prepared owner Voice gate. No target writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re

_source = Path(__file__).resolve().parent.parent / 'patch_firmware.py'
_spec = importlib.util.spec_from_file_location('owner_voice_patch', _source)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
NAMES = ('StarfishKeyFilter.qml', 'appLaunch.js')
WIRE = '''
    // Owner local capture: uses the existing keyfilter LS2 identity.
    property var ownerLocalCapture: OwnerLocalCapture.create(root.lunaCall,
        WebOS.Key_webOS_Voice, function() { return Date.now(); },
        {portFactory: OwnerLocalCapturePort.create})
    onOwnerVoiceKeyEvent: {
        if (root.ownerLocalCapture)
            root.ownerLocalCapture.key(key, pressed, autoRepeat, deviceId);
    }
    Timer {
        interval: 20
        repeat: true
        running: true
        onTriggered: {
            if (root.ownerLocalCapture)
                root.ownerLocalCapture.tick();
        }
    }
    Component.onDestruction: {
        if (root.ownerLocalCapture)
            root.ownerLocalCapture.close();
    }
'''
CANCEL = '''            if (root.ownerLocalCapture)
                root.ownerLocalCapture.cancel();
'''
APP_LAUNCH_INIT = '''function init() {
    isSystemUIPreloaded = false;
    pmLog.info("keyfilter_init", {});
    lunaCall.subscribe('com.webos.service.config', '/getConfigs', '{"configNames": ["profile.keyfilter"]}', cb_applyKeyfilterProfile, true);
}'''


APP_LAUNCH_VOICE_GUARD = '''    case WebOS.Key_webOS_Voice:
        // Second guard: protected mic never dispatches vendor recognition/UI.
        // The earliest compositor gate owns physical press/release delivery.
        return handleVoiceOwner(key, pressed, autoRepeat, deviceId);
'''


def app_launch_bridge(module_directory):
    controller_url = 'file://' + module_directory + '/controller.js'
    port_url = 'file://' + module_directory + '/service_port.js'
    return (
        'var ownerVoiceBridge = null;\n\n'
        'function initOwnerVoiceBridge() {\n'
        '    try {\n'
        '        if (ownerVoiceBridge) {\n'
        '            try { ownerVoiceBridge.destroy(); } catch (ignored) {}\n'
        '            ownerVoiceBridge = null;\n'
        '        }\n'
        '        var bridgeAppId = "com.webos.surfacemanager.r2d2_" + Date.now();\n'
        '        var qml = \'import QtQuick 2.4\\n\' +\n'
        '            \'import WebOS.Global 1.0\\n\' +\n'
        '            \'import WebOSServices 1.0\\n\' +\n'
        '            \'import ' + json.dumps(controller_url) + ' as OwnerLocalCapture\\n\' +\n'
        '            \'import ' + json.dumps(port_url) + ' as OwnerLocalCapturePort\\n\' +\n'
        '            \'Item {\\n\' +\n'
        '            \'    id: bridge\\n\' +\n'
        '            \'    property var ctrl: null\\n\' +\n'
        '            \'    property bool needsReinit: false\\n\' +\n'
        '            \'    property var bus: Service {\\n\' +\n'
        '            \'        id: bridgeBus\\n\' +\n'
        '            \'        appId: "\' + bridgeAppId + \'"\\n\' +\n'
        '            \'        property var callbackMap: ({})\\n\' +\n'
        '            \'        onResponse: {\\n\' +\n'
        '            \'            try {\\n\' +\n'
        '            \'                var parsedReply = JSON.parse(payload);\\n\' +\n'
        '            \'                if (parsedReply && parsedReply.returnValue === false)\\n\' +\n'
        '            \'                    bridge.needsReinit = true;\\n\' +\n'
        '            \'            } catch (ignored) {}\\n\' +\n'
        '            \'            var item = callbackMap[token];\\n\' +\n'
        '            \'            if (item !== undefined) {\\n\' +\n'
        '            \'                if (!item.isSubscription)\\n\' +\n'
        '            \'                    delete callbackMap[token];\\n\' +\n'
        '            \'                if (typeof item.callback === "function")\\n\' +\n'
        '            \'                    item.callback(payload);\\n\' +\n'
        '            \'            }\\n\' +\n'
        '            \'        }\\n\' +\n'
        '            \'    }\\n\' +\n'
        '            \'    function setup(lc) {\\n\' +\n'
        '            \'        var hidrawSeq = 0;\\n\' +\n'
        '            \'        var hidrawRealTokens = {};\\n\' +\n'
        '            \'        var hBus = {\\n\' +\n'
        '            \'            callbackMap: {},\\n\' +\n'
        '            \'            call: function(uri, method, payload) {\\n\' +\n'
        '            \'                var svc = String(uri).replace(/^luna:\\\\/\\\\//, "");\\n\' +\n'
        '            \'                var parsed = {};\\n\' +\n'
        '            \'                try { parsed = JSON.parse(payload); } catch (ignored) {}\\n\' +\n'
        '            \'                var isSub = parsed.subscribe === true;\\n\' +\n'
        '            \'                hidrawSeq = hidrawSeq >= 1000000000 ? 1 : hidrawSeq + 1;\\n\' +\n'
        '            \'                var token = hidrawSeq;\\n\' +\n'
        '            \'                var onReply = function(resPayload) {\\n\' +\n'
        '            \'                    var item = hBus.callbackMap[token];\\n\' +\n'
        '            \'                    if (item !== undefined) {\\n\' +\n'
        '            \'                        if (!item.isSubscription)\\n\' +\n'
        '            \'                            delete hBus.callbackMap[token];\\n\' +\n'
        '            \'                        if (typeof item.callback === "function")\\n\' +\n'
        '            \'                            item.callback(resPayload);\\n\' +\n'
        '            \'                    }\\n\' +\n'
        '            \'                };\\n\' +\n'
        '            \'                if (isSub) {\\n\' +\n'
        '            \'                    var realToken = lc.subscribe(svc, method, payload, onReply, true);\\n\' +\n'
        '            \'                    hidrawRealTokens[token] = realToken;\\n\' +\n'
        '            \'                } else {\\n\' +\n'
        '            \'                    lc.send(svc, method, payload, onReply);\\n\' +\n'
        '            \'                }\\n\' +\n'
        '            \'                return token;\\n\' +\n'
        '            \'            },\\n\' +\n'
        '            \'            cancel: function(token) {\\n\' +\n'
        '            \'                var realToken = hidrawRealTokens[token];\\n\' +\n'
        '            \'                delete hBus.callbackMap[token];\\n\' +\n'
        '            \'                delete hidrawRealTokens[token];\\n\' +\n'
        '            \'                if (realToken > 0) {\\n\' +\n'
        '            \'                    try { lc.unsubscribe(realToken); } catch (ignored) {}\\n\' +\n'
        '            \'                }\\n\' +\n'
        '            \'            }\\n\' +\n'
        '            \'        };\\n\' +\n'
        '            \'        bridge.ctrl = OwnerLocalCapture.create(bridgeBus, WebOS.Key_webOS_Voice, function() { return Date.now(); }, {portFactory: function(unusedSvc, clk, out) { return OwnerLocalCapturePort.create(hBus, clk, out); }});\\n\' +\n'
        '            \'    }\\n\' +\n'
        '            \'    Timer {\\n\' +\n'
        '            \'        interval: 20\\n\' +\n'
        '            \'        repeat: true\\n\' +\n'
        '            \'        running: true\\n\' +\n'
        '            \'        onTriggered: {\\n\' +\n'
        '            \'            if (bridge.ctrl)\\n\' +\n'
        '            \'                bridge.ctrl.tick();\\n\' +\n'
        '            \'        }\\n\' +\n'
        '            \'    }\\n\' +\n'
        '            \'    Component.onDestruction: {\\n\' +\n'
        '            \'        if (bridge.ctrl)\\n\' +\n'
        '            \'            bridge.ctrl.close();\\n\' +\n'
        '            \'    }\\n\' +\n'
        '            \'}\\n\';\n'
        '        ownerVoiceBridge = Qt.createQmlObject(qml, root, "OwnerVoiceBridge");\n'
        '        if (ownerVoiceBridge && typeof ownerVoiceBridge.setup === "function")\n'
        '            ownerVoiceBridge.setup(lunaCall);\n'
        '        pmLog.info("KEYFILTER_APPLAUNCH", {"ownerVoiceBridgeInit": ownerVoiceBridge && ownerVoiceBridge.ctrl ? "ok" : "null"}, "", true);\n'
        '    } catch (e) {\n'
        '        pmLog.info("KEYFILTER_APPLAUNCH", {"ownerVoiceBridgeErr": String(e)}, "", true);\n'
        '    }\n'
        '}\n\n'
        'function handleVoiceOwner(key, pressed, autoRepeat, deviceId) {\n'
        '    if (key === WebOS.Key_webOS_Voice) {\n'
        '        if (typeof pressed === "boolean" && typeof autoRepeat === "boolean") {\n'
        '            try {\n'
        '                if (!ownerVoiceBridge || !ownerVoiceBridge.ctrl || (pressed && ownerVoiceBridge.needsReinit))\n'
        '                    initOwnerVoiceBridge();\n'
        '                if (ownerVoiceBridge && ownerVoiceBridge.ctrl)\n'
        '                    ownerVoiceBridge.ctrl.key(key, pressed, autoRepeat, deviceId);\n'
        '            } catch (ignored) { /* No vendor fallback on owner failure. */ }\n'
        '        }\n'
        '        return KeyPolicy.Accepted;\n'
        '    }\n'
        '    return KeyPolicy.NextPolicy;\n'
        '}\n\n'
        'function init() {\n'
        '    isSystemUIPreloaded = false;\n'
        '    pmLog.info("keyfilter_init", {});\n'
        '    initOwnerVoiceBridge();\n'
        '    lunaCall.subscribe(\'com.webos.service.config\', \'/getConfigs\', \'{"configNames": ["profile.keyfilter"]}\', cb_applyKeyfilterProfile, true);\n'
        '}'
    )


def wire_in_place_bridge(app_launch_bytes, expected_sha256, module_directory):
    """Wire the zero-compositor-restart QML voice bridge into appLaunch.js."""
    need(type(app_launch_bytes) is bytes and type(expected_sha256) is str and
         re.fullmatch('[a-f0-9]{64}', expected_sha256) and
         hashlib.sha256(app_launch_bytes).hexdigest() == expected_sha256, 'PREIMAGE_DRIFT')
    need(type(module_directory) is str and len(module_directory) <= 512 and
         re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+', module_directory) and
         all(part not in ('.', '..') for part in module_directory.split('/')), 'MODULE_DIRECTORY')
    app = app_launch_bytes.decode('utf-8')
    need(app.count(_base.VOICE_GUARD) == 1 and app.count(APP_LAUNCH_INIT) == 1 and
         'ownerVoiceBridge' not in app and 'handleVoiceOwner' not in app, 'OWNER_GATE_SCOPE')
    app = app.replace(APP_LAUNCH_INIT, app_launch_bridge(module_directory), 1)
    app = app.replace(_base.VOICE_GUARD, APP_LAUNCH_VOICE_GUARD, 1)
    return app.encode('utf-8')


def need(value, code):
    if not value:
        raise ValueError(code)


def wire_prepared(inputs, expected, module_directory):
    """Preserve all non-voice changes, including an owner's Home replacement.

    expected must come from the installer's independently recorded current
    preimages. This function verifies bytes; it does not obtain live evidence.
    """
    need(type(inputs) is dict and set(inputs) == set(NAMES) and
         type(expected) is dict and set(expected) == set(NAMES), 'PREIMAGE_SCOPE')
    for name in NAMES:
        need(type(inputs[name]) is bytes and type(expected[name]) is str and
             re.fullmatch('[a-f0-9]{64}', expected[name]) and
             hashlib.sha256(inputs[name]).hexdigest() == expected[name], 'PREIMAGE_DRIFT')
    need(type(module_directory) is str and len(module_directory) <= 512 and
         re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+', module_directory) and
         all(part not in ('.', '..') for part in module_directory.split('/')), 'MODULE_DIRECTORY')
    qml = inputs[NAMES[0]].decode('utf-8')
    app = inputs[NAMES[1]].decode('utf-8')
    need(qml.count(_base.SIGNAL) == 1 and qml.count(_base.GATE) == 1 and
         app.count(_base.VOICE_GUARD) == 1 and 'ownerLocalCapture' not in qml and
         'Component.onDestruction:' not in qml, 'OWNER_GATE_SCOPE')
    service = 'appId: "com.webos.starfish.keyfilters"'
    need(qml.count(service) == 1 and qml.count('property var callbackMap: ({})') == 1,
         'KEYFILTER_SERVICE_SCOPE')
    marker = 'import "base"\n'
    need(qml.count(marker) == 1, 'IMPORT_SCOPE')
    imports = ''.join('import ' + json.dumps('file://' + module_directory + '/' + file) +
                      ' as ' + alias + '\n'
                      for file, alias in (('controller.js', 'OwnerLocalCapture'),
                                          ('service_port.js', 'OwnerLocalCapturePort')))
    qml = qml.replace(marker, marker + imports, 1)
    qml = qml.replace(_base.SIGNAL, _base.SIGNAL + WIRE, 1)
    marker = '        onKeyFiltersChanged: {\n'
    need(qml.count(marker) == 1, 'RELOAD_SCOPE')
    qml = qml.replace(marker, marker + CANCEL, 1)
    return {NAMES[0]: qml.encode('utf-8'), NAMES[1]: inputs[NAMES[1]]}


def transform_original(inputs, module_directory):
    """Convenience for the exact original build, not current target discovery."""
    prepared = _base.transform(inputs)
    return wire_prepared(prepared, {name: hashlib.sha256(raw).hexdigest()
                                   for name, raw in prepared.items()}, module_directory)


if __name__ == '__main__':
    print('{"preparedOnly":true,"deviceWrites":0,"captureStarted":false}')
