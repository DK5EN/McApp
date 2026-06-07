# Release History

## v1.6.12 (2026-06-07)

Fixes the long-running mHeard chart corruption on the 30-day/1-year views and rewrites chat scroll-back to hold its reading position reliably. Also hardens REST input validation and rolls up dependency/security updates.

### Backend (MCProxy)

- **[fix]** Nightly job now aggregates hourly buckets **before** pruning. Previously `prune_messages` deleted the 5-minute buckets before `aggregate_hourly_buckets` could roll them up, so only a ~2 h/day sliver survived into the 1-hour buckets — corrupting the 30 d / 1 y mHeard charts (24 h / 7 d read 5-minute buckets directly and were unaffected). Also fixes the `prune_messages` cutoff: `datetime.utcnow().timestamp()` treated UTC as local time, shifting cutoffs by the local offset. Verified end-to-end (see `doc/charts-wrong.md`).
- **[refactor]** REST endpoints validated with Pydantic models. Manual `request.json()` + `.get()` + hand-written `HTTPException(400)` parsing across 14 POST/PATCH endpoints is replaced with typed request models in the new `src/mcapp/schemas.py`. Removes uncaught 500s on malformed input (now 422), consolidates coercion, and preserves partial-update semantics on rules PATCH. The webapp only checks `res.ok`, so it is unaffected by the 400→422 change.
- **[chore]** Dependency and security updates: starlette 1.0.0 → 1.2.1, uvicorn 0.47, sse-starlette 3.4.4, ruff 0.15.13, click 8.4, numpy 2.4.6, watchfiles 1.2, idna 3.15; `ble_service` standalone lockfile refreshed with security patches; `uv lock --upgrade` sweeps.
- **[docs]** `doc/charts-wrong.md` — mHeard chart gap root-cause analysis, nightly-job fix deploy record, and end-to-end verification checklist.

### Frontend (webapp)

- **[fix]** Chat scroll-back rewritten with element-anchored correction. Replaces the `scrollHeight`-delta compensation (unreliable when the client-side spam filter hides a variable share of messages) with a per-bubble `data-msg-id` anchor that pins a real message element, so the reading position holds regardless of how many rows render or get filtered. Adds idle-only loading (150 ms trailing debounce near the top), a scroll-lock that freezes the container while a backend page is in flight, and an end-of-data fix that stops the request loop cleanly. Diagnostics scaffolding removed. Verified against rpizero (8243 msgs): 0–1 px jump across 1220 bubbles, zero requests at end of data.
- **[chore]** Security fix: `brace-expansion` bumped to 5.0.6 (CVE-2026-45149).
- **[chore]** Minor/patch dependency updates: vite, vue, vue-router, vue-tsc, @vitejs/plugin-vue, date-fns, eslint, typescript-eslint, typescript.
- **[docs]** Scroll-jump fix analysis and forward plan.

---

