# Reproducible content-blocker build

This is a modified uBlock Origin Lite build based on version `2026.1012.1545`, commit `0d79e9ea78e8bfe31fa139abbce5d63957638fa3`. Upstream attribution and license terms remain applicable; see [third-party notices](../../THIRD_PARTY_NOTICES.md).

[`build.py`](build.py) verifies the pinned upstream archive and local transformations against [`upstream-tree.json`](upstream-tree.json) and [`expected-files.json`](expected-files.json). [`patch.py`](patch.py) and [`strip-console.mjs`](strip-console.mjs) are the corresponding modification source.

The modifications remove the session error ring/request-logger API, visited-host cosmetic cache, console calls, external list downloads, URL-sharing issue flow, and unused `userScripts` permission. Bundled filtering/scriptlets remain. This is reviewable hardening, not proof that any extension is free of defects or that every advertisement is blocked.

## Build

Requires Python 3.9+ and Node.js 18+. Supply these pinned public inputs locally; the builder does not fetch them:

- [uBOLite_2026.1012.1545.chromium.zip](https://github.com/uBlockOrigin/uBOL-home/releases/download/2026.1012.1545/uBOLite_2026.1012.1545.chromium.zip)
- [acorn-8.15.0.tgz](https://registry.npmjs.org/acorn/-/acorn-8.15.0.tgz)

```sh
python3 -B protection/hardened-ubol/build.py \
  --upstream-zip "$UPSTREAM_ZIP" \
  --acorn-tar "$ACORN_TAR" \
  --output .build/hardened-ubol
```

Check input snapshots separately:

```sh
python3 -B protection/hardened-ubol/test_input_snapshot.py \
  --builder-dir protection/hardened-ubol \
  --upstream-zip "$UPSTREAM_ZIP" \
  --acorn-tar "$ACORN_TAR" \
  --report .build/input-snapshot-check.json
```

Keep original copyright/license notices with redistributed output and provide the required corresponding source under its applicable license. Do not present the modified build as an unmodified upstream release.
