# POL-005: Schema migrations and system epoch

| Field | Value |
|---|---|
| ID | POL-005 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to | `src/mcapp/storage/migrations.py`, `src/mcapp/storage/constants.py` (`LATEST_SCHEMA_VERSION`), `bootstrap/mcapp.sh` (`SYSTEM_EPOCH`), `bootstrap/lib/converge.sh`, `src/mcapp/system_converge.py` |
| Sources | `CLAUDE.md` ("Schema Migrations", "System Epoch"); ADR-0017; `reference/database-reference.md` |

## 1. Rule

**Schema:**

1. Add a schema change as a new `current_version < N` block in `storage/migrations.py`.
2. **In the same commit, bump `LATEST_SCHEMA_VERSION`** in `storage/constants.py`. The migration-chain suite
   and the connection-lifecycle suite both assert against it.
3. **Never edit an existing block.** Migrations are forward-only and additive.
4. **Older code on a newer database must keep working.** The schema marker is never written downward.
5. Each step number is a literal, not derived from the constant. Forgetting either half fails the chain suite loudly.

**Epoch:**

6. `SYSTEM_EPOCH` in `bootstrap/mcapp.sh` and `REQUIRED_SYSTEM_EPOCH` in `system_converge.py` change together.
7. Changing a converge step means raising **its row** in the table to the new epoch, in the same commit.
8. A new function in `setup_system` or `install_packages` needs a row. The parity test fails otherwise.
9. Every converge step must be safe on an installed box, because a full bootstrap run executes all of them.

## 2. Why

Production runs one SQLite file that cannot be restored from a backup without losing data (POL-006). A
migration that is not additive, or an epoch row that is not raised, reaches boxes that will never run
the change.

## 3. Verify

```bash
grep -n "LATEST_SCHEMA_VERSION =" src/mcapp/storage/constants.py
grep -oE "current_version < [0-9]+" src/mcapp/storage/migrations.py | tail -1
grep -n "SYSTEM_EPOCH=" bootstrap/mcapp.sh; grep -n "REQUIRED_SYSTEM_EPOCH =" src/mcapp/system_converge.py
ssh mcapp.local 'cat /var/lib/mcapp/system-epoch'
```

The last number in the migrations grep must equal `LATEST_SCHEMA_VERSION`. The two epoch numbers must be equal.

## 4. Exceptions

- None for the schema. For the epoch, a release that changes nothing system-level keeps the epoch as it is.

## 5. Related

- ADR-0017 (epoch design), `../sop/SOP-002-converge-acceptance-test.md`
