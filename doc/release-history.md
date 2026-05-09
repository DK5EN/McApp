# Release History

## v1.6.4 (2026-05-09)

### Highlights

The two headline features in this release:

- **BLE PIN authentication**: Adds support for the BLE PIN recently introduced
  into the MeshCom firmware. The `ble_service` now negotiates an app-layer PIN
  via `PATCH /api/ble/pin`, and the webapp exposes PIN management on the node
  settings page. This fixes the loss-of-connection issue caused by the firmware's
  BLE PIN changes — the proxy can once again pair and stay connected to PIN-protected nodes.
- **Spam filtering (active by default)**: A new three-layer message classifier
  auto-detects spam patterns and beacon templates in real time. Every inbound
  message is annotated with category, tags, info score, and a template hash
  (schema v16). Recurring beacons are auto-promoted/demoted via lifetime + 24 h
  + 72 h thresholds. Spam filters can be toggled individually on the Settings
  page, and prefs are persisted server-side via `/api/filter_prefs`. Classifier
  rules, beacon templates, and stats stream live over SSE.

### Backend (MCProxy)

- **[feat]** Three-layer classifier (rules, template fingerprint, info score)
  wired into `store_message`, with REST endpoints, SSE event plumbing, stats
  broadcaster, and one-shot backfill on classifier version bumps
- **[feat]** Schema v16: classifier columns on `messages` plus
  `classifier_rules`, `beacon_templates`, and `classifier_meta` tables
- **[feat]** Classifier subtree synced from mc-chat (`src/mcapp/classifier/`)
- **[feat]** `ble_service`: app-layer PIN authentication via `PATCH /api/ble/pin`
- **[feat]** Persist spam filter prefs via `/api/filter_prefs` endpoint
- **[feat]** Full mypy `--strict` compliance across all source files
- **[refactor]** Extract suppression module, typed SSE events, print→logger,
  debounce BLE disconnect, dead-endpoint cleanup
- **[fix]** Classifier: atomic UPSERT in `update_stats`, srcs clobber +
  duplicate auto-beacon event, restore reclassify progress SSE
- **[fix]** `release.sh`: merge main back into development after production release
- **[chore]** Upgrade dependencies (fastapi, uvicorn, pydantic, ruff, sse-starlette)

### Frontend (webapp)

- **[feat]** BLE PIN management in node settings
- **[feat]** Spam Filter, Classifier Rules, and Beacon Templates settings cards;
  individual filter toggles synced with backend on connect and change
- **[feat]** Chat bubble category chips, auto-beacon flag, info score display
- **[feat]** Toast notification on auto-detected beacon templates
- **[feat]** Consume `system:connected` and `system:ping` SSE events
- **[ui]** Rename Promote/Demote to "Spam" / "Always show" on beacon templates
- **[refactor]** Typed SSE events, CSS tokens for error colors, BLE UX improvements
- **[perf]** Coalesce MapLibre marker rebuilds with `requestAnimationFrame`
- **[fix]** Unwrap envelope on `proxy:*` SSE events
- **[fix]** Release page-request lock on dst switch (pagination stall)
- **[fix]** Normalize `proxy:blocked_texts` payload (ChatContainer crash)
- **[fix]** Raise SSE quick-health-check from 5 s to 15 s
- **[chore]** Update dependencies (vue, maplibre-gl, vite, eslint);
  patch `protocol-buffers-schema` (CVE-2026-5758) and vite security advisories

