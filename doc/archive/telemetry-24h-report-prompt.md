# Telemetry 24h Analysis Report Prompt

> **Purpose:** Execute this prompt ~24h after deploying schema v14 telemetry changes to verify all code changes work correctly.
>
> **Prerequisites:** SSH access to `mcapp.local` (DK5EN-99) and `rpizero.local` (DK5EN-12).
>
> **Deployment:** 2026-02-21 08:10 CET on both Pis.

---

## Prompt

Run the following analysis and produce a structured report. Execute all SSH commands, collect the data, then write the report.

### 1. Basic Health Check

For each Pi (`mcapp.local`, `rpizero.local`), run:

```bash
ssh <host> "uptime && echo '---' && sudo systemctl is-active mcapp.service && echo '---' && vcgencmd measure_temp"
```

Confirm both services are running and note uptime (should be >24h since the 08:10 restart).

### 2. Schema Version

```bash
ssh <host> "python3 -c '
import sqlite3
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
print(\"Schema:\", conn.execute(\"SELECT version FROM schema_version\").fetchone()[0])
# Verify batt column exists
cur = conn.execute(\"PRAGMA table_info(telemetry)\")
cols = [r[1] for r in cur.fetchall()]
print(\"Telemetry columns:\", cols)
print(\"Has batt:\", \"batt\" in cols)
conn.close()
'"
```

**Expected:** Schema version 14, `batt` column present in telemetry table.

### 3. Telemetry Volume Since Update

Query telemetry rows inserted after the 08:10 deployment (timestamp > 1771654200000 = 2026-02-21 07:10 UTC):

```bash
ssh <host> "python3 -c '
import sqlite3
from datetime import datetime
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
cutoff = 1771654200000  # 2026-02-21 07:10 UTC (08:10 CET)

total = conn.execute(\"SELECT COUNT(*) FROM telemetry WHERE timestamp > ?\", (cutoff,)).fetchone()[0]
print(\"Total telemetry rows since update: %d\" % total)

# Per callsign breakdown
rows = conn.execute(\"SELECT callsign, COUNT(*) as cnt, MIN(timestamp) as first_ts, MAX(timestamp) as last_ts FROM telemetry WHERE timestamp > ? GROUP BY callsign ORDER BY cnt DESC\", (cutoff,)).fetchall()
for r in rows:
    first = datetime.fromtimestamp(r[1+1] / 1000).strftime(\"%H:%M\") if r[2] else \"?\"
    last = datetime.fromtimestamp(r[3] / 1000).strftime(\"%H:%M\") if r[3] else \"?\"
    print(\"  %s: %d rows (first=%s last=%s)\" % (r[0], r[1], first, last))
conn.close()
'"
```

### 4. Verify: batt Column Stored

Check that battery values are being stored (not all NULL):

```bash
ssh <host> "python3 -c '
import sqlite3
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
cutoff = 1771654200000

# Rows with non-NULL batt
with_batt = conn.execute(\"SELECT COUNT(*) FROM telemetry WHERE timestamp > ? AND batt IS NOT NULL\", (cutoff,)).fetchone()[0]
total = conn.execute(\"SELECT COUNT(*) FROM telemetry WHERE timestamp > ?\", (cutoff,)).fetchone()[0]
print(\"Rows with batt: %d / %d\" % (with_batt, total))

# Sample batt values per callsign
rows = conn.execute(\"SELECT callsign, batt, COUNT(*) as cnt FROM telemetry WHERE timestamp > ? AND batt IS NOT NULL GROUP BY callsign, batt ORDER BY callsign\", (cutoff,)).fetchall()
for r in rows:
    print(\"  %s: batt=%s (%d rows)\" % (r[0], r[1], r[2]))
conn.close()
'"
```

**Pass criteria:** At least some rows have non-NULL `batt` values (0-100 range). Stations without battery sensors will have NULL — that is expected.

### 5. Verify: QFE < 850 Rejected

Check that no new telemetry rows have QFE values below 850:

