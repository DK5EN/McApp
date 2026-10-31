# Ansible instead of mcapp.sh, nginx instead of uvicorn - evaluation

**Status:** Ideation, backlog. 2026-10-31. Two questions up for debate; no code changed.

## BLUF

- **Ansible: do not switch.** It would not remove the hard parts of the bootstrap, it adds a
  60-100 MB tool to a 415 MB Pi Zero (estimate), and the self-update path runs the installer on
  the box itself, so Ansible's main strength (pushing from a controller) is never used. Spend the
  effort on shellcheck in CI and on test coverage for the untested libs instead.
- **nginx instead of uvicorn: not possible as a swap, and not worth it as an addition.** uvicorn
  runs the Python app; nginx cannot. nginx could only sit in front of uvicorn, which the container
  already gets from the operator's reverse proxy.
- **The real simplification is on bare metal: drop lighttpd.** Caddy already fronts everything and
  already has `file_server`. Serving the SPA from Caddy removes one process from the Zero 2W. That
  one is worth a backlog item (B14).

## Question 1: Ansible instead of the shell bootstrap

### What exists today

| Fact                  | Value                                                                                                                                    |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Size                  | about 6,800 lines of bash in 9 files, 184 functions (`mcapp.sh` 1003, `lib/system.sh` 1176, `lib/deploy.sh` 1016, `lib/packages.sh` 986) |
| Entry points          | `curl ... mcapp.sh \| sudo bash` (README:20), the update runner (`scripts/update-runner.py:455` `--skip`, `:565`/`:863` `--converge`)    |
| Self-healing          | `system_converge.py` watchdog re-runs `--converge` when the epoch marker is behind `SYSTEM_EPOCH`                                        |
| Tests                 | 5 bootstrap suites, all driving real functions under bash from Python; none for `deploy.sh`, `config.sh`, `health.sh`, `sd-card.sh`      |
| Lint                  | none: no `shellcheck`, no `bash -n` in `.github/workflows/tests.yml`                                                                     |
| Ansible files in repo | none                                                                                                                                     |

### What Ansible would give us

- **Idempotent modules** for the common chores: `apt`, `systemd`, `template`, `lineinfile`,
  `blockinfile`, `mount`. Today each of these is hand-written idempotence (marker blocks in
  `config.txt`, `_mcapp_fstab_ensure_line`, epoch markers).
- **`--check --diff`** for free, where today `mcapp.sh --check` is a hand-maintained dry-run report.
- **Readable, declarative tasks**, familiar to some admins.
- **Inventory and parallel runs** across a fleet of boxes.

### Why it does not pay off here

- **No controller in the real install path.** The operator flashes a Pi and runs one `curl | bash`.
  Ansible either needs a second machine with Ansible and SSH set up (a new hurdle for every ham),
  or `ansible-pull` / `ansible-playbook -c local` on the Pi itself.
- **On the Pi it costs memory we fought hard for.** `ansible-core` plus Jinja2, PyYAML and
  cryptography is tens of MB on disk and a 60-100 MB Python process per run (estimate,
  unverified on the Zero). The bootstrap already needs a temporary swap file to survive
  `apt-get upgrade` on 415 MB (`packages.sh:42-59`); a playbook run would sit on top of that.
- **The self-update loop runs locally anyway.** The update runner and the converge watchdog call
  the installer on the box, unattended, without a TTY. With Ansible that becomes a local playbook
  run, which carries the whole footprint and none of the remote-management benefit.
- **The hard parts are domain logic, not chores.** The wpasupplicant hold and pin, apt under
  `systemd-run` so a dropped SSH session cannot kill dpkg, the link check after apt, ramlog board
  detection, the `[all]` section rule for `gpu_mem`, the single-line `cmdline.txt`, reboot markers.
  In Ansible these become `shell:`/`command:` tasks or custom modules: the same logic, in YAML,
  harder to test than the bash we drive from Python today.
- **A rewrite risks exactly the regressions we just fixed.** The 2026-09-18 WiFi outage took the
  box off the network from inside the bootstrap. Re-implementing 6,800 lines of hardened
  network-safety logic means re-learning those lessons.
