# Offline Home planner

[`plan_home.py`](plan_home.py) produces Home/idle overlays and rollback payloads from an exact firmware contract and captured state. It reads private inputs and creates a new private output directory; it does not connect to or modify the TV.

```sh
python3 -B tv/integration/plan_home.py \
  --contract "$LOCAL_CONTRACT" \
  --snapshot "$LOCAL_SNAPSHOT" \
  --original-root "$LOCAL_ORIGINALS" \
  --owner-app-id local.owner.applibrary \
  --output "$NEW_PRIVATE_CANDIDATE_DIRECTORY"
```

## Inputs

The contract requires schema 1, firmware release/build/architecture, hashes and paths for the application manager, profile, configuration service, compositor and boot sequencer, a snapshot digest, and expected Home/idle IDs.

The snapshot must match that firmware and contain the complete `defaultApps`/`idleApp` profile values, effective `configIdleApp`, `configProfileList`, `lastAppHandlerPolicy`, and prior factory idle value. Exact field validation is in [`plan_home.py`](plan_home.py); preserve complete objects and use captured values rather than reconstructed defaults.

## Outputs

| File | Purpose |
| --- | --- |
| `home_profile.candidate.json` | Full Home-category candidate |
| `idle_profile.candidate.json` | Full native idle candidate |
| `config-set.candidate.json` | Configuration-service idle payload |
| `config-restore.candidate.json` | Exact prior value/removal payload |
| `profile-restore.reference.json` | Original profile references |
| `plan.json` | Input/output hashes and changed fields |

Application and recovery are separate target-specific steps. Applying the candidates needs root on the TV and changes vendor system configuration; see the [disclaimer](../../README.md#disclaimer).

```sh
python3 -B -m unittest discover -s tv/integration -p 'test_*.py'
```