```bash
ssh <host> "python3 -c '
import sqlite3
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
cutoff = 1771654200000

# Bad QFE in new data (should be 0)
bad_new = conn.execute(\"SELECT COUNT(*) FROM telemetry WHERE timestamp > ? AND qfe IS NOT NULL AND qfe < 850\", (cutoff,)).fetchone()[0]
print(\"Bad QFE (<850) since update: %d (expected: 0)\" % bad_new)

# Bad QFE in old data (pre-update, for comparison)
bad_old = conn.execute(\"SELECT COUNT(*) FROM telemetry WHERE timestamp <= ? AND qfe IS NOT NULL AND qfe < 850\", (cutoff,)).fetchone()[0]
print(\"Bad QFE (<850) before update: %d (these are pre-fix)\" % bad_old)

# Show QFE distribution in new data
rows = conn.execute(\"SELECT callsign, MIN(qfe) as min_qfe, MAX(qfe) as max_qfe, AVG(qfe) as avg_qfe, COUNT(qfe) as cnt FROM telemetry WHERE timestamp > ? AND qfe IS NOT NULL GROUP BY callsign ORDER BY callsign\", (cutoff,)).fetchall()
for r in rows:
    print(\"  %s: qfe range=%.1f-%.1f avg=%.1f (%d rows)\" % (r[0], r[1], r[2], r[3], r[4]))
conn.close()
'"
```

**Pass criteria:** Zero rows with QFE < 850 after the update cutoff. Pre-update bad values are expected.

### 6. Verify: QNH→QFE Calculation

Check for evidence that QFE was calculated from QNH when QFE was missing:

```bash
ssh <host> "python3 -c '
import sqlite3
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
cutoff = 1771654200000

# Rows where QFE is set but qnh is NULL (qnh is always nulled before insert)
# We cannot distinguish calculated vs direct QFE from the DB alone.
# Instead, check journal logs for the calculation log line.
print(\"Check journal logs for QNH->QFE calculation evidence.\")

# Show all rows with QFE to see if values look reasonable (900-1050 hPa typical)
rows = conn.execute(\"SELECT callsign, qfe, alt, timestamp FROM telemetry WHERE timestamp > ? AND qfe IS NOT NULL ORDER BY timestamp DESC LIMIT 20\", (cutoff,)).fetchall()
from datetime import datetime
for r in rows:
    dt = datetime.fromtimestamp(r[3] / 1000).strftime(\"%m-%d %H:%M\")
    print(\"  %s %s: qfe=%.1f alt=%s\" % (dt, r[0], r[1], r[2]))
conn.close()
'"
```

Also check journal logs for the QNH→QFE calculation log message:

```bash
ssh <host> "sudo journalctl -u mcapp.service --since '2026-02-21 08:10' --no-pager | grep -i 'qfe\|qnh\|calculated\|barometric' | head -20"
```

**Pass criteria:** If any station sends QNH without QFE, the log should show the calculation. QFE values in the 900-1050 hPa range indicate plausible results.

### 7. Verify: Dedup Behavior

Check that duplicate telemetry within 60s is being handled:

```bash
ssh <host> "python3 -c '
import sqlite3
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
cutoff = 1771654200000

# Find callsigns with multiple rows within 60s windows
# This checks if any two consecutive rows for the same station are <60s apart
rows = conn.execute(\"\"\"
    SELECT callsign, COUNT(*) as cnt,
           MIN(diff) as min_gap_s, AVG(diff) as avg_gap_s
    FROM (
        SELECT callsign,
               (timestamp - LAG(timestamp) OVER (PARTITION BY callsign ORDER BY timestamp)) / 1000.0 as diff
        FROM telemetry
        WHERE timestamp > ?
    )
    WHERE diff IS NOT NULL
    GROUP BY callsign
    ORDER BY callsign
\"\"\", (cutoff,)).fetchall()
for r in rows:
    print(\"  %s: %d intervals, min_gap=%.0fs, avg_gap=%.0fs\" % (r[0], r[1], r[2], r[3]))
    if r[2] < 60:
        print(\"    WARNING: gaps < 60s found — dedup may not be working\")
conn.close()
'"
```

Also check dedup log messages:

```bash
ssh <host> "sudo journalctl -u mcapp.service --since '2026-02-21 08:10' --no-pager | grep -i 'dedup\|duplicate\|skip.*telemetry\|better.*record' | head -20"
```

**Pass criteria:** Minimum gap between consecutive telemetry rows for same station should be >= 60s. Dedup log lines confirm the mechanism is active.

