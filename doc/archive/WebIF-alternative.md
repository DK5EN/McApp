# WebIF-Alternative: Gap-Analyse BLE vs. WebServer-API

**Datum:** 2026-02-21
**Firmware-Version:** 4.35k
**Status:** Konzeptdokument / Analyse

---

## 1. Einleitung

### Motivation

Der MeshCom-Firmware-Entwickler empfiehlt, zukünftig über das Web-Interface (HTTP) statt über Bluetooth Low Energy (BLE) auf den MeshCom-Node zuzugreifen. Dieses Dokument analysiert systematisch, welche der aktuell über BLE genutzten Funktionen durch das WebServer-API der Firmware ersetzt werden können — und wo kritische Lücken bestehen.

### Zielarchitektur

```
McApp ──HTTP──> ESP32 WebServer (:80)
         statt
McApp ──BLE──> ESP32 GATT Service
```

**Vorteil WebIF:** Kein Bluetooth-Hardware nötig, kein BLE-Service auf dem Pi, direkte TCP/IP-Verbindung, größere Reichweite via WiFi.

**Risiko:** Die WebServer-API ist laut Firmware-Doku *"not yet finished or stable"*. Zentrale Funktionen fehlen oder liefern nur HTML statt JSON.

---

## 2. Funktions-Mapping (Übersicht)

| Bereich | BLE-Funktion | WebIF-Äquivalent | Status |
|---------|-------------|-------------------|--------|
| **Nachrichten senden** | 0xA0 Text-Message | `/?sendmessage&tocall=X&message=Y` | ✅ Vorhanden |
| **Nachrichten empfangen** | GATT Notifications (Echtzeit) | `/?getmessages` (Polling, HTML!) | ⚠️ Nur HTML, kein JSON |
| **MHeard-Daten** | TYP:MH JSON-Beacons (Echtzeit) | `/?page=mheard` (HTML!) | ❌ Kein JSON, kein RSSI/SNR |
| **Device-Info** | `--info` → TYP:I (JSON) | Teilweise via `/getparam/` | ⚠️ Kein gebündeltes Info-JSON |
| **Node-Settings** | `--nodeset` → TYP:SN (JSON) | `/getparam/?` + `/setparam/?` | ⚠️ Einzeln pro Parameter |
| **APRS-Settings** | `--aprsset` → TYP:SA (JSON) | `/getparam/?` + `/setparam/?` | ⚠️ Einzeln pro Parameter |
| **GPS/Position** | `--pos` → TYP:G (JSON) | Position-Seite (HTML!) | ❌ Kein JSON-Endpoint |
| **Position senden** | `--sendpos` via BLE | `/callfunction/?sendpos` | ✅ Vorhanden |
| **Sensor/Wetter** | `--weather` → TYP:W (JSON) | WX-Seite (HTML!) | ❌ Kein JSON-Endpoint |
| **Sensor-Config** | `--seset` → TYP:SE+S1 (JSON) | `/setparam/?bme=on` etc. | ⚠️ Setzen ja, Query-Bundle nein |
| **WiFi-Config** | `--wifiset` → TYP:SW+S2 (JSON) | `/getparam/?` + `/setparam/?` | ⚠️ Einzeln pro Parameter |
| **Analog-Config** | `--analogset` → TYP:AN (JSON) | `/setparam/?` | ⚠️ Einzeln pro Parameter |
| **Telemetrie-Config** | `--tel` → TYP:TM (JSON) | Nicht vorhanden | ❌ Fehlt |
| **GPIO/IO** | `--io` → TYP:IO (JSON) | Nicht vorhanden | ❌ Fehlt |
| **ACK-Tracking** | Binäre ACK-Frames (0x41) | Nicht vorhanden | ❌ Fehlt |
| **Positions-Beacons** | Binär payload_type=33 mit APRS | Nicht vorhanden | ❌ Fehlt |
| **Telemetrie-Beacons** | Binär T# Format | Nicht vorhanden | ❌ Fehlt |
| **Reboot** | `--reboot` / 0xF0 | `/callfunction/?reboot` | ✅ Vorhanden |
| **OTA-Update** | `--ota-update` | `/callfunction/?otaupdate` | ✅ Vorhanden |
| **Save Settings** | `--save` / 0xF0 | Nicht dokumentiert | ❌ Unklar |
| **Hello-Handshake** | 0x10 Message | Nicht nötig (HTTP ist stateless) | ✅ Entfällt |
| **Zeitsync** | 0x20 Timestamp | Nicht vorhanden | ❌ Fehlt |
| **LoRa-Config** | `--txpower`, `--txfreq` etc. | `/setparam/?txpower=X` | ⚠️ Nur txpower dokumentiert |
| **Spectrum-Scan** | `--spectrum` etc. | Spectrum-Seite (HTML!) | ⚠️ Nur HTML |
| **Passwort-Auth** | BLE PIN (6-stellig) | Web-Passwort (IP-basiert, Timeout) | ✅ Vorhanden |
| **Echtzeit-Stream** | GATT Notifications (Push) | Kein SSE/WebSocket | ❌ Fehlt komplett |
| **manualcommand** | — | `/setparam/?manualcommand=CMD` | ✅ Wildcard! |

