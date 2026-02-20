# McApp v1.5.0 Release Notes

**Date**: February 20, 2026

---

### Bug Fixes

- **Fix stale .venv surviving slot deployment** — Bash glob `*` doesn't match dotfiles, so `.venv` from a previous deployment survived slot cleanup in `deploy_release()`. Also improved stale shebang detection to check all scripts instead of just the first one found, preventing missed stale third-party scripts (e.g., uvicorn) when project scripts look correct.

### Chores

- Bump bootstrap script version to 2.3.0
- Documentation updates (update runner, TLS setup)

---

# McApp v1.4.4 Release Notes

**Date**: February 20, 2026

---

### Features

- **Frontend-triggered deployment with slot-based rollback** — New update system allowing OTA deployment directly from the webapp UI. Uses a slot-based architecture with automatic database snapshot/restore for safe rollbacks. Includes a real-time log terminal with subway-map step indicator showing bootstrap progress via pattern matching.

- **mHeard sidebar persistence** — The mHeard page now has a configurable sidebar with backend-persisted visibility and ordering. Includes a new "Last Month" (30-day) tab and a monthly dump command for 30-day stats processing.

- **WX sidebar persistence** — Weather sidebar visibility and ordering are now persisted to the backend via new REST endpoints.

- **Editable Temp Offset** — The temperature offset in the BLE Weather section is now editable directly from the UI.

- **TLS/SSL tunnel setup guide** — New German-language user guide for the TLS remote access setup.

### Bug Fixes

- **Update system hardening** — Extensive fixes to the update/rollback pipeline:
  - Prevent reverse DNS lookup failure from aborting health check
  - Open firewall port 2985 for the update runner SSE stream
  - Frontend: retry update/rollback start for 40s if backend is not ready

- **mHeard sidebar sync** — Apply sidebar selection and ordering to all mHeard time tabs; fix "Last Month" icon.

### Refactoring

- Reorder BLE Node Configuration card layout
- Remove redundant sensor rows from BLE Sensors card
- Remove unused Altitude asl row from Weather card
- Add mobile 2×2 grid layout for WX data tab buttons
