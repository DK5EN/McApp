# McApp v1.4.1 Release Notes

**Date**: February 16, 2026

---

### Features

- **WX station sidebar** — New sidebar on the weather page with drag-and-drop reordering (via vuedraggable) and toggle visibility per station, persisted to IndexedDB. Follows the same grid layout pattern as the positions page.

- **24h/7d time range toggle** — Tab switcher on the WX page so charts, statistics, and station filtering adapt to the selected window. Backend API now accepts an `hours` query param. Also fixed pressure chart tooltip to show both QFE and QNH.

- **WX statistics card** — New 4th quadrant card showing min/median/max for all weather metrics, datapoint counts, and estimated real altitude from cross-station QNH consensus.

- **Add hours query parameter to telemetry API** (`b409b4a`) — The `/api/telemetry` endpoint now accepts an `hours` parameter to control the time window for chart data.

- **Rewrite release.sh as interactive script** (`c204721`) — Complete rewrite of the release script with dual-repo support (MCProxy + webapp), interactive prompts, automatic tarball building, GitHub release publishing, and full rollback on failure.

### Bug Fixes

- **Extend message dedup window from 5 to 20 minutes** (`8a8d07f`) — The duplicate message detection window was too short, allowing repeated commands to slip through. Increased to 20 minutes for more reliable deduplication on the mesh network.

- **Add 48h time filter to telemetry chart query** (`a0186d8`) — The telemetry API endpoint returned all historical data, causing slow chart rendering. Added a 48-hour default filter to keep responses fast.

- **Redirect release menu to stderr** (`167debe`) — The interactive release type menu was printed to stdout, making it invisible when stdout was captured. Redirected to stderr so the menu is always visible.

- **Preserve WX sidebar state across navigation** — Lifted stationOrder, hiddenStations, and loaded refs to module-level scope so they survive mount/unmount cycles.

- **Watchdog false "no time sync"** — Reset the watchdog timer on tab resume instead of re-evaluating staleness, preventing false alarms after browsers suspend SSE connections.

- **Qualified stations only in sidebar** — Filter sidebar to stations with >= 6 recent datapoints (matching the chart threshold) so no empty entries appear.

- **Sidebar collapse shrinks grid** — Use auto grid column so the content area expands when the sidebar collapses.

- **Altitude chart stabilization** — Removed barometric input from the Kalman filter (was causing ~20m fluctuations) and switched to GPS-only filtering.

### Chore

- Align pyproject.toml versions to 1.4.1 (`f6db6be`)
- Remove resolved deficits from version-logic.md (`aaa9ea0`)
