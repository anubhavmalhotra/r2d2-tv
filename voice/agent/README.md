# Voice command agent

The agent maps one transcript to a validated browser or TV action. During build, `prototype/agent/` is packaged into `spark/voice_control/agent/`. The directory name is historical; enabled behavior comes from the current registry and capability profile.

| Area | Source |
| --- | --- |
| Tool names, argument schemas, destinations, and apps | `registry.py`, `sites.json`, `router.py` |
| Strict parsing and text validation | `parse.py`, `validate.py` |
| Cancel/confirm grammar and negation checks | `fastpath.py`, `confirm.py` |
| Rate limits, pause/TV-off checks, and dispatch | `policy.py`, `killswitch.py`, `agent.py` |
| Browser/TV outcome verification | `executors/` |
| Default in-memory audit validation | `audit.py` |

Supported categories include website/search, browser close/back/media, native app/Home, and optional content/volume/mute operations. Read the [registry](prototype/agent/registry.py) for exact tools and active-profile requirements. A listed tool is not proof that every target app supports its action.

Model output has no shell or arbitrary service authority. Page content and search results are untrusted data. Invalid output, expired authority, a stopped TV, or failed cleanup must refuse dispatch. Default audit handling discards validated records; the chained audit writer is an explicit test/diagnostic option and should not be enabled for normal private operation.

```sh
(cd voice/agent/prototype && python3 -B -m unittest discover -s tests -t .)
(cd voice/agent/prototype && python3 -B -m unittest discover -s tests_adversarial -t .)
```

See [security boundaries](../../SECURITY.md).
