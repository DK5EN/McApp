# Stall tracking — plan and campaign state (2026-09-15)

**Goal:** every stall between the webapp and the API is recorded with the full parameters needed
to reproduce it, on both ends, keyed by one correlation id, and is retrievable by a coding agent
through one endpoint. Companion: the B4 memory quick wins in `doc/backlog.md` ship in the same
campaign (bootstrap only, no application code).

**Decisions (operator, 2026-09-15):** capture-and-upload only on the webapp, no UI (a coding
agent consumes `/api/stalls`); anything above **0.5 s is a stall**, above **2 s is critical**; we
are on a LAN with spare CPU and a database that can hold far more rows than the journal can.

## 1. What a stall is

| Kind (`kind`)    | Where measured                          | Stall at | Critical at | Notes                                                          |
| ---------------- | --------------------------------------- | -------- | ----------- | -------------------------------------------------------------- |
| `http`           | server ASGI middleware, per request     | 500 ms   | 2000 ms     | plus 1-in-50 sampling of normal requests → `severity = sample` |
| `client_http`    | webapp `useProxyAPI` wrapper            | 500 ms   | 2000 ms     | includes Caddy/lighttpd hop; same 1-in-50 sampling             |
| `client_timeout` | webapp abort timeout                    | always   | always      | fetch aborted at 10 s; the abort is a safety net, not the SLO  |
| `client_error`   | webapp fetch rejected / non-2xx         | always   | —           | network error or HTTP ≥ 400, with the same parameters          |
| `sse_answer`     | webapp, `page_request` → SSE `response` | 500 ms   | 2000 ms     | missing after 10 s → `sse_answer_missing`                      |
| `sse_heartbeat`  | webapp heartbeat watchdog (90 s)        | always   | always      | existing reconnect path, now recorded                          |
| `loop_lag`       | server drift monitor task               | 100 ms   | 500 ms      | proves the event loop was blocked, independent of any request  |
| `pool_wait`      | server instrumented default executor    | 100 ms   | 500 ms      | queue wait before a `to_thread` job ran — pool starvation      |
| `handler`        | server `MessageRouter.publish` loop     | 500 ms   | 2000 ms     | one subscriber slow; carries the routed message                |

All thresholds live under a `stalls` key in `config.json` with these defaults; absent key = defaults.

## 2. The record (shared by both ends)

Table `stall_events` (migration adds it; `LATEST_SCHEMA_VERSION` bumps in the same commit):

| column        | type    | meaning                                                                       |
| ------------- | ------- | ----------------------------------------------------------------------------- |
| `id`          | INTEGER | PK                                                                            |
| `ts_ms`       | INTEGER | event time, ms (server clock for server records, client clock for client)     |
| `origin`      | TEXT    | `server` / `client`                                                           |
| `kind`        | TEXT    | see §1                                                                        |
| `severity`    | TEXT    | `sample` / `stall` / `critical`                                               |
| `request_id`  | TEXT    | correlation id, nullable for `loop_lag` / `handler`                           |
| `session_id`  | TEXT    | per-tab id from the client, nullable                                          |
| `method`      | TEXT    |                                                                               |
| `path`        | TEXT    | route path without query                                                      |
| `query`       | TEXT    | raw query string                                                              |
| `body`        | TEXT    | JSON body, redacted, capped at 8 KB (`body_truncated` in context if cut)      |
| `status`      | INTEGER | HTTP status or NULL                                                           |
| `duration_ms` | REAL    |                                                                               |
| `context`     | TEXT    | JSON: server snapshot or client snapshot (below)                              |
| `detail`      | TEXT    | JSON: kind-specific (handler name + message type + routed message; lag value) |

Indexes: `(ts_ms)`, `(kind, ts_ms)`. Row cap `max_rows` (default 5000), pruned by the recorder.

Server context snapshot: `loop_lag_ms`, `pool_queued`, `pool_running`, `pool_max`, `sse_clients`,
`rss_kb`, `db_bytes`, `wal_bytes`, `ble_connected`, `version`, `slot`, `resp_bytes`.
Client context snapshot: `app_version`, `ua`, `online`, `visibility`, `sse_state`, `sse_age_ms`,
`outbox_len`, `base_url`, `device_memory`, `connection_type`.

Redaction (both ends, before the record leaves the handler): push subscription `endpoint`, `keys`,
any header/field named `*api_key*`/`authorization`. Message text is ham radio traffic and stays.

