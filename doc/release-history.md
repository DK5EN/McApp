# McApp v1.4.3 Release Notes

**Date**: February 19, 2026

---

### Features

- **Yearly mHeard report** — New "Last Year" tab in the mHeard view querying pre-aggregated 1-hour signal buckets for long-term station signal trends.

- **Yearly and monthly WX weather tabs** — New "Last Month" and "Last Year" tabs in the weather view using 4-hour bucket aggregation for extended weather history.

- **Bucketed telemetry endpoint** — New API endpoint for pre-aggregated telemetry data with retention extended to 365 days for long-term trend analysis.

- **Telemetry retention and API improvements** — Extended telemetry retention to 31 days and raised the API hour cap to 744 (31 days).

- **Sensor data in node configuration** — New "Sensors" box in the Node Configuration view showing SE register data from the device.

- **Persist chat input draft** — Chat input text is now preserved across navigation via sessionStorage, so unsent messages aren't lost when switching views.

### Bug Fixes

- **Include recent 5-min buckets in yearly mHeard query** — Fixed missing recent data in yearly signal reports by including the most recent 5-minute buckets alongside hourly aggregates.

- **Bootstrap dev→production switch** — Fixed bootstrap detection of `-dev` versions before `version_gte` comparison, preventing incorrect upgrade paths.

- **Bluetooth settings layout on iPhone** — Fixed clipping of Bluetooth settings layout on mobile Safari.

- **Station popup from messages view** — Fixed station popup not opening when navigating to a station from the messages view.

- **Group number validation** — Extended group number validation to allow 5-digit values and group 0 in network settings.

- **Filter input UX** — Blur filter input on Enter key press for better mobile usability.

### Chore

- Move `ssl-tunnel-setup.sh` from `scripts/` to `bootstrap/` directory
- Update dependencies and fix lint errors
- Clean up ESLint config, update frontend dependencies, remove unused imports
