# QRZ.com callsign lookup (issue #14) — plan and secret-storage concept

Status: implemented on `development` (2026-10-04), not released. Issue:
<https://github.com/DK5EN/McApp/issues/14>.

## 1. Goal

Show first name and QTH next to a callsign. The backend looks names up against the QRZ.com XML API
with the operator's own account, caches them in `messages.db`, and serves them to the webapp. This
document covers the backend service, the setup surface, and how the QRZ password is stored. Display
in chat and station lists is a follow-up.

## 2. Measured facts (2026-10-04, account DK5EN, MFA on, no subscription)

- XML login with username and password succeeds with MFA enabled on the account. MFA is not
  enforced on the XML interface.
- A free account gets `call fname name addr2 state country`, plus the session message "A
  subscription is required to access the complete record". `SubExp` reads `non-subscriber`.
- Login does not count as a lookup (`Count 0` after login, `1` after the first lookup).
- **A subscriber's `Count` is not a 24 h tally** (2026-10-04, account DM3KS, subscribed until 2027):
  the login reported `Count 77678` while QRZ's own account page showed 1 XML lookup that day and
  an "unlimited" daily limit. `SubExp` carries the expiry date instead of `non-subscriber`.
- Raw values need normalising: `fname` can carry middle names (`Martin Stefan`), `addr2` can carry a
  postal code (`A-4060 Leonding`).

## 3. QRZ protocol rules (spec 1.36, "The Session Node" / "Error Conditions")

| Response                                | Meaning                                        | Our reaction                               |
| --------------------------------------- | ---------------------------------------------- | ------------------------------------------ |
| `<Callsign>` present                    | hit                                            | cache `found`, reset backoff               |
| `<Error>` **with** `<Key>`              | data error (`Not found: X`)                    | cache `not_found`, session stays           |
| `<Error>` **without** `<Key>`           | session invalid (`Session Timeout`, IP change) | drop key, re-login at the next slot        |
| `Connection refused`                    | "login not possible for at least 24 h"         | suspend 24 h                               |
| login: error without key, anything else | credentials wrong                              | stop (`auth_failed`) until new credentials |
| HTTP 429/503, `limit`/`exceeded` text   | rate limited                                   | exponential backoff                        |
| network error, other 5xx                | transient                                      | exponential backoff                        |
| `Count` ≥ daily cap, free account       | QRZ already counts us at cap                   | suspend 24 h                               |

`Count` is QRZ's own 24 h tally for the account, including lookups other software made with it. Using
it as a second gate keeps us under the free tier's ~100/day even when a logging program shares the
account.

The gate applies only while `SubExp` is `non-subscriber` or absent (fail closed). On a subscriber it
suspended every login for good (DM3KS, see §2), so it is skipped there, and so is our own ledger
cap (§4) since v2.1.4. A cap or Count suspension already stored on a subscriber is lifted at the
next step; a refusal is not. Every login rewrites `subscription`, so a reply without `SubExp`
drops the account back to the free-tier rules.
The reason is kept in `qrz_state.suspend_reason` (`cap`, `server_count`, `refused`, migration 34),
because replacing the credentials clears `last_error` but not the suspension. The status carries
`account_tier` (`subscriber` / `free`, derived from `SubExp`) for the settings card.

A wrong password stops the service instead of retrying: repeated failed logins are what gets an
account locked, and nothing changes until the operator enters new credentials.

## 4. Lookup budget

All values are constants in `qrz_service.py`.

- **Hard cap 50 lookups per rolling 24 h on a free account.** A paid subscription has no daily
  XML limit at QRZ, so the cap is off there (`daily_cap: null` in the status) and only the 30 s
  spacing bounds the rate (at most 2880 requests a day). The lookup is recorded in `qrz_lookups` BEFORE the
  request is sent, so a crash or timeout mid-request still counts. The cap counts attempts, not
  successes.
- **Suspend 24 h when the cap is reached.** The 50th lookup writes `suspended_until = now + 24 h`
  into `qrz_state`, which survives a restart.
- **At most one request per 30 s**, logins included. `last_request_ms` is persisted, so a restart
  loop cannot burst.
- **Exponential backoff** on rate limiting and transient failures: 60 s × 2^n, capped at 6 h, reset
  by the next successful response. A `Retry-After` header wins when it is longer.
- **Refresh:** a `found` entry is looked up again after 90 days, a `not_found` entry after 7 days.
- **Order:** never-looked-up callsigns first, chat partners before stations that were only heard,
  most recent first. Candidates are base callsigns (SSID stripped) seen in the last 30 days that look
  like an amateur callsign; placeholders and aliases like `WLNK-1` are skipped.

## 5. Password storage concept

### Threat model

The password has to be recoverable — the XML login needs it in plain text — so it cannot be hashed.
It is encrypted, and the question is where the key lives.

| Attacker has                                                       | Protected? |
| ------------------------------------------------------------------ | ---------- |
| a copy of `messages.db` (backup, support bundle, shared DB)        | yes        |
| a copy of `/var/lib/mcapp` or the whole SD card, but not the board | yes        |
| code running as the service user on the running Pi                 | **no**     |
| root on the running Pi                                             | **no**     |

