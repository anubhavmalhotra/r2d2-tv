/* Authored press-to-talk arbitration contract. No LG/cloud API or UI fallback.
 * Instantiate in the compositor's earliest voice-key path only after its global
 * priority and owner capture permission are qualified. This file installs nothing.
 */
function createVoiceRoute(voiceKeys, owner, now) {
    if (!Array.isArray(voiceKeys) || voiceKeys.length < 1 || voiceKeys.length > 8 ||
        voiceKeys.some(function (key) { return !Number.isInteger(key); }) ||
        new Set(voiceKeys).size !== voiceKeys.length || typeof now !== 'function') {
        throw new Error('VOICE_ROUTE_CONFIG');
    }
    var active = false;
    var started = null;
    var completed = 0;
    var closed = false;
    var stopUnconfirmed = false;
    function send(event) {
        if (!owner || owner.qualified !== true || typeof owner.send !== 'function') return false;
        try { return owner.send(event) === true; } catch (_) { return false; }
    }
    function cancel() {
        if (active && !send('cancel')) { stopUnconfirmed = true; closed = true; }
        active = false;
        started = null;
    }
    function tick() {
        if (active && (!Number.isFinite(now()) || now() - started < 0 || now() - started >= 10000)) cancel();
    }
    return {
        handle: function (key, pressed, repeat) {
            if (voiceKeys.indexOf(key) === -1) return false;
            // Always consume the protected keys, including malformed/unavailable,
            // startup, duplicate/repeat, cancellation, and rejected local requests.
            tick();
            if (closed || typeof pressed !== 'boolean' || typeof repeat !== 'boolean') return true;
            if (pressed) {
                if (!repeat && !active && completed < 256) {
                    completed += 1;
                    if (send('down')) { active = true; started = now(); }
                }
            } else if (active) {
                if (!send('up')) {
                    // Best-effort owner cancellation, then retire this route.
                    // Neither ACK means physical capture stopped: the owner
                    // daemon needs its independent watchdog/caller teardown.
                    send('cancel');
                    stopUnconfirmed = true;
                    closed = true;
                }
                active = false;
                started = null;
            }
            return true;
        },
        tick: tick,
        cancel: cancel,
        close: function () { cancel(); closed = true; },
        active: function () { return active; }, // Routing state, not physical mic state.
        stopUnconfirmed: function () { return stopUnconfirmed; }
    };
}
if (typeof module !== 'undefined') module.exports = { createVoiceRoute: createVoiceRoute };
