---
name: ai-ops
description: The "is McApp actually healthy on mcapp.local" check — services, active slot, database, live data flow, logs, host headroom and secret hygiene on the Raspberry Pi that runs production. Use for "daily check", "check the Pi", "is mcapp healthy", "check mcapp.local", "any issues with McApp", "do your ops job", or before claiming production is fine.
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# AI-Ops — the McApp production check

`mcapp.local` (Raspberry Pi Zero 2W) is **the** production target and the only host running
MCProxy; there is no staging box. `rpizero.local` no longer runs MCProxy: do not check or "fix" it.

Work top to bottom. Each phase is cheap; stop and chase only what a phase actually flags.

## Ground rules

- **Read-only on the Pi.** Never edit code in a slot or write to the production database; fixes go into the repo and reach the Pi through `/dev-release`.
- **Never pipe the ssh through `tail`/`head`** (it reports tail's exit code). Redirect to a file on the Pi, echo `$?` inside the remote shell, then tail the file.
- **Never print `/etc/mcapp/config.json` wholesale** (`BLE_API_KEY` in clear); select the keys you need.
- **The Pi's shell is German-localised** (`free` prints `Speicher:`). Parse by column position or `--output=`, never by an English label: a failed match reads like a healthy zero.

## Phase 1 — Services and restart history

```bash
ssh mcapp.local '
  systemctl is-active mcapp mcapp-ble caddy lighttpd
  systemctl show mcapp     -p NRestarts -p ActiveEnterTimestamp --value
  systemctl show mcapp-ble -p NRestarts --value
'
```

`NRestarts` is the check that matters. **The app cannot report a crash loop about itself** (a restarted service shows a short, healthy `uptime_seconds`).
Any non-zero `NRestarts` is a finding; get the reason from the journal (Phase 6), do not guess. Expected: all four units active, `NRestarts=0` on both McApp units.

## Phase 2 — The app's own status surface

```bash
curl -sk https://mcapp.local/health
curl -sk https://mcapp.local/api/status | python3 -m json.tool
```

`/api/status` carries four UDP-provenance fields that exist for a security reason — port 1799
is **unauthenticated**, so anything on the LAN can inject frames:

| field                           | read it as                                                               |
| ------------------------------- | ------------------------------------------------------------------------ |
| `udp_target_kind`               | `identified` = we know which node we talk to; anything else is a finding |
| `udp_multiple_sources`          | `true` means more than one host is sending — investigate, never ignore   |
| `udp_untrusted_source_ips`      | non-empty is a finding: someone else is injecting                        |
| `udp_suppressed_target_changes` | a climbing count means something is trying to steal the target           |

## Phase 3 — Which code is actually running

A green health banner says nothing about _which build_ is live.

```bash
ssh mcapp.local '
  readlink -f ~/mcapp-slots/current
  cat /var/lib/mcapp/system-epoch
  grep -h "^LATEST_SCHEMA_VERSION" ~/mcapp-slots/current/src/mcapp/storage/constants.py
'
```

- **Grep the ACTIVE slot for a symbol only the change you care about introduces**; everything else can look healthy while the service runs last week's code.
- **Slot order is not fixed**, and re-deploying the version the active slot already holds deploys in place: an unchanged `current` symlink is not evidence of a failed deploy.
- **`version.html` reports what was deployed, not what the browser runs** (the service worker may serve an older bundle). Its path is **`https://mcapp.local/webapp/version.html`**; the bare `/version.html` is a 404 that reads like a failed frontend deploy.
- **System epoch parity**: `/var/lib/mcapp/system-epoch` must match `REQUIRED_SYSTEM_EPOCH`
  in `src/mcapp/system_converge.py` (and `SYSTEM_EPOCH` in `bootstrap/mcapp.sh`). A stale
  epoch means the converge watchdog has system-level work pending.

## Phase 4 — Database

There is **no `sqlite3` CLI** on the Pi. Write a python3 heredoc, open read-only, delete it after.
**All timestamps are milliseconds** — divide by 1000 before `datetime.fromtimestamp()`, or you
get `ValueError: year 58089 is out of range`.

```bash
ssh mcapp.local "cat > /tmp/q.py << 'PYEOF'
import sqlite3, datetime
c = sqlite3.connect('file:/var/lib/mcapp/messages.db?mode=ro', uri=True)
q = lambda s, p=(): c.execute(s, p).fetchone()
f = lambda v: '-' if not v else datetime.datetime.fromtimestamp(v/1000).strftime('%Y-%m-%d %H:%M')
print('schema      :', q('SELECT version FROM schema_version')[0])
print('messages    :', q('SELECT COUNT(*) FROM messages')[0], 'newest', f(q('SELECT MAX(timestamp) FROM messages')[0]))
print('stations    :', q('SELECT COUNT(*) FROM station_positions')[0])
print('signal_log  :', q('SELECT COUNT(*) FROM signal_log')[0])
print('classifier  :', q(\"SELECT value FROM classifier_meta WHERE key='classifier_version'\"))
print('unclassified:', q(\"SELECT COUNT(*) FROM messages WHERE category IS NULL AND type='msg' AND timestamp > (SELECT MAX(timestamp)-3600000 FROM messages)\")[0], '(last 1h, expect 0)')
PYEOF
python3 /tmp/q.py; rm -f /tmp/q.py"
```

Judge:

- **schema version must equal `LATEST_SCHEMA_VERSION`** from Phase 3. A mismatch means a
  migration did not run — the loudest possible finding.
- **DB size** against the 1 GB hard limit (`MAX_DB_SIZE_MB`). Nightly prune runs at 04:00;
  a DB growing past ~900 MB means pruning is not keeping up.
- A `-wal` file of a few MB is normal; one that never checkpoints and grows into the hundreds of MB is not.
- **`{CET}` rows in `messages` should be ZERO** (dropped at ingest by `_should_filter_message`); their absence is correct, not a broken feed.
- **The classifier's DB key is `classifier_version`, NOT `classifier_ver`** (that is the code's name for the concept and the `messages` column). Querying the former key by the latter name returns `None` with no error, which reads like a dead classifier. The value must match the running code's classifier version and have a matching `backfill_done:v{N}` marker. The `classifier_rules` table no longer exists (dropped in schema v38); do not query it.

