# Local capture owner

This service connects the early remote voice-key gate to a supervised native capture provider and the host's mutually authenticated PCM listener. It keeps device access, transport, and command execution under separate owners.

## Lifecycle

Press prepares one authenticated utterance and one provider. The keyfilter obtains the actual remote record before requesting streaming. Release requests native STOP, drains accepted bytes, reconciles the completion counters, and reaps the provider before completing the utterance. Cancellation discards the utterance. A successful process exit alone is insufficient to prove native STOP or audio completeness.

A held press is released at its 9.2-second hold limit (`controller.js` `HOLD_MS`) and submitted like a key-up; the press feedback calls that release a cut. The provider lease (`provider.maxMs`, 10000 ms, the native cap) outlasts it by 800 ms, as 6000 did the earlier 5.2-second limit; a config still carrying 6000 runs, but holds past about 6 seconds then end in a fault. A press the keyfilter never releases is cancelled after 13 seconds, and the PCM transport bounds hello to release at 15 seconds, under Spark's 16.

| Module | Responsibility |
| --- | --- |
| `controller.js`, `service_port.js`, `patch_keyfilter.py` | Early key event and correlated begin/release/cancel requests |
| `bootstrap_entry.js`, `bus_context.js`, `main.js` | Genuine service bootstrap, validated configuration, power monitoring, and owner lifetime |
| `owner_service.js`, `session.js` | One utterance, transport preparation, cancellation, and cleanup |
| `ui_capture.js`, `ui_heartbeat.js`, `ui_qualification.js` | Authenticated on-screen commands, genuine owner bus calls, and private target-trial admission |
| `supervised_provider.js`, `legacy_provider.js` | Held native image, child protocol, PCM draining, and final receipts |
| `power_reader.js` | Bounded default-identity power query; unknown/stale power refuses capture |
| `voice_feedback.js` | Physical-press phase pings, surface-aware toasts and Spark receipts; feedback only |
| `quiescence.js` | Current-boot competing-owner checks |
| Native C/C++ sources | Exact-build provider hooks and syscall supervision |

## Activation contract

An installer must provide all of the following; the public build renderer does not create them:

- Exact original provider image and matching native helpers. See [NATIVE.md](NATIVE.md).
- Exclusive hardware ownership, current-boot migration evidence, and [quiescence](QUIESCENCE.md).
- The actual original executable opened read-only before any exclusion mount, retained by an independent owner, and inherited through `--original-fd`. A numeric descriptor in JSON is not evidence of that handoff.
- Narrow effective bus permissions and the early keyfilter gate, with a verified recovery path outside the compositor's process group.
- Root-private configuration, enrolled TLS identity, certificate pins, and the host endpoint.

[`main.configuration`](main.js) is the authoritative configuration schema, including the exact internal mode identifier, provider hashes/paths, migration record, and endpoint fields. Preserve those protocol identifiers when installing. The configuration must be a protected single-link `0600` regular file. Keep keys and identity files on the target; do not publish them.

Use the platform's genuine bootstrap via [`bootstrap_entry.js`](bootstrap_entry.js), not a fabricated service identity. The bootstrap, original-descriptor producer, permissions, and exclusion mounts are one deployment contract. A copied original binary can have different executable-based bus permissions; do not assume it is interchangeable with the held original.

## On-screen capture

The optional UI path uses the same owner, power checks, current-utterance lock,
held provider, PCM transport and cleanup receipts as the physical path. It
does not generate key events. Its explicit `ui-capture` native mode registers
`/r2d2-ui/getDevices`, `startStreaming` and `stopStreaming`; the native gate
requires the actual `local.owner.r2d2mic` LS2 sender and that category. The
physical root category keeps its keyfilter-only capture rule. Root category
and method callback data are mirrored to preserve the original native objects.

`main.js` defaults to UI unavailable. To authorize an exact installed candidate
for a target trial, its private configuration must include both
`uiQualificationPath` and `uiCaptureKeyPath`. Each refers to a canonical,
root-owned, single-link `0600` file below protected directories. The trial
record has exactly these fields:

```json
{
  "schema": 1,
  "owner": "local.owner.r2d2mic",
  "category": "/r2d2-ui",
  "backendProfile": 1,
  "providerSha256": "2b9f9e318fba385801e160349159cb3238d1fc370136a385413168170879e28b",
  "moduleSha256": "<configured native module hash>",
  "supervisorSha256": "<configured supervisor hash>",
  "enabledForTargetTrial": true
}
```

