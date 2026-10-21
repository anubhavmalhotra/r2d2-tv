# Local voice backend

The host backend accepts authenticated PCM, runs pinned local speech/intent models, and passes validated actions to the browser or locally paired TV control. The builder packages it into `spark/voice_control/`.

| Area | Modules |
| --- | --- |
| Mutual TLS and per-utterance challenge | `ingress.py`, `service.py`, `physical_capture/ram_credentials.py` |
| Pinned model assets and inference | `pinned_models.py`, `model_assets.py`, `model_worker.py`, `worker_runtime.py` |
| Worker isolation and readiness | `sealed_startup.py`, `process_isolation.py`, filesystem/memory/descriptor guards |
| Browser control and viewer handoff | `browser_channel.py`, `viewer_handoff.py` |
| Paired local TV actions | `tv_transport.py` |
| Resident service lifetime | `shared_owner.py`, `resident_entry.py`, `resident_manager.py` |

The ingress limits utterance size and ordering. Workers initialize using public synthetic data, then verify the configured filesystem, socket, memory, and descriptor restrictions before private input. Unsupported isolation or failed readiness is a refusal, not permission to run a weaker worker.

Default model identities are recorded in `pinned_models.py`. Keep snapshots local and validate them; do not silently substitute a download or unpinned model. No cloud inference is required by this path, while requested websites and search providers can still receive requests.

Private service configuration and credentials are deployment inputs, never repository contents. [SECURITY.md](../../SECURITY.md) describes the limits of in-memory handling and local network controls.

```sh
python3 -B tools/check_voice_backend.py
node voice/backend/test_voice_route.js
node voice/backend/test_open_handoff.js
```
