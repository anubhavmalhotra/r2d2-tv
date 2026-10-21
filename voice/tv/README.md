# TV voice components

The TV side connects the remote's voice key to bounded local capture and encrypted PCM transport. [Local capture](local-capture/README.md) describes activation requirements.

| Path | Purpose |
| --- | --- |
| `patch_firmware.py` | Exact-source early keyfilter patch |
| `local-capture/` | Capture owner, key lifecycle, native provider, and supervision |
| `transport/` | Mutual TLS PCM client and provider adapter |
| `decoder/` | Standalone mSBC report decoder |
| `resident_mic.js`, `resident_hid.js`, `owner_key.js` | Alternative direct-HID implementation |
| `control/`, `process-boundary/` | Stop retries and process/stream closure checks |

Do not run the direct-HID implementation and local provider against the same remote simultaneously. Native offsets, keyfilter anchors, permissions, and executable hashes are pinned to one reference firmware build; re-derive them for another build.

These components run as root and patch or hook vendor system services. Read the [disclaimer](../../README.md#disclaimer) before installing them.

The [offline CI workflow](../../.github/workflows/offline.yml) runs the TV JavaScript, native, and TLS tests. Focused native checks:

```sh
python3 -B -m unittest discover -s voice/tv/local-capture -p 'test_*.py'
node voice/tv/local-capture/test_controller.js
node voice/tv/local-capture/test_release_drain.js
```
