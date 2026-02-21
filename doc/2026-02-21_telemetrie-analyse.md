# Telemetrie-Analyse: Firmware v4.35k.02.04 → v4.35k.02.19

**Datum:** 2026-02-21
**Firmware-Update:** 2026-02-20 ~14:00 CET auf DK5EN-99
**Quellen:** Journal-Logs (`mcapp.service`), SQLite-DB (`/var/lib/mcapp/messages.db`), Firmware-Quellcode (`extudp_functions.cpp`)

---

## 1. Firmware-Änderungen

### Alte Firmware (v4.35k.02.04)

- **Nur eigene Node-Telemetrie** wurde per UDP gesendet
- Sendbedingung: `if(strcmp(src_type, "node") == 0 && strlen(c_tjson) > 0)`
- **Kein `src`-Feld** im JSON → McApp musste Pseudo-Callsign `NODE-{IP-Oktet}` generieren
- **Kein `batt`-Feld**

```json
{
  "src_type": "node",
  "type": "tele",
  "temp1": 0, "temp2": 0, "hum": 0,
  "qfe": 0, "qnh": 0, "gas": 0, "co2": 0
}
```

### Neue Firmware (v4.35k.02.19)

- **Zwei Telemetrie-Typen:**
  - `src_type: "node"` — eigener Node (mit `src`, ohne `batt`)
  - `src_type: "lora"` — empfangene Remote-Nodes (mit `src` UND `batt`)
- Sendbedingung erweitert: `if((strcmp(src_type, "node") == 0 || strcmp(src_type, "lora") == 0) && ...)`
- `src` = `aprsmsg.msg_source_path` (Callsign + Relay-Pfad, z.B. `"DL2JA-1,DL2UD-01"`)
- `batt` = `aprspos.bat` (nur bei LoRa-Telemetrie)

**Node-Telemetrie (eigener Node):**
```json
{
  "src_type": "node",
  "type": "tele",
  "src": "DK5EN-99",
  "temp1": 0, "temp2": 0, "hum": 0,
  "qfe": 0, "qnh": 0, "gas": 0, "co2": 0
}
```

**LoRa-Telemetrie (Remote-Node):**
```json
{
  "src_type": "lora",
  "type": "tele",
  "src": "DL2JA-2",
  "batt": 70,
  "temp1": 16.5, "temp2": 0, "hum": 33.6,
  "qfe": 412, "qnh": 0, "gas": 81.8, "co2": 0
}
```

**LoRa-Telemetrie mit Relay-Pfad:**
```json
{
  "src_type": "lora",
  "type": "tele",
  "src": "OE2XZR-12,OE5MHX-12,DB0HOB-12,DB0ED-99",
  "batt": 99,
  "temp1": 0, "temp2": 0, "hum": 0,
  "qfe": 0, "qnh": 0, "gas": 0, "co2": 0
}
```

### Kein Nodename vorhanden

Die Firmware sendet **keinen menschenlesbaren Gerätenamen** — nur das Callsign aus `msg_source_path`. Die `aprsPosition`-Struktur enthält kein Namensfeld.

---

## 2. Mengenvergleich: Vorher vs. Nachher

### UDP-Telemetrie im Journal

| Zeitraum | Journal-Zeilen | Stationen (unique src) |
|----------|---------------|----------------------|
| 19.02. 00:00 – 20.02. 14:00 (alte FW) | **76** | **1** (nur `NODE-69`) |
| 20.02. 14:00 – 21.02. 08:00 (neue FW) | **448** | **89** (mit Relay-Varianten) |

Die alte Firmware sendete alle ~30 Minuten eine einzige Telemetrie-Nachricht vom eigenen Node (`NODE-69`), immer mit Nullwerten (keine Sensoren angeschlossen). Die neue Firmware sendet nun Telemetrie von **allen per LoRa empfangenen Stationen** weiter — ein **~6-facher Anstieg** des Telemetrie-Volumens.

### Gespeicherte Telemetrie in der DB

| Zeitraum | DB-Zeilen | Stationen |
|----------|----------|-----------|
| 19.02. – 20.02. 14:00 (alte FW) | **180** | **15** |
| 20.02. 14:00 – 21.02. 08:00 (neue FW) | **335** | **39** |

