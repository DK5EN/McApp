# McApp as a container image (Alpine, GHCR, Traefik)

Status: Ideation, backlog B12, 2026-10-31. Nothing in this paper is implemented. Sibling paper
(BLE for the container): `doc/2026-10-31_0830-ble-service-and-lan-discovery-concept.md` (B13).

## BLUF

- **One Alpine container, one process, no front proxy inside.** uvicorn serves the API, the SSE
  stream and the Vue SPA; Traefik (labels) does TLS and routing. Updates are new image tags from
  `ghcr.io/dk5en/mcapp` (linux/arm64 + linux/amd64).
- **The app is closer to container-ready than expected, but 10 small backend code changes are needed
  first** (bind host, listen port, static webapp mount, `MCAPP_DEPLOYMENT=container` gate for update/converge,
  secret-box binding, UDP target learning switch, Node Admin host allow-list, `/api/timezone` degrade,
  stall-middleware passthrough, `MCAPP_VERSION` for version reporting). None is above size M; the
  webapp needs one gate that names the container (`features` contains `container`).
- **The 60-80 MB target was missed: the measured spike image is 228 MB (arm64) / 225 MB (amd64)**
  (CT1, see Size estimate). The Python venv is only 61.5 MB; the webapp bundle is 112 MB, 107 MB of
  it the Browser AI WASM runtime. Operator decision: the webapp loads that runtime on demand (webapp
  change, outside this campaign). `timezonefinder` is a separate question: it drags in numpy and a
  32.8 MB data wheel, has no aarch64 wheel at all and serves one endpoint (`/api/timezone`).
  Recommendation: leave it out of the image; the endpoint answers 501.
- **musl verdict: Alpine works.** Every other dependency has a cp313 aarch64 `musllinux` wheel or is
  pure Python. `h3` (no musl aarch64 wheel) is irrelevant on Python 3.13: `timezonefinder` 9.2.0
  no longer depends on it. No `-slim` fallback needed.
- **Two traps will bite real users.** (1) The app learns the node's IP from the first inbound UDP
  datagram; if Docker rewrites the source address (userland proxy, rootless, some NAS builds) it
  learns the Docker gateway and every outbound frame is lost. (2) The secret box (QRZ password, Node
  Admin target passwords, internet-feed credentials) is bound to host data (`/proc/cpuinfo` serial
  on a Pi host); a host migration or k3s reschedule makes them unreadable. Change 5 binds to the key
  file alone.
- **Own reverse proxy and shared hosts are first-class (operator requirement 2026-10-31).** An
  operator's existing proxy (nginx, Caddy, Nginx Proxy Manager, Traefik) may put a user/password
  or SSO login in front of McApp, and the HTTP port is freely remappable so the container coexists
  with other web services on the box. UDP 1799 is the one port that cannot move: the node firmware
  sends to a compile-time `EXTERN_PORT`.
- **Not supported by design:** BLE inside the container, the in-app Update page, slots, system
  converge, Kubernetes (appendix recipe only), and any NFS/SMB-backed database. **Wave 1 must land
  before any image is published** (without change 4 the converge watchdog writes into the volume).

## Spike results (2026-10-31)

Source: `doc/2026-10-31_1200-container-test-campaign.md` (CT1-CT5), spike image on arm64 and amd64.

- **Image (CT1).** Builds and smokes on arm64 and amd64 (health, SPA, SSE, read-only rootfs). Idle
  RSS 55 MiB on arm64 (amd64 107 MiB under QEMU, not a baseline). Sizes: see Size estimate.
- **SSE through Caddy and Traefik (CT3a W2).** Unbuffered with `flushinterval=-1`: first events within
  20 ms, 26 lines in 0.4 s.
- **Traefik `basicauth` (W3).** 401 without and with wrong credentials, 200 with; SSE streams with
  credentials.
- **Authelia 4.39.28, cookie login on the same origin (W4).** No session gives a 302 to the portal; the
  login sets an HttpOnly cookie, after which API, SPA and SSE work. **An XHR without a session gets
  401, so the webapp must answer it with a full-page navigation.**
- **Persistence (CT5).** P1 (volume survives container removal), P3 (2026-10-20 bare-metal backup DB,
  6191 messages, migrated to schema 39 in about 6 s) and P4 (`json-file` 10 MB x 3 applied) pass.
  `secret.key` is not copied on purpose; QRZ, Node Admin and feed credentials need re-entry.
- **BLE via a host-side service (CT4c).** Container to macOS `ble_service` to DK5EN-1: connect, 14
  registers, DM plus peer ACK on SSE. Transport is chosen by the client: `type: "BLE"` goes over BLE,
  `type: "msg"` always goes down the UDP handler.
- **UDP (CT2).** The NAT trap is confirmed on OrbStack bridge (U1) and on CT4c; see Target learning.
- **Deferred / open.** U4 (amd64 on the Mac) does not start containers under emulation: retest on real
  x86 or a NAS. W5 (iPhone PWA) waits for router DNS entries.

## Works / degrades / does not work

| Area                           | In the container   | Why                                                                             |
| ------------------------------ | ------------------ | ------------------------------------------------------------------------------- |
| Chat, storage, classifier      | Works              | Pure Python + SQLite on the volume                                              |
| SSE, REST API, stall tracking  | Works              | Same ASGI app; `loop_lag` sampler needs no host access                          |
| Web Push, VAPID                | Works              | Needs outbound internet; key file on the volume                                 |
| QRZ, Node Admin, internet feed | Works after fix    | Secret-box binding must not use host data (change 5)                            |
| UDP to/from a node             | Works with care    | Source-IP learning vs Docker NAT, see Networking                                |
| Webapp                         | Works after change | FastAPI serves no static files today                                            |
| BLE                            | Remote only        | `MCAPP_BLE_URL` to a native `ble_service` on a LAN host                         |
| Node TZ push                   | Remote only        | Lives in `ble_service`; uses that host's `/etc/localtime`                       |
| `/api/timezone` (node GPS UI)  | Degrades (501)     | `timezonefinder` left out; webapp retry fix still required (see Open questions) |
| Internet-feed hardware label   | Degrades           | Falls back to `Pi Zero 2W` (change 8)                                           |
| Update page, slots, converge   | Hidden / disabled  | No slots, no runner, no systemd inside                                          |
| Bootstrap tuning               | Not applicable     | `gpu_mem`, journald, nftables, Caddy are the host's job                         |
| Journald persistence           | Replaced           | `docker logs` / the log driver, rotation set in compose                         |

## Image design

### Decisions (operator, 2026-10-31)

- `python:3.13-alpine`, multi-stage, `uv` in the builder; `-slim` only if a dependency cannot be built
  for musl (verdict below: none). One container; uvicorn serves SPA + API + SSE (no Caddy, no
  lighttpd). Registry `ghcr.io/dk5en/mcapp`, built in GitHub Actions from release tags. Updates are
  new tags; the Update page and slot activation are hidden.

### Size estimate

- **Measured (CT1, 2026-10-31):** the spike image is **228 MB arm64 / 225 MB amd64**, not 60-80 MB.
  Venv 61.5 MB; webapp bundle 112 MB, of which 107 MB is the Browser AI WASM runtime
  (`assets/litertlm_*.wasm`, four variants). **Operator decision:** the webapp loads the Browser AI
  WASM on demand instead of shipping it in the bundle (a webapp change, outside this campaign). The
  estimates below predate the measurement.

