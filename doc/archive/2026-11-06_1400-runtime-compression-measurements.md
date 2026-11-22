# Runtime compression on the Caddy front door: measurements

**Decision: `encode { gzip 1 }` on `/events*` and `/api/*` only.** Static files stay on pre-built
sidecars, and small responses stay plain through Caddy's 512-byte minimum.

## Where it applies

- **LAN boxes get it through the normal update.** `--converge` renders the new Caddyfile because the
  system epoch and `CADDY_CONFIG_VERSION` both moved.
- **Public-TLS boxes do not.** `--converge` leaves their Caddyfile alone but still records the new
  epoch, so they need another `ssl-tunnel-setup.sh` run to get the new template.
- **`/events` needs Caddy 2.11.5 or newer to compress.** The install gate accepts 2.9. On an older
  Caddy the first event is under the 512-byte minimum, so the stream stays plain and still works.

## Method

- **Tool:** `scripts/measure_front_door.py`, from a Mac on the same LAN, with the median of 5 runs.
- **Slow link:** curl `--limit-rate 125k` (about 1 Mbit/s) simulates a weak link.
- **SSE timing:** request start until `proxy:summary` arrived, the event right behind the large
  `proxy:initial`.
- **SSE wire bytes:** counted over a 4 s window. On the slow link, that window cuts the transfer
  short, so its byte count is not the full burst.

## Baseline: v2.1.23, before the change

Caddy RSS 23 MB, mcapp RSS 85 MB, load 0.11, no other clients.

| Target                | Proto | Link | Encoding | Wire bytes | Median ms | Worst ms |
| --------------------- | ----- | ---- | -------- | ---------- | --------- | -------- |
| /events connect burst | h2    | LAN  | none     | 630692     | 1585      | 1687     |
| /api/status           | h2    | LAN  | none     | 382        | 28        | 31       |
| /api/monitor/frames   | h2    | LAN  | none     | 239129     | 113       | 228      |
| /api/stalls?limit=200 | h2    | LAN  | none     | 195349     | 141       | 185      |
| /events connect burst | h2    | 125k | none     | 384287     | 5190      | 5193     |
| /api/status           | h2    | 125k | none     | 382        | 28        | 118      |
| /api/monitor/frames   | h2    | 125k | none     | 238496     | 1932      | 1969     |
| /api/stalls?limit=200 | h2    | 125k | none     | 195178     | 1629      | 1729     |
| /webapp/              | h2    | LAN  | br       | 644        | 34        | 117      |
| wasm, 24 MB           | h2    | LAN  | none     | 23955905   | 5224      | 5635     |
| /events connect burst | h3    | LAN  | none     | 630692     | 1607      | 1960     |
| /api/status           | h3    | LAN  | none     | 382        | 33        | 109      |
| /api/monitor/frames   | h3    | LAN  | none     | 238954     | 111       | 200      |
| /api/stalls?limit=200 | h3    | LAN  | none     | 195178     | 157       | 228      |
| /events connect burst | h3    | 125k | none     | 383940     | 5146      | 5171     |
| /api/status           | h3    | 125k | none     | 382        | 27        | 29       |
| /api/monitor/frames   | h3    | 125k | none     | 238474     | 1911      | 2003     |
| /api/stalls?limit=200 | h3    | 125k | none     | 195178     | 1614      | 1729     |
| /webapp/              | h3    | LAN  | br       | 644        | 23        | 39       |
| wasm, 24 MB           | h3    | LAN  | none     | 23955905   | 5759      | 5899     |

Only compare rows with the same window. The burst is 631 KB at 4 s and 781 KB at 8 s, because
`proxy:callsign_info` and other late events arrive after the first 4 s.

## Choosing the codec

Real Caddy 2.11.9 on a loopback port of the Pi, with the production `GOMEMLIMIT` and `GOGC`. Event
delay comes from a stub stream with a timestamp in every event. RSS and burst bytes come from 3 real
`/events` clients held for 6 s. CPU is Caddy's total for those 3 connects.

| Variant | Median delay | First event | RSS, 3 clients | Burst wire | /api/monitor/frames | Caddy CPU |
| ------- | ------------ | ----------- | -------------- | ---------- | ------------------- | --------- |
| plain   | 0.8 ms       | 1.2 ms      | 39 MB          | 781044     | 239950              | 130 ms    |
| gzip 1  | 1.0 ms       | 6.6 ms      | 44 MB          | 128962     | 33477               | 250 ms    |
| gzip 5  | 1.0 ms       | 7.1 ms      | 44 MB          | 106382     | 25891               | 370 ms    |
| zstd    | 0.9 ms       | 12.0 ms     | 51 MB          | 107191     | 23394               | 380 ms    |

- **gzip 1 wins on this box.** It gets most of the size gain at the lowest CPU cost.
- **Events are still delivered at once**, including a first event under the 512-byte minimum.
- **`/api/status` (382 bytes) stays plain** in every variant.

## After the change: v2.1.24-dev.1

**Result: compression saves 84% of the bytes and makes the connect burst 3 times faster at 1 Mbit/s.
On the LAN, the burst barely changes, because about 1.5 s of it is server time.**

Plain and gzip were requested alternately from the same box with the same payload, so only the
encoding differs. A TCP relay on the Mac throttled the server-to-client bytes. Median of 5 runs,
7 on the LAN, over h2.

| Target                | Link      | Plain ms | Gzip ms | Faster by | Plain bytes | Gzip bytes |
| --------------------- | --------- | -------- | ------- | --------- | ----------- | ---------- |
| /events connect burst | 1 Mbit/s  | 6518     | 2161    | 67%       | 630692      | 103038     |
| /events connect burst | 10 Mbit/s | 2081     | 1574    | 24%       | 630692      | 103038     |
| /events connect burst | LAN       | 1605     | 1573    | 2%        | 630692      | 103038     |
| /api/stalls?limit=200 | 1 Mbit/s  | 1794     | 381     | 79%       | 199894      | 31376      |
| /api/stalls?limit=200 | 10 Mbit/s | 304      | 131     | 57%       | 199894      | 31376      |
| /api/stalls?limit=200 | LAN       | 166      | 126     | 24%       | 199894      | 31376      |

The burst's byte counts come from the 4 s window in `measure_front_door.py`. h3 sends the same number
of bytes (103 KB).

**Other checks**

- **Live events:** 89 of 89 real events over 200 s arrived through Caddy with gzip 2.9 ms after the
  direct connection (5.0 ms at worst). Plain through Caddy took 1.8 ms (4.1 ms at worst).
- **RAM:** 8 SSE clients peaked at 28 MB of Caddy RSS for both plain and gzip, with 160 MB of memory
  available. Idle Caddy RSS moves between 23 and 53 MB, so a single reading says nothing.
- **Small responses:** `/api/status` (380 bytes) stays plain.
- **Chrome:** `/events` and `/api/stalls` both arrive as gzip, 111 message elements render.

**Measurement limits**

- **`curl --limit-rate` is not a valid slow link for these responses.** 31 KB of gzip took 110 ms under
  125 KB/s, while 31 KB of plain data took 280 ms. Do not use the 125k rows of the script for a
  comparison; the relay above replaces them.
- **`/api/monitor/frames` is not comparable before and after.** The restart emptied its in-memory ring.
  The fair figure is the codec trial: 239950 to 33477 bytes.
- **Not tested:** iPhone Safari over h3 with gzip, and a real slow link.
