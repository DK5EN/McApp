# Tech Debt: Komplexe Funktionen & Refactoring-Kandidaten

Stand: 2026-07-06 (refreshed nach fable-verdict.md Wellen 1-7 + Track M; siehe dort
für den vollständigen Audit-Trail)

## Verifiziert 2026-11-10

Geprüft gegen den Code mit grep und AST-Zeilenzahlen (Stand 2026-11-10).

- **Geprüft, bestätigt:** `route_command` (main.py), `_message_handler`, `_parse_and_execute`, `_error_response_text`, `_should_execute_command` (commands/routing.py), `handle_search` (52 Zeilen), `get_search_summary` (storage/query.py, drei SQL-Abfragen), `_record_ack_result` und der Idempotenz-Docstring von `_trigger_completion_if_done` (commands/ctcping.py), Entfernung des Reconciliation-Blocks in ctcping (`test_summary.completed/.timeouts` als einzige Quelle), `_udp_message_handler`/`_ble_message_handler` als dünne Wrapper um `_handle_outbound` (main.py), `compare_outbound_decision` und `doc/check-and-remove-outbound-shadow.md` (beide nicht mehr vorhanden), `decode_binary_message` mit Rückgabetyp `dict | None`, extrahierte `_decode_ack_frame`/`_decode_data_frame`, kein `locals()` mehr und benannte Offset-Konstanten in ble_protocol.py, `fable-verdict.md` (Repo-Root) mit Section 9 "Open decisions" und "Discovered during waves".
- **Korrigiert:** `parse_command_v2` heißt im Code `parse_command` (parsing.py:381), `_COMMAND_PARSERS` steht bei parsing.py:345; `_parse_command_v1` existiert nicht mehr (kein Shadow-Vergleich offen). `_message_handler` hat 153 Zeilen statt 79 (routing.py:23). `route_command` hat 18 if/elif-Zweige statt 16. `_handle_ack_message` hat 58 Zeilen statt 54 (ctcping.py:251). `_migrate_v3_to_v4` und `_backfill_new_tables` liegen in `storage/migrations.py` (Zeilen 1294 bzw. 1113), nicht in `sqlite_storage.py`. `_handle_outbound` steht bei main.py:2037 (nicht 864). `_decode_ack_frame`/`_decode_data_frame` stehen bei ble_protocol.py:211 bzw. 276 (nicht 78 bzw. 112).
- **Nicht prüfbar bzw. nur Historie:** Die "Was war"-Angaben und Vorher-Zeilenzahlen (vor dem Refactoring nicht mehr rekonstruierbar); Wave- und Commit-Zuordnungen (nur in fable-verdict.md belegt); die "Kein Problem"-Urteile zu `_migrate_v3_to_v4` (heute 161 Zeilen) und `_backfill_new_tables` (heute 130 Zeilen) sind nicht neu bewertet; Laufzeitverhalten (Idempotenz, Migrationsergebnisse) nicht per Test geprüft; "Offen: keine" gilt nur für dieses Dokument, nicht für fable-verdict.md Section 9.
- **Offene Frage:** `_message_handler` ist damit wieder rund doppelt so lang wie im Refactoring-Eintrag notiert; ob das Ziel noch gilt, entscheidet der Eigentümer.

## Ziel

Code wartbar machen: keine tiefen Verschachtelungen, keine Spaghetti-Logik,
alles erwartbar und einfach verständlich.

---

## Bereits erledigt

### `commands/parsing.py` — parse_command (Dispatch-Table)
- **Was war:** `_parse_command_v1()` in `routing.py` — 130-Zeilen if/elif-Kette,
  topic-Parsing mit 5 Verschachtelungsebenen
- **Was ist:** Dispatch-Table `_COMMAND_PARSERS` (parsing.py:345) + je eine kleine Funktion pro Command; Einstieg `parse_command()` (parsing.py:381)
- **Status:** erledigt. `_parse_command_v1` existiert in `src/` nicht mehr (Stand 2026-11-10), es gibt keinen Shadow-Vergleich mehr.

### `main.py: route_command()` — Kein Problem
- Analysiert und für sauber befunden. 18 if/elif-Zweige (Stand 2026-11-10, vorher als 16 notiert), jeder Branch ein Einzeiler-Dispatch.
  `startswith`-Checks für Device-Commands passen nicht in ein Dict. Bleibt so.

### `commands/routing.py: _message_handler()` — Extrahiert + Logger
- **Was war:** 152 Zeilen, `has_console`/`print`-Blocks, Exception-Handling inline
- **Was ist:** ursprünglich 79 Zeilen (Stand 2026-11-10: 153 Zeilen, routing.py:23; Zunahme nicht untersucht). `_parse_and_execute()` (routing.py:183) und `_error_response_text()` (routing.py:232) extrahiert.
  Alle prints durch `logger.debug()`/`logger.warning()` ersetzt.

