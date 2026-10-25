# Conversation navigation + group labels — implementation plan

Status: **APPROVED 2026-10-25**. ALL WAVES DONE; live on mcapp.local as v2.1.20-dev.1 (see §7). Repos: MCProxy (backend) and webapp
(frontend), committed independently. Dispatch mechanics: `/orchestrate-waves`.

## 1. Approved problem statement (v2, with operator answers)

1. **Segment toggle** `All | Groups | Direct | Other` above the conversation list, identical on
   desktop and mobile. Groups = numeric, `*`, `#hashtag`. Direct = own DMs. Other = overheard
   pairs (`A~B`). Unread of non-selected segments stays visible. Not remembered: every load starts
   on All.
2. **Mobile drawer cleanup**: the category chips leave the drawer (they stay on the chat view's
   filter button).
3. **Group labels** = Präfix + Hinweis (+ remark). Compact in the sidebar row and the destination
   picker (`262 · DL`, full text on hover); full in the chat header
   (`262 · DL · Deutschland — <remark>`). Resolution: own override → exact ICSSW entry → derived
   from an ICSSW range (`2325` → `OE5`, `26244` → `DL44`) → bare number. Never load the
   Brandmeister list.
4. **Storage on the Pi**: ICSSW snapshot ships pre-built in the package; overrides in the McApp
   DB, shared by every device. ICSSW `Reichweite` is the **remark** field. An override is
   per field, so a newer snapshot still reaches the fields not overridden.
5. **Editor** in Settings > Groups (`GroupManager`): label columns, add an unheard number, source
   shown (ICSSW / range / own), revert to upstream. **Pairs and DM rows** show the QRZ first name
   and QTH per callsign, editable the same way (own override over QRZ).
6. **Snapshot refresh** is a step in the prod-release skill
   (`.claude/skills/prod-release/SKILL.md`), run on the Mac before every production release.

Out of scope: mc-chat parity (backlog item), remembering the segment, routing/send, unread logic,
the `/admin` page.

## 2. Design decisions

- **D1 — Server expands ranges; the client does a plain map lookup.** The snapshot holds ~71 rows,
  of which a handful are ranges (`2321-2329`, `26200-26299`). The backend expands every range into
  exact entries (`source: "range"`) and merges overrides on top, so the wire payload is one flat
  map `{key: {prefix, hint, remark, source}}` of a few hundred entries. No resolver is duplicated
  in TypeScript, so no new hand-copied vector corpus. A range wider than 1000 numbers is not
  expanded (logged) to keep the payload bounded.
- **D2 — Range derivation rule.** The refresh script parses a range row's Präfix (`OE1-OE9`,
  `DL00-DL99`) into `{lo, hi, prefix_base, first_suffix, width}`; number `n` gets
  `prefix_base + zfill(first_suffix + n - lo, width)` and the range's Hinweis as hint. A range
  whose Präfix does not parse keeps the generic Präfix text for every member. Pinned by unit
  vectors, including the two real ranges.
- **D3 — Snapshot format.** `src/mcapp/group_labels_icssw.json`: `{source_url, fetched_at,
entries: [{group | lo,hi, prefix, hint, remark}]}`, sorted, `null`/empty remark normalised to
  `null`. Added by name to the prod tarball whitelist in `scripts/release.sh` (next to
  `classifier_contract.json`) — it is runtime data, not a test corpus.
- **D4 — Overrides are per field, `NULL` = inherit.** Migration 39 adds
  `group_label_overrides(group_key TEXT PK, prefix, hint, remark, updated_at)` and
  `callsign_info_overrides(callsign TEXT PK, first_name, qth, updated_at)`. `group_key` is the
  verbatim sidebar key (`262`, `*`, `#OE-SOTA`); the API validates it with the existing group /
  hashtag predicates. An all-NULL row is deleted (= revert). QRZ refreshes never touch the
  override table, so a manual name survives the next lookup.
