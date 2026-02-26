# Tech Debt: Komplexe Funktionen & Refactoring-Kandidaten

Stand: 2026-02-26

## Ziel

Code wartbar machen: keine tiefen Verschachtelungen, keine Spaghetti-Logik,
alles erwartbar und einfach verständlich.

---

## Bereits erledigt

### `commands/parsing.py` — parse_command_v2 (Dispatch-Table)
- **Status:** v2 fertig, läuft im Shadow-Mode parallel zu v1
- **Was war:** `_parse_command_v1()` in `routing.py` — 130-Zeilen if/elif-Kette,
  topic-Parsing mit 5 Verschachtelungsebenen
- **Was ist:** Dispatch-Table `_COMMAND_PARSERS` + je eine kleine Funktion pro Command
- **Nächster Schritt:** Shadow-Vergleich bestätigen, dann v1 entfernen

### `main.py: route_command()` — Kein Problem
- Analysiert und für sauber befunden. 16 Commands, jeder Branch ein Einzeiler-Dispatch.
  `startswith`-Checks für Device-Commands passen nicht in ein Dict. Bleibt so.

---

## Analysiert — Bewertung abgeschlossen

### 1. `routing.py: _message_handler()` (L20-171, 152 Zeilen) — REFACTORING SINNVOLL

**Was die Funktion tut:** Eingehende Messages prüfen und Commands ausführen.

**Struktur ist eigentlich ein linearer Pipeline-Flow:**
1. Early returns: kein `msg`, Echo, ACK, kein `!`-Prefix, Duplicate (L33-66)
2. Normalisierung (L56-59)
3. Routing-Entscheidung: `_should_execute_command()` (L71-76)
4. Response-Target berechnen (L83-92)
5. Block-Check (L98-108)
6. Throttle-Check (L111-120)
7. Parse + Execute + Respond (L122-170)

**Problem:** Nicht die Verschachtelung — die ist max 3 Ebenen (try→if→if).
Das eigentliche Problem ist die **Länge**. 150 Zeilen für einen linearen Flow
mit viel Debug-Logging (`has_console`-Blocks) dazwischen.

**Empfehlung:** Exception-Handling (L151-170) in eigene Methode extrahieren.
Die `has_console`-Blöcke aufräumen (Logger statt print). Sonst OK — der Flow
ist linear und verständlich, nur lang.

**Priorität:** MITTEL

---

### 2. `sqlite_storage.py: _migrate_v3_to_v4()` (L555-712, 158 Zeilen) — KEIN REFACTORING

**Was die Funktion tut:** Schema-Migration v3→v4 mit 8 Schritten.

**Analyse:** Die 8 Schritte sind bereits sauber mit Kommentaren markiert:
1. Neue Spalten auf messages (L558-577) — Loop, sauber
2. Telemetry-Spalten auf station_positions (L580-595) — Loop, sauber
3. Telemetry-Tabelle erstellen (L598-608) — SQL, trivial
4. Neue Indexes (L611-617) — SQL, trivial
5. Backfill aus raw_json (L620-635) — Ein UPDATE, klar
6. Echo ID Backfill (L638-650) — Python-Loop mit Regex, OK
7. Conversation Key Backfill (L653-681) — 3 SQL + Python-Loop für DMs
8. ACK Matching (L683-710) — Python-Loop, klar kommentiert

**Fazit:** Ja, die Funktion ist lang. Aber sie ist **strikt sequentiell**,
jeder Schritt ist klar kommentiert und in sich verständlich. Migrationen
aufzuspalten bringt keinen Wartungsgewinn — man liest sie genau einmal
und sie laufen genau einmal. Aufspalten würde nur die Lesereihenfolge
fragmentieren.

**Priorität:** KEINE — so lassen

---

### 3. `sqlite_storage.py: _backfill_new_tables()` (L385-482, 98 Zeilen) — KEIN REFACTORING

**Was die Funktion tut:** Bei Schema-v2-Migration Daten in neue Tabellen füllen.

**Analyse:** 5 SQL-Statements, jedes mit Kommentar:
1. signal_log aus MHeard Beacons (L388-401)
2. station_positions aus Position Beacons (L405-450) — grösstes SQL
3. Signal-Updates auf station_positions (L453-469)
4. Signal-only Stations einfügen (L472-482)
5. signal_buckets aggregieren (L485-503)