- **The container removes the second audience.** NAS owners and admins who want declarative
  deployment get the image and a compose file (B12). The bootstrap stays a Pi installer.

### Cheaper improvements that get most of the benefit

1. **`shellcheck` and `bash -n` in CI** for `bootstrap/**/*.sh`. Today nothing lints 6,800 lines of
   bash; this is the single largest quality gap.
2. **Test suites for `deploy.sh`, `config.sh`, `health.sh`**, in the style of the existing
   `bootstrap_*_tests.py` (source the lib with stubbed `log_*`, drive functions via subprocess).
3. **Keep splitting `mcapp.sh`** into libs so each phase is testable on its own.
4. **Optional, later:** a 30-line example playbook in the docs for fleet admins that only runs
   `mcapp.sh --tag vX.Y.Z` on a list of hosts. Ansible as a remote trigger, not a rewrite.

### When to reconsider

- One operator manages many McApp boxes and wants inventory-driven rollouts.
- The supported OS matrix grows (Armbian, Orange Pi, Debian x86 bare metal) until per-distro
  branching dominates the scripts.
- The bootstrap gains a large number of plain package/file/service chores with no network risk.

## Question 2: nginx instead of uvicorn

### Two different jobs

- **uvicorn is the application server.** It runs McApp's Python code (FastAPI, the SSE stream, the
  UDP and BLE tasks in the same event loop). nginx cannot execute Python, so "replace uvicorn with
  nginx" is not an option. The only possible change is to put nginx **in front of** uvicorn.
- **The web server role** (TLS, static files, routing) is what nginx would take. On bare metal
  that role is Caddy plus lighttpd; in the container it is the operator's reverse proxy plus
  uvicorn's static mount (B12, change 2).

### Container: nginx inside the image

| Aspect      | uvicorn serves the SPA (B12 plan)                              | nginx + uvicorn in one image                                     |
| ----------- | -------------------------------------------------------------- | ---------------------------------------------------------------- |
| Processes   | one                                                            | two, plus a supervisor (s6, supervisord) to start and reap both  |
| Image size  | baseline                                                       | a few MB more for nginx; the supervisor adds its own             |
| Config      | one `StaticFiles(html=True)` mount                             | nginx.conf with SPA fallback, SSE buffering off, upstream config |
| Performance | ample: a handful of clients, the service worker caches the SPA | faster static serving and compression, unused at this traffic    |
| TLS / login | operator's proxy                                               | still the operator's proxy; nginx inside would be a second hop   |

**Verdict: no.** The operator's reverse proxy already does TLS, compression and the login. A
second proxy inside the container adds a process, a supervisor and a config file to keep in sync,
for traffic that is one operator with a few devices.

### Bare metal: nginx instead of Caddy + lighttpd

- **Replacing Caddy with nginx loses automatic TLS.** Caddy issues the local CA certificate
  (`tls internal`, Caddyfile.mcapp:118) and handles ACME in the public modes
  (`doc/tls-architecture.md`). nginx needs certbot or hand-made certificates for the same result.
- **lighttpd only serves static files.** Its config loads `mod_indexfile`, `mod_access`,
  `mod_alias`, `mod_staticfile` and binds `127.0.0.1:8082`; Caddy proxies everything that is not
  the API to it (Caddyfile.mcapp:78-81).
- **Caddy can do that job itself.** It already serves `/root.crt` with `file_server`
  (Caddyfile.mcapp:71-76). A `try_files {path} /webapp/index.html` plus `file_server` for the
  webapp removes lighttpd entirely: one process, one config file and one package fewer on the Zero.

**Verdict: no nginx; drop lighttpd instead** (proposed B14). Points to check before doing it:

- the SPA rewrite and cache headers lighttpd provides today, reproduced in Caddy;
- the update runner's webapp copy into `/var/www/html/webapp` keeps working with a new root;
- a `SYSTEM_EPOCH` bump, because packages and units change on existing boxes;
- the memory win measured on mcapp.local (PSS of lighttpd), as in the B4 campaign.

## Open questions

- Is the expected lighttpd saving on the Zero 2W large enough to justify an epoch bump? Measure
  first.
- Do any operators run more than a couple of McApp boxes, which would make a fleet playbook
  useful?