- **D5 — Callsign overrides ride the existing channel.** `get_callsign_info_map` and the live
  `proxy:callsign_info` publish return the merged values plus an additive `manual: true` flag
  when an override applies. `CallsignIdentity` (chat bubbles) picks up edited names with no
  change. A callsign with an override but no QRZ row still appears (QRZ disabled boxes).
- **D6 — Transport for labels.** New SSE event `proxy:group_labels` (full merged map) in the
  initial burst and re-broadcast on every edit; `GET /api/group_labels` returns the same map plus
  the upstream values per key for the editor. Writes: `PUT/DELETE /api/group_labels/{key}`,
  `PUT/DELETE /api/callsign_info/{call}/override`.
- **D7 — No extra write protection (operator decision 2026-10-25).** The label and override
  writes behave like the rest of the API; no Host/Origin guard.
- **D8 — Desktop has no chat header today.** A new slim one-line `ConversationHeader` above the
  desktop filter bar shows the full label (group) or `CALL · Name · QTH` (DM) / both sides (pair).
  `MobileChatHeader` uses the same formatter for its title.
- **D9 — Drawer content** becomes header + `ChatFilterBar` WITHOUT the category chips + segment
  toggle + list (operator decision 2026-10-25: keep the filter bar, drop only the chips). The chips
  stay on the desktop filter bar and on the mobile chat view's filter button.
- **D10 — Segment badge.** Each segment button shows the summed unread of its rows (`+N`), so a
  message in a hidden segment is never invisible. Segment membership: Groups = `classifyDst`
  `group | hashtag | time`, Direct = `person`, Other = `pair | unknown`. The "No Filter" row stays on top in every
  segment.

- **D11 — Country-prefix fallback (operator decision 2026-10-25).** A numeric key with more than
  3 digits that has no entry in `labels` falls back to the entry of its first 3 digits when that
  is a 3-digit key in `countries` (`2621` → `DL · Deutschland`, `source: "country"`). The set is
  unbounded, so it cannot be expanded server-side: the payload carries the 3-digit entries
  (after overrides) as `countries`, and the webapp formatter does `labels[key] ??
countries[key.slice(0, 3)]`. That is the only resolution step in TypeScript, pinned by
  webapp unit tests.

## 3. Wire contract (frozen before Wave 1, both waves code against it)

```text
SSE proxy:group_labels      {labels: {"<key>": {prefix, hint, remark, source}},
                             countries: {"<3-digit key>": {prefix, hint, remark, source}}}
                            source ∈ "icssw" | "range" | "own" | "mixed"  (mixed = some fields own)
GET /api/group_labels       {labels: {...as above}, upstream: {"<key>": {prefix, hint, remark,
                            source: "icssw"|"range"}}, snapshot: {fetched_at, source_url}}
PUT /api/group_labels/{key} body {prefix?, hint?, remark?}  null = inherit  → 200 {label}
DELETE /api/group_labels/{key}                                             → 204
PUT /api/callsign_info/{call}/override  body {first_name?, qth?}           → 200 {info}
DELETE /api/callsign_info/{call}/override                                  → 204
proxy:callsign_info entry   {first_name, qth, country, manual?: true}    (additive key only)
Limits: prefix ≤ 12 chars, hint ≤ 40, remark ≤ 80, first_name ≤ 30, qth ≤ 40; 422 never echoes.
```

## 4. Waves and file ownership

Orchestrator-owned hotspots (never given to an agent): `scripts/release.sh`,
`scripts/run_startup_tests.py`, `storage/constants.py`, webapp `src/events/eventTypes.ts`,
`.claude/skills/prod-release/SKILL.md`, `CLAUDE.md`, both backlogs, this doc.

### Wave 1 — foundations (4 writers in parallel, both repos)

