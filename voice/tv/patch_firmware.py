"""Hash-bound offline transformation. Returns bytes; no filesystem/device writes."""
import hashlib
PINS={'StarfishKeyFilter.qml':'32ee60b96792797c93db1561c7d09b601a13742d0fa7ac35cdad9865c491855d','appLaunch.js':'7975fbb56c38cf36e2b79965ff23e30f7c45052436c117bf9e1531f738f21943'}
SIGNAL='    signal ownerVoiceKeyEvent(var key, var pressed, var autoRepeat, var deviceId)\n'
GATE='''            // Owner voice gate: consume before generic key/boot/vendor work.
            if (key === WebOS.Key_webOS_Voice) {
                if (typeof pressed === "boolean" && typeof autoRepeat === "boolean") {
                    try { root.ownerVoiceKeyEvent(key, pressed, autoRepeat, deviceId); }
                    catch (ignored) { /* No vendor fallback on owner failure. */ }
                }
                return KeyPolicy.Accepted;
            }
'''
VOICE_GUARD='''    case WebOS.Key_webOS_Voice:
        // Second guard: protected mic never dispatches vendor recognition/UI.
        // The earliest compositor gate owns physical press/release delivery.
        return KeyPolicy.Accepted;

'''
def transform(inputs):
    if type(inputs)is not dict or set(inputs)!=set(PINS):raise ValueError('PREIMAGE_SCOPE')
    for n,h in PINS.items():
        if type(inputs[n])is not bytes or hashlib.sha256(inputs[n]).hexdigest()!=h:raise ValueError('PREIMAGE_DRIFT')
    q=inputs['StarfishKeyFilter.qml'].decode('utf-8');a=inputs['appLaunch.js'].decode('utf-8')
    marker='    signal goMagicTips()\n'
    if q.count(marker)!=1:raise ValueError('PATCH_SCOPE')
    q=q.replace(marker,marker+SIGNAL,1)
    marker='        function preProcess(key, pressed, autoRepeat, deviceId) {\n'
    if q.count(marker)!=1:raise ValueError('PATCH_SCOPE')
    q=q.replace(marker,marker+GATE,1)
    start=a.index('    case WebOS.Key_webOS_Voice:',a.index('function handleHotkeys'))
    end=a.index('    case WebOS.Key_webOS_TonePlusVoice:',start)
    prior=a[start:end]
    if "'com.webos.service.voiceconductor', '/setInputEvent'"not in prior or 'return KeyPolicy.Accepted;'not in prior:raise ValueError('PATCH_SCOPE')
    a=a[:start]+VOICE_GUARD+a[end:]
    return {'StarfishKeyFilter.qml':q.encode('utf-8'),'appLaunch.js':a.encode('utf-8')}
if __name__=='__main__':print('{"preparedOnly":true,"nativeCalls":0,"deviceWrites":0,"captureArmed":false}')
