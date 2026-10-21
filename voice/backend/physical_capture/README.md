# Capture and credential contracts

`ram_credentials.py` loads TLS material through sealed anonymous Linux memory descriptors and closes temporary descriptors after context construction. It verifies mutual TLS settings and wipes mutable temporary buffers. This reduces temporary-file exposure; it is not a claim that a language runtime can erase every internal copy.

`capture_contract.py`, `cleanup_join.py`, `remote_lifecycle.py`, and `physical_consumer.py` validate bounded byte/frame accounting, one-use authority, and actual child/pipe/socket cleanup. EOF alone is not a successful native capture receipt.

```sh
python3 -B tools/check_voice_backend.py
python3 -B -m voice.backend.physical_capture.mutations
python3 -B -m voice.backend.physical_capture.lifecycle_mutations
python3 -B -m voice.backend.physical_capture.ram_credentials_mutations
```

See [the backend guide](../README.md) for the production ingress and resident owner.