| Agent | Repo    | Exclusive files                                                                                                                                                                                  | Delivers                                                                                                  |
| ----- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| B1    | MCProxy | `src/mcapp/group_labels.py`, `src/mcapp/group_labels_icssw.json`, `src/mcapp/group_labels_tests.py`, `scripts/refresh_group_labels.py`                                                           | Snapshot (scraped once), refresh script, pure `build_label_map(snapshot, overrides)` + range rule (D1/D2) |
| B2    | MCProxy | `storage/migrations.py` (v39 block), `storage/label_overrides.py` (new), `storage/callsign_info.py`, `storage/label_overrides_tests.py`                                                          | Migration 39, override CRUD, merged `get_callsign_info_map` with `manual` (D4/D5)                         |
| F1    | webapp  | `ContactsSidebar.vue`, new `components/chat/ConversationSegments.vue`, `views/MessagesView.vue`, their specs                                                                                     | Segment toggle + per-segment unread, drawer cleanup (D9/D10)                                              |
| F2    | webapp  | new `stores/groupLabels.ts`, new `utils/groupLabel.ts` (compact/full formatter), `types/groupLabels.ts`, `composables/useSSEClient.ts`, `stores/callsignInfo.ts`, `types/callsignInfo.ts`, specs | Store + SSE handler for `proxy:group_labels`, `manual` flag, API client fns for the writes                |

Gate: both repos' full gates; advisor (Opus) on the B1+B2 diff and on the F1 diff. Commit per repo.

### Wave 2 — surfaces (3 writers in parallel)

| Agent | Repo    | Exclusive files                                                                                                                                     | Delivers                                                                       |
| ----- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| B3    | MCProxy | new `sse_routes/labels.py`, `sse_handler.py` (burst + router include), `main.py` (wiring), `sse_routes/labels_tests.py`                             | Routes, SSE burst + re-broadcast, merged callsign publish (D6/D7)              |
| F3    | webapp  | `ContactsSidebar.vue` (row label), `DestinationPicker.vue`, `MobileChatHeader.vue`, new `ConversationHeader.vue`, `MessagesView.vue` (mount), specs | Compact label in rows + picker, full label in both headers (D8)                |
| F4    | webapp  | `components/settings/GroupManager.vue`, new `components/settings/GroupLabelRow.vue`, new `CallsignOverrideRow.vue`, specs                           | Editor: label columns, add number, source badge, revert; pair/DM name+QTH edit |

F3 re-touches two Wave 1 files — legal because Wave 1 is committed before Wave 2 starts.
Gate: full gates; advisor on B3 (422 echo, burst order, override merge) and on F3+F4.

### Wave 3 — integration (orchestrator + 1 scout)

- Local end-to-end: `uv run mcapp` (dev) + webapp dev server, Chrome screenshots at desktop and
  390 px widths of: segments, drawer, sidebar labels, picker, both headers, editor add/edit/revert.
  No transmission involved; nothing touches mcapp.local.
- `CLAUDE.md` section "Group Labels", backlog entries (mc-chat parity; prefix-only derivation for
  numbers outside any range if wanted later), prod-release skill step "refresh the ICSSW snapshot
  with `uv run python scripts/refresh_group_labels.py`, review the diff, commit".
- Dev release + deploy only on explicit request (`/dev-release`).

## 5. Agents, models, cost

| Wave | Agents                   | Model / effort           | Est. cost | Wall-clock |
| ---- | ------------------------ | ------------------------ | --------- | ---------- |
| 1    | B1 B2 F1 F2 implementers | Sonnet 5.5 / medium      | ~$14      | ~30 min    |
| 1    | 2 advisors               | Opus 5.5 / medium        | ~$6       | ~10 min    |
| 2    | B3 F3 F4 implementers    | Sonnet 5.5 / medium      | ~$12      | ~30 min    |
| 2    | 2 advisors               | Opus 5.5 / medium        | ~$6       | ~10 min    |
| 3    | 1 scout + orchestrator   | Haiku 5.5 low / Opus 5.5 | ~$5       | ~25 min    |
|      | Rework reserve (1 round) | Sonnet 5.5 / high        | ~$8       | ~20 min    |
|      | **Total**                |                          | **~$50**  | **~2.5 h** |

