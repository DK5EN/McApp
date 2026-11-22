# RB-012 Classifier soak check on live traffic

| Field                 | Value                                                                                                                                                                                                                  |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ID                    | RB-012                                                                                                                                                                                                                 |
| Status                | DRAFT (written 2026-11-11 for the taxonomy-v7 release, first run the same day)                                                                                                                                         |
| Last changed          | 2026-11-11                                                                                                                                                                                                             |
| Owner                 | Martin Werner (review pending)                                                                                                                                                                                         |
| Next review due       | 2027-05-11                                                                                                                                                                                                             |
| Applies to (hosts)    | `mcapp`                                                                                                                                                                                                                |
| Applies to (variants) | `debian-13-trixie`                                                                                                                                                                                                     |
| Sources               | `scripts/soak_classifier_check.py`; `src/mcapp/storage/classifier_api.py` (`_history_exempt_sql`); `src/mcapp/classifier/template.py` (`_HUMAN_CATEGORIES`); `src/mcapp/storage/suppression.py` (`_LEGACY_HIDDEN_TAG`) |

## 1. Purpose and scope

A read-only check, run after a release that changes the classifier (contract, taxonomy, L2
promotion), that the box classifies live traffic correctly. It changes, restarts, deploys and
writes nothing. Run it a few hours after the deploy, so the box has received enough traffic.

What it checks:

- every new row is classified, with one `classifier_ver` and no obsolete tag;
- auto-promotion never marks a template whose history holds a human message (category
  `directed`/`alert`, or an `alert` tag);
- the auto_beacon and human samples are judged by reading the message text;
- the stored filter preferences, and how legacy tag names map.

## 2. Preconditions

- SSH to `mcapp.local` as `martin`, with `sudo -n` for `journalctl`.
- The box has only the system python3. There is no sqlite3 CLI. The script uses the standard
  library only, opens the DB with `mode=ro`, and is deleted after the run.
- Never attach a consumer to the BLE service's `/api/ble/notifications` stream.
- Before you trust a verdict after a classifier sync, check that `_HUMAN_CATEGORIES` and
  `_history_exempt_sql` still match the constants at the top of the script.

## 3. Procedure

**Step 0 - which build is running:**

```bash
ssh mcapp.local 'readlink -f ~/mcapp-slots/current; systemctl is-active mcapp mcapp-ble;
  systemctl show mcapp -p ActiveEnterTimestamp --value;
  curl -s http://127.0.0.1/webapp/version.html'
```

If this is not the version you meant to soak, judge the box against the deployed version.

**Step 1 - service health since the restart** (put the `ActiveEnterTimestamp` in `--since`):

```bash
ssh mcapp.local 'T="$(systemctl show mcapp -p ActiveEnterTimestamp --value)";
  sudo -n journalctl -u mcapp.service --since "$T" -p warning --no-pager | tail -30;
  echo ---;
  sudo -n journalctl -u mcapp.service --since "$T" --no-pager |
    grep -iE "traceback|attributeerror|template_has_human|backfill|classif" | tail -20'
```

Expected: no warnings. The info lines name the contract bump (`version bumped to N`), the
auto-beacon backfill (`promoted K templates`) and the reclassify job (`rows=...`). The reclassify
marker is written when the job is **scheduled**, not when it finishes. Step 2b shows whether the
job has finished.

**Steps 2-4 - the database check:**

```bash
scp scripts/soak_classifier_check.py mcapp.local:~/ &&
ssh mcapp.local 'S=$(date -d "$(systemctl show mcapp -p ActiveEnterTimestamp --value)" +%s%3N);
  python3 ~/soak_classifier_check.py --since-ms $S; rc=$?;
  rm -f ~/soak_classifier_check.py; exit $rc'
```

The script prints one section per step:

| Section              | Pass condition                                                                                                                                               |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 2a tags on new rows  | `tags NULL` is `{}`. `'[]'` is the normal common case.                                                                                                       |
| 2b `classifier_ver`  | One value on new rows. `all msg rows by ver` holds one value once the reclassify job has finished.                                                           |
| 2c obsolete tags     | `0` on new rows and on all rows (`bot`/`node`/`software`/`time` fold into `auto_beacon`).                                                                    |
| 2d samples           | Read the text. The auto_beacon rows are machine traffic (position beacons, telemetry, WX, BBS, client adverts). The rows without auto_beacon are human chat. |
| 3a contradictions    | `0`. Each row listed is an auto_beacon template with human history.                                                                                          |
| 3b counts            | Each template with `user_action` set keeps the operator's decision. Suppression reads `user_action`, not the flag.                                           |
| 3c recently promoted | Judge each template by its text, `srcs` and cadence.                                                                                                         |
| 4 filter prefs       | The default (no `hiddenTags` key) hides nothing. A stored legacy name maps to `auto_beacon` and must be reported.                                            |

If a 3c template is doubtful, check its cadence. A message that arrives at the same wall-clock
time every day is a scheduled bot, even when the text reads like a greeting. Also check
`/api/filter_prefs` on the box:

```bash
ssh mcapp.local 'curl -s http://127.0.0.1/api/filter_prefs'
```

## 4. Verdict and where a fix goes

PASS only when the service is healthy, no row carries an obsolete tag, 3a is zero, and the
samples in 2d and 3c read correctly. For each misclassified message, record its text and
`template_hash` and decide which layer is wrong:

- **Wrong tag from L1** (the tag is wrong although the message is not auto_beacon): the mcmap
  contract. Fix it in mcmap (`npm run classifier:export`), never here or in mc-chat.
- **Wrong `auto_beacon` from L2 promotion**: `classifier/template.py`. Fix it in mc-chat and sync
  it through the `classifier` subtree.

## 5. Run log

| Date       | Build                                      | Verdict | Notes      |
| ---------- | ------------------------------------------ | ------- | ---------- |
| 2026-11-11 | v2.1.27-dev.2 (slot-2, up since 18:38 CET) | PASS    | See below. |

The 2026-11-11 run checked 493 new rows (219 msg, 274 pos), all at `classifier_ver` 8. The
reclassify had finished: all 10257 msg rows are at ver 8. The run found no NULL tags, no
obsolete tags and no 3a contradictions. Of the 29244 templates, 324 are auto_beacon and none has
`user_action` set. All 10 templates promoted since the restart are machine traffic. That
includes IS0HHA-10's trilingual "good night", which arrives every day at 22:00 ±45 s. The stored
prefs are `hiddenTags: ["time"]`, which now hides all of `auto_beacon` (see the note in the
release report).