### `commands/data_commands.py: handle_search()` — SQL-Aggregation
- **Was war:** 108 Zeilen — `search_messages()` holte ALLE Messages (ignorierte
  callsign/search_type komplett), Filtering + Counting + MAX-Tracking + SID-Extraktion
  alles in Python-Loop
- **Was ist:** `get_search_summary()` im Storage-Layer macht 3 gezielte SQL-Queries
  (COUNT/MAX/GROUP BY, DISTINCT destinations, SID-Gruppierung). `handle_search()`
  ist jetzt 52 Zeilen reines Response-Formatting.

### `commands/ctcping.py: _handle_ack_message()` — Erledigt
- **Was war:** 124 Zeilen, 4 Verschachtelungsebenen, vermischte Verantwortlichkeiten
- **Was ist:** 58 Zeilen (ctcping.py:251-308), flache Early-Returns, Logik in `_record_ack_result()` extrahiert
- **Dual-Tracking behoben** (fable-verdict.md Legibility-Audit, 2026-07-06): die
  Reconciliation-Block, der `results`-abgeleitete Counts gegen `test_summary.completed`/
  `.timeouts` verglich und bei Abweichung warnte, wurde entfernt — CMD-03s
  Single-Increment-Design macht die beiden Counts durch Konstruktion gleich, der Block
  konnte also nur "diese können auseinanderlaufen" für einen Fall behaupten, der nicht
  eintreten kann. `test_summary.completed`/`.timeouts` sind jetzt die einzige Quelle.
  Zusätzlich: das Completion-State-Machine-Invariant (kein `await` zwischen
  Completion-Check und Guard-Set) ist jetzt direkt im Code dokumentiert
  (`_trigger_completion_if_done`-Docstring), nicht mehr nur in fable-verdict.md.

### `commands/routing.py: _should_execute_command()` — Kein Problem
- 42 Zeilen, Early Returns, sauber strukturiert. Bleibt so.

### `storage/migrations.py: _migrate_v3_to_v4()` (vormals `sqlite_storage.py`) — Kein Problem
- Strikt sequentielle Migration, klar kommentiert. Bleibt so.

### `storage/migrations.py: _backfill_new_tables()` (vormals `sqlite_storage.py`) — Kein Problem
- 5 eigenständige SQL-Statements mit Kommentaren. Bleibt so.

---

## Erledigt (fable-verdict.md Wellen 1-7)

### `main.py: _udp_message_handler()` / `_ble_message_handler()` — Erledigt
- **Shadow-Logik** (`compare_outbound_decision()`) vollständig entfernt (Wave 1/Track U-Ära;
  `doc/check-and-remove-outbound-shadow.md` existiert nicht mehr).
- **Logger statt print/has_console** — seit Wave 4 (CO-09) durchgehend `logger.debug/info`.
- **CO-02 (Wave 5):** beide Handler sind jetzt ~90% identische dünne Wrapper um einen
  gemeinsamen `_handle_outbound(routed_message, protocol, send)` (main.py:2037) —
  ~70 Zeilen Duplikation entfernt.

### `ble_protocol.py: decode_binary_message()` — Erledigt
- **BLE-05/06 (Wave 6.5):** in `_decode_ack_frame()`/`_decode_data_frame()` extrahiert
  (ble_protocol.py:211, 276); `locals()`-Dict-Comprehension durch explizites Dict-Bauen ersetzt;
  Rückgabetyp `dict | None` statt `dict | str` (kein Bare-Error-String mehr); benannte
  Frame-Offset-Konstanten statt Magic Numbers.

---

## Offen

Keine offenen Punkte aus diesem Dokument mehr. Für laufende/zukünftige Code-Qualitätsarbeit
siehe `fable-verdict.md` (Waves 1-7 abgeschlossen; Section 9 "Open decisions" und die
"Discovered during waves"-Liste dort für alles, was seither neu aufgefallen ist).

---

## Zusammenfassung

| Funktion | Status |
|----------|--------|
| `_udp_message_handler()`/`_ble_message_handler()` (main) | Erledigt |
| `decode_binary_message()` (ble_protocol) | Erledigt |
| `_message_handler()` (routing) | Erledigt |
| `_handle_ack_message()` (ctcping) | Erledigt |
| `handle_search()` (data_commands) | Erledigt |
| `_should_execute_command()` (routing) | Kein Problem |
| `_migrate_v3_to_v4()` (storage/migrations) | Kein Problem |
| `_backfill_new_tables()` (storage/migrations) | Kein Problem |
