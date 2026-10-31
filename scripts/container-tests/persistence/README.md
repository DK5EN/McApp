# Container persistence tests (P1, P3, P4)

`./persist.sh [image:tag]` (default `mcapp-spike:arm64`) runs, against the image from
`../image/`, with read-only rootfs, tmpfs `/tmp` and config from `image/config.example.json`:

- **P1** fresh volume; a synthetic inbound UDP datagram is sent from a sibling container
  (private source IP, so `runtime.json` is written; the learned target is that throwaway
  container, never the real node). The container is removed, a new one started on the same
  volume; message count, `vapid.json` and `runtime.json` sha256 must survive.
- **P3** (covers P2's migration path) copies the bare-metal backup `messages.db` (+wal/shm,
  read-only source, override with `MCAPP_BACKUP_DIR`) into a fresh volume owned by 10001;
  asserts healthy, "Migration" log lines, schema == `LATEST_SCHEMA_VERSION`, messages > 0.
  `secret.key` is deliberately not copied: QRZ / Node Admin / feed credentials need re-entry.
- **P4** `--log-opt max-size=1m --log-opt max-file=2` shows up in `docker inspect`.

Message counts come from the `/events` connect burst (`proxy:conversations`); there is no
REST message list. Uses only `mcapp-ct-p<pid>-*` containers/volumes, host port 127.0.0.1:2992,
and removes them on exit. Never calls `/api/send`.