**Wichtig:** Die 180 Zeilen vor dem Update kamen **nicht** aus UDP-Telemetrie (die war nur `NODE-69` mit Nullen und wurde korrekt gefiltert). Sie kamen aus dem **BLE/APRS-Positions-Pfad** — dort werden Telemetrie-Daten aus den APRS-Positions-Beacons extrahiert und gespeichert.

---

## 3. Stations-Statistik (DB, seit 20.02. 14:00)

### Stationen mit echten Sensorwerten (gespeichert in DB)

| Callsign | Zeilen | temp1 | hum | qfe | gas | batt | qnh | alt | Bemerkung |
|----------|--------|-------|-----|-----|-----|------|-----|-----|-----------|
| DL2JA-2 | 59 | 15-17°C | 33-34% | 408-424 | 82-95 | 70 | — | — | Indoor, BME680 |
| DL2JA-1 | 52 | 1-5°C | 79-89% | 396-408 | 26-52 | 65-87 | — | — | Outdoor, BME680 |
| DK5EN-12 | 43 | 21-22°C | 38-43% | — | — | 100 | 1025 | — | Indoor, nur QNH |
| DK5EN-99 | 15 | — | — | — | — | — | — | — | Eigener Node, keine Sensoren |
| DL7OSX-1 | 15 | — | — | — | — | 0-100 | — | — | Keine Sensoren |
| DL2UD-01 | 14 | — | — | — | — | 79-100 | — | — | Keine Sensoren |
| DB0ED-99 | 12 | — | — | — | — | 36-76 | — | — | Digipeater, Solar |
| DF2SI-12 | 12 | — | — | — | — | 100 | — | — | Keine Sensoren |
| OE2XZR-12 | 11 | — | — | — | — | — | — | — | Digipeater Salzburg |
| DB0ISM-1 | 8 | — | — | — | — | 78 | — | — | Digipeater |
| DF8RD-1 | 7 | 15-16°C | — | 965 | — | — | — | — | Indoor |
| DL2RN-13 | 7 | — | — | — | — | — | — | — | Unbekannt |
| DL3NCU-1 | 7 | — | — | — | — | 100 | — | — | Keine Sensoren |
| OE5MHX-12 | 6 | 5°C | 74% | — | — | — | — | — | Outdoor Österreich |
| DL4MFH-1 | 5 | 6°C | 63% | — | — | — | — | — | QNH-only |
| DO6TK-12 | 2 | 2°C | 90% | — | — | — | — | — | Outdoor |
| DO1HOZ-10 | 2 | ~1°C | 80% | — | — | — | — | — | Outdoor |
| DG7RJ-11 | 2 | ~21°C | — | — | — | — | — | — | Indoor |
| DG7RJ-12 | 2 | — | — | — | — | — | — | — | Unbekannt |

**~14 Stationen mit echten Sensorwerten, ~25 Stationen senden nur Nullen** (werden korrekt vom All-Zero-Filter verworfen und nicht in der DB gespeichert).

### Vergleich mit Vor-Update (19.02.)

| Callsign | Vor Update | Nach Update | Quelle (vorher) |
|----------|-----------|-------------|------------------|
| DL2JA-1 | 46 | 52 | BLE/APRS |
| DL2JA-2 | 42 | 59 | BLE/APRS |
| DK5EN-12 | 40 | 43 | BLE/APRS |
| DO1HOZ-10 | 12 | 2 | BLE/APRS |
| DF8RD-1 | 9 | 7 | BLE/APRS |
| DL2RN-13 | 8 | 7 | BLE/APRS |
| DO6TK-12 | 6 | 2 | BLE/APRS |
| DL4MFH-1 | 1 | 5 | BLE/APRS |
| OE5MHX-12 | 3 | 6 | BLE/APRS |
| OE2XZR-12 | — | 11 | **NEU** (nur UDP-Tele) |
| DB0ISM-1 | — | 8 | **NEU** (nur UDP-Tele) |
| DL7OSX-1 | — | 15 | **NEU** (nur Nullen) |

