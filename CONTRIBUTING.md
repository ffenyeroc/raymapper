# Contributing

## Invariants

- Only a hash of a random per-install id is uploaded. The ADB serial, IMSI, IMEI and phone
  number are never hashed, logged or transmitted.
- Coordinates are redacted from logs unless `--show-coords` is passed explicitly.
- The proof-of-work rule must stay byte-identical to `pow_verify()` in `raymap-map/app_api.py`;
  change both together or uploads start failing with 428.
- The device's `qmdl` captures are never uploaded, only the analyzer `ndjson` summary.

## Running things

```bash
./.venv/bin/python -m unittest -q test_collector
./.venv/bin/python mock_rayhunter.py --port 8080 &     # fake hotspot, no device needed
./.venv/bin/python collector.py --once --mock -v       # one poll + upload cycle
```

## Style

Standard library first, no build step, no dependencies beyond what `requirements.txt` lists.
Match the surrounding code.

## Licence

Apache-2.0. By contributing you agree your changes are licensed the same way.
