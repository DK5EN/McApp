# McApp Release History

---

## v1.6.1

**Date**: March 11, 2026

**Security**: All backend (Python) and frontend (npm) dependencies updated to latest versions, resolving known vulnerabilities (minimatch, ajv).

### New Features

- **Flexible telemetry columns** — Fixed temp2 data loss and added hum2 + extras columns to telemetry storage, with dynamic chart display in the webapp.
- **Weather preview endpoint** — New `/api/weather/preview` for formatted WX message output, with a live preview dialog in the webapp `!wx` command.
- **Bootstrap `--tag` flag** — Pin a specific release version during deployment via `--tag`, useful for rollbacks and debugging.

### Bug Fixes

- **BLE shutdown race condition** — Guard BLE remote client against a race during shutdown that could cause unhandled exceptions.
- **Reduce production log noise** — Demote SSE churn, UDP_DIAG, ACK-unknown, and per-message tracing to DEBUG level.
- **Fix remote command responses** — Remove stale original key from `normalize_unified` output and add UDP send diagnostics for correct remote command delivery.
- **Fix `--tag` downgrade** — Use correct branch for piped libs and bypass webapp version guard when deploying a specific tag.
- **Strip `src_type` from UDP payload** — Prevent firmware from receiving unexpected fields in outbound UDP packets.
- **Fix copy-to-clipboard on HTTP** — Webapp clipboard now works in non-secure (HTTP) contexts.

### Refactoring

- **Command dispatch cleanup** — Inlined `parse_command` wrapper, validated via shadow mode, removed shadow scaffolding after parity confirmation.
- **Outbound handler unification** — Moved `normalize_unified` to `parsing.py`, validated outbound path via shadow mode, removed after validation.
- **Code quality** — Extracted methods in `ctcping.py` and `_message_handler`, replaced prints with logger, moved search counting to SQL aggregation.
- **Test suite** — Fixed 8 stale test expectations, added DB fixture loading for storage-dependent tests.

### Documentation

- **High-hop forensics SOP** — Standard operating procedure for AI-assisted log analysis of high-hop message patterns, with production data analysis.
- **FW 4.35m community feedback** — Field report from 22–24 Feb 2026.

---

## v1.6.0

**Date**: February 24, 2026

### New Features

- **Send-ACK checkmarks** — Binary ACK packets from the BLE link are now parsed and published as `msg:status` SSE events, enabling delivery confirmation checkmarks in the webapp.
- **Delete messages** — New `DELETE /api/delete_messages` endpoint allows bulk-deleting messages by destination (channel/group), with a matching filter-bar action in the webapp.
- **Personal !wx text prefix** — The `!wx` weather command now supports a configurable personal text prefix, set from the webapp settings.
- **BLE reconnect visibility** — BLE connection state changes, reconnect attempts, and recovery guidance are surfaced in a new activity log in the webapp.
- **Telemetry storage improvements** — Battery voltage (`batt`) is now stored; QFE pressure is validated and calculated from QNH when missing.
- **Telemetry configuration page** — New webapp page for configuring BLE node sensors (auto-load, toast feedback, 2-column grid layout). Hidden from main nav while in beta. Can be accessed via /webapp/telemetry

### Bug Fixes

- **Fix own-echo re-execution** — Commands echoed back from the mesh were re-executed because the `{NNN` echo-ID suffix was stripped too late. Moved stripping into `normalize_command_data` so dedup catches the echo before routing.
- **Fix remote command execution for ping** — Ping response messages lacked the `[CTC]` signature, causing them to be treated as new inbound commands on the remote path. Added the marker.
- **Fix ACK binary parser** — Corrected an off-by-one error where `ack_id` and `ack_type` were shifted in the 7-byte BLE ACK format, and fixed field mapping for ACK matching.
- **Preserve command casing** — Removed unintended uppercasing in `MessageValidator.normalize_message_data` that broke case-sensitive command arguments.
- **Dedup window alignment** — Frontend dedup window extended from 5 min to 20 min to match the backend, preventing duplicate message display. Fixes echos from the mesh, that came from broken retransmit timers in older firmware

### Webapp Improvements

- **mHeard mobile layout** — 2×2 tab grid and full-width charts on mobile viewports.
- **Dependency updates** — npm packages updated with known audit issues documented.

### Internal / Diagnostics

- **Debug logging uplift** — Replaced `has_console`/`debug` guards with `logger.info` for consistent log output in all environments.
- **ACK and command tracing** — Added diagnostic logging for msg_id correlation, remote UDP command response path, and ACK lifecycle.