## Phase 5 — Live data flow (the part that actually catches things)

A green suite and a green deploy still prove nothing about live behaviour. **Read the values,
not just the row counts.**

```bash
ssh mcapp.local "cat > /tmp/f.py << 'PYEOF'
import sqlite3, datetime, time
c = sqlite3.connect('file:/var/lib/mcapp/messages.db?mode=ro', uri=True)
now = int(time.time()*1000); h = 3600_000
for label, sql in [
    ('msgs last 1h',  \"SELECT COUNT(*) FROM messages WHERE type='msg' AND timestamp > ?\"),
    ('pos  last 1h',  \"SELECT COUNT(*) FROM messages WHERE type='pos' AND timestamp > ?\"),
    ('signal last 1h', 'SELECT COUNT(*) FROM signal_log WHERE timestamp > ?')]:
    print('%-15s: %s' % (label, c.execute(sql, (now-h,)).fetchone()[0]))
r = c.execute('SELECT first_observed_ms,last_beacon_ms,last_tick_ms FROM link_uptime_state').fetchone()
if r:
    age = (now - r[1])/1000 if r[1] else None
    print('last CET beacon:', ('%.0f s ago' % age) if age else 'never')
    print('heartbeat age  : %.0f s' % ((now - r[2])/1000))
    print('gap/dark rows  :', c.execute('SELECT COUNT(*) FROM link_uptime_segments').fetchone()[0])
PYEOF
python3 /tmp/f.py; rm -f /tmp/f.py"
```

The same ledger over HTTP: `curl -sk 'https://mcapp.local/api/uptime?range=24h'`. **`range` is
required** (`24h` / `7d`) — the bare endpoint returns a 422 `Field required`, not a default window.

Known-good shapes, so you do not report normal behaviour as a fault:

- **The `{CET}` uplink beacon cadence is set upstream and is not constant** (303 s until 2026-08-22, 606.5 s since). A last-beacon age under ~12 min is healthy; `GAP_TOLERANCE_MS` is 12 min because anything at or below the cadence marks a perfect link as silent. See CLAUDE.md § Gateway Uptime.
- **Heartbeat age must stay under ~60 s** (30 s tick). A stale `last_tick_ms` with a live
  service means the heartbeat task died — the coverage metric is blind from that point on.
- **`gap` and `dark` are different claims** (CLAUDE.md § Gateway Uptime). A `dark` row after a deploy is correct, not an outage.
- **`state: "unknown"` on a fresh ledger is expected** until the first beacon lands (up to one cadence after a restart). The API reports `uptime_pct: null` inside the tolerance and `0.0` once it has passed with nothing ever heard; a fresh ledger reporting **100.0 %** means the old bug regressed.
- **MHeard beacons are throttled to one row per station per 2 min.**
- **RSSI/SNR are only real for `src_type == "lora"`** (`node`/`udp` send a `0/0` sentinel) and are final values: never re-scale (CLAUDE.md § Key Gotchas).

