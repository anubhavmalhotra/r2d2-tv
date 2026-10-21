# Browser content protection

The browser loads a pinned Manifest V3 extension into each disposable profile. Startup verifies activation and required rule sets before accepting the session.

| Layer | Source |
| --- | --- |
| Declarative network rules and upstream exceptions | `convert.py` with pinned EasyList, EasyPrivacy, and uBlock Badware inputs |
| Generic cosmetic selectors | `cosmetics.py`, extracted as data without evaluating JavaScript |
| Popup and unexpected-target handling | `spark/runtime/extension/popup_guard.js.in` and `spark/runtime/popup_census.py.in` |
| Reproducible modified extension | [hardened-ubol/](hardened-ubol/README.md) |

Filters reduce known advertising, tracking, and unwanted requests. They do not identify every unsafe script or guarantee an ad-free site. Review compatibility exceptions narrowly; disabling the Chromium sandbox is not an appropriate workaround.

The modified extension's corresponding build source is in [`hardened-ubol/`](hardened-ubol/README.md), including [`patch.py`](hardened-ubol/patch.py), [`strip-console.mjs`](hardened-ubol/strip-console.mjs), upstream/output pins, and build instructions. Retain upstream attribution and the applicable [third-party notices](../THIRD_PARTY_NOTICES.md) when redistributing it. The project license does not replace those terms.

[NOTICE.md](NOTICE.md) lists which files here are GPL-3.0-or-later and the upstream filter-list authors; the GPL text is in [LICENSE](LICENSE).

```sh
python3 -B -m unittest discover -s tests
node tests/test_protection_proof.js
```