### 8. BLE vs UDP Path Comparison

Check which transport each station's telemetry arrives on:

```bash
ssh <host> "python3 -c '
import sqlite3
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
cutoff = 1771654200000

# Check if messages table has src_type for telemetry correlation
# Telemetry rows do not store src_type directly, so we check message logs
rows = conn.execute(\"\"\"
    SELECT src, src_type, COUNT(*) as cnt
    FROM messages
    WHERE timestamp > ? AND type = \"pos\"
    GROUP BY src, src_type
    ORDER BY src
\"\"\", (cutoff,)).fetchall()
print(\"Position messages by transport (proxy for telemetry path):\")
for r in rows:
    print(\"  %s via %s: %d messages\" % (r[0], r[1] or \"unknown\", r[2]))
conn.close()
'"
```

### 9. Station Sensor Summary

Full sensor value summary for all stations since the update:

```bash
ssh <host> "python3 -c '
import sqlite3
from datetime import datetime
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
cutoff = 1771654200000

rows = conn.execute(\"\"\"
    SELECT callsign,
           COUNT(*) as cnt,
           AVG(temp1) as avg_temp, MIN(temp1) as min_temp, MAX(temp1) as max_temp,
           AVG(hum) as avg_hum,
           AVG(qfe) as avg_qfe, COUNT(qfe) as qfe_cnt,
           AVG(alt) as avg_alt,
           MIN(batt) as min_batt, MAX(batt) as max_batt, COUNT(batt) as batt_cnt,
           MAX(timestamp) as last_ts
    FROM telemetry
    WHERE timestamp > ?
    GROUP BY callsign
    ORDER BY cnt DESC
\"\"\", (cutoff,)).fetchall()

for r in rows:
    last = datetime.fromtimestamp(r[12] / 1000).strftime(\"%m-%d %H:%M\")
    print(\"%s (%d rows, last=%s):\" % (r[0], r[1], last))
    if r[3] is not None:
        print(\"  temp: %.1f-%.1f C (avg %.1f)\" % (r[3], r[4], r[2]))
    if r[5] is not None:
        print(\"  hum: %.1f%%\" % r[5])
    if r[6] is not None:
        print(\"  qfe: %.1f hPa (%d/%d rows)\" % (r[6], r[7], r[1]))
    if r[8] is not None:
        print(\"  alt: %.0f m\" % r[8])
    if r[11] > 0:
        print(\"  batt: %s-%s%% (%d/%d rows)\" % (r[9], r[10], r[11], r[1]))
    print()
conn.close()
'"
```

### 10. Error/Warning Log Scan

Check for any telemetry-related errors since the update:

```bash
ssh <host> "sudo journalctl -u mcapp.service --since '2026-02-21 08:10' --no-pager | grep -iE 'telemetry|tele.*error|store_tele|batt|qfe|qnh' | grep -iE 'error|exception|traceback|warning|fail' | head -30"
```

---

## Report Template

After collecting all data from both Pis, produce a report in this format:

```
# Telemetry 24h Report — 2026-02-22

## Summary
- **Period:** 2026-02-21 08:10 CET → now
- **mcapp.local (DK5EN-99):** [uptime], schema v[X], [N] telemetry rows
- **rpizero.local (DK5EN-12):** [uptime], schema v[X], [N] telemetry rows

## Verification Results

| Check | mcapp.local | rpizero.local | Status |
|-------|-------------|---------------|--------|
| Schema v14 + batt column | | | |
| batt values stored | N/M rows | N/M rows | |
| QFE < 850 rejected | 0 bad / N old | 0 bad / N old | |
| QNH→QFE calculated | evidence? | evidence? | |
| Dedup working (min gap) | Xs | Xs | |

## Station Details

### mcapp.local
| Station | Rows | Temp | Hum | QFE | Alt | Batt | Transport |
|---------|------|------|-----|-----|-----|------|-----------|
| ... | | | | | | | |

### rpizero.local
| Station | Rows | Temp | Hum | QFE | Alt | Batt | Transport |
|---------|------|------|-----|-----|-----|------|-----------|
| ... | | | | | | | |

## Issues Found
- [any errors, warnings, or unexpected values]

## Conclusion
[PASS/FAIL] — All 4 code changes verified: [details]
```
