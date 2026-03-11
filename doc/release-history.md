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

