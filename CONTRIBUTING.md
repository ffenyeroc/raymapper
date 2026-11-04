# Contributing

## Invariants

Every patch must keep these true. Breaking one is a security bug, not a feature:

- Coordinates are turned into H3 cells at ingest and discarded. Nothing finer than **r7** is
  stored; nothing finer than **r6** is served.
- `/internal/*` stays loopback-only unless `RAYMAP_ADMIN_TOKEN` is set, and every mutation
  writes to `audit_log`.
- Ingest keeps requiring proof of work, the body/record caps, the per-device quota and the
  blocklist.
- Responses carry counts and a day (or month) — never a report timestamp.
- Grey means "no reports here", never "safe". Keep that wording in the UI.

## Running things

```bash
docker compose up -d                      # Postgres on 127.0.0.1:5433
cp .env.example .env                      # then set RAYMAP_ADMIN_TOKEN
./.venv/bin/python seed.py --cluster --rows 20000 --days 3 --devices 200
./scripts/run_api.sh                      # API + UI on 127.0.0.1:8000

./.venv/bin/python -m unittest -q test_api test_adb
node --test tests/test_geo.js tests/test_search.js tests/test_downloads.js
node --check ui/app.js                    # syntax check, no build step

cd collector && ./.venv/bin/python -m unittest -q test_collector   # device client
```

## Style

Match the file you are editing: standard library first, no build step, no CDN. The UI is plain
JavaScript with vendored H3 and MapLibre — do not introduce a bundler or a framework.

## Tests

New non-trivial logic (a parser, a loop over data, anything touching ingest or moderation)
needs a test in `test_api.py` or `tests/test_*.js`. Trivial edits do not.

## Licences

Code is Apache-2.0 (`LICENSE`); aggregate data and methodology text are CC BY 4.0
(`LICENSE-DATA`). Contributions are accepted under the licence of the file they touch.