## 3. Correlation

- Client mints `X-Request-Id` (UUID v4) per call and a `X-Session-Id` per tab; server echoes
  `X-Request-Id` on the response and stores both.
- `mcapp.stalls.current_request_id` is a `ContextVar[str | None]` set by the middleware.
  The `page_request` path already carries its own `request_id` (webapp `messages.ts`, echoed on
  `proxy:messages_page` by `_handle_messages_page_command`); the client's `sse_answer` timer keys
  on that existing id, no backend change needed.
- Every server log line emitted while a request is in flight can be joined on the id via the
  journal timestamp; the record is the primary artefact, the journal is secondary.

## 4. Endpoints

- `GET /api/stalls?since=<ms>&kind=&severity=&limit=` → `{ "rows": [...] }`, newest first,
  default limit 200, max 2000. Full records, no summarisation — this is what the coding agent reads.
- `GET /api/stalls/summary?since=<ms>` → per `path`: count, p50, p95, p99, max over `http` rows
  (samples and stalls together) — the baseline.
- `POST /api/stalls/client` → one record or an array; validated, redacted again server-side,
  capped; `{ "accepted": n }`. No auth (like every other route); rate-capped by `max_rows`.

## 5. Replay

`scripts/replay_stall.py --base http://mcapp.local --id <row id> [--repeat 20]` re-issues the
recorded request against a running instance, prints p50/p95/max of the new run next to the
recorded duration and the recorded server context. `--json <file>` takes a row saved from
`/api/stalls`. Also `--summary` to print the baseline table.

## 6. Interface contract (agents implement against this, orchestrator wires it)

```python
# src/mcapp/config_loader.py  (W1-A adds this next to UDPConfig/BLEConfig, populated in Config.load
# from raw["stalls"], absent key = defaults; Config gets a `stalls: StallsConfig` field)
@dataclass
class StallsConfig:
    stall_ms: int = 500; critical_ms: int = 2000; sample_every: int = 50
    loop_lag_ms: int = 100; pool_wait_ms: int = 100; handler_ms: int = 500
    body_cap_bytes: int = 8192; max_rows: int = 5000

# src/mcapp/stalls.py
current_request_id: ContextVar[str | None]          # set by the middleware, read by anyone

class StallRecorder:
    def __init__(self, db_path: Path | str, config: StallsConfig, *, version: str, slot: str) -> None
    async def start(self) -> None      # ensures table (same DDL as migration 32), starts own writer
                                       # thread + bounded queue.Queue, starts the loop-lag task
    async def stop(self) -> None
    def record(self, *, kind: str, severity: str, origin: str = "server", request_id: str | None = None,
               session_id: str | None = None, method: str | None = None, path: str | None = None,
               query: str | None = None, body: Any = None, status: int | None = None,
               duration_ms: float | None = None, detail: Any = None) -> None
                                       # non-blocking; fills ts_ms + context=snapshot() itself; redacts +
                                       # caps body/detail; drops on full queue and counts the drop
    def severity_for(self, duration_ms: float) -> str | None   # "critical" / "stall" / "sample" / None
    def snapshot(self) -> dict[str, Any]                       # server context, cheap, sync
    def register_gauge(self, name: str, fn: Callable[[], Any]) -> None   # e.g. sse_clients, ble_connected
    def install_executor(self, loop: asyncio.AbstractEventLoop) -> None  # counting ThreadPoolExecutor,
                                       # same max_workers default as asyncio; records pool_wait
    def time_handler(self, message_type: str, handler_name: str,
                     routed_message: dict[str, Any]) -> AbstractContextManager[None]   # for publish()
    async def query(self, *, since_ms: int | None = None, kind: str | None = None,
                    severity: str | None = None, limit: int = 200) -> list[dict[str, Any]]
    async def summary(self, *, since_ms: int | None = None) -> list[dict[str, Any]]
    async def ingest_client(self, records: list[dict[str, Any]]) -> int   # validates, redacts, origin=client
    @property
    def dropped(self) -> int

def redact(obj: Any) -> Any     # pure, recursive; keys endpoint/keys/p256dh/auth/*api_key*/authorization

# src/mcapp/stall_middleware.py
class StallMiddleware:           # pure ASGI (NOT BaseHTTPMiddleware); skips /events, /update-stream,
                                 # /health; caches body once; sets/echoes X-Request-Id; reads
                                 # X-Session-Id; sets current_request_id for the request's lifetime
    def __init__(self, app: ASGIApp, recorder: StallRecorder) -> None

# src/mcapp/sse_routes/stalls.py   (same pattern as the other sse_routes modules)
def build_stalls_router(manager: SSEManager) -> APIRouter   # uses manager.stall_recorder (None → 503)
```