Das grosse SQL (#2) hat eine Window Function mit ROW_NUMBER(), aber das ist
**notwendige Komplexität** — "neueste Position pro Callsign" geht nicht einfacher
in SQL. Jedes Statement ist eigenständig und kommentiert.

**Priorität:** KEINE — so lassen

---

### 4. `commands/ctcping.py: _handle_ack_message()` (L172-295, 124 Zeilen) — REFACTORING SINNVOLL

**Was die Funktion tut:** ACK empfangen, RTT berechnen, Test-Fortschritt tracken.

**Probleme:**
- **4 Verschachtelungsebenen** im Kern (L227-289):
  `try → if test_id → if status=="running" → if sequence in completed`
- **Vermischte Verantwortlichkeiten:**
  - ACK-Validierung (L185-210)
  - RTT-Berechnung (L214-223)
  - Test-Fortschritt-Tracking (L227-281)
  - Completion-Event-Koordination (L270-281)
- **Completion-Event-Pattern** (L270-281) ist fragil:
  `hasattr` + dynamisches `_completion_events` dict + asyncio.Event

**Empfehlung:**
- ACK-Validierung als eigene Methode (return early bei Fehler)
- Test-Fortschritt-Recording in `_record_ping_result()` verschieben
  (existiert schon, wird aber nur von `_ping_timeout_task` genutzt)
- Completion-Event-Pattern vereinheitlichen (wird in 3 Stellen dupliziert:
  L270-281, L372-382 in `_record_ping_result`, und implizit in `_monitor_test_completion`)

**Priorität:** HOCH — Dual-Tracking (test_summary["completed"] vs. completed_sequences set)
ist eine Fehlerquelle

---

### 5. `main.py: _udp_message_handler()` (L917-1024, 108 Zeilen) — WIRD SICH SELBST LÖSEN

**Was die Funktion tut:** Ausgehende UDP-Messages verarbeiten und senden.

**Analyse:** Der Flow ist linear und klar:
1. Shadow v2 classify (L928-933)
2. Normalize (L936-946)
3. Suppress check + shadow compare (L953-968)
4. Self-message check + shadow compare (L971-981)
5. Shadow compare for send path (L983-986)
6. Send via UDP handler (L992-1024)

**Das "Problem" ist nur die Shadow-Logik** — 3x `compare_outbound_decision()`
Aufrufe verstreut über den Flow. Sobald Shadow-Routing bestätigt und entfernt
wird, fallen ~20 Zeilen weg und die Funktion ist 85 Zeilen, linearer Flow.

**Priorität:** KEINE JETZT — erledigt sich mit Shadow-Removal
(siehe `doc/check-and-remove-outbound-shadow.md`)

---

### 6. `commands/routing.py: _should_execute_command()` (L176-217, 42 Zeilen) — KEIN PROBLEM

**Was die Funktion tut:** Entscheiden ob ein Command ausgeführt werden soll.

**Analyse:** Die Funktion nutzt bereits **Early Returns** und ist sauber strukturiert:
- Broadcast-Destinations (L189-192) — 2 Zeilen
- Eigene Commands (L195-200) — 4 Zeilen
- Direct P2P zu uns (L203-206) — 3 Zeilen
- Group Messages (L209-214) — 4 Zeilen
- Fallback (L217) — 1 Zeile

Max 2 Verschachtelungsebenen. Jeder Block hat einen Kommentar.
Die Funktion tut genau eine Sache und ist gut lesbar.

**Priorität:** KEINE — so lassen

---

### 7. `ble_protocol.py: decode_binary_message()` (L62-172, 111 Zeilen) — REFACTORING MÖGLICH

**Was die Funktion tut:** Binary BLE-Nachricht dekodieren (3 Formate: ACK, Msg, Pos).

**Analyse:** Drei klar getrennte Branches:
- `@A` = ACK Frame (L77-106) — 30 Zeilen, eigenständig, OK
- `@:` / `@!` = Message/Position (L108-169) — 62 Zeilen, hier liegt die Komplexität
- else = Invalid (L171-172) — 1 Zeile

**Im Message/Position-Branch:**
- Path-Parsing (L110-115) — OK
- Dest-Type Extraktion mit Magic Numbers `payload_type 58 vs 33` (L118-124)
- Footer-Unpacking mit 9 Feldern aus fixer Position (L134-136)
- Dict-Comprehension aus `locals()` (L153-167) — unüblich aber funktional

**Empfehlung:** Die drei Branches als eigene Funktionen extrahieren:
`_decode_ack_frame()`, `_decode_data_frame()`. Das macht jede Funktion
testbar und unter 40 Zeilen.

**Priorität:** NIEDRIG — Code ist stabil (Firmware-Protokoll ändert sich selten),
aber `locals()`-Pattern ist fragil bei Refactoring

---

### 8. `commands/data_commands.py: handle_search()` (L11-118, 108 Zeilen) — REFACTORING SINNVOLL

**Was die Funktion tut:** Messages nach Callsign durchsuchen und Zusammenfassung bauen.

**Probleme:**
- **Search-Pattern Setup** (L21-32) — 3 Branches (prefix/exact/all), OK
- **Message-Loop** (L45-88) — hier liegt das Problem:
  - 3 Matching-Strategien verschachtelt (L53-66)
  - SID-Activity-Tracking nur für prefix-Search (L67-72)
  - msg vs pos Counting (L74-85)
  - Alles in einer Loop
- **Response-Building** (L90-117) — 5 Conditional Blocks aneinandergereiht

**Empfehlung:** Message-Loop-Body in eigene Methode extrahieren.
Oder besser: Die Zähllogik in den Storage-Layer verschieben (SQL kann
COUNT, GROUP BY, MAX direkt). Das würde die ganze Funktion auf ~30 Zeilen
Response-Formatting reduzieren.

**Priorität:** MITTEL — funktioniert, ist aber unnötig komplex für eine
Aufgabe die SQL besser lösen könnte

---

## Zusammenfassung: Handlungsbedarf

| Funktion | Bewertung | Priorität |
|----------|-----------|-----------|
| `_handle_ack_message()` (ctcping) | Refactoring nötig — Dual-Tracking, tiefe Verschachtelung | **HOCH** |
| `_message_handler()` (routing) | Aufräumen — zu lang, Exception-Handling extrahieren | MITTEL |
| `handle_search()` (data_commands) | Refactoring sinnvoll — Logik in SQL verschieben | MITTEL |
| `decode_binary_message()` (ble_protocol) | Optional — in 2-3 Funktionen aufteilen | NIEDRIG |
| `_udp_message_handler()` (main) | Erledigt sich mit Shadow-Removal | WARTET |
| `_should_execute_command()` (routing) | Kein Problem — sauber mit Early Returns | KEINE |
| `_migrate_v3_to_v4()` (sqlite_storage) | Kein Problem — einmalige Migration, klar kommentiert | KEINE |
| `_backfill_new_tables()` (sqlite_storage) | Kein Problem — sequentielle SQL-Statements | KEINE |
