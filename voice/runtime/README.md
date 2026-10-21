# Resident voice runtime

`pool.py` owns warm model workers and TV-power-aware lifecycle. It keeps workers ready while the TV is active, uses a standby grace period, and coordinates finite shutdown and worker rotation. Readiness and actual cleanup are separate outcomes; a timeout does not prove a worker has exited.

`result.py` validates action records and discards them by default. Its optional chained writer is for explicit tests/diagnostics, not normal private operation. Neither mode should receive credentials or raw audio.

The build packages these modules into `spark/voice_control/`. [The backend](../backend/README.md) owns ingress and inference; [the agent](../agent/README.md) owns command policy.

```sh
python3 -B -m unittest voice.runtime.test_result voice.runtime.test_pool voice.runtime.test_pool_creator
```
