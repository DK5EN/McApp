# RAM and front door campaign (2026-11-05)

Resume point for the wave campaign. Operator asked for it after the Rust / container evaluation;
measurements are from mcapp.local, v2.1.23, 48 min uptime, no client connected.

## Baseline

| Process     | PSS    | Swapped |
| ----------- | ------ | ------- |
| `mcapp`     | 80 MB  | 12 MB   |
| `mcapp-ble` | 21 MB  | 21 MB   |
| `caddy`     | 27 MB  | 1.5 MB  |
| `lighttpd`  | 1.5 MB | 0       |

- `numpy` import +16.4 MB, `TimezoneFinder()` +3.1 MB, in a fresh process.
- FastAPI +17.3 MB against Starlette +5.8 MB at import, per process.
- `GET /webapp/` carries no `Cache-Control`; nothing is compressed on the wire.

## Decisions

- **Timezone:** a subprocess saves the RAM. It does NOT save the 90 MB of disk, because
  `timezonefinder` and numpy stay installed.
- **Static files:** Caddy serves `/webapp` with `file_server { precompressed br gzip }`. Sidecars are
  built at release time. WASM gets no sidecars: 107 MB raw, B16 handles it, and sidecars would add
  about 70 MB to every tarball.
- **lighttpd stays installed and running** for now: public-TLS Caddyfiles on existing boxes and an
  older slot's `mcapp.sh` still proxy to it. Its cache rule is fixed too. Removal stays B15.
- **FastAPI port:** `weblite`, a small FastAPI-compatible layer on Starlette, so routes barely change.
  `ble_service` is standalone and carries a sha256-pinned copy.

## Waves

| Wave | Content                               | Owner        | Status |
| ---- | ------------------------------------- | ------------ | ------ |
| 1A   | Timezone subprocess                   | implementer  | done   |
| 1B   | Caddy static, sidecars, cache headers | implementer  | done   |
| 2    | `weblite` module and its test suite   | implementer  | done   |
| 3A   | `mcapp` routes onto `weblite`         | implementer  | done   |
| 3B   | `ble_service` routes onto its copy    | implementer  | done   |
| 4    | Docs, gate, commit, push, dev release | orchestrator | open   |

Acceptance on the box: `mcapp` and `mcapp-ble` PSS re-read against the baseline, `/api/timezone` works
inside the webapp's fetch timeout, `/webapp/` and a deep route send `no-cache`, an asset sends `immutable`
and `Content-Encoding: br` or `gzip`, and a missing asset is a 404, not HTML.
