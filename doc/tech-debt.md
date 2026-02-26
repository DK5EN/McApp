# Tech Debt: Komplexe Funktionen & Refactoring-Kandidaten

Stand: 2026-02-27

## Ziel

Code wartbar machen: keine tiefen Verschachtelungen, keine Spaghetti-Logik,
alles erwartbar und einfach verständlich.

---

## Bereits erledigt

### `commands/parsing.py` — parse_command_v2 (Dispatch-Table)
- **Was war:** `_parse_command_v1()` in `routing.py` — 130-Zeilen if/elif-Kette,
  topic-Parsing mit 5 Verschachtelungsebenen
- **Was ist:** Dispatch-Table `_COMMAND_PARSERS` + je eine kleine Funktion pro Command
- **Nächster Schritt:** Shadow-Vergleich bestätigen, dann v1 entfernen

### `main.py: route_command()` — Kein Problem
- Analysiert und für sauber befunden. 16 Commands, jeder Branch ein Einzeiler-Dispatch.
  `startswith`-Checks für Device-Commands passen nicht in ein Dict. Bleibt so.

### `commands/routing.py: _message_handler()` — Extrahiert + Logger
- **Was war:** 152 Zeilen, `has_console`/`print`-Blocks, Exception-Handling inline
- **Was ist:** 79 Zeilen. `_parse_and_execute()` und `_error_response_text()` extrahiert.
  Alle prints durch `logger.debug()`/`logger.warning()` ersetzt.

### `commands/data_commands.py: handle_search()` — SQL-Aggregation
- **Was war:** 108 Zeilen — `search_messages()` holte ALLE Messages (ignorierte
  callsign/search_type komplett), Filtering + Counting + MAX-Tracking + SID-Extraktion
  alles in Python-Loop
- **Was ist:** `get_search_summary()` im Storage-Layer macht 3 gezielte SQL-Queries
  (COUNT/MAX/GROUP BY, DISTINCT destinations, SID-Gruppierung). `handle_search()`
  ist jetzt 52 Zeilen reines Response-Formatting.

### `commands/ctcping.py: _handle_ack_message()` — Grösstenteils erledigt
- **Was war:** 124 Zeilen, 4 Verschachtelungsebenen, vermischte Verantwortlichkeiten
- **Was ist:** 54 Zeilen, flache Early-Returns, Logik in `_record_ack_result()` extrahiert
- **Offen:** Dual-Tracking (`test_summary["completed"]` Counter vs. abgeleiteter Count
  aus `results`-Liste) existiert noch. Ist durch Guard in `_check_test_completion()`
  abgesichert und funktional harmlos, aber ein Design-Wart.

### `commands/routing.py: _should_execute_command()` — Kein Problem
- 42 Zeilen, Early Returns, sauber strukturiert. Bleibt so.

### `sqlite_storage.py: _migrate_v3_to_v4()` — Kein Problem
- Strikt sequentielle Migration, klar kommentiert. Bleibt so.

### `sqlite_storage.py: _backfill_new_tables()` — Kein Problem
- 5 eigenständige SQL-Statements mit Kommentaren. Bleibt so.

---

## Offen

### 1. `main.py: _udp_message_handler()` (L917-1025, 109 Zeilen) — AUFRÄUMEN

**Was die Funktion tut:** Ausgehende UDP-Messages verarbeiten und senden.

**Zwei Probleme:**

1. **Shadow-Logik noch aktiv** — 3x `compare_outbound_decision()` verstreut über den Flow.
   Erledigt sich mit Shadow-Removal (siehe `doc/check-and-remove-outbound-shadow.md`).

2. **`has_console`/`print`-Blocks** — Noch nicht auf Logger umgestellt (im Gegensatz
   zu `routing.py`, wo das bereits passiert ist). Enthält auch 2 ungeschützte `print()`-Aufrufe.

**Priorität:** MITTEL — Logger-Umstellung unabhängig von Shadow-Removal machbar

---

### 2. `ble_protocol.py: decode_binary_message()` (L62-172, 111 Zeilen) — OPTIONAL

**Was die Funktion tut:** Binary BLE-Nachricht dekodieren (3 Formate: ACK, Msg, Pos).

**Analyse:** Drei klar getrennte Branches:
- `@A` = ACK Frame (L77-106) — 30 Zeilen, eigenständig
- `@:` / `@!` = Message/Position (L108-169) — 62 Zeilen
- else = Invalid (L171-172) — 1 Zeile

**Empfehlung:** Branches als `_decode_ack_frame()`, `_decode_data_frame()` extrahieren.
Macht jede Funktion testbar und unter 40 Zeilen.

**Priorität:** NIEDRIG — Code ist stabil (Firmware-Protokoll ändert sich selten),
aber `locals()`-Pattern ist fragil bei Refactoring

---

## Zusammenfassung

| Funktion | Status | Priorität |
|----------|--------|-----------|
| `_udp_message_handler()` (main) | Offen — Logger + Shadow-Removal | MITTEL |
| `decode_binary_message()` (ble_protocol) | Offen — optional in 2-3 Funktionen aufteilen | NIEDRIG |
| `_message_handler()` (routing) | Erledigt | — |
| `_handle_ack_message()` (ctcping) | Erledigt (Dual-Tracking harmlos offen) | — |
| `handle_search()` (data_commands) | Erledigt | — |
| `_should_execute_command()` (routing) | Kein Problem | — |
| `_migrate_v3_to_v4()` (sqlite_storage) | Kein Problem | — |
| `_backfill_new_tables()` (sqlite_storage) | Kein Problem | — |
