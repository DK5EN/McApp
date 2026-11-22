# Configuration reference

| Field | Value |
|---|---|
| ID | REF-CFG |
| Status | DRAFT. Written 2026-11-10 from `src/mcapp/config_loader.py` and `bootstrap/lib/config.sh`. Replaces the configuration section of the archived `operations-reference`. |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Sources | `src/mcapp/config_loader.py` (dataclasses and `_from_dict`); `config.sample.json`; `bootstrap/lib/config.sh`; `src/mcapp/internet_feed.py`, `runtime_state.py`, `secret_box.py` (environment variables) |

## 1. Files

| File | Used when | Written by |
|---|---|---|
| `/etc/mcapp/config.json` | production | bootstrap (`config.sh`), backed up before each write (POL-004 §1.4) |
| `/etc/mcapp/config.dev.json` | `MCAPP_ENV=dev` | you |
| `config.sample.json` (repo) | template only | - |
| `/var/lib/mcapp/runtime.json` (runtime state, `DEFAULT_RUNTIME_PATH`) | always, overlay for `CALL_SIGN`, `MESHCOM_IOT_TARGET`, `LAT`, `LONG` | the app (last known GPS, learned target) |

The runtime overlay keeps the position the paired node reported, so the next boot starts from the last known
position and not from the seed in `config.json`. Setting `LAT`/`LONG` in `config.json` does not override a newer overlay value.

## 2. Top-level keys (`config.json`)

| Key | Default in code | Meaning |
|---|---|---|
| `CALL_SIGN` | `""` | Node callsign with SSID as the node stores it (`DK5EN-98`). Needed for command handling and Node Admin. |
| `USER_INFO_TEXT` | `""` | Free text in the user info beacon. |
| `MESHCOM_IOT_TARGET` | written by the bootstrap as `<CALLSIGN>.local` (`bootstrap/lib/config.sh`, line 257); dataclass default `DX0XXX-99` is a placeholder | MeshCom node to send to (hostname or IP). Overrides `UDP_TARGET`. Behind NAT set the node's real IP. |
| `UDP_TARGET` | - | Older name for `MESHCOM_IOT_TARGET`, read only if the new key is absent. |
| `LAT`, `LONG` | none | Cold-start seed for weather. Refined by the node's GPS. |
| `STAT_NAME` | `""` | Station name for weather. |
| `DB_PATH` | `/var/lib/mcapp/messages.db` | SQLite database. |
| `PRUNE_HOURS` | `720` | Chat message retention (30 days). |
| `PRUNE_HOURS_POS` | `192` | Position and signal retention (8 days). |
| `PRUNE_HOURS_ACK` | `192` | ACK retention (8 days). |
| `WIRE_LOG_ENABLED` | `true` | Keep every raw frame in the wire log. Never pruned. |
| `WIRE_LOG_DIR` | `/var/lib/mcapp/wire` | Where the wire log lives. |
| `BLE_MODE` | `remote` | `remote` or `disabled`. There is no local mode (removed in v1.01.1). |
| `BLE_API_KEY` | generated (16 chars) by the bootstrap | Sent as `X-Api-Key` to the BLE service. The service reads `BLE_SERVICE_API_KEY` from its own environment. |
| `NODE_CONSOLE_PASSWORD` | `""` | Password of the node's TCP net console (ESP32 only). Never logged or shown in status. |
| `NODE_CONSOLE_PORT` | `2323` | Net console port. |
| `NODE_CONSOLE_MAX_SESSION_S` | `1800` | Auto-stop of a DBG session, with flag restore. |
| `stalls` | defaults below | Nested object, see §3. |
| `node_admin` | `{}` | Optional. `allowed_origins` extends the Origin check for the Vite dev server. Nothing else. |

Not in the code any more (documented before, now absent): `SSE_ENABLED`, `SSE_HOST`, `SSE_PORT` (fixed in code, §4),
`BLE_REMOTE_URL` (the BLE URL is `BLE_SERVICE_URL` in code, `127.0.0.1:8081`; override with `MCAPP_BLE_URL`),
`BLE_DEVICE_NAME`, `BLE_DEVICE_ADDRESS`, `MAX_STORAGE_SIZE_MB`, `UDP_PORT_list/send`.