This is authorization to test the candidate, not evidence of successful
capture. Build against the actual target ABI and test its genuine category
permissions, START, PCM, STOP and retirement; retain those results separately.
No record or key is created by startup. The key file contains an independently
generated 32-byte key encoded as 64 lowercase hex characters, optionally with
a trailing newline. It must differ from the existing routing key. Provision
the matching independent key on Spark without placing it in web preferences.

The UI control loop polls every 500 ms, independently of the five-second power
loop, with one outstanding request and a 1500 ms timeout. Its HMAC protocol
binds the owner generation, broker session, sequence and nonce. A broker change
is accepted only after cleanup and must be echoed before a command can run.
The browser routing key provides no UI command authentication. Channel loss
cancels the current UI utterance.

An explicit UI release waits for the native STOP response before releasing the
PCM session. The 5.2-second UI ceiling cancels and discards; it never submits
automatically. Cancellation after release reports an unknown action outcome,
because a dispatched command cannot be undone. Native START/STOP and accepted
status come from runtime replies and final receipts. Uncertain native or local
bus cleanup prevents a subsequent capture.

The owner adapter uses the platform's documented
[palmbus Call-object API](https://github.com/webosose/nodejs-module-webos-sysbus)
on the already established genuine handle. The new category still requires
target testing; host tests do not establish firmware compatibility.

## Press feedback

`voice_feedback.js` gives physical presses the on-screen voice UI
without taking part in capture. The owner tells it about an accepted press,
the first admitted PCM chunk, a valid release, the closed session receipt and
fresh presses it refused; nothing it does is awaited, and a missing or failing
module leaves every capture decision unchanged.

- Phase pings `pressed`, `listening`, `released` and `fault` go to
  `POST /<voiceKey>/voice-phase` on the HTTP port, fire-and-forget with a
  300 ms budget. The body is canonical sorted-key JSON with an HMAC-SHA256
  over `R2D2-voice-phase-v1\0` plus that text, keyed by the PCM device key,
  and `(boot, seq)` strictly increases. The key loads after the current turn
  and is wiped after the press's last ping. `voice/tv/test_voice_phase_golden.json`
  fixes the bytes.
- A toast appears only when the reply's surface is `other` (any doubt counts
  as `other`). Results come from `GET /<voiceKey>/voice-receipt/<press>`,
  asked again while it answers `done:false` for up to 15 s. A 4xx (an older
  Spark has no route) keeps the fixed code mapping at once; no answer at all
  becomes `RESULT_UNCONFIRMED`.
- A quick tap (cancelled before the provider was ready) and a refused press
  (`BUSY`, or `STOP_UNCONFIRMED` while cleanup is unresolved) toast at most
  once every 3 s. A newer press supersedes an older one and silences its
  pending result.
- When the owner latches (no new capture until the TV restarts) or clears that
  latch, it tells Spark why with `POST /<voiceKey>/owner-latch`: closed keys
  `{schema, boot, seq, state, reason, facts, mac}`, `reason` from a closed
  allowlist (else `OTHER`) and `facts` a bitmask of the receipt's booleans,
  MAC'd like the phase pings over `R2D2-owner-latch-v1\0`. It shares their
  `(boot, seq)` counter and 300 ms budget, ignores the reply (an older Spark
  answers 404), and is resent every 30 s while latched (120 times; a refused
  press re-arms that budget).

## Owner latch

The owner refuses every new capture while a previous one might still be live
(`unresolved`). Two cases no longer latch for good:

- A launch refused before any process was spawned (the supervised provider's
  precondition and `/proc` checks) closes with `providerStarted:false` and
  `cleanupComplete:true`. Only `supervised_provider.js`'s own typed refusal
  counts; any throw at or after the spawn call stays unknown.
- The session's `settled` promise resolves with the provider's own receipt if
  it arrives within 3 s of the close (else `null`). The owner keeps the latch
  while it waits (a fresh press is only `BUSY`) and clears it only when that
  receipt passes the same clean test as an on-time receipt (`session.evidence`:
  reaped, stopped and sealed). The press then settles as `CAPTURE_LOST`;
  anything else stays latched with `STOP_UNCONFIRMED`.
- Toasts use the closed `FAULTS`/code tables or Spark's validated text,
  printable ASCII up to 60 characters, sent through the pinned
  `/usr/bin/luna-send` (`com.webos.notification/createToast`): the owner's
  LS2 role allows only `bluetooth2` and `tvpower`. No transcript or free text
  is sent, logged or shown.

## Checks

```sh
for f in voice/tv/local-capture/test_*.js; do node "$f"; done
python3 -B -m unittest discover -s voice/tv/local-capture -p 'test_*.py'
```

These tests exercise lifecycle, protocol, and native logic offline. Physical voice commands work in the reference deployment; a new target still needs press/release, repeated-command, cancellation, standby, and recovery testing.