---

## 3. Detailanalyse

### 3.1 Konfiguration / A0-Befehle

**BLE:** ~60 A0-Befehle (`--setcall`, `--txpower`, `--bme on`, etc.) mit sofortigem Feedback. Gebündelte Abfrage über `--nodeset`, `--aprsset`, `--seset` etc. liefert komplette JSON-Register.

**WebIF:**
- `/setparam/?key=value` — Setzt einzelne Parameter. JSON-Response mit `returncode` (0=OK, 1=FAIL, 2=UNKNOWN) und aktuellem Wert.
- `/getparam/?key` — Liest einzelne Parameter. Gleiche JSON-Response.
- `/setparam/?manualcommand=COMMAND` — **Wildcard-Endpoint**: Kann beliebige Firmware-Befehle ausführen (URL-encoded). Damit sind theoretisch alle `--` Befehle auch über HTTP erreichbar.

**Bewertung:** Konfiguration ist funktional abgedeckt. Die `manualcommand`-Funktion ist der Schlüssel — damit können auch Befehle wie `--info`, `--pos`, `--weather` über HTTP gesendet werden. **Problem:** Die JSON-Responses (TYP:I, TYP:G etc.) werden nur über BLE zurückgesendet, nicht als HTTP-Response. Der `manualcommand`-Endpoint liefert nur den `returncode`, nicht die eigentlichen Daten.

**Dokumentierte setparam-Parameter:**

| Parameter | Beschreibung |
|-----------|-------------|
| `setcall` | Callsign |
| `onewiregpio`, `onewire` | OneWire-Konfiguration |
| `buttongpio`, `button` | User-Button |
| `setctry` | Country-Code (LoRa-Preset) |
| `txpower` | TX-Leistung (dBm) |
| `utcoffset` | UTC-Offset |
| `maxv` | Batterie-Referenzspannung |
| `display`, `small`, `volt` | Display-Einstellungen |
| `mesh` | Mesh-Forwarding |
| `gateway` | Gateway-Modus |
| `setlat`, `setlon`, `setalt` | Position |
| `gps`, `track` | GPS und SmartBeaconing |
| `setname`, `atxt`, `symid`, `symcd` | APRS-Einstellungen |
| `angpio`, `checkanalog` | Analog-Input |
| `bmp`, `bme`, `680`, `811`, `ina226` | Sensor-Aktivierung |
| `softser` | SoftSerial |
| `setgrc` | Gruppen-Codes |
| `setssid`, `setpwd` | WiFi-Credentials |
| `setownip`, `setownms`, `setowngw` | Statische IP |
| `extudpip`, `extudp` | Externer UDP |
| `manualcommand` | Beliebiger Befehl |

### 3.2 Nachrichten senden / empfangen

**BLE (senden):** 0xA0 Text-Message → wird als APRS-Nachricht ins Mesh gesendet. Unterstützt Gruppencode und Direkt-Messages.

**WebIF (senden):** `/?sendmessage&tocall=AB1CDE-12&message=Hello%20World` — Funktional äquivalent. Die Firmware-Doku warnt: *"This API call is most likely to be changed soon."*

**BLE (empfangen):** Binäre GATT-Notifications in Echtzeit (payload_type 58 für Chat, 33 für Position, 65 für ACK). Jede Nachricht enthält:
- Source-Callsign (inkl. Relay-Pfad)
- Destination (Gruppe oder Callsign)
- Message-ID (4 Byte)
- Hop-Count, Mesh-Info
- Hardware-ID, LoRa-Mod, Firmware-Version
- Frame-Checksum

**WebIF (empfangen):** `/?getmessages` — Liefert **nur HTML**, kein JSON. Muss per Polling abgefragt werden (Frontend nutzt 10s-Intervall). Die Firmware hat einen `TODO`-Kommentar: `//ToDo: get messages as json`.

