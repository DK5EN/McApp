# Proxy tests W1-W4 (mcapp behind Caddy -> Traefik)

Prereqs: image `mcapp-spike:arm64` built (sibling `../image`), `.env` and `authelia/secrets/` present
(gitignored; recreate from `.env.example` / `authelia/users_database.yml.example`). User `ct`, password in `.env`.

Deploy (orchestrator):

1. `cp ~/docker/caddy/Caddyfile ~/docker/caddy/Caddyfile.bak-ct`
2. Paste `Caddyfile.snippet` into the `*.air-maxx.net` block, before the fallback `handle {`.
3. `docker exec caddy-service caddy validate --config /etc/caddy/Caddyfile`, then
   `docker exec caddy-service caddy reload --config /etc/caddy/Caddyfile` (Caddyfile is bind-mounted at that path).
4. Add `hosts.txt` lines to /etc/hosts (operator).
5. ONE profile at a time: `docker compose --profile open up -d` (or `basic` / `authelia`).

Tests (plain https, wildcard cert is real; `. .env` first for `$CT_PASSWORD`):

- W1 open: `curl -sS -o /dev/null -w '%{http_code}\n' https://mcapp-test.air-maxx.net/api/status` -> 200
- W2 SSE: `curl -sN -m 20 https://mcapp-test.air-maxx.net/events | head` -> events arrive live (not buffered)
- W3 basic (`--profile basic`): no creds -> 401; `curl -u ct:$CT_PASSWORD ...` -> 200
- W4 authelia (`--profile authelia`): unauthenticated `curl -sI ...` -> 302 to `auth-test.air-maxx.net`;
  login in a browser at that portal (user `ct`), then the app loads. Authelia log: `docker logs mcapp-ct-authelia-1`
- UDP: `echo '{}' | nc -u -w1 127.0.0.1 1799`

Teardown: `docker compose --profile open --profile basic --profile authelia down` (add `-v` to drop volumes),
remove the snippet (restore `Caddyfile.bak-ct`), reload caddy, remove the hosts lines.
