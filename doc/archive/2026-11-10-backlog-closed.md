# Backlog items closed 2026-11-10

Moved from `doc/backlog.md` by the verification run (see `doc/review-log.md`).

## B18 — the full `mcapp.sh` run ignores a failed apt phase (CLOSED 2026-11-10)

**Outcome:** fixed in the code. `bootstrap/mcapp.sh` line 843 now reads `if ! install_packages; then log_error ...; exit 1; fi`
before `write_system_epoch`. The epoch is no longer written after a failed apt phase. Checked by reading the file; the
full-run path has no separate test for it (candidate for a suite in `bootstrap_converge_steps_tests.py`).

`if ! install_packages` tests only the last function, so a failed `install_apt_deps`, including its link check, is ignored and the epoch is written. `--converge` already stops there (`bootstrap/lib/converge.sh`). Found by the coexistence campaign review, 2026-11-06.