DDL (identical in migration 32 and in `StallRecorder.start`):

```sql
CREATE TABLE IF NOT EXISTS stall_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    origin TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    request_id TEXT,
    session_id TEXT,
    method TEXT,
    path TEXT,
    query TEXT,
    body TEXT,
    status INTEGER,
    duration_ms REAL,
    context TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_stall_events_ts ON stall_events(ts_ms);
CREATE INDEX IF NOT EXISTS idx_stall_events_kind_ts ON stall_events(kind, ts_ms);
```

## 7. Waves and ownership

| Wave | Agent          | Exclusive files                                                                                                                                                                                                                                               | Status                                                                                                                                                                       |
| ---- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | W1-A core      | `src/mcapp/stalls.py`, `src/mcapp/stall_tests.py`                                                                                                                                                                                                             | done                                                                                                                                                                         |
| 1    | W1-B http      | `src/mcapp/stall_middleware.py`, `src/mcapp/sse_routes/stalls.py`, `src/mcapp/stall_http_tests.py`, `scripts/replay_stall.py`                                                                                                                                 | done                                                                                                                                                                         |
| 1    | W1-C schema    | `src/mcapp/storage/migrations.py`, `src/mcapp/storage/constants.py` (schema version only), `src/mcapp/storage/migration_chain_tests.py`                                                                                                                       | done                                                                                                                                                                         |
| 1    | W1-D bootstrap | `bootstrap/lib/system.sh`, `bootstrap/mcapp.sh` (epoch), `src/mcapp/system_converge.py` (epoch), `bootstrap/templates/mcapp.service`, `bootstrap/templates/mcapp-ble.service`, `bootstrap/templates/caddy/caddy.service`, `bootstrap/templates/journald.conf` | done                                                                                                                                                                         |
| 1    | W1-E webapp    | webapp repo: `src/composables/useProxyAPI.ts`, `src/services/stallReporter.ts` (+ spec), `src/composables/useSSEClient.ts`, the page_request sender/consumer, `docs/`                                                                                         | done                                                                                                                                                                         |
| 2    | orchestrator   | `src/mcapp/main.py`, `src/mcapp/sse_handler.py`, `scripts/run_startup_tests.py` (hotspot wiring)                                                                                                                                                              | done (backend gate green 15:05, e2e smoke OK)                                                                                                                                |
| 2    | W2-docs        | `CLAUDE.md` (section), `doc/database-reference.md`, `doc/operations-reference.md`, `doc/backlog.md` (B4 status)                                                                                                                                               | done                                                                                                                                                                         |
| gate | fable-review   | advisor pass on the combined diff                                                                                                                                                                                                                             | backend REWORK (ingest caps, test gaps) and webapp REWORK (store import broke a spec, /api/send abort would re-send, settle order, body shape) both fixed and re-gated green |
| ship | dev-release    | both repos, deploy to mcapp.local, verify `/api/stalls` live and one client record landing                                                                                                                                                                    | open                                                                                                                                                                         |

Shared resources: one test runner per repo (`scripts/run_startup_tests.py`, `npm test`) — writers
run only their own suite; the whole run is the gate. No device, no port in wave 1.

## 8. B4 quick wins in this campaign (W1-D)

`gpu_mem=16`, `dtoverlay=vc4-kms-v3d,cma-64`, `dtparam=audio=off`, `cgroup_enable=memory
cgroup_memory=1` on `cmdline.txt`; journald `RuntimeMaxUse=8M`; `unattended-upgrades.service` disabled (timers stay); `MALLOC_ARENA_MAX=2` and direct
`.venv/bin/...` `ExecStart` in both unit templates; Caddy `GOMEMLIMIT` 256MiB → 48MiB (`GOGC=50` is already set). The
`/run/journalxship` tmpfs is managed by the AIOps units on the box, not by this repo — out of scope.
`SYSTEM_EPOCH` and `REQUIRED_SYSTEM_EPOCH` bump together. Boot-config changes take effect after a
reboot; the converge path must record that and never assume they apply live.