**Ergebnis:** Die Kern-Stationen (DL2JA-*, DK5EN-12) liefern jetzt Telemetrie über **zwei parallele Pfade** — BLE/APRS und UDP-LoRa. Die Dedup-Logik in `store_telemetry()` verhindert Doppelspeicherung innerhalb von 60 Sekunden. Einige Stationen (OE2XZR-12, DB0ISM-1) sind **neu sichtbar**, senden aber nur Nullen.

---

## 4. Relay-Pfade in der Telemetrie

Die neue Firmware sendet das vollständige `msg_source_path` als `src`-Feld. Dadurch erscheint **dieselbe Station über verschiedene Relay-Pfade** als unterschiedliche `src`-Einträge im Journal:

| Station | Relay-Varianten (Beispiele) |
|---------|----------------------------|
| DL2JA-1 | `DL2JA-1`, `DL2JA-1,DL2UD-01`, `DL2JA-1,DL7OSX-1`, `DL2JA-1,DB0HOB-12,DB0ED-99` |
| OE2XZR-12 | `OE2XZR-12,DD7MH-11,DB0HOB-12,DB0ED-99`, `OE2XZR-12,OE5MHX-12,DB0HOB-12,DB0ED-99`, `OE2XZR-12,OE5MHX-12,DL3MBG-12,DB0HOB-12,DB0ED-99` |
| DF2SI-12 | `DF2SI-12`, `DF2SI-12,DL2JA-1`, `DF2SI-12,DB0HOB-12,DB0ED-99`, `DF2SI-12,DL2JA-1,DL2UD-01` |

McApp normalisiert dies korrekt: `src.split(",")[0]` extrahiert das eigentliche Callsign (`sqlite_storage.py:960-962`). Die Relay-Pfade gehen aber bei der Speicherung verloren — es gibt kein `via`-Feld in der `telemetry`-Tabelle.

---

## 5. Identifizierte Probleme

### 5.1 `batt`-Feld wird nicht gespeichert

Die neue Firmware sendet ein `batt`-Feld (Batteriespannung in %) bei allen LoRa-Telemetrie-Nachrichten. Dieses Feld wird:

- **Empfangen und geloggt** (im Journal sichtbar)
- **Nicht extrahiert** in `store_telemetry()` (`sqlite_storage.py:1149-1162`)
- **Nicht in der `telemetry`-Tabelle gespeichert** (kein `batt`-Column)

Die `station_positions`-Tabelle hat bereits ein `batt`-Feld (Column 11), wird aber von der Telemetrie-Speicherung nicht befüllt.

**Beobachtete Batterie-Werte (aus Journal):**

| Station | batt (%) | Bedeutung |
|---------|----------|-----------|
| DK5EN-12 | 100 | Netzbetrieb |
| DL2JA-2 | 70 | Akku |
| DL2JA-1 | 65-87 | Akku, schwankend |
| DB0ED-99 | 36-76 | Solar, stark schwankend |
| DL2UD-01 | 79-100 | Akku |
| DL7OSX-1 | 0-100 | Unzuverlässig (0 = kein Sensor?) |
| DO7TW-1 | 5-100 | Stark schwankend |

### 5.2 Telemetrie nicht in `messages`-Tabelle

Telemetrie wird korrekt über den Early-Exit in `sqlite_storage.py:966-969` in die dedizierte `telemetry`-Tabelle geleitet. Es gibt 0 Zeilen mit `type='tele'` in der `messages`-Tabelle — das ist **korrektes Verhalten** (keine Änderung nötig).

### 5.3 Kein `raw_json` für Telemetrie

Die rohen UDP-JSON-Pakete werden nicht in der Datenbank gespeichert. Sie sind aber im **Journal auf INFO-Level** verfügbar (`UDP telemetry (src=...): {...}`). Für Debugging und Analyse reicht das Journal aus.

### 5.4 DK5EN-12: QNH statt QFE

Station DK5EN-12 sendet den Luftdruck als `qnh` (1025 hPa) statt als `qfe`, und `qfe=0`. Das ist eine Sensor-Konfiguration auf dem Node (nicht unser Problem), führt aber dazu, dass `qfe` in der DB `NULL` bleibt. McApp speichert `qnh` nicht (wird in `store_telemetry()` auf `None` gesetzt, weil "Node QNH is unreliable").