- Alpine base with Python 3.13: roughly 50 MB uncompressed. Runtime deps without
  `timezonefinder`/numpy (cryptography, pydantic-core, uvloop, httptools, regex, httpx, fastapi):
  estimate 90-110 MB on disk, 35-45 MB compressed (all unverified, measure in Wave 0).
- With `timezonefinder`: add numpy and `timezonefinder-data` (32.8 MB `py3-none-any` wheel, verified on
  PyPI): well above 150 MB. That is the budget breaker. Read the 60-80 MB goal as the compressed pull
  size.

### musl wheel verdict (checked on PyPI JSON, 2026-10-31)

| Package (version)                                                                             | musllinux aarch64, cp313 | Note                                                     |
| --------------------------------------------------------------------------------------------- | ------------------------ | -------------------------------------------------------- |
| cryptography 50.0.2, uvloop 0.23.0, httptools 0.9.0, regex 2026.9.29, cffi 2.1.1, numpy 2.5.4 | yes                      |                                                          |
| pydantic-core 2.50.2                                                                          | yes                      | Ships `musllinux_1_1_aarch64`                            |
| py-vapid, flatbuffers                                                                         | pure Python              |                                                          |
| http-ece 1.2.1                                                                                | sdist only (pure)        | Already built from sdist today; builder stage handles it |
| h3 4.5.0                                                                                      | NO (x86_64 only + sdist) | Only pulled by `timezonefinder` 9.0.0 on Python < 3.12   |
| timezonefinder 9.2.0                                                                          | NO (x86_64 only)         | No aarch64 wheel of any libc; arm64 builds from sdist    |
| timezonefinder-data 4.2026.5                                                                  | pure, 32.8 MB            | Size problem, not a build problem                        |

- `uv.lock:1397`: `h3` belongs to `timezonefinder` 9.0.0 only (`python < 3.12`); the 9.2.0 entry for
  `>= 3.12` has no `h3` (PyPI `requires_dist` confirms).
- `timezonefinder` is imported lazily in `weather.py:31` (`_get_tz_finder`), from two callers:
  `GET /api/timezone` (`weather.py:115`) and the startup warm-up `warm_timezone_finder`
  (`weather.py:37`), scheduled 30 s after start by `_warm_timezone_finder_later` (`main.py:3321`,
  task created at `:3369`). The warm-up catches `Exception` and logs at DEBUG, so it is harmless but
  wasteful when the module is absent (change 8 skips it). The only webapp caller of the endpoint is
  `NodeGpsCard.vue:63,164` (webapp repo). numpy has no other user in `src/mcapp` or `ble_service/src`.
- **Options:**

| Option                                          | Image size   | arm64                       | Effort | Verdict          |
| ----------------------------------------------- | ------------ | --------------------------- | ------ | ---------------- |
| A. Exclude `timezonefinder` from the image      | smallest     | clean                       | S      | **Recommended**  |
| B. Build it in the builder stage (gcc), copy it | +60 MB (est) | compile under QEMU          | M      | Optional `:full` |
| C. `-slim` for arm64 only                       | +50 MB (est) | same gap (no aarch64 wheel) | M      | Does not help    |
| D. Replace with a pure-Python/offline lookup    | small        | clean                       | L      | Not worth it     |

- Implementation of A: `uv sync --frozen --no-dev --no-editable --package mcapp
--no-install-package timezonefinder --no-install-package timezonefinder-data
--no-install-package numpy --no-install-package flatbuffers`. `cffi` stays (cryptography needs it).
  The lock file stays untouched, so bare metal is unaffected.

### Dockerfile (example)

The build context is the **extracted release tarball** (`mcapp-<ver>.tar.gz`, see Release pipeline):
it already holds `pyproject.toml`, `uv.lock`, `src/mcapp`, `ble_service/` (workspace member) and the
built `webapp/`. The Dockerfile itself lives in the repo (`docker/Dockerfile`).

```dockerfile
# syntax=docker/dockerfile:1
ARG PYTHON=3.13

FROM ghcr.io/astral-sh/uv:latest AS uv   # pin an exact version in the real file

FROM python:${PYTHON}-alpine AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY ble_service/pyproject.toml ble_service/pyproject.toml
COPY ble_service/src ble_service/src
COPY src src
# Compiler only in this stage; nothing of it reaches the final image.
RUN apk add --no-cache build-base libffi-dev \
 && uv sync --frozen --no-dev --no-editable --package mcapp \
      --no-install-package timezonefinder --no-install-package timezonefinder-data \
      --no-install-package numpy --no-install-package flatbuffers

FROM python:${PYTHON}-alpine
ARG MCAPP_VERSION
RUN apk add --no-cache tzdata \
 && addgroup -g 10001 mcapp \
 && adduser -D -H -u 10001 -G mcapp -s /sbin/nologin mcapp \
 && install -d -o mcapp -g mcapp /var/lib/mcapp
COPY --from=build /opt/venv /opt/venv
COPY webapp /app/webapp
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MCAPP_DEPLOYMENT=container \
    MCAPP_VERSION=${MCAPP_VERSION} \
    MCAPP_BIND_HOST=0.0.0.0 \
    MCAPP_WEBAPP_DIR=/app/webapp \
    MCAPP_BLE_MODE=disabled \
    MCAPP_SECRET_KEY_PATH=/var/lib/mcapp/secret.key \
    MESHCOM_VAPID_PATH=/var/lib/mcapp/vapid.json \
    MCAPP_RUNTIME_STATE=/var/lib/mcapp/runtime.json \
    TZ=UTC
LABEL org.opencontainers.image.source="https://github.com/DK5EN/McApp"
USER 10001:10001
VOLUME /var/lib/mcapp
EXPOSE 2981 1799/udp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD wget -qO- http://127.0.0.1:2981/health >/dev/null || exit 1
CMD ["mcapp"]
```

- **`MALLOC_ARENA_MAX=2` is deliberately absent:** a glibc knob (B4) that musl ignores. Measure RSS
  instead (Risks).
- **`HEALTHCHECK` target exists:** `GET /health` (`sse_routes/stream.py:255-258`) passes
  `StallMiddleware` untouched (`stall_middleware.py:72`); BusyBox `wget` is in the base image.
- **`mcapp` entry point** is `mcapp.main:run`, the binary the systemd unit starts
  (`bootstrap/templates/mcapp.service:17`).
- **Version reporting needs `MCAPP_VERSION` (required, change 10).** `_get_version()`
  (`__init__.py:5-24`) runs `git describe`, which fails without git, and falls back to
  `version("mcapp")`. `set_pyproject_version` (`release.sh:576`) runs only in `post_release_prep`
  (`:679`, the bump for the NEXT cycle), so a dev tag's tarball carries the plain `X.Y.Z`: a `:dev`
  image would report e.g. `2.1.23`, never `2.1.23-dev.1`. `_get_version()` therefore reads
  `MCAPP_VERSION` first; the workflow passes the tag minus `v` as a build arg.
- **Read-only rootfs** works if nothing writes outside `/var/lib/mcapp` and `/tmp` (bytecode is
  precompiled). Confirm in Wave 0 with `read_only: true`.
- **`tzdata`** is needed: Alpine has no zoneinfo database; the app uses `zoneinfo` and `TZ`.

### Webapp in the image

- **Bundle size (CT1):** 112 MB, 107 MB of it the Browser AI WASM runtime; see Size estimate for the
  on-demand loading decision.

