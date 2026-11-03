# Security policy

Raymap maps IMSI-catcher detections reported by volunteer devices. Two classes of bug are
treated as security issues here, not ordinary bugs:

1. **Privacy failures** — any path where a precise coordinate, a raw device identifier, a
   report timestamp finer than a day, or a cell finer than r7 reaches storage, a response, a
   log or a snapshot. The invariants are listed in `README.md` → *Privacy model*.
2. **Integrity failures** — bypassing proof of work, the per-device quota, the record/body
   caps or the blocklist, or reaching `/internal/*` from off-host.

Functional bugs (rendering, search index, docs) are ordinary issues.

## Reporting

Use **GitHub's private vulnerability reporting** — the *Security* tab → *Report a
vulnerability* — rather than a public issue. Include your version (`/openapi.json`), the
request or code path, and what data was exposed.

Expect an acknowledgement within a week. This is a volunteer project with no paid on-call, so
there is no SLA beyond that; privacy fixes outrank every other kind of work.

## Scope

- This repository: API, ingest, aggregation, moderation, UI.
- `raymap-collector`: the device-side uploader.

Out of scope: Rayhunter and the hotspot firmware, the basemap tiles (OpenFreeMap/OpenMapTiles),
and the accuracy of Rayhunter's own analyzers — please report those upstream.