Open findings and watch points live in `doc/ops-mcapp-health-log.md` (the header's open list and the latest sweep section): confirm they have not changed, do not re-litigate them.

## Phase 6 — Log triage

```bash
ssh mcapp.local 'sudo journalctl -u mcapp --since "-24h" --no-pager -p warning' > /tmp/mcapp-warn.log
wc -l /tmp/mcapp-warn.log
```

Aggregate by message rather than reading the tail — one flapping upstream otherwise looks like
dozens of distinct problems:

```bash
python3 -c "
import collections, re, sys
c = collections.Counter()
for line in open('/tmp/mcapp-warn.log'):
    m = re.search(r'\[(WARNING|ERROR|CRITICAL)\]\s*(\S+)?[: ]\s*(.*)', line)
    if m: c[(m.group(1), (m.group(2) or '')[:40], m.group(3)[:80])] += 1
for (lvl, name, msg), n in c.most_common(25): print('%5d  %-8s %s %s' % (n, lvl, name, msg))
"
```

A healthy day is a handful of lines; a sudden jump into the hundreds is the signal, and the exact line matters more than the count.

## Phase 7 — Host headroom

```bash
ssh mcapp.local '
  df -h --output=source,size,used,avail,pcent /
  free -m | awk "NR==2 {print \"mem total=\" \$2 \" used=\" \$3 \" avail=\" \$7}"
  grep -E "^(SwapTotal|SwapFree):" /proc/meminfo
  uptime
  vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp
'
```

**This is a Pi Zero 2W with ~415 MB of usable RAM**: memory is the scarce resource, not disk. Steady swap usage under zram is normal and a WATCH, not a finding; SwapFree trending toward zero, MemAvailable in the low tens of MB, or disk above 85 % is a finding (three slots plus a growing DB fill the card). Compare against the baselines in the health log.

Swap is read from `/proc/meminfo` rather than `free`, because `/proc` is English regardless of
the shell's locale and needs no `$field` references — `free`'s own labels are German here.

## Phase 8 — Secret and TLS hygiene

```bash
ssh mcapp.local '
  stat -c "%a %n" /var/lib/mcapp/vapid.json /etc/mcapp/config.json
  head -c 40 /var/lib/mcapp/vapid.json; echo
'
curl -skI https://mcapp.local/ | head -3
```

- **`vapid.json` must be `0600`** (`load_or_create_vapid` re-tightens a wider file on load, so a wide mode means it has not been reloaded since). It must hold the **raw base64url scalar, not PEM**, and must not regenerate on every restart (an ephemeral key silently kills every stored push subscription). Web Push **degrades silently** without outbound internet. See CLAUDE.md § Web Push.
- TLS is Caddy on :80/:443, which also serves the SPA; lighttpd on :8082 is legacy (backlog B15). For certificate work follow `doc/sop/SOP-003-tls-maintenance.md`.
- **A near-term `notAfter` on this host is NOT an expiry finding.** Caddy's internal CA
  (`issuer=CN=Caddy Local Authority - ECC Intermediate`) issues **12-hour** leaf certs and
  renews them automatically, so a spot check almost always shows a cert expiring within hours.
  Read `notBefore` and the issuer before judging `notAfter`; only a cert that has _actually_
  expired, or a public-CA cert inside its last days, is a finding.

```bash
echo | openssl s_client -connect mcapp.local:443 2>/dev/null \
  | openssl x509 -noout -issuer -dates -ext subjectAltName
```

## Pi gotchas that cost round-trips

- **No `tcpdump`.** For wire capture write a `socket.AF_PACKET` sniffer and run it under
  `sudo nohup setsid` — a bare `cmd &` in an ssh one-liner dies with the session.
- **Never open a second consumer on the BLE service's `/api/ble/notifications` SSE stream.**
  The queue is single-consumer (`popleft()`), so a debugging client _steals frames from
  production_.
- `sudo` is passwordless here, so `ssh -o BatchMode=yes` works for the whole sweep.

## Reporting

Verdict first, then the numbers. State explicitly which signals are **absent** — for most of
these checks absence _is_ the result, so quote the zero rather than staying silent about it.
Separate genuine findings from noise you classified and dismissed, and say why you dismissed it.

For every finding give the evidence (log line, callsign, counter) and say whether it is ours or
upstream. Anything needing a code change goes into the repo and ships via `/dev-release` — never
patched on the box. If the sweep finds nothing, "all green" is a valid result, but only after
the phases above actually ran, not from `/health` alone.

### Then append to the log — this is not optional

**[`doc/ops-mcapp-health-log.md`](../../../doc/ops-mcapp-health-log.md) is the source of truth
for observed baselines**, and the point of the whole exercise is the time series, not any single
run. Append one new dated section following the shape of the existing ones — anchors table,
verdict, measured values, absent signals, watch points `W<n>`, findings `F<n>` continuing the
series across runs — and update the banner at the top to name the newest run. **Append, never
edit a previous section**: a run that rewrites history destroys the comparison it exists to make.

Compare against the **rate table in §1** and re-measure it. Raw counters cannot be compared
across runs with different uptimes — a box six hours past a deploy and the same box six days
later produce very different totals while being equally healthy. Record rates with their window;
record totals only as context.

Baselines live in the log, not in this skill; when a number here and the log disagree, the log wins.

Finish with:

```bash
npx --yes prettier@3 --write doc/ops-mcapp-health-log.md && uvx ruff format --check .
```

Prettier and ruff both touch markdown, so run prettier first, then the ruff check — a docs-only
commit has turned CI red here twice. Commit only if the user asks.