- **No separate fetch needed.** `release.sh` already bakes the built SPA into the release tarball
  (`build_webapp` at `scripts/release.sh:765`, copied into `webapp/` in `build_tarball` at `:805`).
  `deploy.sh` consumes the same bundle (`deploy_webapp_from_tarball`, `bootstrap/lib/deploy.sh:572`),
  so image and bare-metal release are the same bits by construction.
- **The SPA is built with `base: '/webapp/'`** (webapp `vite.config.js:49`) and calls same-origin
  `/api/...` and `/events`. The container therefore serves the bundle at `/webapp/`, redirects `/`
  to `/webapp/`, and falls back to `index.html` for unknown `/webapp/*` paths. This mirrors
  lighttpd's `99-mcapp.conf` (`bootstrap/lib/packages.sh:496-514`).
- **Cache headers to replicate** (owned by lighttpd today, `packages.sh:510-514`):
  `index.html`, `sw.js`, `manifest.webmanifest`, `version.html` get `no-cache`; `/webapp/assets/*`
  gets `public, max-age=31536000, immutable` (a cached `sw.js` leaves the service worker stale).
- **Implementation:** a small `static_webapp.py` with a `StaticFiles(html=True)` subclass
  (`get_response` catches 404 and returns `index.html`; `html=True` or an explicit index is needed so
  `/webapp/` itself resolves), mounted at `/webapp` after all routers in `_create_app`
  (`sse_handler.py:631`, last include at `:679`), plus a `GET /` redirect. Active only when
  `MCAPP_WEBAPP_DIR` is set and exists, so bare metal behaviour is unchanged.
- **Stall middleware must skip `/webapp/`, bare `/webapp` and the `/` redirect.** It samples 1 in 50
  sub-threshold calls (`stall_middleware.py:72`, `_passthrough`); static assets would flood
  `stall_events` (change 6).

## Persistence

One volume at `/var/lib/mcapp`, owned by uid 10001. Everything the app persists is already resolved
under that directory or has an env override.

| File                                                     | Content                             | Source of the path                                            |
| -------------------------------------------------------- | ----------------------------------- | ------------------------------------------------------------- |
| `messages.db`, `-wal`, `-shm`                            | SQLite (WAL)                        | `StorageConfig.db_path`, `config_loader.py:142` (config only) |
| `runtime.json`                                           | learned node IP, callsign, position | `runtime_state.py:30`, env `MCAPP_RUNTIME_STATE`              |
| `secret.key`                                             | install key for the QRZ password    | `secret_box.py:36`, env `MCAPP_SECRET_KEY_PATH` (`:59-66`)    |
| `vapid.json`                                             | Web Push keypair (0600)             | `push_delivery.py:70`, env `MESHCOM_VAPID_PATH`               |
| `classifier_rules_retired.json`                          | one-off backup next to the DB       | `storage/migrations.py:976`                                   |
| `converge-attempt`, `update-args.json`, `update-trigger` | written today, stops with change 4  | `system_converge.py:34-52,179-188`                            |

- **Mount the directory, never a single file.** SQLite writes `-wal` and `-shm` next to the DB; a
  file-level bind mount of `messages.db` loses them and corrupts or loses recent writes.
- **Local filesystem only.** WAL needs shared memory and POSIX locks; NFS/SMB (the NAS reflex) breaks
  that. Use a named volume or a bind mount on a local disk (ext4/btrfs/zfs).
- **The last row is not "never written" today.** The converge watchdog is gated only on dev env,
  non-Linux and a missing `/var/lib/mcapp` (`system_converge.py:202-204`); the volume satisfies all
  three. The epoch marker reads 0 < 6, so `_do_attempt` writes the three files into the volume and
  tries to start a runner that does not exist. Change 4 stops it; Wave 1 lands before any image.
- **`runtime.json` must be persisted:** it holds the learned UDP target; atomic temp+rename needs a
  writable directory (`runtime_state.py:5`).
- **Config file is optional.** Without `/etc/mcapp/config.json` the loader logs a WARNING and uses
  defaults, BLE env overrides still applied (`config_loader.py:288-296`); this matches the GUI-only
  policy. For a setting without a GUI, mount a read-only file and set `MCAPP_CONFIG_PATH` (new,
  change 2); today the path is hard-coded (`config_loader.py:334`).
- **Ownership:** a named volume inherits the image's `chown`; a bind mount must be `chown 10001` by
  the operator (NAS users hit this first).
- **Migrating from bare metal:** stop `mcapp`, copy `messages.db` (+ `-wal` if present),
  `runtime.json`, `vapid.json` into the volume. Push subscriptions survive via `vapid.json`.
  `secret.key` is **not** portable: it is bound to the Pi's board serial today, and change 5 makes the
  container key independent of the host, so even a `secret.key` copied from a Pi is unreadable
  there (a different binding, also on that same Pi). **Three consumers** go
  `credentials_unreadable` and must be re-entered: the QRZ password, the Node Admin target
  passwords (`node_admin_service.py`, `storage/node_admin.py`) and the internet-feed credentials
  (`internet_feed.py:34,233`).
- **Backups:** online, through Python's `sqlite3` module (consistent with WAL, no stop needed; no
  `sqlite3` CLI is shipped):

  ```sh
  docker exec mcapp python -c "import sqlite3; s=sqlite3.connect('/var/lib/mcapp/messages.db'); d=sqlite3.connect('/var/lib/mcapp/backup.db'); s.backup(d)"
  ```

  Copy `backup.db` off the volume; copying the live files of a running container is not safe.

## Networking

### HTTP: Traefik in front, nothing published

- The container listens on `2981` (`config_loader.py:115`). Today the bind is hard-coded to loopback
  (`config_loader.py:114`, used at `main.py:2936`, `sse_handler.py:1108-1110`): without a change the
  container is unreachable. Add `MCAPP_BIND_HOST` (change 1).
- **Do not publish 2981 to the host.** Traefik reaches it over the shared Docker network. The API
  has no authentication (Security).
- **SSE through Traefik:**
  - `/events` is a long-lived `text/event-stream`. Traefik flushes event-stream responses
    immediately; `responseForwarding.flushInterval` is the knob if anything buffers. Not verified
    against the deployed version (Wave 4); the label sets a small explicit value.
  - **No `compress` middleware on this router** (Caddy sets `flush_interval -1` for `/events*`,
    `Caddyfile.mcapp`; the Traefik equivalent is "no buffering middleware").
  - **No `writeTimeout` on `websecure`** (static config): a non-zero value cuts every SSE connection.
  - The webapp reconnects after 90 s of silence (`sse_heartbeat`); idle timeouts below that would loop.
- **Certificates:** a LAN-only host needs DNS-01 (or an internal CA); HTTP-01 cannot validate a private
  name.
- **Host header and Node Admin:** the Node Admin router refuses any `Host` that is not a LAN name
  (`localhost`, `mcapp`, the machine hostname, each with `local|fritz.box|home.arpa|lan|home`, or a
  private IP) (`sse_routes/node_admin.py:34-35,68-82`). A Traefik rule such as
  `Host(\`mcapp.example.com\`)`is refused, and Node Admin plus any router sharing the guard stop
working. Fix:`MCAPP_ALLOWED_HOSTS`(change 7), or choose a LAN-style name and set the container`hostname:` to its first label.