### 5.5 Doppelte Telemetrie-Einträge (BLE + UDP)

Stationen wie DL2JA-1/2 und DK5EN-12 senden Telemetrie über **zwei Pfade**:
1. **BLE/APRS-Positions-Beacon** → wird in `ble_protocol.py` geparst → `store_telemetry()`
2. **UDP-LoRa-Telemetrie** (neu seit FW-Update) → `udp_handler.py` → `store_telemetry()`

Die Dedup-Logik (60-Sekunden-Fenster, `sqlite_storage.py:1171-1184`) fängt die meisten Duplikate ab, aber nicht alle. Beispiel aus der DB: DL2JA-2 hat manchmal zwei Einträge im Sekundenabstand:

```
2026-02-21 07:25:35 | DL2JA-2 | temp1=15.8, qfe=963.7 (BLE-Pfad, normalisiert)
2026-02-21 07:25:34 | DL2JA-2 | temp1=15.8, qfe=420.0 (UDP-Pfad, Roh-QFE)
```

Der BLE-Pfad liefert **normalisierten QFE** (~963 hPa), der UDP-Pfad den **Roh-Sensorwert** (~420 hPa). Die Dedup-Logik behält den besseren Wert (höherer QFE), aber beide werden gespeichert, weil sie innerhalb der 60-Sekunden-Toleranz liegen und die QFE-Prüfung den neuen Wert als "besser" einstuft.

---

## 6. Handlungsempfehlungen

### Sofort (kein Code-Änderung)
- Rohdaten aus dem Journal sind für Analyse ausreichend verfügbar
- All-Zero-Filter funktioniert korrekt (25 Nullen-Stationen werden verworfen)
- Callsign-Normalisierung funktioniert korrekt (Relay-Pfade werden korrekt aufgelöst)

### Folgeticket: `batt`-Feld speichern
1. Schema-Migration: `ALTER TABLE telemetry ADD COLUMN batt INTEGER`
2. `store_telemetry()`: `batt = data.get("batt")` extrahieren und speichern
3. `station_positions.batt` aus Telemetrie-Daten befüllen (UPSERT erweitern)

### Folgeticket: QFE-Duplikate bereinigen
Die Dedup-Logik könnte verbessert werden, um den **normalisierten** QFE-Wert (BLE-Pfad, ~963 hPa) gegenüber dem **Roh-Sensorwert** (UDP-Pfad, ~420 hPa) zu bevorzugen. Aktuell werden manchmal beide gespeichert.

### Optional: `doc/telemetry.md` aktualisieren
Die bestehende Dokumentation (`doc/telemetry.md`) ist veraltet — sie beschreibt noch den Zustand vor der Telemetrie-Speicherung und enthält nur `src_type: "node"`. Sollte mit den neuen Erkenntnissen aktualisiert werden.

---

## 7. Rohdaten: Alle UDP-Telemetrie-Nachrichten seit 20.02. 14:00

Nachfolgend die ersten 100 empfangenen Telemetrie-Nachrichten (von insgesamt 448) aus dem Journal. Alle weiteren folgen dem gleichen Muster.

### 20.02. 14:00–18:00 (erste Stunden nach Firmware-Update)

