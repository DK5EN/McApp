# POL-003: Secrets and sensitive data

| Field | Value |
|---|---|
| ID | POL-003 |
| Status | REVIEWED (approved by owner 2026-11-10) |
| Last changed | 2026-11-10 |
| Owner | Martin Werner (approved 2026-11-10) |
| Next review due | 2027-05-10 |
| Applies to | VAPID keys, BLE API key, QRZ password, Node Admin node passwords, RM1 tags, push subscriptions, the TLS DNS-provider tokens |
| Sources | `CLAUDE.md` ("Web Push", "QRZ", "Node Admin", "Stall Tracking"); `../archive/operations-reference-pre-split-2026-11-10.md` (archived); ADR-0011, ADR-0013 |

## 1. Rule

1. **Name secrets, never write their values** into docs, tickets, commit messages or logs.
2. **Secrets at rest** are either `0600` files owned by the service user (VAPID key, install key) or
   encrypted in the database (QRZ and node passwords, with AES-GCM and the board serial). A moved SD card
   cannot read them and the operator re-enters the password.
3. **Logs and stalls never carry a credential.** Redaction replaces values, not keys, for push
   subscriptions and `*api_key*` and `authorization` fields. The credentials route body is withheld
   entirely. RM1 tags are cut from every log line and failed capture.
4. **The API has no authentication.** Anything it stores is reachable by any LAN device. Node Admin is
   therefore LAN-only (Host and Origin guard), and must never be exposed through the public TLS hostname.
5. **A password is never retried against a live login.** A rejected login stops the QRZ service until new
   credentials arrive.
6. **Node passwords are the node's console password too.** `K = SHA-256(password)` is unsalted, so one captured
   frame is an offline guessing oracle. The service generates 14-character random passwords.
7. **Pydantic 422 errors echo the input.** Routes that take passwords validate by hand.

## 2. Why

The box is an internet-reachable appliance run by hams, not a managed server. A credential that lands in
a log, a stall record or a shared doc stays there for good.

## 3. Verify

```bash
# No password-shaped field in the recent stall records (should print 0)
curl -s 'http://mcapp.local/api/stalls?limit=200' | grep -ci password
```

File modes: `stat -c %a /var/lib/mcapp/vapid.json /var/lib/mcapp/secret.key` should both print `600`.

## 4. Exceptions

- A frame that was actually transmitted is public on air. Its tag cannot be recalled, so the rule is to
  keep the password strong, not to pretend the tag is secret.
- Message text is deliberately kept in stall records. It is radio traffic, not a secret.

## 5. Related

- ADR-0011 (Node Admin), ADR-0013 (Web Push keys), `../runbook/RB-006-node-admin-triage.md`