**Gap:** Echtzeit-Nachrichtenempfang ist die **kritischste Lücke**. Ohne SSE/WebSocket muss McApp pollen, was zu:
- 0-10 Sekunden Latenz führt
- Unnötigem Traffic auf dem ESP32 führt
- Keine Message-Metadaten (Hardware-ID, Hop-Count, FCS etc.) liefert
- Kein JSON-Format hat

### 3.3 MHeard / Signal-Statistiken

**BLE:** MHeard-Beacons kommen als JSON mit TYP:MH in Echtzeit:
```json
{"TYP":"MH", "CALL":"DL8DD-7", "RSSI":-95, "SNR":7.5,
 "HW":10, "MOD":1, "MESH":1, "DATE":"2026-02-21", "TIME":"12:30:00"}
```
Diese Beacons (~130/Stunde) sind die Grundlage für:
- Signal-Charts (RSSI/SNR über Zeit)
- Station-Tracking (wer ist aktiv)
- Signal-Buckets (5-min/1-hour Aggregation)

**WebIF:** `/?page=mheard` — Liefert **nur HTML-Tabelle**. Kein JSON, keine RSSI/SNR-Werte im API-Format. Keine Echtzeit-Push.

**Gap:** **Showstopper für Signal-Monitoring.** Die gesamte Signal-Analyse (signal_log, signal_buckets, mHeard-Charts) basiert auf den Echtzeit-MH-Beacons. Ohne JSON-API und Push-Mechanismus ist diese Funktionalität nicht über WebIF abbildbar.

### 3.4 Position / GPS

**BLE:**
- `--pos` → TYP:G JSON mit LAT, LON, ALT, SAT, SFIX, HDOP, RATE, NEXT, DIST, DIRn, DIRo, DATE
- Positions-Beacons (payload_type 33) mit APRS-Format inkl. Altitude, Battery, Groups
- GPS-Caching für Weather-Service-Lokation

**WebIF:**
- `/callfunction/?sendpos` — Löst Positions-Beacon aus ✅
- `/setparam/?setlat=X` / `/setparam/?setlon=X` / `/setparam/?setalt=X` — Position setzen ✅
- `/getparam/?setlat` etc. — Position lesen ✅
- Position-Seite — Nur HTML, kein JSON ❌

**Gap:** Grundkoordinaten sind über get/setparam lesbar. Aber die erweiterten GPS-Infos (Satellites, HDOP, Fix-Status, SmartBeaconing-State) sind nur über den `--pos` Befehl verfügbar — dessen JSON-Response aber nur via BLE kommt, nicht als HTTP-Response. Eingehende Positions-Beacons anderer Stationen fehlen komplett.

### 3.5 Telemetrie / Sensordaten

