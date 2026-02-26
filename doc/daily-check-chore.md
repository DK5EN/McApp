# Daily Shadow Mode Check

Paste this prompt into Claude Code to run the daily check:

---

## Prompt

Check the shadow mode status on rpizero.local and mcapp.local. SSH into both and:

1. Check journal for any SHADOW MISMATCH warnings since last restart:
   `sudo journalctl -u mcapp --no-pager | grep -i SHADOW`

2. Check how long the service has been running (uptime):
   `sudo systemctl status mcapp | head -5`

3. Get a sample of recent traffic volume to confirm shadow is actually exercised (not just silent because idle):
   `sudo journalctl -u mcapp --since "1 hour ago" --no-pager | wc -l`

Then evaluate:

- **If SHADOW MISMATCH warnings exist:** Report the mismatches (normalize vs routing, counts, examples). These are bugs to fix before removing old code.
- **If zero mismatches after 24h+ uptime with real traffic:** The shadow mode has validated parity. Propose a concrete commit that:
  1. Replaces `normalize_command_data()` body in `routing.py` with a call to `normalize_unified(..., "command")`
  2. Replaces `normalize_message_data()` body in `main.py` with a call to `normalize_unified(..., "message")`
  3. Renames `_should_execute_command_v2` to `_should_execute_command` and deletes the old v1
  4. Removes all `compare_normalize` / `compare_routing` calls and the `_stats` counter from `shadow.py`
  5. Keeps `normalize_unified()` and `get_shadow_stats()` in `shadow.py` (or moves to a utils module)
  Do NOT commit yet — just show me the plan and wait for approval.
- **If zero mismatches but <24h or very low traffic:** Say "not enough data yet" and come back tomorrow.

---

## Check Log

| Date | rpizero.local | mcapp.local | Result |
|---|---|---|---|
| 2026-02-25 | 0 mismatches, 16h uptime, 38 lines/h | 0 mismatches, 15h uptime, 60 lines/h | Not enough data yet (<24h) |
| 2026-02-26 | 0 mismatches, 22h uptime, 52 lines/h | 0 mismatches, 21h uptime, 103 lines/h | Not enough data yet (<24h) |
