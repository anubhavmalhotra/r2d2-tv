# Security

R2D2 separates TV input, browser execution, model inference, and command authorization. Its controls reduce exposure; they do not make a website, TV firmware, or local network inherently trusted.

## Root and firmware risk

The TV components run as root and patch or hook vendor system services: the compositor keyfilter, Home and idle routing, and the native voice capture provider. A defect in them runs with full device privileges, and a failed change can disable the remote, Home, or voice until the TV is rebooted or factory reset. Their SHA-256 pins, code offsets, and symbol names target one reference firmware build; on any other build they must be re-derived, not bypassed. Installing them is unsupported by the TV vendor and at your own risk; see the [disclaimer](README.md#disclaimer).

Gaining system access to a TV is outside this project. Whatever method you use, do not leave a root shell or developer service reachable from the network.

## Boundaries

| Component | Intended boundary |
| --- | --- |
| Browser | Chromium sandbox, disposable memory-backed profile, private display/audio session, and content blocking |
| TV capture | One supervised provider per press, bounded audio, native stop acknowledgement, and verified child retirement |
| Voice transport | Mutual TLS 1.3, certificate pins, and a fresh per-utterance HMAC challenge |
| Model worker | Pinned local models, filesystem/network restrictions, memory checks, and no shell tools |
| Commands | Closed tool schema, allowlisted destinations/apps, bounded concurrency, and state verification |
| TV audio & maintenance | 24/7 OS-level sinkhole and null routes for LG telemetry/ACR domains; local DSP auto voice leveling (`autoVolume` and `clearVoice`) hard-gated on the active LG network block with AI/cloud sound modes excluded |
| LAN services | Configured peer checks and capability-bearing requests; keep them on the intended local network |

A normal browser session discards its profile on verified cleanup. Default voice audit handling validates and discards records rather than writing a usage log. Mutable audio buffers are wiped on completion or cancellation. These are application controls, not a claim that operating systems, DNS resolvers, remote sites, or third-party apps retain nothing.

Content filters cannot guarantee that every advertisement or harmful script is blocked. Local speech recognition does not establish a whole-TV network block. Streaming apps and websites still contact their providers; optional weather also needs external services, and ipinfo.io sees the host's public IP when deriving an approximate location.

## Private material

Never commit credentials, cookies, capability URLs, private network details, device identifiers, voice recordings, transcripts, firmware images, or operational logs. Ignored paths are convenient defaults, not access controls.

Before committing selected files:

```sh
python3 tools/privacy_check.py
git diff --cached --stat
git diff --cached
```

The scanner checks staged blobs, including supported embedded payloads. Review the diff as well; a scanner cannot recognize every secret or personal detail.

Keep configuration and key files private to their service account. The manager uses HTTP capability requests; the capability does not encrypt that traffic or make a shared LAN confidential. Voice uses a separate mutually authenticated TLS channel. Do not expose the manager, media, or voice ports through public forwarding. Validate native image hashes and recovery before changing system routing or capture components.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open this repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Describe the affected commit and a minimal reproduction with synthetic data. Do not include keys, recordings, firmware dumps, or private device logs. Problems in Chromium, TV firmware, upstream filter lists, or other dependencies belong with their maintainers; report here when this project's use of them creates the issue.