The last two rows are out of reach for any software-only scheme on a Raspberry Pi: it has no TPM or
secure element, and the service must be able to decrypt the password unattended. Whoever can run code
as `martin` can do what the service does. The scheme therefore aims at what actually leaves a box:
database copies, backups and SD card images.

### Construction

1. **Install key.** 32 random bytes, generated on first use, stored as
   `/var/lib/mcapp/secret.key` with mode `0600`. Never in the database, never in a slot, never in a
   release tarball. Path resolution matches `vapid.json`: `MCAPP_SECRET_KEY_PATH` overrides, dev
   (`MCAPP_ENV=dev`) uses the user state dir.
2. **Board binding.** The Raspberry Pi's board serial
   (`/sys/firmware/devicetree/base/serial-number`, else `Serial` in `/proc/cpuinfo`) is mixed in. It
   lives in the SoC, not on the SD card, so a stolen card or image cannot decrypt on its own. Without
   a board serial (dev machines) `/etc/machine-id` is used; with neither, the install key stands
   alone and a warning is logged.
3. **Key derivation.** `KEK = HKDF-SHA256(ikm = install key, salt = board serial, info =
"mcapp-secret-box-v1")`.
4. **Encryption.** AES-256-GCM, 12-byte random nonce per write, associated data
   `qrz.password:<USERNAME>`, so a ciphertext cannot be replayed under another username or purpose.
   Stored as `v1:<base64(nonce ‖ ciphertext ‖ tag)>` in `qrz_state.password_enc`.
5. **Failure mode.** If decryption fails — SD card moved to another Pi, `secret.key` lost — the
   status reads `credentials_unreadable` and the operator enters the password again. Nothing is
   retried against QRZ with a broken credential.

### Keeping it out of every other channel

- The API is write-only for the password: no endpoint ever returns it, and status shows only whether
  one is stored.
- QRZ requests are POSTed with a form body, never as a query string, so neither password nor session
  key appears in an httpx log line.
- The session key is held in memory only. A restart logs in again, which costs no lookup.
- Stall tracking: `password` is now a redacted key on both ends (`stalls.py`, webapp
  `stallReporter.ts`), and the middleware never captures the body of `/api/qrz/credentials` at all —
  a truncated or non-JSON body is stored raw and would bypass key-based redaction.

### Not chosen

- **`systemd-creds` / `LoadCredentialEncrypted=`.** Encrypts with `/var/lib/systemd/credential.secret`,
  which is on the same SD card. Without a TPM it protects against nothing the board binding does not
  already cover, and it adds a bootstrap/systemd dependency to a feature the app owns.
- **Key derived from the board serial alone.** The serial is readable by every local user and is not
  secret; it binds, it does not protect.
- **Operator passphrase at startup.** Breaks unattended restarts, which an always-on proxy needs.

## 6. API

No authentication exists on this API (same as every other endpoint); anyone on the LAN can replace or
delete the credentials, but nobody can read them.

| Method   | Path                   | Body / response                                                                     |
| -------- | ---------------------- | ----------------------------------------------------------------------------------- |
| `GET`    | `/api/qrz/status`      | status object, see below                                                            |
| `PUT`    | `/api/qrz/credentials` | `{username, password}` → status; verified by a login at the next slot               |
| `DELETE` | `/api/qrz/credentials` | wipes username, password, session → status                                          |
| `PUT`    | `/api/qrz/enabled`     | `{enabled}` → status                                                                |
| `GET`    | `/api/callsign_info`   | `{"DK5EN": {"first_name": "Martin", "qth": "Freising", "country": "Germany"}, ...}` |

Status object:

```json
{
  "configured": true,
  "enabled": true,
  "username": "DK5EN",
  "state": "active",
  "lookups_24h": 12,
  "daily_cap": 50,
  "suspended_until": null,
  "backoff_until": null,
  "next_request_at": 1791100000000,
  "server_count": 14,
  "subscription": "non-subscriber",
  "last_login_at": 1791099000000,
  "last_error": null,
  "last_error_at": null,
  "cached_found": 230,
  "cached_not_found": 4
}
```

`state` is one of `unconfigured`, `disabled`, `verifying`, `active`, `idle` (nothing due),
`suspended`, `backoff`, `auth_failed`, `credentials_unreadable`. Timestamps are milliseconds.

## 7. Schema (migration 33)

- `callsign_info(callsign PK, status, first_name, qth, country, fname, name, addr2, state,
fetched_at, source)` — the cache, keyed by base callsign.
- `qrz_lookups(id, ts_ms, callsign, outcome)` — one row per lookup attempt, the cap's ledger, pruned
  after 2 days.
- `qrz_state(id = 1, username, password_enc, enabled, suspended_until_ms, backoff_until_ms,
backoff_level, last_request_ms, last_login_ms, auth_failed, last_error, last_error_ms, server_count,
subscription)` — single row.

## 8. Follow-ups

- Webapp display: first name after the callsign in chat and station lists, QTH as tooltip.
- HamQTH as an alternative source (free account, same cache).