```yaml
# compose.yml (example)
services:
  mcapp:
    image: ghcr.io/dk5en/mcapp:latest
    container_name: mcapp
    hostname: mcapp
    init: true
    restart: unless-stopped
    logging:
      driver: json-file
      options: { max-size: 10m, max-file: "3" }
    user: "10001:10001"
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 256m # starting point, measure
    stop_grace_period: 30s
    environment:
      TZ: Europe/Berlin
      MCAPP_BLE_MODE: disabled # or remote + MCAPP_BLE_URL / MCAPP_BLE_API_KEY
    volumes:
      - mcapp-data:/var/lib/mcapp
    ports:
      - "192.168.1.10:1799:1799/udp" # bind to the LAN interface, see UDP section
    networks: [proxy]
    labels:
      traefik.enable: "true"
      traefik.docker.network: proxy
      traefik.http.routers.mcapp.rule: Host(`mcapp.home.arpa`)
      traefik.http.routers.mcapp.entrypoints: websecure
      traefik.http.routers.mcapp.tls.certresolver: letsencrypt-dns
      traefik.http.routers.mcapp.middlewares: mcapp-lan
      traefik.http.middlewares.mcapp-lan.ipallowlist.sourcerange: 192.168.1.0/24 # your real LAN, not 172.16.0.0/12
      traefik.http.services.mcapp.loadbalancer.server.port: "2981"
      traefik.http.services.mcapp.loadbalancer.responseforwarding.flushinterval: 10ms
volumes:
  mcapp-data:
networks:
  proxy:
    external: true
```

- **The allowlist must name the real LAN only.** `172.16.0.0/12` is the Docker bridge range: wherever
  Traefik sees the bridge gateway as the client (userland proxy, IPv6 without ip6tables, rootless
  Docker) every internet client would match it, so it is not in the example. After deploying, check
  the client IP in Traefik's access log and confirm it is the real LAN address, not `172.x`.
- **Logs:** journald is replaced by the `json-file` driver, rotated by the `logging` block above
  (10 MB x 3). Without it the default driver grows unbounded on a NAS disk.

### Behind your own reverse proxy (login, other web services on the box)

Operator requirement (2026-10-31): many home servers already run a reverse proxy that asks for a
user and password (or SSO) before anything behind it is reachable, and already host other web
services. Both must work without Traefik.

**Port remapping.**

- **Bridge mode (default): no code change.** The host port is the operator's choice, the container
  port stays 2981: `ports: ["127.0.0.1:8085:2981"]` for a proxy on the same host, or
  `"192.168.1.10:8085:2981"` for one elsewhere in the LAN. Bind the published port to loopback or
  the LAN address, never `0.0.0.0` on an internet-facing box, because the API itself has no auth.
- **Host network mode needs a configurable listen port.** There is no mapping in host mode, so a
  second service on 2981 collides. Add `MCAPP_PORT` (change 12), default 2981, read where `SSE_PORT`
  is used today (`config_loader.py:115`, `main.py:2936`). The HEALTHCHECK must use the same value.
- **UDP 1799 cannot be remapped on the host side.** The node sends to `EXTERN_PORT` (firmware
  `src/extudp_functions.cpp:169`, `:458`, `:781`), which is compile-time, not a node setting. So:
  one McApp per host IP. A second instance on the same box needs its own LAN IP (macvlan), not a
  different port. The container-internal UDP port could change, but nothing gains from it.

**Login in front of McApp.**

- **This is the recommended way to make McApp reachable beyond the LAN.** The proxy's login is the
  missing API authentication. Without it the "LAN-only router" rule in Security stands.
- **Every path must sit behind the login, including `/api/` and `/events`.** Protecting only the
  SPA leaves the API open. The container's own HEALTHCHECK calls `127.0.0.1:2981/health` directly,
  so no path needs an exception for it.
- **Browser tabs: HTTP Basic auth works.** The browser caches the credentials per origin and sends
  them on `fetch` and on the same-origin `EventSource` that carries SSE. Fine for a desktop
  browser, not for the installed app (next bullet).
