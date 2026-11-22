# SQLite Performance-Analyse

**Datum:** 2026-03-14
**System:** mcapp.local (Raspberry Pi Zero 2W)
**DB-Pfad:** `/var/lib/mcapp/messages.db`

## Ausgangslage

Die MeshCom Web App bezieht alle Daten beim Verbindungsaufbau ueber SSE (`/events`).
Das Backend (McApp) liest dazu die SQLite-Datenbank und liefert einen initialen Payload
mit Messages, Positionen und Metadaten. Da die Datenbank ueber die Zeit waechst, stellt
sich die Frage, ob Queries langsamer werden und welche Optimierungen moeglich sind.

## Zielsetzung

1. Aktuellen Fuellstand und Fragmentierung der Datenbank erfassen
2. Query-Performance auf dem Pi Zero 2W messen
3. Pruning-Verhalten verifizieren
4. Konkrete Optimierungspunkte identifizieren

---

## Beobachtungen

### Datenbankgroesse

| Datei | Groesse |
|-------|---------|
| messages.db | 31 MB |
| messages.db-wal | 9.4 MB |
| messages.db-shm | 32 KB |
| **Gesamt** | **~40 MB** |

### Fuellstand (Zeilen pro Tabelle)

| Tabelle | Zeilen | Anteil |
|---------|--------|--------|
| signal_log | 62.953 | groesste Tabelle |
| messages | 23.867 | davon 58% pos, 42% msg |
| signal_buckets | 13.134 | aggregierte Signaldaten |
| telemetry | 4.646 | Wetterdaten |
| station_positions | 113 | eine Zeile pro Station |

### Message-Typ-Verteilung

| Typ | Anzahl | Anteil |
|-----|--------|--------|
| pos (Position Beacons) | 13.938 | 58% |
| msg (Chat Messages) | 9.928 | 42% |

### Datenbereich (Retention)

| Tabelle | Aeltester Eintrag | Neuester Eintrag | Zeitraum |
|---------|-------------------|------------------|----------|
| messages | 2026-02-12 | 2026-03-14 | ~30 Tage |
| signal_log | 2026-03-06 | 2026-03-14 | ~8 Tage |
| telemetry | 2026-02-13 | 2026-03-14 | ~30 Tage |

Autoincrement-Sequenz messages: 118.645 (nur 23.864 verbleiben) -> ~80% wurden gepruned.

### Pruning-Status: Funktioniert

Die konfigurierten Retention-Zeiten werden eingehalten:

| Datentyp | Retention | Beobachtet |
|----------|-----------|------------|
| Chat Messages (type=msg) | 30 Tage | 30 Tage |
| Position Beacons (type=pos) | 8 Tage | 8 Tage |
| ACK Messages | 8 Tage | 8 Tage |
| signal_log | 8 Tage | 8 Tage |
| signal_buckets (5-Min) | 8 Tage | OK |
| signal_buckets (1-Std) | 365 Tage | OK |
| telemetry | 365 Tage | OK |
| station_positions | 30 Tage inaktiv | OK |
| Size-Limit | 1 GB mit VACUUM | nicht ausgeloest |

ANALYZE wird nach jedem Prune-Lauf ausgefuehrt (Query-Planner-Statistiken aktuell).

### DB-Konfiguration

| Parameter | Wert | Bewertung |
|-----------|------|-----------|
| journal_mode | WAL | gut |
| page_size | 4.096 | Standard |
| page_count | 7.866 | |
| freelist_count | 1.328 (5.2 MB) | 17% Fragmentierung |
| WAL-Checkpoint | sauber | |

### Query-Performance (gemessen auf Pi Zero 2W)

| Query | Dauer |
|-------|-------|
| `SELECT * FROM messages ORDER BY timestamp DESC LIMIT 100` | 13.8 ms |
| `SELECT COUNT(*) FROM messages` | 0.2 ms |
| `WHERE type='msg' ORDER BY timestamp DESC LIMIT 100` | 7.1 ms |
| `WHERE type='pos' ORDER BY timestamp DESC LIMIT 100` | 5.5 ms |
| `SELECT * FROM messages` (alle 23.867 Zeilen) | **4.269 ms (4.3 s)** |

Einzelne Queries sind schnell dank guter Index-Abdeckung (13 Indexes auf messages).

### Frontend-Performance (Chrome, http://mcapp.local/webapp/messages/all)

| Metrik | Wert |
|--------|------|
| TTFB | 3 ms |
| DOM Interactive | 36 ms |
| DOM Content Loaded | 48 ms |
| Page Load Complete | 69 ms |
| SSE /events erste Daten | ~800 ms |
| JS-Chunks | alle gecacht |

Kein Frontend-Engpass. Die SSE-Verbindung liefert die initialen Daten in unter 1 Sekunde.

### Index-Uebersicht

Alle vorhandenen Indexes auf der messages-Tabelle:

| Index | Spalten |
|-------|---------|
| idx_messages_timestamp | timestamp |
| idx_messages_src | src |
| idx_messages_dst | dst |
| idx_messages_type | type |
| idx_messages_type_timestamp | type, timestamp DESC |
| idx_messages_type_dst_timestamp | type, dst, timestamp DESC |
| idx_messages_type_src_timestamp | type, src, timestamp DESC |
| idx_messages_msgid_timestamp | msg_id, timestamp DESC |
| idx_messages_echo_id | echo_id (partial, WHERE NOT NULL) |
| idx_messages_convkey_ts | conversation_key, timestamp DESC (partial, WHERE type='msg') |
| idx_signal_log_cs_ts | callsign, timestamp DESC |
| idx_telemetry_cs_ts | callsign, timestamp DESC |

---

## Optimierungspunkte

### 1. raw_json aus Initial-Query ausschliessen

**Problem:** Die SSE-Initial-Query (`get_smart_initial_with_summary`) liest mit `SELECT *`
auch das `raw_json`-Feld (Text-Blob) aus jeder Message-Zeile. Dieses Feld wird im Frontend
nicht benoetigt und vergroessert den Transfer unnoetig.

**Massnahme:** Explizite Spaltenliste statt `SELECT *`, ohne `raw_json`.

**Aufwand:** gering
**Wirkung:** weniger Speicher, schnellerer SSE-Connect

### 2. Window-Function-Query optimieren

**Problem:** Die Initial-Query nutzt `ROW_NUMBER() OVER (PARTITION BY conversation_key
ORDER BY timestamp DESC)` ueber alle Messages. Das ist ein Full Table Scan mit
Window Function. Bei 23.867 Rows noch akzeptabel, skaliert aber schlecht.

**Massnahme:** Vorab nach Zeitfenster filtern (`WHERE timestamp > now - X`), bevor die
Window Function angewendet wird. Alternativ: materialisierte "last N per group"-Tabelle
oder CTE mit vorgefiltertem Zeitraum.

**Aufwand:** mittel
**Wirkung:** bessere Skalierung bei wachsender Datenmenge

### 3. Periodisches VACUUM nach Time-Based-Pruning

**Problem:** 1.328 freie Pages (5.2 MB, 17% der DB) liegen brach. VACUUM wird nur beim
Size-Based-Pruning (1 GB Limit) ausgefuehrt, nicht nach dem regulaeren taeglichen Pruning.

**Massnahme:** Nach dem regulaeren Prune-Lauf `VACUUM` ausfuehren, oder besser:
`PRAGMA auto_vacuum = INCREMENTAL` setzen und periodisch
`PRAGMA incremental_vacuum(N)` ausfuehren. Alternativ VACUUM nur wenn freelist_count
ueber einem Schwellwert liegt (z.B. > 20% der page_count).

**Aufwand:** gering
**Wirkung:** kompaktere DB-Datei, bessere I/O-Performance auf SD-Karte

### 4. WAL-Datei periodisch truncaten

**Problem:** Die WAL-Datei ist mit 9.4 MB ungewoehnlich gross (30% der DB-Groesse).
Regulaere Checkpoints schreiben die WAL-Daten zurueck in die Hauptdatei, lassen aber
die WAL-Datei auf ihrer Maximalgroesse. Auf einer SD-Karte belastet das den Flash-Speicher.

**Massnahme:** Periodisch (z.B. einmal taeglich nach dem Pruning)
`PRAGMA wal_checkpoint(TRUNCATE)` ausfuehren. Das schreibt die WAL zurueck und
truncated die Datei auf 0 Bytes.

**Aufwand:** gering
**Wirkung:** ~9 MB Speicherplatz, weniger SD-Karten-Verschleiss

### 5. signal_log Wachstum beobachten

**Problem:** signal_log ist mit 62.953 Zeilen die groesste Tabelle (~8.000 Eintraege/Tag).
Die 8-Tage-Retention haelt das in Schach, aber bei Schwankungen im Beacon-Volumen kann
die Tabelle schnell wachsen.

**Massnahme:** Monitoring oder Logging der Tabellengroesse nach dem Pruning. Optional:
Bucket-Aggregation haeufiger als einmal naechtlich ausfuehren, damit Rohdaten frueher
durch Aggregate ersetzt werden koennen.

**Aufwand:** gering
**Wirkung:** fruehwarnung bei ungewoehnlichem Wachstum

### 6. Taegliches Volumen hat sich veraendert

**Beobachtung:** Das taegliche Message-Volumen ist seit ~5. Maerz von 300-400 auf
1.800-2.200 Messages/Tag gestiegen. Das sollte beobachtet werden, da es die
Pruning-Effektivitaet und DB-Groesse beeinflusst.

**Massnahme:** Ursache klaeren (mehr Stationen? Mehr Beacon-Typen? Konfigurationsaenderung?).
Bei Bedarf Retention-Zeiten anpassen.

---

## Gesamtbewertung

Die Datenbank ist mit 31 MB moderat gefuellt und funktioniert zuverlaessig. Pruning
arbeitet korrekt, Indexes sind umfangreich und gut gewaehlt. Die Query-Performance
ist auf dem Pi Zero 2W im gruenen Bereich.

Die empfohlenen Optimierungen (1-4) sind praeventiv und geringem Aufwand. Sie sollten
in die Deploy-Scripte bzw. den SQLiteStorage-Code im MCProxy aufgenommen werden.
Direkte Aenderungen auf dem Pi sind nicht vorgesehen.