## 3. The `stalls` object

| Key | Default | Meaning |
|---|---|---|
| `stall_ms` | 500 | Stall threshold. |
| `critical_ms` | 2000 | Critical threshold. |
| `sample_every` | 50 | 1-in-N sample of sub-threshold HTTP calls. |
| `loop_lag_ms` | 100 | Event-loop lag threshold. |
| `pool_wait_ms` | 100 | Queue wait before a `to_thread` job runs. |
| `handler_ms` | 500 | Publish-subscriber threshold. |
| `handler_sample_every` | 500 | Handler samples have their own rate (about 160 rows/day). |
| `body_cap_bytes` | 8192 | Body stored per stall row. |
| `max_rows` | 5000 | Row cap, pruned oldest-first by the writer thread. |

ADR-0007 and `../sop/SOP-006-stall-investigation.md` explain the thresholds.

## 4. Fixed values (code, not configurable)

| Name | Value | Where |
|---|---|---|
| `MESHCOM_UDP_PORT` | 1799 | `config_loader.py` |
| `SSE_HOST` / `SSE_PORT` | `127.0.0.1` / `2981` | `config_loader.py` (loopback; Caddy proxies to it) |
| `BLE_SERVICE_URL` | `http://127.0.0.1:8081` | `config_loader.py` |

## 5. Environment overrides

| Variable | Effect |
|---|---|
| `MCAPP_ENV=dev` | use `config.dev.json`, verbose logging |
| `MCAPP_BLE_MODE` | override `BLE_MODE` |
| `MCAPP_BLE_URL` | override the BLE service URL |
| `MCAPP_BLE_API_KEY` | override `BLE_API_KEY` |
| `MCAPP_WIRE_LOG_ENABLED`, `MCAPP_WIRE_LOG_DIR` | override the wire log |
| `MCAPP_NODE_TZ`, `MCAPP_NODE_TZ_POLICY` | node time zone (ADR-0020); policy `never` opts out |
| `MCAPP_INTERNET_FEED_URL` | meshmap feed base URL |
| `MCAPP_RUNTIME_STATE` | relocate the runtime state directory |
| `MCAPP_SECRET_KEY_PATH` | relocate the install key used by `secret_box.py` (POL-003) |
| `MESHCOM_VAPID_PATH`, `MESHCOM_VAPID_SUB` | VAPID key location and `sub` (ADR-0013) |

## 6. Checks

```bash
ssh mcapp.local 'sudo python3 -m json.tool /etc/mcapp/config.json > /dev/null && echo valid'
curl -s http://mcapp.local/api/status | python3 -m json.tool | head -40
```

## 7. UDP node target, learning and NAT deployments

Restored 2026-11-10 from the archived operations reference (linked from the webapp document `implementation-send-failed.md`).
`MESHCOM_IOT_TARGET` names the MeshCom node (default `<CALLSIGN>.local`). On a
real LAN the exact value barely matters: the outbound target is **learned**
from inbound node traffic (trust gate: private IPv4 sources only, see
`_is_trusted_node_source` in `udp_handler.py`) — `/api/status` then reports
`udp_target_kind: identified`.

That self-healing does NOT work behind NAT/port-forwarding (OrbStack/Docker
style), where every inbound datagram arrives with a loopback source that the
trust gate deliberately refuses. Receiving works; sending silently dies with
`UDP_SEND failed ... Temporary failure in name resolution` in the journal.
Fix: set `MESHCOM_IOT_TARGET` to the node's real IP explicitly (outbound
through NAT is fine). Verified 2026-08-09 in the OrbStack simulation.

A failed UDP send is reported to the webapp as a `msg:status` SSE event
(`send_failed: true` plus `src`/`dst`/`msg`/`reason` for content correlation
— no msg_id exists before the firmware mints one), alongside the existing
global error toast.

