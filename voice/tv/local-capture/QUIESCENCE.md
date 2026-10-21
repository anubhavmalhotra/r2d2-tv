# Current-boot quiescence

`assertQuiescent(recordPath)` checks a root-private record before service registration and capture. It prevents the local provider from overlapping a legacy direct-HID owner. A record is evidence to validate, not permission to assume an old process has stopped.

| Record | Required evidence |
| --- | --- |
| Schema 1: in-boot retirement | Current boot, actual prior PID/start generation absent, and matching retired unit/entry hashes |
| Schema 2: cold-boot tombstone | Current boot and mount namespace, pinned Node identity, exact inert legacy entry, and no matching root Node owner |

The Node tombstone is require-safe: it exports an empty object and exits naturally when run alone. It must not call `process.exit()` when imported by a shared service worker.

Use real current-boot observations to produce the record. Do not invent a PID, unit, or retirement result to fit a schema. The guard does not install exclusion mounts, recover firmware, or prove permanent startup behavior; those remain installer responsibilities.

```sh
node voice/tv/local-capture/test_quiescence.js
```