- **Installed PWA (iOS): no Basic auth; a same-origin login page with a session cookie**
  (researched 2026-10-31). A home-screen app reportedly never shows the Basic auth prompt and
  renders the 401 body instead, and a service worker's fetch gets the 401 without a prompt
  (whatwg/fetch#1132; the iOS report is a single project, verify on a device). Cookies work:
  since iOS 17.2 login cookies are copied into the installed app, and the app's own domain is
  exempt from ITP's 7-day cap; set the cookie with `Set-Cookie` and a long `Max-Age`. **The login
  page must be on the same origin (or registrable domain)** as McApp: a redirect to a separate
  `auth.example.com` leaves the standalone app, and an expired session turns `fetch`/SSE into
  cross-origin redirects that CORS blocks. So Authelia/Authentik work when served on a path or
  subdomain of the same site, and the webapp must answer a 401 or login redirect with a full-page
  navigation. iPhone test checklist (iOS 17 and 18): log in from the icon; force-quit and
  relaunch; SSE still arrives after 5 min in the background and after a lock; Wi-Fi off/on
  reconnects without a prompt; push subscribe plus one test push (operator OK first); 8+ days
  idle, then relaunch; offline load; an expired session lands on the login page inside the app.
- **Web Push is unaffected.** Delivery goes from McApp out to the push service; the subscribe call
  is a normal same-origin request behind the login.
- **SSE through the proxy needs buffering off** (nginx `proxy_buffering off;` plus a long
  `proxy_read_timeout`, Caddy `flush_interval -1`; Traefik is covered above). Same requirement as
  today's Caddy/lighttpd chain.
- **Node Admin stays LAN-only even behind a login.** Its Host/Origin guard (`node_admin.py:68-82`)
  refuses a public host name unless it is in `MCAPP_ALLOWED_HOSTS`; adding a public name there is a
  deliberate operator decision, not a default.
- **The login does not protect UDP 1799.** Frame injection is a LAN matter; see Security.
- **Example (Traefik `basicAuth`, if Traefik is the proxy):**
  `traefik.http.routers.mcapp.middlewares: mcapp-lan,mcapp-auth` and
  `traefik.http.middlewares.mcapp-auth.basicauth.users: <htpasswd line>` (escape `$` as `$$` in
  compose). A `forwardauth` middleware replaces `basicauth` for SSO.

### UDP 1799: published directly, not through Traefik

- Traefik UDP routers match on entrypoint (port) only, there is no host routing, so they add a hop and
  no function. **Strongest reason against it:** as a proxy Traefik opens its own socket to the backend,
  so the app would see Traefik's container IP as every datagram's source and learn that as the node
  (target learning below). Publish `1799/udp` on the host.
- **Node setup:** the node's EXT-UDP IP must be the **Docker host's** LAN address (not the container's).
  The host firewall must allow `1799/udp` in (on bare metal the bootstrap opens it in `bootstrap/lib/system.sh:788` for nftables and `:901` for the iptables fallback; the container host is the operator's). Bind the published port to the LAN interface address, not
  `0.0.0.0`, on multi-homed NAS boxes.
- **Inbound:** the app binds all interfaces inside the container (`udp_handler.py:479`,
  `bind(("", port))`), so the port mapping is the only gate. Anyone who can reach 1799 can inject
  frames; the wire protocol has no authentication (`udp_handler.py:357-380` docstring).
- **Outbound is a separate socket, not port 1799.** Sends use their own long-lived socket
  (`_ensure_send_socket`, `udp_handler.py:457-471`) with an ephemeral source port. The bridge SNATs it
  (MASQUERADE) to the host IP, and the node accepts datagrams on its own 1799 regardless of the
  sender's port. So the return path needs no conntrack luck; only inbound DNAT matters.

#### Target learning vs Docker NAT (the real trap)

- **Mechanism.** The app does not need to be told the node's address. Every inbound datagram from a
  private IPv4 source goes through `_learn_target_from_source` (`udp_handler.py:868`, called at
  `:528`). First sight adopts the source IP as the outbound target (`first_seen`); a frame whose `src`
  equals our own live callsign can replace a locked target (`identified`), debounced by a 60 s cooldown
  (`_TARGET_CHANGE_COOLDOWN_S`, `:63`). `_adopt_target` (`:779`) persists it to `runtime.json`
  (`MESHCOM_IOT_TARGET`), which `Config.load` layers over `config.json` at the next boot. Trust is
  private IPv4 only, loopback excluded (`_is_trusted_node_source`, `:357`).
- **The failure.** The Docker bridge gateway (`172.17.0.1`) is a private address and is accepted. If the
  source is rewritten before the app, it learns the gateway or host, persists it, and sends every reply
  into the void; the UI shows no error.
- **The source survives** on rootful Docker with iptables/nftables DNAT (the kernel DNATs, the source
  is unchanged).
- **When it does not (all unverified for specific products, check per platform):**
  - the userland `docker-proxy` handles the packet (`userland-proxy: true` with hairpin, or a platform
    that disables iptables NAT);
  - **rootless Docker:** the default `builtin` port driver (rootlesskit) rewrites the source; the
    `slirp4netns` port driver preserves it. **Rootless Podman:** `pasta` likely preserves it (verify in
    Wave 0);
  - some NAS Docker builds with custom network stacks.
- **Detect:** `GET /api/status` already reports `udp_target`, `udp_target_kind`,
  `udp_known_source_ips` and `udp_untrusted_source_ips` (`sse_routes/stream.py:227-237`, from
  `source_ip_status()`, `udp_handler.py:760-776`). The symptom is a learned `udp_target` (kind
  `first_seen`) **equal to the container's default gateway, read from `/proc/net/route`**. Not a
  subnet test: an `x.x.x.1` in `172.16.0.0/12` heuristic is wrong, because bridge networks outside
  that range are common. Evidence: CT2 U1 (OrbStack bridge rewrote the source to `192.168.215.1`) and
  CT4c (learned `192.168.97.1`, the traefik-net gateway, persisted to the volume over the config's
  node IP). `runtime.json` holds the same value. Add the check to the Wave 4 smoke test and the
  operator docs.
- **macOS dev/test host (OrbStack, Docker Desktop).** Grant the app Local Network access in macOS
  settings, then restart it (`orb stop` / `orb start`); without it no datagram arrives (CT2 U2 first
  run). On a Mac the bridge rewrites the source (U1), so pin the target to the node IP with learning
  off (change 3). Host network mode fails safe: the source is `127.0.0.1`, learning is refused (CT2
  U2). OrbStack's `host.docker.internal` reaches the Mac's `127.0.0.1` (CT4c), so a host-side
  `ble_service` can stay on loopback; then every container on the Mac can reach it and the API key
  is its only guard.
- **Avoid, in order of preference:**

| Option                                        | Source IP preserved | Cost                                                                                                          |
| --------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------- |
| Rootful Docker, default bridge, iptables DNAT | yes                 | None; default on most Linux and Pi hosts                                                                      |
| `network_mode: host`                          | yes                 | Loses network isolation and Traefik label routing by container IP; mDNS works; **see the bind warning below** |
| macvlan (own LAN IP for the container)        | yes                 | Host cannot reach the container directly; router config                                                       |
| Pin the target and disable learning           | n/a                 | **New switch needed** (change 3)                                                                              |

- **`network_mode: host` and `MCAPP_BIND_HOST=0.0.0.0` publish the unauthenticated API on
  `host:2981`**, bypassing Traefik and its allowlist. In host mode set `MCAPP_BIND_HOST` to
  `127.0.0.1` (Traefik on the same host) or the LAN IP, never `0.0.0.0`.
- **Pin and disable learning (new, change 3):** `MCAPP_UDP_TARGET=<node-ip>` plus
  `MCAPP_UDP_LEARN=0`. Setting the target in `config.json` alone is not enough: the runtime overlay
  wins at the next boot (`_RUNTIME_OVERLAY_KEYS`, `config_loader.py:37`), and there is no off switch
  today (grep for a disable flag in `udp_handler.py` and `config_loader.py`: none).
- **The default target is a placeholder callsign** (`DX0XXX-99`, `config_loader.py:127`); with learning
  working no hostname is resolved.

### mDNS and `.local`

- **The node resolves as `<CALL>.local` through the firmware's `MDNS.begin` only.** The Alpine image
  has no `nss-mdns` and the default bridge does not forward multicast, so `<CALL>.local` fails inside
  the container. Use the IP, or let learning provide it. The bootstrap installs neither
  `avahi-daemon` nor `libnss-mdns` either (`bootstrap/lib/health.sh:315-316`, `:411`); a bare target
  such as `DX0XXX-99` goes to the plain OS resolver and is not mDNS at all.
- **If multicast is needed** (BLE-service discovery in B13): `network_mode: host`, or macvlan, or the
  host's avahi through the D-Bus socket (`/run/dbus/system_bus_socket` mounted read-only). The default
  bridge never works. B13 owns the discovery design; this paper only records the container constraint.
- The UI name is a Traefik `Host()` rule on a DNS name the operator controls. McApp does not publish
  `mcapp.local` from a container.

### BLE

- No BLE stack in the image. A native `ble_service` on some LAN host is reached over HTTP+SSE:
  `MCAPP_BLE_MODE=remote`, `MCAPP_BLE_URL=http://<host>:8081`, `MCAPP_BLE_API_KEY=<key>`
  (`config_loader.py:372-377`; URL read at `main.py:2974`, default `http://127.0.0.1:8081` at
  `config_loader.py:117`). With none: `MCAPP_BLE_MODE=disabled`. Discovery and pairing: sibling paper.

## Config and environment

| Variable                | State      | Purpose / default                                                                                         | Reference                         |
| ----------------------- | ---------- | --------------------------------------------------------------------------------------------------------- | --------------------------------- |
| `MCAPP_ENV`             | existing   | `dev` selects dev config + verbose logs; leave unset in the image                                         | `config_loader.py:331-334`        |
| `MCAPP_BLE_MODE`        | existing   | `remote` / `disabled`                                                                                     | `config_loader.py:372`            |
| `MCAPP_BLE_URL`         | existing   | remote `ble_service` URL                                                                                  | `main.py:2974`                    |
| `MCAPP_BLE_API_KEY`     | existing   | key for the remote service                                                                                | `config_loader.py:376`            |
| `MCAPP_RUNTIME_STATE`   | existing   | `runtime.json` path (set explicitly in image)                                                             | `runtime_state.py:34`             |
| `MCAPP_SECRET_KEY_PATH` | existing   | install key path                                                                                          | `secret_box.py:59-66`             |
| `MESHCOM_VAPID_PATH`    | existing   | VAPID keypair path                                                                                        | `push_delivery.py:81`             |
| `TZ`                    | standard   | container time zone (needs `tzdata`)                                                                      | image                             |
| `MCAPP_DEPLOYMENT`      | **to add** | `container` disables converge/update/slots, selects container key binding, adds `container` to `features` | changes 4, 5                      |
| `MCAPP_VERSION`         | **to add** | version string read first by `_get_version()`; set from the tag                                           | change 10, `__init__.py:5`        |
| `MCAPP_BIND_HOST`       | **to add** | uvicorn bind, default `127.0.0.1`, image sets `0.0.0.0`                                                   | `config_loader.py:114`            |
| `MCAPP_PORT`            | **to add** | uvicorn listen port, default 2981; needed in host network mode next to other web services                 | change 12, `config_loader.py:115` |
| `MCAPP_WEBAPP_DIR`      | **to add** | enables the static mount; unset on bare metal                                                             | change 2                          |
| `MCAPP_CONFIG_PATH`     | **to add** | optional config file, default as today                                                                    | `config_loader.py:331-334`        |
| `MCAPP_UDP_TARGET`      | **to add** | pinned node IP                                                                                            | change 3                          |
| `MCAPP_UDP_LEARN`       | **to add** | `0` disables source-IP learning                                                                           | change 3, `udp_handler.py:868`    |
| `MCAPP_ALLOWED_HOSTS`   | **to add** | extra Host names for the Node Admin guard                                                                 | `node_admin.py:68`                |
| `MCAPP_HARDWARE`        | **to add** | hardware label for the internet feed (default `container`)                                                | `internet_feed.py:136`            |
| `MCAPP_SECRET_KEY_FILE` | later      | read the key from `/run/secrets/...` (Docker secret), Wave 5                                              | `secret_box.py`                   |

- Not added: `MCAPP_DB_PATH` (the volume sits at the default path) and a UDP port variable: UDP 1799
  (`config_loader.py:113`) is fixed by the node firmware. The HTTP port is remapped by Docker in
  bridge mode and by `MCAPP_PORT` in host mode.

## Required code changes

| #   | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Where                                                                                                             | Size |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ---- |
| 1   | `MCAPP_BIND_HOST` replaces the hard-coded `SSE_HOST` for the uvicorn bind. Keep the constant as the default; the update-runner proxy logic still reads `self.host` (`sse_handler.py:743-745`).                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `config_loader.py:114`, `main.py:2936`, `sse_handler.py:1108-1110`                                                | S    |
| 2   | Static webapp: `StaticFiles` subclass with SPA fallback + cache headers, `GET /` redirect, mounted last. Add `MCAPP_CONFIG_PATH` in the same wave.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | new `static_webapp.py`; `sse_handler.py:679` (after the last `include_router`); `config_loader.py:331-334`        | M    |
| 3   | UDP target pin + learning switch: `MCAPP_UDP_TARGET`, `MCAPP_UDP_LEARN=0` makes `_learn_target_from_source` a no-op (still counts untrusted sources for diagnosis). Test: pinned target survives a datagram from `172.17.0.1`. The accompanying startup warning compares the source with the container's default gateway (`/proc/net/route`), not a `172.16/12` subnet.                                                                                                                                                                                                                                                                           | `udp_handler.py:868`, `config_loader.py:127`                                                                      | S    |
| 4   | `MCAPP_DEPLOYMENT=container` (one helper, `deployment.py`): converge watchdog returns early (today it is NOT gated in a container, see Persistence); `features` gains `container` (and only then); `/api/update/*` answers 404/409; slots listing skipped.                                                                                                                                                                                                                                                                                                                                                                                        | `system_converge.py:191-204`; `sse_routes/stream.py:243-252`; `sse_routes/deploy.py:93-151`; `sse_handler.py:800` | M    |
| 5   | Secret box: in container mode `board_binding()` returns empty bytes so the key file alone decides. Docker masks `/sys/firmware`; only `/proc/cpuinfo` `Serial` leaks through (Pi host only), and Alpine ships no `/etc/machine-id`, so on x86 the binding is already empty. A new image pull on the same host keeps the same binding; it breaks on host migration (Pi to Pi) or a k3s reschedule. Consumers: QRZ, Node Admin target passwords, internet-feed credentials. Side effect: a `secret.key` copied from a Pi is unreadable even on that Pi. **MUST-fix** before any of the three is offered. The empty-binding log line already exists. | `secret_box.py:88-110`                                                                                            | S    |
| 6   | Stall middleware passes `/webapp/`, bare `/webapp` and the `/` redirect through (no id, no sampling).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | `stall_middleware.py:72-77`                                                                                       | S    |
| 7   | Node Admin host guard accepts `MCAPP_ALLOWED_HOSTS` (and the container hostname is already covered by `socket.gethostname()`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `sse_routes/node_admin.py:34-35,68-82`                                                                            | S    |
| 8   | Small degradations: `/api/timezone` returns 501 when `timezonefinder` is not importable (today it would 500 inside `to_thread`); `_warm_timezone_finder_later` (`main.py:3321,3369`) skips the warm-up when the module is absent; internet-feed `hardware` from `MCAPP_HARDWARE` instead of the `Pi Zero 2W` fallback (the `wlan0` MAC and model reads are already `OSError`-safe, `internet_feed.py:94-98`).                                                                                                                                                                                                                                     | `sse_routes/weather.py:115-135`; `main.py:3321,3369`; `internet_feed.py:130-136`                                  | S    |
| 9   | Webapp (separate repo): hide Update nav entry, `/update` route, and the update affordances in `TheStatus`, `StatusPanel`, `BuildInfoCard` **only when `features` contains `container`**. Add a `isContainerDeployment` computed beside `nodeAdminAvailable` in `stores/adminStatus.ts:21-50`.                                                                                                                                                                                                                                                                                                                                                     | webapp `constants/navigation.ts:40`, `router` (`/update`), `views/Update.vue`                                     | M    |
| 10  | Version reporting: `_get_version()` reads `MCAPP_VERSION` before `git describe`; the workflow sets it from the tag; a test pins that the env value wins and that unset keeps today's behaviour.                                                                                                                                                                                                                                                                                                                                                                                                                                                   | `__init__.py:5-24`, `container.yml`                                                                               | S    |
| 11  | Container assets: `docker/Dockerfile`, `docker/compose.example.yml`, `docker/Dockerfile.dockerignore`, `.github/workflows/container.yml`, `doc/container-reference.md`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | new files                                                                                                         | M    |
| 12  | `MCAPP_PORT` replaces the hard-coded `SSE_PORT` for the uvicorn listen port (default 2981); HEALTHCHECK reads it too. Only host network mode needs it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | `config_loader.py:115`, `main.py:2936`, `docker/Dockerfile`                                                       | S    |

- **Gate direction (change 9):** the flag names the exception (`container`), absence means bare metal
  and the Update page is shown. Gating on the presence of a bare-metal flag fails the wrong way: a
  failed `/api/status` (`features=[]`) or a newer cached PWA meeting an older slot after Activate
  would hide the recovery path. mc-chat is handled separately via `backendIsMcChat`.
- **Tests** for 1-8 and 10 belong in the existing suites, wired into `scripts/run_startup_tests.py` `main()`
  (CLAUDE.md Testing: an unwired suite gates nothing). Mutation-pin changes 3 and 5.
- `release.sh` and `webapp_deploy_tests.py` pin the tarball shape (production = runtime only). The
  container build consumes that shape; do not change it for the container.

## Release and GHCR pipeline

### Hook point

- `scripts/release.sh` already publishes the combined tarball as a GitHub release asset in
  `upload_production` (`:948`, `gh release create` at `:973`) and `upload_dev` (`:988`, pre-release,
  `:1006`), tag pushed first. **Recommendation: no container step inside `release.sh`.** A workflow
  triggered by `release: published` consumes the asset, so a Docker or registry outage never blocks or
  half-publishes a release, and the image is built from exactly the artifact the Pi fleet installs.
- Releases created by `gh` with a user token trigger `release` events (`GITHUB_TOKEN`-created ones do not).

### Tags

| Release type                     | Git tag        | Image tags                   |
| -------------------------------- | -------------- | ---------------------------- |
| Production (`upload_production`) | `vX.Y.Z`       | `:vX.Y.Z`, `:X.Y`, `:latest` |
| Dev pre-release (`upload_dev`)   | `vX.Y.Z-dev.N` | `:vX.Y.Z-dev.N`, `:dev`      |

- `:latest` is production only; `:dev` moves with every pre-release. Pin `:vX.Y.Z` for stability.
- Dev images are built from the dev tarball, which ships the test corpora and `scripts/`
  (`release.sh:805` `include_tests`), so the gate can run inside the image in CI.
- Platforms: `linux/amd64`, `linux/arm64`. A 32-bit Pi OS (armv7) is not a target (Open questions).

### Workflow (sketch)

```yaml
name: container
on:
  release:
    types: [published]
permissions:
  contents: read
  packages: write
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with: { ref: "${{ github.event.release.tag_name }}" }
      - name: Fetch and verify the release tarball
        id: tarball
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ github.event.release.tag_name }} # unset TAG makes `gh release download ""` fetch the latest
        run: |
          gh release download "$TAG" -p 'mcapp-*.tar.gz*' -D dl
          (cd dl && sha256sum -c ./*.sha256)
          mkdir ctx && tar -xzf dl/mcapp-*.tar.gz -C ctx --strip-components=1
          echo "version=${TAG#v}" >> "$GITHUB_OUTPUT"
      - uses: docker/setup-qemu-action@<pin>
      - uses: docker/setup-buildx-action@<pin>
      - uses: docker/login-action@<pin>
        with:
          {
            registry: ghcr.io,
            username: "${{ github.actor }}",
            password: "${{ github.token }}",
          }
      - id: meta
        uses: docker/metadata-action@<pin>
        with:
          images: ghcr.io/dk5en/mcapp
          flavor: latest=false
          tags: |
            type=semver,pattern=v{{version}}
            type=semver,pattern={{major}}.{{minor}},enable=${{ !github.event.release.prerelease }}
            type=raw,value=latest,enable=${{ !github.event.release.prerelease }}
            type=raw,value=dev,enable=${{ github.event.release.prerelease }}
      - uses: docker/build-push-action@<pin>
        with:
          context: ctx
          file: docker/Dockerfile # from the checkout, not from ctx
          build-args: MCAPP_VERSION=${{ steps.tarball.outputs.version }}
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

- Pin every action to an exact release as `tests.yml` does; `<pin>` marks versions to resolve later.
- **Multi-arch:** QEMU is enough for the first cut (every wheel is prebuilt; arm64 only installs
  them plus the pure-Python `http-ece` sdist). Switch to native arm runners (`ubuntu-24.04-arm`) with a
  manifest merge if QEMU is slow or option B is adopted.
- **Smoke job** after the build (amd64): run the image, wait for `healthy`, then assert `GET /health`,
  `GET /webapp/` and `GET /api/status` (`features` contains `container`; the call is made through the
  published port). For `:dev`, additionally run `python scripts/run_startup_tests.py` inside the image.
- **GHCR:** `GITHUB_TOKEN` with `packages: write` creates the package on the first push from the
  owner repo; no PAT is needed. It starts private: set it public once; the
  `org.opencontainers.image.source` label links it to the repo.
- **`.dockerignore`** must sit in the build context root, which is the extracted tarball (`ctx/`), not
  the repo. Ship it as `docker/Dockerfile.dockerignore` (BuildKit reads it beside the Dockerfile) or
  drop it; the tarball is already runtime-only.

## Security

- **The API has no authentication** (CLAUDE.md, Node Admin) and CORS is `allow_origins=["*"]`
  (`sse_handler.py:650-657`). A public Traefik router would expose message history, sending and QRZ
  credentials to the internet. The compose example therefore ships a **LAN-only router**
  (`ipallowlist` on the real LAN range only, never the Docker bridge range; v3 name, `ipwhitelist` on
  Traefik v2) and states the rule: never add a public entrypoint to this router.
- Do not publish `2981` openly. Reach it through Traefik over the Docker network, or publish it on
  loopback / the LAN address for your own reverse proxy (in host mode, see the bind warning in
  Networking).
- **A login at the reverse proxy is the supported way to reach McApp from outside the LAN** (see
  "Behind your own reverse proxy"). It must cover every path, `/api/` and `/events` included.
- Publish `1799/udp` on the LAN interface address only; allow it from the node's IP at the host
  firewall. Any source that reaches 1799 can inject frames (`udp_handler.py:357-380`).
- Non-root uid 10001, `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, tmpfs `/tmp`, memory limit.
  No docker socket, no `privileged`, no host paths except the data volume.
- Secrets: the QRZ password, Node Admin target passwords and internet-feed credentials are AES-GCM
  encrypted with the key file in the volume. Anyone with the volume can decrypt; same trust model as
  bare metal. A Docker secret (`/run/secrets/...`, Wave 5) only moves the key out of the volume: it
  helps backups, not a compromised container.
- Image hygiene: rebuild for base-image CVEs (scheduled or Dependabot), optional SBOM/provenance.

## Phases (waves with file ownership)

| Wave | Goal                             | Owns (exclusive)                                                                                                                                                                                                                                                                                                                                      | Gate                                                                  |
| ---- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| 0    | Spike: prove the premise by hand | local scratch only: hand-built image from a dev tarball, no repo files                                                                                                                                                                                                                                                                                | Image runs, size measured, `read_only` ok, startup suites pass inside |
| 1    | Backend container mode           | `config_loader.py`, `main.py` (bind line), `system_converge.py`, `sse_routes/stream.py`, `sse_routes/deploy.py`, `sse_handler.py` (slots line), `secret_box.py`, `udp_handler.py`, `sse_routes/node_admin.py`, `sse_routes/weather.py`, `internet_feed.py`, `__init__.py`, new `deployment.py`, matching `*_tests.py`, `scripts/run_startup_tests.py` | ruff, `uv run mypy src/mcapp ble_service/src`, `run_startup_tests.py` |
| 2    | Static webapp serving            | new `static_webapp.py`, `sse_handler.py` (`_create_app` only), `stall_middleware.py`, tests                                                                                                                                                                                                                                                           | Same gate; SPA fallback and cache-header tests                        |
| 3    | Webapp gate (webapp repo)        | webapp `stores/adminStatus.ts`, `constants/navigation.ts`, router, `views/Update.vue`, status components                                                                                                                                                                                                                                              | webapp lint/typecheck/vitest                                          |
| 4    | Image and CI                     | `docker/Dockerfile`, `docker/compose.example.yml`, `docker/Dockerfile.dockerignore`, `.github/workflows/container.yml`, `doc/container-reference.md`, `CLAUDE.md` section                                                                                                                                                                             | Smoke job green on amd64; manual run on a Pi (arm64) and an x86 NAS   |
| 5    | Docker secret for the QRZ key    | `secret_box.py`, `doc/container-reference.md`                                                                                                                                                                                                                                                                                                         | Key from file, same suites                                            |
| 6    | BLE with the container           | depends on B13; compose example gains the remote BLE variant                                                                                                                                                                                                                                                                                          | Remote BLE end to end                                                 |

- Waves 1 and 2 are independent of 3 and 4 but must land before the first image is published (without
  change 4 the converge watchdog writes into the volume); Wave 3 must be released together with Wave 1
  (same webapp/backend tarball).
- Wave 0 has one more job: prove the UDP source IP survives on each target platform (Pi Docker, a
  Synology/QNAP/Unraid host, rootless Podman) before documenting a "supported" list.

## Risks

| Risk                                                                                | Impact                                                                      | Mitigation                                                                                       |
| ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Source IP rewritten by NAT/userland proxy                                           | Replies lost, silent                                                        | Detect via `/api/status` + `runtime.json`; change 3; docs                                        |
| CI tests Python 3.11 only (`tests.yml`); the image runs 3.13                        | Untested interpreter in production                                          | Run the startup gate inside the `:dev` image; consider 3.13 in CI                                |
| musl allocator behaves differently from glibc (no `MALLOC_ARENA_MAX`)               | Memory or thread-pool latency                                               | Measure RSS and `pool_wait`/`loop_lag` stalls in Wave 0                                          |
| `timezonefinder` excluded                                                           | Node GPS card lacks UTC offset hint                                         | 501 + webapp tolerance (verify the card handles a failure); `:full` later                        |
| Node Admin host guard refuses public-style host names                               | Feature "broken" behind Traefik                                             | Change 7, document in the compose example                                                        |
| Volume on NFS/SMB                                                                   | Corrupt or locked DB                                                        | Docs warning, startup log line when `db_path` is on a network FS (S, optional)                   |
| Secret-box binding                                                                  | `credentials_unreadable` on host migration or k3s reschedule (not on pulls) | Change 5 before the first image; document re-entry of QRZ, Node Admin, feed credentials          |
| Two update channels (webapp Update page vs `docker pull`) confuse mixed-fleet users | Support load                                                                | `container` gate (absence shows the page), docs                                                  |
| LAN allowlist matches the Docker bridge range                                       | Internet clients pass the allowlist                                         | Real LAN range only; check Traefik's access log for the client IP                                |
| SQLite fsync cost on NAS disks (HDD, btrfs CoW)                                     | Handler stalls like the SD card ones                                        | `synchronous=NORMAL` already set; stall tracking shows it                                        |
| Image/tarball drift (`build_tarball` shape); GHCR/Actions outage                    | No image for a release                                                      | Smoke job; `webapp_deploy_tests.py` pins the shape; workflow is re-runnable, release not blocked |

## Open questions

- (Researched 2026-10-31; Wave 0 confirms) UDP source-IP support by platform:
  - **Supported:** rootful Docker CE on Pi OS 64-bit, Debian, Ubuntu (DNAT keeps the source;
    docker-proxy only handles loopback and cross-network traffic); Synology Container Manager,
    QNAP Container Station and Unraid in bridge mode, each with `network_mode: host` as the
    documented fallback (no vendor documentation on UDP source IP found; Synology users run UDP
    services in host mode); rootful Podman (netavark DNAT).
  - **Conditional:** rootless Podman 5 only on its default pasta network (`-p`, no
    compose-created network), after a smoke test.
  - **Unsupported:** rootless Docker with the default builtin port driver (rootlesskit docs: source
    IP propagated "for TCP only", not UDP); rootless Podman on bridge or custom networks
    (rootlessport does not preserve the client address; Pi-hole reports the same for DNS).
  - **Startup warning:** if the first UDP source equals the container's default gateway (from
    `/proc/net/route`; not a `172.16.0.0/12` subnet test, see Target learning), log a WARNING naming
    this section; it catches the NAT failure on day one.
  - 32-bit Pi OS is moot for Docker anyway: Docker Engine 29 ships no 32-bit Raspberry Pi OS
    packages.
- (Decided 2026-10-31) 64-bit only: `linux/arm64` and `linux/amd64`. No `linux/arm/v7`; a
  32-bit Pi OS user reinstalls with 64-bit (the Zero 2W runs 64-bit already).
- (Answered 2026-10-31) The GPS card survives a failing `/api/timezone`, but needs a small webapp
  change. The automatic lookup (`NodeGpsCard.vue:55-70`, a `watchEffect`) swallows any error and the
  timezone field falls back to `SN.UTCOF` or `--`; the Locate button shows "UTC offset lookup
  failed" and skips `--utcoff`. But the `watchEffect` retries on every GPS register update while no
  timezone is known, and `stallReporter.ts:493-505` records a `client_error` row for every non-2xx,
  so a container would fill `stall_events` with noise. Webapp change: remember a failed lookup for
  the session and pass `skipRecord` for this path. Backend change 8 must map the missing module to
  501 explicitly: today `_get_tz_finder()` (`weather.py:28-33`) has no guard, so the `ImportError`
  surfaces as a plain 500.
- Should `:full` (with `timezonefinder`, amd64 only) exist at all?
- GitHub Actions (checked 2026-10-31): Actions are enabled on the repository and it is public;
  only the `tests` workflow is `disabled_manually` (the operator recalls it was switched off when
  it ran out of resources). For a public repository, standard runners and public GHCR storage cost
  nothing, and native arm64 runners (`ubuntu-24.04-arm`) are available for public repositories
  (verify before Wave 4), so no QEMU is needed. Fallback if Actions stay off: build and push the
  multi-arch image locally with `docker buildx` during `release.sh`. Decide which before Wave 4.
- Traefik specifics (default `compress` behaviour for `text/event-stream`, `flushInterval` default) are
  recalled, not verified against the deployed version.
- (Researched 2026-10-31) iOS PWA: no Basic auth, same-origin cookie login; see "Behind your own
  reverse proxy" for the reasoning and the iPhone checklist. The device test in Wave 0 remains.

## Appendix: k3s recipe (unsupported)

Not a supported target. For operators who run McApp on k3s anyway:

- **`replicas: 1`, `strategy: Recreate`.** SQLite has one writer; a rolling update would run two pods
  against one database for a moment. Never scale above 1.
- **RWO PVC on local storage** (`local-path` provisioner, node-pinned). **Do not use NFS or SMB for the
  database PVC**: WAL needs POSIX locks and shared memory; a NAS share is the most tempting and the most
  dangerous choice. Longhorn/NFS-backed classes are not safe for `messages.db`.
- **UDP 1799:** `hostNetwork: true` (keeps the source IP; bind `MCAPP_BIND_HOST` to the LAN IP, see
  Networking) or a `LoadBalancer` Service with `externalTrafficPolicy: Local` (kube-proxy SNAT breaks
  learning otherwise). Pin the target with `MCAPP_UDP_TARGET` + `MCAPP_UDP_LEARN=0`.
- **HTTP:** a normal Service + Ingress/IngressRoute. Same SSE rules: no compression or buffering on
  `/events`, no short idle timeout, LAN-only middleware.
- `fsGroup: 10001`, `readOnlyRootFilesystem: true`, `emptyDir` for `/tmp`, image tag pinned to a release.
- A reschedule to another node changes the host binding: see change 5.