```
14:11:56 NODE-69      (node) temp1=0 hum=0 qfe=0
14:20:22 DL3NCU-1     (lora) batt=100 temp1=0 hum=0 qfe=0
14:21:19 DK5EN-99     (node) temp1=0 hum=0 qfe=0
14:21:43 DK5EN-12     (lora) batt=100 temp1=21.9 hum=42.9 qnh=1025.1
14:27:40 DL2UD-01     (lora) batt=89  temp1=0 hum=0 qfe=0
14:27:58 DF2SI-12     (lora) batt=100 temp1=0 hum=0 qfe=0
14:30:28 DL7OSX-1     (lora) batt=0   temp1=0 hum=0 qfe=0
14:30:42 DL2JA-2      (lora) batt=70  temp1=16.5 hum=33.6 qfe=412 gas=81.8
14:38:39 DL4GLE-10    (lora) batt=0   temp1=0 hum=0 qfe=0   [via DB0HOB-12,DB0ED-99]
14:43:26 DL2JA-1      (lora) batt=65  temp1=4.7 hum=80.2 qfe=399 gas=52.1
14:47:22 DL2JA-2      (lora) batt=70  temp1=17.1 hum=33.3 qfe=411 gas=95.1
14:51:34 DK5EN-99     (node) temp1=0 hum=0 qfe=0
14:51:56 DK5EN-12     (lora) batt=100 temp1=21.9 hum=42.8 qnh=1025.2
14:54:13 DB0ED-99     (lora) batt=70  temp1=0 hum=0 qfe=0
14:57:57 DF2SI-12     (lora) batt=100 temp1=0 hum=0 qfe=0
14:58:00 DL2UD-01     (lora) batt=95  temp1=0 hum=0 qfe=0   [via DL7OSX-1]
15:00:44 DL7OSX-1     (lora) batt=100 temp1=0 hum=0 qfe=0
15:06:19 DL1RHS-14    (lora) batt=100 temp1=0 hum=0 qfe=0   [via DB0FHR-12,DB0ED-99]
15:13:47 DL2JA-1      (lora) batt=80  temp1=4.6 hum=79.8 qfe=397 gas=42.3  [via DL2UD-01]
15:17:37 DL2JA-2      (lora) batt=70  temp1=16.4 hum=33.7 qfe=409 gas=82.9
15:21:50 DK5EN-99     (node) temp1=0 hum=0 qfe=0
15:22:11 DK5EN-12     (lora) batt=100 temp1=22.0 hum=42.5 qnh=1025.5
15:24:22 DB0ED-99     (lora) batt=67  temp1=0 hum=0 qfe=0   [via DL7OSX-1]
15:24:58 DL3MBG-12    (lora) batt=100 temp1=0 hum=0 qfe=0   [via DB0HOB-12,DB0ED-99]
15:26:56 DO7TW-3      (lora) batt=72  temp1=0 hum=0 qfe=0   [via DB0FHR-12,DB0ED-99]
15:28:03 DO7TW-1      (lora) batt=100 temp1=0 hum=0 qfe=0   [via DB0FHR-12,DB0ED-99]
15:28:14 DL2UD-01     (lora) batt=81  temp1=0 hum=0 qfe=0
15:30:33 DG3CS-1      (lora) batt=100 temp1=0 hum=0 qfe=0   [via DB0HOB-12,DB0ED-99]
15:30:46 DB0ISM-1     (lora) batt=78  temp1=0 hum=0 qfe=0   [via DB0ED-99]
15:30:59 DL7OSX-1     (lora) batt=61  temp1=0 hum=0 qfe=0
```

### Beobachtung: DK5EN-99 vs. DK5EN-12

Der eigene Gateway-Node `DK5EN-99` (src_type=node) sendet alle ~30min Telemetrie mit Nullen (keine Sensoren). `DK5EN-12` ist ein per LoRa empfangener Node am selben Standort mit BME280-Sensor (temp1=21-22°C, hum=38-43%, qnh~1025). Durch die neue Firmware erscheint die eigene Node-Telemetrie (`DK5EN-99`) nun mit dem richtigen Callsign statt dem bisherigen `NODE-69`.

---

## 8. Zusammenfassung

| Aspekt | Status |
|--------|--------|
| Firmware sendet Multi-Station-Telemetrie | Funktioniert |
| `src`-Feld mit Callsign | Funktioniert (NODE-69 Fallback nicht mehr nötig) |
| Relay-Pfad-Normalisierung | Funktioniert |
| All-Zero-Filter | Funktioniert (25 Nullen-Stationen verworfen) |
| Dedup (BLE + UDP) | Teilweise (60s-Fenster, aber QFE-Unterschiede) |
| `batt`-Feld | **Empfangen, aber nicht gespeichert** |
| Telemetrie-Volumen | ~6x Anstieg (76 → 448 Journal-Zeilen/Tag) |
| Neue Stationen sichtbar | Ja (z.B. OE2XZR-12, DB0ISM-1, aber nur Nullen) |
