# RB-001 Health checks on mcapp.local

| Field | Value |
|---|---|
| ID | RB-001 |
| Status | DRAFT (extracted 2026-11-10 from `archive/operations-reference-pre-split-2026-11-10.md`, lines 216-278; corrected where marked) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (review pending) |
| Next review due | 2027-05-10 |
| Applies to (hosts) | `mcapp` |
| Applies to (variants) | `debian-13-trixie` |
| Sources | `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Remote Health Check Commands") |

## 1. Purpose and scope

Read-only checks over SSH: system overview, service state, database counts, log errors. Run after an update, after a reboot, or when a symptom needs a first look. Nothing here changes state.

Commands for checking system health on the Pi via SSH. The Pi locale is German, so output labels may be in German.

**IMPORTANT quoting rule:** When running `python3 -c` over SSH, use single quotes for the Python code and `\"` for strings inside Python. Never use f-strings with dict key access — use `%` formatting instead. Nested double quotes in f-strings break the SSH quoting.

### System overview

```bash
ssh mcapp.local "uptime && echo '---' && free -h && echo '---' && df -h / /tmp /var/log && echo '---' && vcgencmd measure_temp && echo '---' && cat /proc/loadavg"
```

### Service statuses

```bash
ssh mcapp.local "sudo systemctl status mcapp.service --no-pager -l 2>&1 | head -20"
ssh mcapp.local "sudo systemctl status caddy.service --no-pager 2>&1 | head -10"
# lighttpd is the backend on 127.0.0.1:8082 (B15: still running; not the front door)
ssh mcapp.local "sudo systemctl status lighttpd.service --no-pager 2>&1 | head -10"
ssh mcapp.local "sudo systemctl status bluetooth.service --no-pager 2>&1 | head -10"
```

### Process memory usage

```bash
ssh mcapp.local "ps aux | grep -E 'mcapp|uv.*run' | grep -v grep"
```

### Database stats (use % formatting, NOT f-strings)

```bash
ssh mcapp.local "python3 -c '
import sqlite3, os
conn = sqlite3.connect(\"/var/lib/mcapp/messages.db\")
print(\"Schema version:\", conn.execute(\"SELECT version FROM schema_version\").fetchone()[0])
for r in conn.execute(\"SELECT type, COUNT(*) as cnt FROM messages GROUP BY type ORDER BY cnt DESC\"):
    print(\"  messages/%s: %d\" % (r[0], r[1]))
print(\"  station_positions: %d\" % conn.execute(\"SELECT COUNT(*) FROM station_positions\").fetchone()[0])
print(\"  signal_log: %d\" % conn.execute(\"SELECT COUNT(*) FROM signal_log\").fetchone()[0])
print(\"  signal_buckets: %d\" % conn.execute(\"SELECT COUNT(*) FROM signal_buckets\").fetchone()[0])
sz = os.path.getsize(\"/var/lib/mcapp/messages.db\")
print(\"DB size: %.2f MB\" % (sz/1024/1024))
conn.close()
'"
```

### Log checks

```bash
# Overnight logs (pruning runs at 04:00)
ssh mcapp.local "sudo journalctl -u mcapp.service --since '2026-02-13 22:00' --until '2026-02-14 06:00' --no-pager | tail -100"

# Today's logs
ssh mcapp.local "sudo journalctl -u mcapp.service --since today --no-pager | tail -150"

# Errors only (journald priority filter — may miss app-level [ERROR] lines)
ssh mcapp.local "sudo journalctl -u mcapp.service --since today -p err --no-pager"

# Grep for app-level errors/warnings
ssh mcapp.local "sudo journalctl -u mcapp.service --since today --no-pager | grep -i 'error\|exception\|traceback\|warning\|WARN'"

# Log volume count
ssh mcapp.local "sudo journalctl -u mcapp.service --since today --no-pager | wc -l"
```

