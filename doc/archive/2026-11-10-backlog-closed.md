# Backlog items closed 2026-11-10

Moved from `doc/backlog.md` by the verification run (see `doc/review-log.md`).

## B18 — the full `mcapp.sh` run ignores a failed apt phase (CLOSED 2026-11-10)

**Outcome:** fixed in the code. `bootstrap/mcapp.sh` line 843 now reads `if ! install_packages; then log_error ...; exit 1; fi`
before `write_system_epoch`. The epoch is no longer written after a failed apt phase. Checked by reading the file; the
full-run path has no separate test for it (candidate for a suite in `bootstrap_converge_steps_tests.py`).

`if ! install_packages` tests only the last function, so a failed `install_apt_deps`, including its link check, is ignored and the epoch is written. `--converge` already stops there (`bootstrap/lib/converge.sh`). Found by the coexistence campaign review, 2026-11-06.

## B24 — re-sync the classifier contract and suppression vectors from mc-chat (taxonomy v7) (CLOSED 2026-11-11)

**Outcome:** done. Subtree pulls `946bd58` (classifier) and `b4864ef` (contract) make both trees byte-identical to mc-chat `8449aa2`. The non-subtree half of mc-chat `2113012` is ported: `storage/suppression.py` hides nothing by default and maps saved `bot`/`node`/`software`/`time` to `auto_beacon`, and `CATEGORY_TO_TAG` emits `auto_beacon`. The sha256 pins match mc-chat's. Regression cases are in `storage/suppression_tests.py` and mutation-checked. The webapp drift test is green. Stored rows are retagged by the startup reclassify, since the new contract id bumps `classifier_ver`.

mc-chat (2026-11-11) moved the classifier to taxonomy v7: `bot`, `node`, `software` and `time` are now
`auto_beacon`, `url` is added, and the contract's rules carry a `legacy_tag`. The canonical files
are `mc-chat/contract/classifier_contract.json` and `mc-chat/contract/suppression_vectors.json`. The
copies here, `src/mcapp/contract/suppression_vectors.json` and the contract the engine reads, still
have the old vocabulary and the old default hiding (`time` and `bot` hidden by default, now none).

Work: copy both canonical files in (the contract also needs the engine to read `legacy_tag`, if the
MCProxy engine is a port of mc-chat's), bump the pinned sha256 in the tests, and check the hidden-tag
defaults in MCProxy's `SuppressionPolicy`. The webapp's drift check (`has not drifted from MCProxy`)
stays red until this is done, which is the intended signal.
Source: owner decisions of 2026-11-11 (mcmap classifier taxonomy v7, mc-chat migration).
