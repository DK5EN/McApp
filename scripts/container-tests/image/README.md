# Container spike image

Throwaway proof of the container premise in `doc/2026-10-31_0830-container-deployment-concept.md`.
Alpine, `mcapp` package only (no timezonefinder/numpy), SPA taken from release v2.1.22.
`src/mcapp` is not modified; `launcher.py` patches it at runtime.

    ./build.sh                      # mcapp-spike:arm64 and :amd64 (downloads the release tarball once)
    ./smoke.sh mcapp-spike:arm64    # health, /api/status, /webapp/, /events, memory; exit 0 = ok

Build context is the repo root; only explicit paths are COPYed.

| Shim in `launcher.py`                         | B12 change                        |
| --------------------------------------------- | --------------------------------- |
| `MCAPP_BIND_HOST` -> `config_loader.SSE_HOST` | 1 (bind host)                     |
| converge watchdog no-op                       | 4 (`MCAPP_DEPLOYMENT=container`)  |
| `/webapp` StaticFiles + `/` redirect          | 2 (no SPA fallback/cache headers) |
| `/api/timezone` left alone (500)              | 8 (should be 501)                 |

Not shimmed: 3, 5, 6, 7, 9, 10 (version comes from package metadata), 12.