**BLE:**
- `--weather` → TYP:W JSON mit TEMP, HUM, PRES, QNH, CO2, Voltage etc.
- `--tel` → TYP:TM JSON mit Telemetrie-Konfiguration
- Eingehende Telemetrie-Beacons (T#-Format) in Echtzeit

**WebIF:**
- WX-Seite — Nur HTML ❌
- Kein Telemetrie-Query-Endpoint ❌
- Kein JSON-Format für Sensordaten ❌

**Gap:** Sensordaten sind über das WebIF nicht strukturiert abrufbar. Für McApps Telemetrie-Speicherung und -Anzeige nicht nutzbar.

### 3.6 Device-Info / Status

**BLE:** `--info` → TYP:I JSON mit Firmware-Version, Callsign, Gateway-ID, Hardware-ID, Battery (%), Battery (V), BLE-Mode, Country, Boost-Gain.

**WebIF:** Kein gebündelter Info-Endpoint. Einzelne Werte theoretisch über `/getparam/` lesbar, aber:
- Battery-Status: Nicht als getparam dokumentiert
- Firmware-Version: Nicht als getparam dokumentiert
- Hardware-ID: Nicht als getparam dokumentiert

Info-Seite im WebIF zeigt diese Daten, aber nur als HTML.

**Gap:** Device-Metadaten sind nicht strukturiert abrufbar.

### 3.7 ACK-Tracking

**BLE:** Binäre ACK-Frames (payload_type 65 = 0x41) mit:
- Original Message-ID
- ACK-Type (Node ACK vs. Gateway ACK)
- Server-Flag, Hop-Count
- Gateway-ID (bei Gateway-ACKs)

McApp nutzt ACKs für `acked`/`send_success` Tracking in der Datenbank und Echo-ID-Matching.

**WebIF:** Keine ACK-Funktionalität vorhanden.

**Gap:** ACK-Tracking ist über WebIF nicht möglich. Für DM-Zustellbestätigungen essenziell.

### 3.8 Verbindungsmanagement

**BLE:** GATT-Connect → Hello-Handshake (0x10) → Register-Queries → Echtzeit-Notifications. Connection-State wird über D-Bus/BlueZ überwacht. Auto-Reconnect bei Verbindungsverlust.

**WebIF:** Stateless HTTP. Kein Verbindungszustand nötig. Passwort-Authentifizierung IP-basiert mit Timeout. mDNS-Discovery (`callsign.local`).

**Vorteil WebIF:** Kein Connection-Management nötig. Einfachere Fehlerbehandlung. Keine Bluetooth-Hardware erforderlich.

---

## 4. Gap-Analyse (Zusammenfassung)

### Kritische Lücken (Showstopper)

| # | Fehlende Funktion | Auswirkung auf McApp | Priorität |
|---|-------------------|---------------------|-----------|
| 1 | **Kein Echtzeit-Push** (SSE/WebSocket) | Polling-Only = hohe Latenz, kein Event-Driven-Design möglich | P0 |
| 2 | **Kein JSON für MHeard** | Signal-Monitoring (Charts, Buckets) nicht möglich | P0 |
| 3 | **Kein JSON für Messages** | Nachrichten-Empfang nur als HTML-Scraping möglich | P0 |
| 4 | **Keine ACK-Notifications** | Zustellbestätigung nicht möglich | P1 |
| 5 | **Keine eingehenden Mesh-Messages** | Nur gespeicherte Messages, keine Live-Nachrichten | P0 |

### Wichtige Lücken

| # | Fehlende Funktion | Auswirkung auf McApp | Priorität |
|---|-------------------|---------------------|-----------|
| 6 | Kein JSON für Position (`--pos` Äquivalent) | GPS-Details (Sat, HDOP, Fix) nicht abrufbar | P1 |
| 7 | Kein JSON für Sensordaten (`--weather` Äquivalent) | Telemetrie-Speicherung nicht möglich | P1 |
| 8 | Kein JSON für Device-Info (`--info` Äquivalent) | FW-Version, Battery, HW-ID nicht abrufbar | P2 |
| 9 | Kein Zeitsync-Mechanismus | Node-Uhr kann nicht synchronisiert werden | P2 |
| 10 | Kein Save-Endpoint | Settings-Persistierung unklar | P2 |

### Vorhandene Funktionalität (nutzbar)

| # | Funktion | WebIF-Endpoint | Einschränkung |
|---|----------|---------------|---------------|
| 1 | Nachrichten senden | `/?sendmessage` | API-Änderung angekündigt |
| 2 | Parameter setzen | `/setparam/?key=value` | Pro Parameter einzeln |
| 3 | Parameter lesen | `/getparam/?key` | Pro Parameter einzeln |
| 4 | Position senden | `/callfunction/?sendpos` | — |
| 5 | Reboot | `/callfunction/?reboot` | — |
| 6 | OTA-Update | `/callfunction/?otaupdate` | — |
| 7 | Beliebiger Befehl | `/setparam/?manualcommand=X` | Response ohne Daten |

---

## 5. UDP als Fallback

McApp unterstützt bereits UDP (Port 1799) für die Kommunikation mit MeshCom-Gateways. UDP liefert:

| Funktion | UDP-Support | Format |
|----------|-------------|--------|
| Chat-Messages | ✅ | JSON mit src, dst, msg, type, timestamp |
| Position-Beacons | ✅ | JSON mit lat, lon, alt |
| MHeard-Beacons | ✅ | JSON mit RSSI, SNR |
| ACK-Messages | ✅ | JSON mit ack_id |
| Device-Config | ❌ | Nicht verfügbar |
| Sensor-Daten | ❌ | Nicht verfügbar |

**Strategie:** UDP bleibt der primäre Kanal für Mesh-Nachrichten (Empfang und Senden). Das WebIF könnte **ergänzend** für Device-Konfiguration genutzt werden, wo es Vorteile gegenüber BLE bietet (kein Bluetooth nötig, WiFi-Reichweite).

---

## 6. Architektur-Empfehlung

### Option A: WebIF als BLE-Ersatz (NICHT empfohlen)

Das WebIF kann BLE aktuell **nicht** ersetzen. Die Lücken bei Echtzeit-Nachrichten, MHeard-JSON und ACKs sind fundamental und erfordern erhebliche Firmware-Erweiterungen.

### Option B: Hybrid-Ansatz (empfohlen)

```
McApp
├── UDP Handler (Port 1799)          ← Primär: Mesh-Nachrichten, MHeard, ACKs
├── WebIF Client (HTTP, Port 80)     ← Ergänzend: Device-Konfiguration
│   ├── /setparam, /getparam         ← Parameter lesen/setzen
│   ├── /callfunction                ← sendpos, reboot, OTA
│   └── /?sendmessage               ← Nachrichten senden (Alternative zu UDP)
└── BLE Client (optional)            ← Falls BLE-exklusive Features nötig
```

### Neuer `web_client.py` Transport-Layer

Falls das WebIF für Konfiguration genutzt werden soll, wäre ein neuer Transport-Layer sinnvoll:

```python
class WebIFClient:
    """HTTP client for MeshCom WebServer API on the ESP32 node."""

    def __init__(self, node_url: str, password: str | None = None):
        self.node_url = node_url  # z.B. "http://192.168.68.100"
        self.password = password

    async def set_param(self, key: str, value: str) -> dict:
        """Set a parameter: /setparam/?key=value"""

    async def get_param(self, key: str) -> dict:
        """Get a parameter: /getparam/?key"""

    async def call_function(self, name: str, param: str = "") -> dict:
        """Call a function: /callfunction/?name=param"""

    async def send_message(self, tocall: str, message: str) -> bool:
        """Send a message: /?sendmessage&tocall=X&message=Y"""

    async def manual_command(self, command: str) -> dict:
        """Execute arbitrary command: /setparam/?manualcommand=CMD"""
```

**Implementierung erst sinnvoll**, wenn die Firmware JSON-Endpoints für Messages und MHeard bereitstellt.

---

## 7. Offene Fragen an Firmware-Entwickler

### Kritische Fragen (Feature-Requests)

1. **JSON-API für Nachrichten:** Ist ein `/?getmessages` Endpoint mit JSON-Output (statt HTML) geplant? Der Source-Code enthält bereits den Kommentar `//ToDo: get messages as json`.

2. **JSON-API für MHeard:** Ist ein `/?page=mheard` mit JSON-Output geplant? Wir benötigen RSSI, SNR, Callsign, Timestamp pro Station.

3. **Echtzeit-Push:** Gibt es Pläne für SSE (Server-Sent Events) oder WebSocket auf dem ESP32? Ohne Push-Mechanismus ist Echtzeit-Monitoring nicht praktikabel. (ESP32-Arduino hat SSE-Libraries, z.B. `ESPAsyncWebServer`.)

4. **Query-Befehle über HTTP:** Wenn `--info`, `--pos`, `--weather` via `manualcommand` aufgerufen werden — wo landet die JSON-Response? Nur BLE? Könnte sie auch als HTTP-Response zurückgegeben werden?

5. **ACK-Forwarding:** Können ACK-Nachrichten über das WebIF oder einen separaten Endpoint abgerufen werden?

### Klärungsfragen

6. **`/?sendmessage` Stabilität:** Die Doku warnt vor API-Änderungen. Gibt es einen Zeitplan?

7. **Save-Funktion:** Gibt es `/callfunction/?save` oder einen ähnlichen Endpoint zum Persistieren von Settings?

8. **Positions-Beacons anderer Stationen:** Sind empfangene Positionen über das WebIF abrufbar (nicht nur die eigene)?

9. **WebServer-Passwort:** Das Timeout-basierte IP-Auth — wie lang ist das Timeout? Konfigurierbar?

10. **API-Versionierung:** Plant die Firmware eine stabile API-Version, auf die sich Drittanbieter verlassen können?

---

## 8. Fazit

Das WebServer-API der MeshCom-Firmware ist in seiner aktuellen Form (v4.35k) ein **Konfigurations-Tool**, kein vollwertiger Kommunikationskanal. Es eignet sich für:

- Parameter setzen und lesen (einzeln)
- Funktionen auslösen (sendpos, reboot, OTA)
- Nachrichten senden

Es eignet sich **nicht** für:

- Echtzeit-Nachrichtenempfang (kein Push, kein JSON)
- Signal-Monitoring (kein MHeard-JSON)
- ACK-Tracking
- Vollständige Device-Abfrage (kein gebündeltes Info/Pos/WX-JSON)

**Empfehlung:** UDP + BLE beibehalten. WebIF als ergänzenden Konfigurations-Kanal evaluieren, sobald die Firmware JSON-Endpoints und idealerweise SSE bereitstellt. Die `manualcommand`-Funktion ist vielversprechend, aber ohne HTTP-JSON-Responses für Query-Befehle nicht ausreichend.
