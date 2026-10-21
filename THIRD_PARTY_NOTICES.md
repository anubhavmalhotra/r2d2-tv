# Third-party notices

Original project code and documentation use the [MIT License](LICENSE.md).
Files that declare GPL-3.0-or-later and separately identified third-party
material keep their own terms. The MIT license does not relicense that material
or grant trademark rights.

## GPL-3.0-or-later components

| Path | Terms |
| --- | --- |
| `spark/runtime/extension/`: authored adapter, CSS, popup guard, proof page, manifest and rules | GPL-3.0-or-later. Keep its [license](spark/runtime/extension/LICENSE.in) and [notice](spark/runtime/extension/NOTICE.txt.in) with builds. |
| `spark/runtime/profile_stage.py.in`, `spark/runtime/popup_census.py.in` | GPL-3.0-or-later, declared in each file. |
| `protection/convert.py`, `protection/cosmetics.py` | GPL-3.0-or-later, declared in each file. |
| `protection/upstream/` | Unmodified upstream filter data under the terms below. [Provenance](protection/provenance.json) records each file's upstream path and SHA-256. |
| `spark/runtime/extension/rules/`, `spark/runtime/extension/generic.css.in` | Derived from `protection/upstream/` by `convert.py` and `cosmetics.py`. |

The GPL text is in [protection/LICENSE](protection/LICENSE) and
[spark/runtime/extension/LICENSE.in](spark/runtime/extension/LICENSE.in).
[protection/NOTICE.md](protection/NOTICE.md) lists which `protection/` files
are GPL. The distributed Spark runtime (the rendered `spark/` output) combines
MIT-licensed project code with these GPL-3.0-or-later components. Each keeps
its own license. When distributing it, include these notices and offer the
corresponding source; this repository at the matching commit is that source.

## Upstream filter data

| Material | Authors | License | Source |
| --- | --- | --- | --- |
| uBlock Origin Lite compiled rulesets | Raymond Hill | GPL-3.0-or-later | [uBOL-home](https://github.com/uBlockOrigin/uBOL-home) at commit `0d79e9ea78e8bfe31fa139abbce5d63957638fa3` |
| EasyList and EasyPrivacy | The EasyList authors | GPL-3.0-or-later or CC BY-SA 3.0 Unported or later; used here under GPL-3.0-or-later | [easylist.to](https://easylist.to/), via uBOL-home |
| uBlock filters - Badware risks | uBlock Origin uAssets contributors | GPL-3.0 | [uAssets](https://github.com/uBlockOrigin/uAssets), via uBOL-home |

## Optional hardened uBlock Origin Lite build

[`protection/hardened-ubol/`](protection/hardened-ubol/README.md) rebuilds
uBlock Origin Lite release `2026.1012.1545` (Raymond Hill, GPL-3.0-or-later)
with deterministic modifications. The resulting extension is a modified uBOL
under GPL-3.0-or-later and keeps the upstream `LICENSE.txt` and the license
files of the libraries uBOL bundles, including the Inter font
(`css/fonts/Inter/`, The Inter Project Authors, SIL Open Font License 1.1).
The builder, patch scripts, pins and tests in that directory are project code
under the MIT License. Acorn (MIT) is used only at build time and is not in the
output. No upstream archive or build output is stored in this repository.

## Vendor firmware interoperability

The repository contains no vendor firmware, binaries or application packages.
For interoperability with one reference firmware build, a few files carry
SHA-256 pins of specific vendor files and the short identifier or anchor strings
needed to locate patch points:
[`voice/tv/patch_firmware.py`](voice/tv/patch_firmware.py),
[`voice/tv/local-capture/patch_keyfilter.py`](voice/tv/local-capture/patch_keyfilter.py),
and the native sources in [`voice/tv/local-capture/`](voice/tv/local-capture/),
which name vendor symbols and code offsets. Patches are applied by the operator
to files on their own device.

## External dependencies

Chromium, FFmpeg, GStreamer, PipeWire, WirePlumber, libsbc, UxPlay, CUDA
libraries, Python packages and model weights (by default Qwen3-ASR-0.6B and
Qwen3-4B-Instruct-2507) are external dependencies, not included or relicensed
here. Obtain them from their upstream distributors and follow their licenses.
Playwright is a development dependency for UI tests.

## Names, logos and artwork

Brand logos are not shipped. Home shows text tiles unless logos are supplied
privately at build time; whoever supplies them is responsible for having the
right to use them. The project name R2D2 refers to the Star Wars character
R2-D2 and is used without any license from Lucasfilm Ltd. or The Walt Disney
Company. The Home mascot is original artwork made for this project.

LG, webOS, NVIDIA, DGX Spark, Chromium, AirPlay, Star Wars, R2-D2 and the
application names belong to their respective owners. This is an independent
project with no affiliation or endorsement.
