# R2D2

Demo: https://anubhavmalhotra.com/vibing/r2d2-webos/r2d2-demo.mp4

Read the story behind the project: [R2D2 for webOS](https://anubhavmalhotra.com/vibing/r2d2-webos/).

A local home screen, private remote browser, and push-to-talk voice assistant for webOS TVs. A Linux compute host runs Chromium and the voice models; the TV provides the display, applications, and remote control.

## Disclaimer

> [!WARNING]
> The TV components run as root and modify vendor system services on one specific firmware build. This is unsupported, may void your warranty, and can leave the TV unusable until a reboot or a factory reset. Use it at your own risk.

- The keyfilter patch, Home/idle routing, and native voice capture replace or hook vendor services. A mistake can disable the remote, Home, or voice until the TV is restored.
- Firmware SHA-256 pins, code offsets, symbol names, and patch anchors target one reference build. Re-derive them for your own firmware; never force a mismatched pin.
- The software is provided as is, without warranty; see [LICENSE.md](LICENSE.md).
- This is an independent project. It is not affiliated with, endorsed by, or sponsored by LG Electronics, Lucasfilm Ltd., The Walt Disney Company, or any streaming service. Product and character names are trademarks of their respective owners.

## Status

Reference deployment on one TV/firmware: Home, the browser, and remote voice commands work end to end there. Other TV, firmware, remote, and host combinations are unvalidated. This repository provides source and offline tests, not a universal root, firmware, or host installer.

## How it fits together

- **Compute host** (`spark/` in paths): an aarch64 Linux machine. It renders a sandboxed Chromium session in a private display and audio session and streams it to the TV over the LAN. It also runs the local speech recognition and intent models and a closed command registry.
- **TV** (`tv/`, `voice/tv/`): the Home application and the browser viewer, which sends remote input back to the host. A root capture owner takes the remote's voice key and sends bounded audio to the host over mutual TLS.
- **Content protection** (`protection/`): pinned filter rules loaded into each disposable browser profile.

Model inference stays on the host. Requested websites, searches, streaming apps, and optional weather still contact their providers; see [SECURITY.md](SECURITY.md).

## Requirements

- Build machine: Python 3.10+ and Node.js 20+.
- Compute host: aarch64 Linux with a non-root service account (UID 1000); the Chromium snap with its sandbox enabled; Xvfb for a private X display; PipeWire, WirePlumber, and the GStreamer plugins for the WebRTC path; FFmpeg with hardware HEVC encoding for the MSE and AirPlay paths; a CUDA-capable GPU and Python 3.12 for the pinned local models (about 10 GB of weights).
- TV: a webOS TV you have system (root) access to and an authorized way to install packages on. Native capture also needs a toolchain and headers matching the exact firmware build.
- Network: TV and host on the same LAN. Measure throughput and jitter; a nominal link rate does not establish playback quality.

## Build locally

```sh
cp config/example.json config.local.json
# Edit the ignored file for your own devices.
python3 tools/build.py --config config.local.json --output dist/local --include-tests
```

The output contains an unsigned Home package, rendered host services, TV service sources, and a file manifest. Building does not install software or contact a device. The output directory must be new.

To enable the browser shortcut, create a private capability key and pass it to the build:

```sh
umask 077
python3 -c 'import secrets; print(secrets.token_hex(32), end="")' > browser.local.key
python3 tools/build.py --config config.local.json --browser-key-file browser.local.key --output dist/local-browser --include-tests
```

Brand logos are not shipped. Home shows text tiles unless you pass `--home-logos-file`, a private JSON object mapping app IDs to `data:` URI logos that you have the right to use. Keep that file and any build made with it private.

Keep the rendered output private: it contains deployment addresses and may contain the browser capability.

## Test

```sh
python3 -B -m unittest discover -s tests
node tests/test_protection_proof.js
node tests/test_home_core.js
python3 tools/privacy_check.py
```

UI tests use Playwright's Chromium:

```sh
npm ci --ignore-scripts
npx --no-install playwright install chromium
npm run test:home
npm run test:browser
```

The complete offline checks are listed in [the CI workflow](.github/workflows/offline.yml). Offline checks do not establish physical video cadence, microphone quality, or compatibility with another firmware build.

## Deploy the host

Stage a complete rendered `spark/` tree as a versioned directory owned by the service account, then run its preflight before enabling the session manager:

```sh
python3 -B "$RENDERED_SPARK/runtime/trial.py" --run --preflight-only
```

`RENDERED_SPARK` points to the staged directory. Preflight checks the sandbox, display/audio, content protection, DNS, and cleanup. It does not install packages or verify TV playback. There is no general host installer; dependency installation and service supervision are host administration.

The runtime keeps its capability key and optional voice configuration under `$HOME/.local/share/baaz-browser/`. `baaz-browser` is a legacy runtime directory name kept for compatibility. Keep those files mode `0600` in private directories. Resident voice also needs pinned model snapshots, TLS credentials, and locally paired TV control; see [the voice backend](voice/backend/README.md).

## Install on the TV

Install the Home package through your webOS package workflow and test it as an ordinary application first. Changing Home and idle routes is a separate step: [the offline Home planner](tv/integration/README.md) produces candidates and rollback payloads from your firmware's captured state.

Voice capture follows [the local-capture activation contract](voice/tv/local-capture/README.md). The public builder does not gain device access, patch system files, or activate services. Keep original files and a recovery path outside any process that a UI restart terminates. Then check remote controls, existing apps, browser playback, consecutive voice presses, standby/resume, and power cycling.

## Source map

| Path | Purpose |
| --- | --- |
| `tv/home/` | Home application templates |
| `tv/integration/` | Offline Home and idle-return planner |
| `spark/` | Browser, media transport, and session manager |
| `voice/` | TV capture, local models, and validated command execution |
| `protection/` | Content-blocking rules and reproducible extension builder |
| `tools/` | Private build, package, and privacy tools |

Keep device configuration, keys, firmware images, and runtime data outside version control. Security boundaries and reporting are in [SECURITY.md](SECURITY.md).

## License

Project code is MIT-licensed ([LICENSE.md](LICENSE.md)). The browser extension, filter tooling and data, and two runtime adapters are GPL-3.0-or-later; [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists those components and upstream attributions.