## 6. Open questions

None. Answered 2026-10-25: D7 no guard; D8 desktop header yes; D9 keep the filter bar in the
drawer, drop only the chips; D11 country fallback yes.

## 7. Wave log

### Wave 1 — done 2026-10-25

- Backend: snapshot 71 rows (69 exact, ranges `2321-2329` OE1-OE9 and `26200-26299` DL00-DL99),
  expanded payload 160 labels + 17 countries. `26244` has its own ICSSW entry (`DL 44` / Freising),
  so exact beats range there. Migration 39, `storage/label_overrides.py`,
  `get_callsign_info_merged()`. Snapshot shipped by name in the prod tarball (pinned in
  `webapp_deploy_tests.py`).
- Webapp: `ConversationSegments.vue`, drawer without chips, `groupLabels` store + D11 fallback,
  `labelsApi.ts`, `manual` flag on callsign info.
- Advisor rework (test-only, applied by the orchestrator, each mutation-checked): parser range
  rows, inclusive length limits, override on an unusable QRZ row; webapp import order, msgSelect +
  Time/unknown segment specs.
- Carry-forward to Wave 2 (B3): `qrz_service._publish` must publish
  `get_callsign_info_merged()` (else a QRZ refresh overwrites a manual name live); an override
  DELETE must publish an all-null entry for an override-only callsign (the webapp store never
  clears a key); `proxy:group_labels` is a BARE event like `proxy:callsign_info`.
- Carry-forward to Wave 2 (F3): in the 320 px drawer "Groups" + badge truncates to "Grou…" —
  tighten `ConversationSegments`. The drawer remounts on open, so its segment resets to All
  (by design: not remembered).

### Wave 2 — done 2026-10-25

- Backend (`ea14695`): routes, bare `proxy:group_labels` in the burst and after writes, merged
  `_publish`, all-null entry on override-only revert. Advisor APPROVED; its write-race note is
  backlog B9.
- Webapp: compact labels (sidebar rows, picker options + prefix adornment), `ConversationHeader`
  (desktop) + `useConversationTitle` (shared with `MobileChatHeader`), segment badge on the tab
  corner, Settings > Groups editor (`GroupLabelEditor`, `CallsignNameEditor`).
- Advisor rework, two rounds: editor seeds from `source` (country fallback / failed or pending
  GET no longer freeze inherited values as own); added and editing rows pinned past `MAX_ROWS`
  and search; Edit only for callsigns the backend accepts; `0262` → `262`; hashtag keys stay
  VERBATIM (exact resolve, Add reuses the heard spelling) — a case-folding resolve made Revert hit
  a key with no override; picker input keeps a content basis so the adornment shrinks first.
- Accepted: the callsign editor prefills both fields when `manual` (no per-field provenance on
  the wire), so saving a one-field override also pins the other field.
- Wave 3: docs (CLAUDE.md "Group Labels", backlog B8 mc-chat parity + B9, prod-release Step 2b).

### Wave 3 — done 2026-10-25

- Dev release `v2.1.20-dev.1` on mcapp.local (slot-0, schema 39, both override tables, 17 `.json`
  in the slot = local count, no warnings since restart). `GET /api/group_labels`: 160 labels,
  `2325` → `OE5 · OE-Bundesländer` (range), snapshot `fetched_at 2026-10-25`.
- Browser check at 883 px (mobile layout): header `262 · DL · Deutschland`; drawer with segments
  and per-segment badges, no chips; sidebar labels from all four sources (`262 · DL` icssw,
  `2325 · OE5` range, `2628 · DL` country); Other shows only pairs; Settings > Groups lists labels
  with source badges and the editor opens pinned with upstream placeholders.
- Not checked live: the desktop `ConversationHeader` (the Chrome window could not be widened past
  883 px), and no write was made against the production DB (route tests cover writes).
