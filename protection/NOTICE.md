# Protection notices

| Files | Terms |
| --- | --- |
| [`convert.py`](convert.py), [`cosmetics.py`](cosmetics.py) | GPL-3.0-or-later, declared in each file |
| [`upstream/`](upstream/) | Unmodified upstream filter data; terms below |
| [`LICENSE`](LICENSE) | GNU General Public License, version 3 |
| `README.md`, `NOTICE.md`, [`provenance.json`](provenance.json), [`hardened-ubol/`](hardened-ubol/README.md) tooling | Project files under the [MIT License](../LICENSE.md) |

`upstream/` holds compiled rulesets from
[uBlock Origin Lite](https://github.com/uBlockOrigin/uBOL-home) commit
`0d79e9ea78e8bfe31fa139abbce5d63957638fa3`:

- uBlock Origin Lite, Copyright (C) 2022-present Raymond Hill; GPL-3.0-or-later.
- EasyList and EasyPrivacy, The EasyList authors ([easylist.to](https://easylist.to/));
  GPL-3.0-or-later or CC BY-SA 3.0 Unported or later, used here under
  GPL-3.0-or-later.
- uBlock filters - Badware risks, uBlock Origin
  [uAssets](https://github.com/uBlockOrigin/uAssets) contributors; GPL-3.0.

The rules and CSS derived from these files in `spark/runtime/extension/` are
GPL-3.0-or-later. The extension produced by `hardened-ubol/` is a modified
uBlock Origin Lite under GPL-3.0-or-later and keeps upstream notices, including
the Inter font's SIL Open Font License 1.1. Keep this notice, the license and
the provenance record with redistributed filter data or builds. See the
project [third-party notices](../THIRD_PARTY_NOTICES.md).
