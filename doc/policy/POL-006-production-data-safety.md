# POL-006: Production data and slot activation

| Field | Value |
|---|---|
| ID | POL-006 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to | `mcapp.local` (production), `/var/lib/mcapp/messages.db`, the update runner's activate and rollback paths |
| Sources | `CLAUDE.md` ("Deployment"); `doc/archive/operations-reference-pre-split-2026-11-10.md` ("Activation"); `scripts/update-runner.py` |

## 1. Rule

1. **Slot activation never restores the database.** Activating a slot switches code, the webapp bundle and
   the services. `/var/lib/mcapp/messages.db` stays as it is.
2. **No "restore the DB on rollback" feature.** A snapshot in `meta/slot-N.db` can be days or weeks old,
   because deploys through `mcapp.sh` do not refresh it. Restoring it deletes live data.
3. **The served webapp is a copy**, `/var/www/html/webapp`, not a symlink. A slot switch re-installs it.
4. **Production is `main`. Dev is `development`.** `release.sh` is run from `development` (`CLAUDE.md`, Development
   Commands). The production release merges to `main` through the same script.
5. **Blocklist URL points at `main`.** A commit that only reaches `development` blocks nobody.
6. **Read the production DB through Python, never the `sqlite3` CLI**, which is not installed on the box
   (`reference/database-reference.md`, "Querying the Production Database").

## 2. Why

The rollback that restored the database was found on 2026-09-10 to hold snapshots 4 days to 3 weeks old.
It was never pressed, but it would have replaced live traffic with stale data. Forward-only migrations
make the database restore unnecessary.

## 3. Verify

```bash
ssh mcapp.local 'ls -la /var/lib/mcapp/messages.db; readlink -f ~/mcapp-slots/current'
```

After any activation, `messages.db`'s mtime and row count must not jump backwards. Compare the count
before and after (see `../runbook/RB-001-health-checks.md`).

## 4. Exceptions

- A disaster recovery from an off-host backup is a separate, documented procedure, not an activation.
  It has no runbook yet (`../review-log.md`, gap list).

## 5. Related

- `../runbook/RB-004-update-runner-and-slots.md`
- `../sop/SOP-001-release.md`
