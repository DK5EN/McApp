# MeshCom BLE Command Reference

**Verifiziert 2026-11-10**

Abgleich gegen `MeshCom-Firmware-DEV-Main` (SOURCE_VERSION 4.40a) und die Backend-Seite (`ble_service/src/ble_types.py`, `ble_session.py`, `src/mcapp/ble_protocol.py`). Korrigiert: Längenbytes der Beispiele (Gesamtlänge = Nutzlast + 2), Groß-/Kleinschreibung, Bereiche von `--txpower`, `--txbw`, `--txsf`, `--setctry`, Fehler bei `--volt`/`--proz`/`--bmx`/`--390`, Felder von SN/SN1/SE/S1/S2/I, Hello-Gate, Quellzeilen. `--compress`, `--softser test` und `--softser xml` sind im Code nicht vorhanden. Abschnitte ohne Code-Abgleich sind als *nicht verifiziert* markiert. Die Dokumentversion 4.35k bleibt im Kopf; die Quellzeilen beziehen sich auf 4.40a.

Complete reference for Bluetooth Low Energy (BLE) commands in MeshCom firmware.

**Dokumentversion:** 4.35k · **Verifiziert gegen Firmware:** 4.40a (DEV-Main, 2026-11-10)
**Source:** `src/phone_commands.cpp` and `src/command_functions.cpp`

---

## BLE Protocol Overview

### Message Format

```
[Length 1B] [Message ID 1B] [Data...]
```

- **Length:** Total message length in bytes
- **Message ID:** Command type identifier (0x10, 0x20, 0xA0, etc.)
- **Data:** Command-specific payload

---

## BLE Message Types

All BLE commands are handled by `readPhoneCommand()` in `src/phone_commands.cpp:233-645`.

| Msg ID   | Command          | Data Format                           | Description                        |
| -------- | ---------------- | ------------------------------------- | ---------------------------------- |
| **0x10** | Hello Message    | `0x20 0x30`                           | Handshake der App; mit PIN (`--btcode` ≠ 0) 36 Byte: `0x24 0x10 0x20 0x30` + SHA-256 des 6-stelligen PIN-Strings (`ble_types.build_hello_bytes`) |
| **0x20** | Timestamp        | 4B UNIX timestamp (UTC)               | Synchronize device time            |
| **0x50** | Callsign         | 1B length + callsign string           | Set station callsign               |
| **0x55** | WiFi Settings    | 1B SSID_len + SSID + 1B PWD_len + PWD | Configure WiFi credentials         |
| **0x70** | Latitude         | 4B float + 1B save_flag               | Set latitude position              |
| **0x80** | Longitude        | 4B float + 1B save_flag               | Set longitude position             |
| **0x90** | Altitude         | 4B int + 1B save_flag                 | Set altitude in meters             |
| **0x95** | APRS Symbols     | 1B primary + 1B secondary             | Set APRS map symbols (/ or \)      |
| **0xA0** | **Text Message** | **Text string**                       | **Send text message or command**   |
| **0xF0** | Save & Reboot    | (none)                                | Save settings to flash and restart |

### Position Save Flags (0x70, 0x80, 0x90)

- `0x0A` = Save to flash (one-time configuration)
- `0x0B` = Don't save (periodic GPS updates from phone)

---

## 0xA0 Text Message Command

### Format

```
[Total Length] [0xA0] [Message Text]
```

### Behavior

- Messages starting with `--` are treated as **system commands** (see below)
- Other messages are sent as **APRS text messages** to the mesh (prefixed with `:`)
- Puffergröße: `MAX_MSG_LEN_PHONE` = 300 Byte (`configuration_global.h:630`); die Firmware übernimmt den Text ohne Trennzeichen-Entfernung
- Sets `hasMsgFromPhone = true` flag for main loop processing

### Examples

**Send text message to mesh:**

```
[0x0D] [0xA0] [H][e][l][l][o][ ][W][o][r][l][d]
```

Result: `:Hello World` sent to mesh network

**Send system command:**

```
[0x0A] [0xA0] [--reboot]
```

Result: Node reboots

---

## Querying Device State (TYP Responses)

Several commands return JSON-formatted data with a `TYP` field identifying the response type. These responses use message ID `0x44` (data message) instead of `0x40` (text message).

### Response Format

```
[Length][0x44][JSON string]
```

### Query Commands

| Command              | TYP              | Description           | Multi-Part       | Response Fields                                                                                                                       |
| -------------------- | ---------------- | --------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `--pos`              | **G**            | GPS/Position data     | No               | LAT, LON, ALT, SAT, SFIX, HDOP, RATE, NEXT, DIST, DIRn, DIRo, DATE                                                                    |
| `--info`             | **I** + **IS1**  | General device info   | **YES** (2 msgs) | **I:** FWVER, CALL, ID, HWID, MAXV, BLE, BATP, BATV, GCB0-5, CTRY, BOOST, BPIN<br>**IS1:** BDATE<br>**IS2** (optional): CHG, CHGMA                                            |
| `--nodeset`          | **SN** + **SN1** | Node settings         | **YES** (2 msgs) | **SN:** GW, WS, DISP, BTN, MSH, GPS, TRACK, UTCOF, TXP, MQRG, MSF, MCR, MBW, GWNPOS, NOALL, NOPMOTHER, BLED, CHGLED, GWS<br>**SN1:** VIA, VIACALL, WSPWD, ASYM, TZ, MHOP, MTU, VOLT, NCON, KISS, LEDNF |
| `--aprsset`          | **SA**           | APRS settings         | No               | ATXT, SYMID, SYMCD, NAME                                                                                                              |
| `--seset`            | **SE** + **S1**  | Sensor settings       | **YES** (2 msgs) | **SE:** BME, BMP, BMXF, BMP3, BMP3F, 680, 680F, 811, 811F, AHT, AHTF, SS, LPS33, OW, OWPIN, OWF, USERPIN<br>**S1:** INA226, 226, 226F, SHUNT, IMAX, SAMP, SHT, SHTF, UV, UVF, WIND, WINDP  |
| `--wifiset`          | **SW** + **S2**  | WiFi/network settings | **YES** (2 msgs) | **SW:** SSID, IP, GW, AP, DNS, SUB<br>**S2:** OWNIP, OWNGW, OWNMS, OWNDNS, OWNNTP, EUDP, EUDPIP, TXPOW                                        |
| `--analogset`        | **AN**           | Analog input config   | No               | APN, AFC, AK, AFL, ACK, ADC, ADCRAW, ADCE1, ADCE2, ADCSL, ADCOF, ADCAT                                                                |
| `--tel`              | **TM**           | Telemetry data        | No               | PARM, UNIT, FORMAT, EQNS, VALES, PTIME                                                                                                |
| `--weather` / `--wx` | **W**            | Weather/sensor data   | No               | TEMP, TOFFI, TOUT, TOFFO, HUM, PRES, QNH, ALT, GAS, CO2, VBUS, VSHUNT, VAMP, VPOW                                                     |
| `--io`               | **IO**           | GPIO/IO status        | No               | MCP23017, AxOUT, AxVAL, BxOUT, BxVAL                                                                                                  |

### Multi-Part Responses

**IMPORTANT:** Some commands send multiple JSON responses automatically:

1. **`--info`** sends **2 responses** in sequence:
   - First: TYP: **I** (device info — FWVER, CALL, ID, HWID, ...)
   - Then: TYP: **IS1** (build date — the "I" JSON is already at the length limit, so BDATE rides a second frame)
   - Optional third frame: TYP: **IS2** (`CHG` = Ladezustand, `CHGMA` = mA, nur wenn der Ladezustand bekannt ist; Firmware 4.40a). Das Backend kennt IS2 nicht: `ROUTINE_JSON_TYPS` und `_CACHEABLE_REGISTER_TYPS` führen es nicht.

2. **`--nodeset`** sends **2 responses** in sequence:
   - First: TYP: **SN** (node settings)
   - Then: TYP: **SN1** (via-routing state — VIA, VIACALL)

3. **`--seset`** sends **2 responses** in sequence:
   - First: TYP: **SE** (primary sensor settings)
   - Then: TYP: **S1** (extended sensor settings - INA226, shunt config, SHT21)

4. **`--wifiset`** sends **2 responses** in sequence:
   - First: TYP: **SW** (basic WiFi settings - SSID, IP, gateway, DNS)
   - Then: TYP: **S2** (advanced network - static IP config, external UDP, WiFi TX power)

**Note:** There is no **S3** register type in the firmware. IS1 and SN1 are likewise
single continuation frames — neither has a "part 3".

### Field Descriptions

#### TYP: G (GPS/Position)

- **LAT/LON:** Decimal degrees (negative for S/W)
- **ALT:** Altitude in meters
- **SAT:** Number of satellites in view
- **SFIX:** GPS fix status (0=no fix, 1=fix)
- **HDOP:** Horizontal dilution of precision
- **RATE:** Position beacon interval (seconds)
- **NEXT:** Seconds until next beacon
- **DIST:** Distance traveled since last beacon (meters)
- **DIRn:** Current direction (degrees)
- **DIRo:** Previous direction (degrees)
- **DATE:** Current date/time string

#### TYP: I (Device Information)

- **FWVER:** Firmware version, Format `%-4.4s %-1.1s` (z. B. "4.40 a"; das Dokumentbeispiel "4.35 k" ist ein älterer Stand)
- **CALL:** Station callsign with SSID
- **ID:** Gateway ID
- **HWID:** Hardware ID (board type)
- **MAXV:** 100% battery voltage reference
- **BLE:** BLE message mode ("short" or "long")
- **BATP:** Battery percentage (0-100)
- **BATV:** Battery voltage (V)
- **GCB0-5:** Group code bytes
- **CTRY:** Country code (2-letter)
- **BOOST:** Boosted RX gain enabled (true/false)
- **BPIN:** BLE-PIN (`bt_code`) als Zahl im Klartext (Firmware 4.40a). Das Backend wertet BPIN nicht aus.

#### TYP: IS1 (Device Information - Part 2)

Automatically sent after I response (the "I" JSON is already at the 244-char BLE register limit).

- **BDATE:** Firmware build date/time, compiler `__DATE__`/`__TIME__`, format `YYYYMMDD-HHMMSS`, build-host local time (no timezone)

#### TYP: IS2 (Device Information - Part 3, optional)

Nur wenn der Ladezustand des PMU bekannt ist (Firmware 4.40a, `command_functions.cpp` `if(bci.state != BCS_UNKNOWN)`).

- **CHG:** Ladezustand als Text
- **CHGMA:** Strom in mA, vorzeichenbehaftet (+ = in den Akku, - = aus dem Akku); nur wenn gültig

#### TYP: SN (Node Settings)

- **GW:** Gateway mode enabled
- **WS:** Web server enabled
- **DISP:** Display off status
- **BTN:** Button check enabled
- **MSH:** Mesh networking enabled
- **GPS:** GPS chip enabled
- **TRACK:** SmartBeaconing enabled
- **UTCOF:** UTC offset (hours)
- **TXP:** TX power (dBm)
- **MQRG:** LoRa frequency (MHz)
- **MSF:** Spreading factor (7-12)
- **MCR:** Coding rate (5-8)
- **MBW:** Bandwidth (kHz)
- **GWNPOS:** Gateway without position
- **NOALL:** No message to all
- **BLED:** Board LED enabled
- **NOPMOTHER:** Firmware 4.40a (Semantik nicht verifiziert)
- **CHGLED:** Lade-LED (Firmware 4.40a, Semantik nicht verifiziert)
- **GWS:** Gateway server IP. `WSPWD` und `ASYM` stehen in SN1, weil SN sonst die BLE-Länge von 244 Byte überschreitet

#### TYP: SN1 (Node Settings - Part 2)

Automatically sent after SN response, and again on every via-routing change (`--via`).

- **VIA:** Via-routing enabled (true/false)
- **VIACALL:** Comma-separated via callsign list (e.g. `"OE1KBC-24,OE1KFR-12"`), `""` after `--via NONE`, max 39 chars, uppercased by the firmware
- **WSPWD:** Web-Server-Passwort (stand bis 4.40a in SN; jetzt in SN1)
- **ASYM:** GPS-Autosymbol (`--gps autosymbol`)
- **TZ:** POSIX-TZ-Regel (bis 39 Zeichen; `--settz`, leer bei festem `--utcoff`)
- **MHOP, MTU, VOLT, NCON, KISS, LEDNF:** App-Einstellungen (`--maxhop`, `--mtu`, `--volt`, `--netconsole`, `--kiss`), nur vorhanden, wenn der Build das Feature hat. `LEDNF` nennt nicht eingebaute LEDs. Semantik nicht verifiziert

#### TYP: SA (APRS Settings)

- **ATXT:** APRS comment text
- **SYMID:** APRS symbol table ID (`/` or `\`)
- **SYMCD:** APRS symbol code (character)
- **NAME:** Operator name

#### TYP: SE (Sensor Settings - Part 1)

- **BME:** BME280 sensor enabled
- **BMP:** BMP280 sensor enabled
- **BMXF:** BMx sensor found status
- **BMP3:** BMP390 sensor enabled
- **BMP3F:** BMP390 found status
- **680:** BME680 sensor enabled
- **680F:** BME680 found status
- **811:** CCS811 sensor enabled
- **811F:** CCS811 found status
- **AHT:** AHT20 temp/humidity sensor enabled
- **AHTF:** AHT20 found status
- **SS:** SoftSerial enabled
- **LPS33:** LPS33 pressure sensor enabled (RAK4630 only)
- **OW:** OneWire (DS18x20) enabled
- **OWPIN:** OneWire GPIO pin
- **OWF:** OneWire sensor found count
- **USERPIN:** User button GPIO pin

#### TYP: S1 (Sensor Settings - Part 2)

Automatically sent after SE response.

- **INA226:** INA226 sensor found
- **226:** INA226 current sensor enabled
- **226F:** INA226 found status
- **SHUNT:** Shunt resistor value (Ω)
- **IMAX:** Maximum current (A)
- **SAMP:** Current sample count
- **SHT:** SHT21 sensor enabled
- **SHTF:** SHT21 found status
- **UV, UVF:** UV-Sensor, enabled/found (Firmware 4.40a, Semantik nicht verifiziert)
- **WIND, WINDP:** Windsensor (Firmware 4.40a, Semantik nicht verifiziert)

#### TYP: SW (WiFi Settings - Part 1)

- **SSID:** WiFi SSID (or AP name if in AP mode)
- **IP:** Current IP address
- **GW:** Gateway IP address
- **AP:** Access Point mode enabled
- **DNS:** DNS server IP
- **SUB:** Subnet mask

#### TYP: S2 (WiFi Settings - Part 2)

Automatically sent after SW response.

- **OWNIP:** Static IP address (user-configured)
- **OWNGW:** Static gateway IP (user-configured)
- **OWNMS:** Static subnet mask (user-configured)
- **OWNDNS:** Static DNS server IP (user-configured)
- **EUDP:** External UDP enabled
- **EUDPIP:** External UDP server IP
- **TXPOW:** WiFi TX power (dBm)
- **OWNNTP:** Statischer NTP-Server (Firmware 4.40a)

#### TYP: AN (Analog Settings)

- **APN:** Analog input GPIO pin
- **AFC:** Analog scaling factor
- **AK:** Analog filter alpha (smoothing)
- **AFL:** Analog filter enabled
- **ACK:** Analog check/monitoring enabled
- **ADC:** Current analog value (processed)
- **ADCRAW:** Raw ADC reading
- **ADCE1:** ADC exponential filter 1
- **ADCE2:** ADC exponential filter 2
- **ADCSL:** ADC calibration slope
- **ADCOF:** ADC calibration offset
- **ADCAT:** ADC attenuation setting (ESP32)

#### TYP: TM (Telemetry Data)

- **PARM:** Parameter names (comma-separated)
- **UNIT:** Units for each parameter (comma-separated)
- **FORMAT:** Decimal places for each value (comma-separated)
- **EQNS:** Calibration equations (a,b,c coefficients)
- **VALES:** Internal sensor value mappings
- **PTIME:** Measurement interval (minutes)

#### TYP: W (Weather/Sensor Data)

- **TEMP:** Temperature (indoor sensor)
- **TOFFI:** Indoor temperature offset
- **TOUT:** Temperature (outdoor sensor)
- **TOFFO:** Outdoor temperature offset
- **HUM:** Humidity (%)
- **PRES:** Atmospheric pressure (hPa)
- **QNH:** Pressure at sea level (QNH)
- **ALT:** Barometric altitude (m)
- **GAS:** Gas resistance (BME680)
- **CO2:** CO2 level (ppm)
- **VBUS:** Bus voltage (V)
- **VSHUNT:** Shunt voltage (mV)
- **VAMP:** Current (mA)
- **VPOW:** Power (mW)

#### TYP: IO (GPIO/IO Status)

- **MCP23017:** MCP23017 I/O expander detected
- **AxOUT:** Port A output pin configuration (binary string)
- **AxVAL:** Port A pin values (binary string)
- **BxOUT:** Port B output pin configuration (binary string)
- **BxVAL:** Port B pin values (binary string)

### Query Examples

**Query GPS Position:**

```
Send:    [0x07][0xA0][--pos]
Receive: [Length][0x44][{"TYP":"G","LAT":48.208176,"LON":16.373819,"ALT":171,"SAT":12,"SFIX":1,"HDOP":0.8,"RATE":300,"NEXT":45,"DIST":0,"DIRn":0,"DIRo":0,"DATE":"2025-02-14 15:30:00"}]
```

**Query Device Info (Multi-Part):**

```
Send:    [0x08][0xA0][--info]
Receive: [Length][0x44][{"TYP":"I","FWVER":"4.35 k","CALL":"OE1ABC-7","ID":"ABC123","HWID":10,"MAXV":4.2,"BLE":"long","BATP":85,"BATV":4.05,"GCB0":1,"GCB1":2,"GCB2":0,"GCB3":0,"GCB4":0,"GCB5":0,"CTRY":"AT","BOOST":false}]
         [Length][0x44][{"TYP":"IS1","BDATE":"20260925-193817"}]
```

**Query Node Settings (Multi-Part):**

```
Send:    [0x0B][0xA0][--nodeset]
Receive: [Length][0x44][{"TYP":"SN","GW":true,"WS":true,"DISP":false,"BTN":true,"MSH":true,"GPS":true,"TRACK":true,"UTCOF":1.0,"TXP":20,"MQRG":433.175,"MSF":11,"MCR":6,"MBW":250,"GWNPOS":false,"NOALL":false,"BLED":true,"GWS":"192.168.1.100"}]
         [Length][0x44][{"TYP":"SN1","VIA":true,"VIACALL":"OE1KBC-24,OE1KFR-12","WSPWD":"admin","ASYM":false,"TZ":"CET-1CEST,M3.5.0,M10.5.0/3"}]
```

**Query APRS Settings:**

```
Send:    [0x0B][0xA0][--aprsset]
Receive: [Length][0x44][{"TYP":"SA","ATXT":"MeshCom Node","SYMID":"/","SYMCD":"[","NAME":"Martin"}]
```

**Query Sensor Settings (Multi-Part):**

```
Send:    [0x09][0xA0][--seset]
Receive: [Length][0x44][{"TYP":"SE","BME":true,"BMP":false,"BMXF":1,"BMP3":false,"BMP3F":0,"680":false,"680F":0,"811":false,"811F":0,"AHT":true,"AHTF":1,"SS":false,"LPS33":false,"OW":true,"OWPIN":23,"OWF":2,"USERPIN":38}]
         [Length][0x44][{"TYP":"S1","INA226":false,"226":false,"226F":0,"SHUNT":0.1,"IMAX":3.0,"SAMP":10,"SHT":false,"SHTF":0}]
```

**Query WiFi Settings (Multi-Part):**

```
Send:    [0x0B][0xA0][--wifiset]
Receive: [Length][0x44][{"TYP":"SW","SSID":"MyWiFi","IP":"192.168.1.50","GW":"192.168.1.1","AP":false,"DNS":"192.168.1.1","SUB":"255.255.255.0"}]
         [Length][0x44][{"TYP":"S2","OWNIP":"192.168.1.50","OWNGW":"192.168.1.1","OWNMS":"255.255.255.0","OWNDNS":"8.8.8.8","EUDP":true,"EUDPIP":"10.0.0.1","TXPOW":20}]
```

**Query Analog Settings:**

```
Send:    [0x0D][0xA0][--analogset]
Receive: [Length][0x44][{"TYP":"AN","APN":35,"AFC":2.0,"AK":0.1,"AFL":true,"ACK":true,"ADC":12.5,"ADCRAW":2048,"ADCE1":12.4,"ADCE2":12.6,"ADCSL":1.0,"ADCOF":0.0,"ADCAT":3}]
```

**Query Telemetry Data:**

```
Send:    [0x07][0xA0][--tel]
Receive: [Length][0x44][{"TYP":"TM","PARM":"VOLT,TEMP,HUM,,","UNIT":"V,C,%,,","FORMAT":"2,1,1,0,0","EQNS":"0,1,0,0,1,0,0,1,0,0,1,0,0,1,0","VALES":",,temp,press,","PTIME":5}]
```

**Query Weather Data:**

```
Send:    [0x0B][0xA0][--weather]
Receive: [Length][0x44][{"TYP":"W","TEMP":22.5,"TOFFI":0.0,"TOUT":18.3,"TOFFO":-0.5,"HUM":45,"PRES":1013.25,"QNH":1020.0,"ALT":171,"GAS":150000,"CO2":420,"VBUS":12.5,"VSHUNT":0.5,"VAMP":250,"VPOW":3125}]
```

**Query GPIO/IO Status:**

```
Send:    [0x06][0xA0][--io]
Receive: [Length][0x44][{"TYP":"IO","MCP23017":true,"AxOUT":"10101010","AxVAL":"11001100","BxOUT":"01010101","BxVAL":"00110011"}]
```

### Configuration Sync Sequence

*Nicht verifiziert: die Reihenfolge der Abfragen durch die App. Vom Backend gesendet werden beim Connect `--ackinfo on`, dann `--io` und `--tel` (`ble_session.query_extended_registers`).*

Phone apps typically query all registers on initial connection:

```bash
# Basic device info
--info        # → I + IS1 (device info + build date)
--nodeset     # → SN + SN1 (node settings + via state)
--aprsset     # → SA (APRS config)
--pos         # → G (GPS/position)

# Detailed settings
--seset       # → SE + S1 (sensors - 2 messages)
--wifiset     # → SW + S2 (WiFi - 2 messages)
--analogset   # → AN (analog config)
--tel         # → TM (telemetry)

# Real-time data
--weather     # → W (sensor readings)
--io          # → IO (GPIO status)
```

### Important Notes

- **BLE and EXTUDP:** TYP-JSON goes to the BLE output buffer. A register query that arrived over EXTUDP gets its answer on UDP instead (`sendBleJsonRegister`, `extRegSinkActive()`).
- **Message ID 0x44:** All TYP responses use data message flag `0x44` (not `0x40`)
- **Real-time Data:** Commands return current/live values from the device
- **Multi-Part Handling:** `--info`, `--nodeset`, `--seset` and `--wifiset` send 2 responses each - your code must handle all four
- **No S3 Type:** Apart from the optional IS2, only S1 and S2 exist as multi-part continuations (and likewise IS1/SN1 have no "part 3")

### Source Code References

| Register    | Function/Location                                                                                              | Line      |
| ----------- | -------------------------------------------------------------------------------------------------------------- | --------- |
| **G**       | `sendGpsJson()`                                                                                                | 8447      |
| **I**       | Inline (within `bInfo` flag)                                                                                   | 7874-7935 |
| **IS1**     | Inline (within `bInfo` flag, right after I — added 2026-09-25, see PR icssw-org/MeshCom-Firmware#1156)         | -         |
| **SN**      | `sendNodeSetting()`                                                                                            | 8477      |
| **SN1**     | Inline (within `sendNodeSetting()`, right after SN — added 2026-09-25, see PR icssw-org/MeshCom-Firmware#1155) | -         |
| **SA**      | `sendAPRSset()`                                                                                                | 8606      |
| **SE + S1** | Inline (within `bSensSetting` flag)                                                                            | 8306-8372 |
| **SW + S2** | Inline (within `bWifiSetting` flag)                                                                            | 8373-8414 |
| **AN**      | `sendAnalogSetting()`                                                                                          | 8579      |
| **TM**      | Inline (within `bTelemetry` flag)                                                                              | 7675-7700 |
| **W**       | Inline (within `bWeather` flag)                                                                                | 7701-7786 |
| **IO**      | Inline (within `bIO` flag)                                                                                     | 7787-7873 |

All references are in `src/command_functions.cpp` (Stand DEV-Main, SOURCE_VERSION 4.40a, verifiziert 2026-11-10; die Zeilen des 4.35k-Stands sind nicht mehr gültig)

---

## System Commands (via 0xA0)

All commands below are sent as text via the 0xA0 message type.
Commands must start with `--`. The name is compared case-insensitively, and a name without an argument must match the whole token (`--pos` does not match `--posshot`; `--setctry 1` matches `setctry` because the space ends the name).

---

### **System & Info Commands**

| Command        | Parameters | Description                                                            |
| -------------- | ---------- | ---------------------------------------------------------------------- |
| `--help`       | -          | Display all available commands on serial console                       |
| `--info`       | -          | Query device info via JSON (TYP:I) - see Querying Device State section |
| `--reboot`     | -          | Reboot the node immediately                                            |
| `--save`       | -          | Save current settings to flash memory                                  |
| `--ota-update` | -          | Start OTA firmware update process                                      |

**Example:**

```
--info
--reboot
```

---

### **Position & GPS Commands**

| Command       | Parameters   | Description                                                                  |
| ------------- | ------------ | ---------------------------------------------------------------------------- |
| `--pos`       | -            | Query GPS/position data via JSON (TYP:G) - see Querying Device State section |
| `--sendpos`   | -            | Send position beacon to mesh immediately                                     |
| `--sendhey`   | -            | Send APRS "hey" message (@ type)                                             |
| `--sendtele`  | -            | Send telemetry data packet                                                   |
| `--sendtrack` | -            | Send tracking beacon                                                         |
| `--setlat`    | `44.12345`   | Set latitude in decimal degrees                                              |
| `--setlon`    | `016.12345`  | Set longitude in decimal degrees                                             |
| `--setalt`    | `9999`       | Set altitude in meters                                                       |
| `--gps`       | `on` / `off` | Enable or disable GPS chip                                                   |
| `--gps reset` | -            | Factory reset GPS module (Wirkung nicht verifiziert)                                                     |
| `--track`     | `on` / `off` | Enable/disable SmartBeaconing                                                |
| `--posshot`   | -            | Trigger one-shot position update                                             |
| `--postime`   | `seconds`    | Set position beacon interval                                                 |

**Examples:**

```
--setlat 48.208176
--setlon 16.373819
--setalt 171
--sendpos
--track on
```

---

### **Radio/LoRa Configuration**

*Verifiziert: Namen sowie die Bereiche von `--txpower`, `--txbw`, `--txsf`, `--txcr`, `--setctry`. Nicht verifiziert: `--txfreq`-Bereich, `--setboostedgain`-Hardware.*

| Command            | Parameters            | Description                                  |
| ------------------ | --------------------- | -------------------------------------------- |
| `--lora`           | -                     | Display current LoRa settings                |
| `--txpower`        | `-9` to `22`          | Set TX power in dBm (Standard; `TX_POWER_MIN`/`TX_POWER_MAX` in `configuration_default.h`, Board-abhängig) |
| `--txfreq`         | `433.175`             | Set TX frequency in MHz                      |
| `--txbw`           | `125` / `250` | Set TX bandwidth in kHz                      |
| `--txsf`           | `6` to `12`           | Set spreading factor                         |
| `--txcr`           | `5` to `8`            | Set coding rate (4/5, 4/6, 4/7, 4/8)         |
| `--setctry`        | Land (Name oder Nummer)           | Set country preset (RX/TX parameters); Firmware nimmt Ländername oder Index und lehnt `none` ab        |
| `--setboostedgain` | `on` / `off`          | Enable/disable boosted RX gain (SX126x only; nicht verifiziert) |

**Examples:**

```
--txpower 20
--txfreq 433.175
--txbw 250
--txsf 11
--lora
```

---

### **Spectrum Analysis**

*Verifiziert: Namen und `--specsamples` 500–2048. Nicht verifiziert: Start-, End- und Schrittwerte.*

| Command         | Parameters      | Description                             |
| --------------- | --------------- | --------------------------------------- |
| `--spectrum`    | -               | Run spectral scan with current settings |
| `--specstart`   | `430.0`         | Set spectrum start frequency (MHz)      |
| `--specend`     | `436.0`         | Set spectrum end frequency (MHz)        |
| `--specstep`    | `0.1`           | Set spectrum step size (MHz)            |
| `--specsamples` | `500` to `2048` | Set number of samples per step          |

**Example:**

```
--specstart 433.0
--specend 434.0
--specstep 0.05
--specsamples 1024
--spectrum
```

---

### **Network & Communication**

| Command         | Parameters                 | Description                              |
| --------------- | -------------------------- | ---------------------------------------- |
| `--setcall`     | `OE0XXX-1`                 | Set callsign with SSID                   |
| `--setname`     | `name` / `none`            | Set operator first name                  |
| `--setudpcall`  | `CALL-1`                   | Set UDP gateway callsign                 |
| `--mesh`        | `on` / `off`               | Enable/disable mesh networking           |
| `--gateway`     | `on` / `off`               | Enable/disable gateway mode              |
| `--gateway`     | `pos` / `nopos`            | Gateway with/without position forwarding |
| `--gateway srv` | `192.168.1.100`            | Set gateway server IP address            |
| `--extudp`      | `on` / `off`               | Enable/disable external UDP              |
| `--extudpip`    | `255.255.255.255` / `none` | Set external UDP server IP               |
| `--mheard`      | -                          | Show list of heard stations              |
| `--mh`          | -                          | Alias for `--mheard`                     |
| `--path`        | -                          | Show message path information            |
| `--hey`         | -                          | Alias for `--path`                       |

**Examples:**

```
--setcall OE1ABC-7
--setname Martin
--mesh on
--gateway on
--mheard
```

---

### **WiFi Configuration**

| Command         | Parameters           | Description                     |
| --------------- | -------------------- | ------------------------------- |
| `--setssid`     | `MyWiFi` / `none`    | Set WiFi SSID                   |
| `--setpwd`      | `password` / `none`  | Set WiFi password               |
| `--wifiap`      | `on` / `off`         | Enable/disable WiFi AP mode     |
| `--setownip`    | `192.168.1.100`      | Set static IP address           |
| `--setowngw`    | `192.168.1.1`        | Set gateway IP address          |
| `--setownms`    | `mask:255.255.255.0` | Set subnet mask                 |
| `--setowndns`   | `8.8.8.8`            | Set DNS server IP               |
| `--sethamnet`   | -                    | Apply HamNet default settings   |
| `--setinet`     | -                    | Apply Internet default settings |
| `--wifitxpower` | `20`                 | Set WiFi TX power in dBm        |
| `--wifiset`     | -                    | Show WiFi configuration         |

**Examples:**

```
--setssid MyHomeWiFi
--setpwd MySecurePassword123
--setownip 192.168.1.50
--setowngw 192.168.1.1
--setownms mask:255.255.255.0
```

---

### **Web Server**

| Command       | Parameters          | Description                            |
| ------------- | ------------------- | -------------------------------------- |
| `--webserver` | `on` / `off`        | Enable/disable HTTP web server         |
| `--webpwd`    | `password` / `none` | Set web interface password             |
| `--webtimer`  | `0`                 | Set web server timeout (0 = always on) |

**Examples:**

```
--webserver on
--webpwd admin123
```

---

### **APRS Settings**

| Command      | Parameters              | Description                                      |
| ------------ | ----------------------- | ------------------------------------------------ |
| `--symid`    | `/` or `\`              | Set APRS symbol table (primary / or alternate \) |
| `--symcd`    | `[`                     | Set APRS symbol code (e.g., `[` = human)         |
| `--atxt`     | `MeshCom Node` / `none` | Set APRS comment text                            |
| `--setgrc`   | `1;2;3;`                | Set APRS group codes (semicolon-separated)       |
| `--nomsgall` | `on` / `off`            | Show/hide broadcast messages (*) on display      |
| `--aprsset`  | -                       | Show APRS configuration                          |

**APRS Symbol Examples:**

- `/[` = Human/Person
- `/k` = Truck
- `\n` = Red Cross
- `/>` = Car

**Examples:**

```
--symid /
--symcd [
--atxt MeshCom LoRa Node
--setgrc 1;5;7;
```

---

### **Sensor Configuration**

*Verifiziert: Namen, `--bmx off`, `--390` (BMP390). Nicht verifiziert: Sensorverhalten, Pinbelegungen, `--lps33` (RAK4630-only).*

| Command          | Parameters                  | Description                                         |
| ---------------- | --------------------------- | --------------------------------------------------- |
| `--weather`      | -                           | Show temperature/humidity/pressure readings         |
| `--wx`           | -                           | Alias for `--weather`                               |
| `--bmp`          | `on`                        | Enable BMP280 pressure sensor                       |
| `--bme`          | `on`                        | Enable BME280 temp/hum/pressure sensor              |
| `--680`          | `on`                        | Enable BME680 gas sensor                            |
| `--811`          | `on`                        | Enable CCS811 CO2/TVOC sensor                       |
| `--390`          | `on` / `off`                | Enable/disable BMP390 sensor (Firmware `--390`)                        |
| `--aht20`        | `on` / `off`                | Enable/disable AHT20 temp/humidity sensor           |
| `--sht21`        | `on` / `off`                | Enable/disable SHT21 temp/humidity sensor           |
| `--lps33`        | `on` / `off`                | Enable/disable LPS33 pressure sensor (RAK4630 only) |
| `--bmx`          | `BME` / `BMP` / `680` `off` | Disable BMx sensors (Firmware: `--bmx off` schaltet BME, BMP und 680 gemeinsam ab; einzeln über `--bme`, `--bmp`, `--680`)                                 |
| `--onewire`      | `on` / `off`                | Enable/disable DS18x20 OneWire temperature          |
| `--onewire gpio` | `23`                        | Set OneWire GPIO pin number                         |
| `--setpress`     | `1013.25`                   | Set pressure calibration (hPa)                      |
| `--tempoff in`   | `2.5`                       | Set indoor temperature offset (°C)                  |
| `--tempoff out`  | `-1.0`                      | Set outdoor temperature offset (°C)                 |
| `--showi2c`      | -                           | Scan and display I2C devices                        |

**Examples:**

```
--bme on
--onewire on
--onewire gpio 23
--weather
--showi2c
```

---

### **Telemetry/APRS Data**

*Verifiziert: Namen. Nicht verifiziert: Formatregeln von `--eqns`, `--values`, `--format`.*

APRS telemetry allows sending custom sensor data in standardized format.

| Command    | Parameters                   | Description                                     |
| ---------- | ---------------------------- | ----------------------------------------------- |
| `--parm`   | `VOLT,TEMP,HUM,,`            | Set parameter names (5 values, comma-separated) |
| `--unit`   | `V,C,%,,`                    | Set units for each parameter                    |
| `--format` | `2,1,1,0,0`                  | Set decimal places for each value               |
| `--eqns`   | `0,1,0, 0,1,0, ...`          | Set calibration equations (a,b,c coefficients)  |
| `--values` | `press,hum,temp,onewire,co2` | Map internal sensors to telemetry channels      |
| `--ptime`  | `10`                         | Set measurement interval in minutes             |
| `--tel`    | -                            | Show current telemetry configuration            |

**APRS Telemetry Format:**

- Each channel: `value = a*raw^2 + b*raw + c`
- Default equation: `0,1,0` (linear, no offset)

**Examples:**

```
--parm BATT,TEMP,HUM,PRESS,
--unit V,C,%,hPa,
--format 2,1,1,1,0
--values ,,temp,press,
--ptime 5
```

---

### **Hardware & GPIO**

*Verifiziert: Namen, `--analog atten` 0–3. Nicht verifiziert: GPIO-Modi von `--setio`, Wirkung von `--setout`.*

| Command           | Parameters       | Description                                |
| ----------------- | ---------------- | ------------------------------------------ |
| `--button`        | `on` / `off`     | Enable/disable user button                 |
| `--button gpio`   | `38`             | Set button GPIO pin number                 |
| `--analog gpio`   | `35`             | Set analog input GPIO pin                  |
| `--analog factor` | `2.0`            | Set analog scaling factor                  |
| `--analog alpha`  | `0.1`            | Set analog filter smoothing (0.0-1.0)      |
| `--analog slope`  | `1.0`            | Set analog calibration slope               |
| `--analog offset` | `0.0`            | Set analog calibration offset              |
| `--analog atten`  | `0` to `3`       | Set ADC attenuation (ESP32: 0=0dB, 3=11dB) |
| `--analog filter` | `on` / `off`     | Enable/disable analog value filtering      |
| `--analog check`  | `on` / `off`     | Enable/disable analog monitoring           |
| `--board led`     | `on` / `off`     | Enable/disable onboard LED                 |
| `--io`            | -                | Show GPIO pin status                       |
| `--setio`         | `pin,mode,value` | Configure GPIO (mode: 0=input, 1=output)   |
| `--setio clear`   | -                | Clear all GPIO configurations              |
| `--setout`        | `pin,value`      | Set output pin value (0 or 1)              |
| `--analogset`     | -                | Show analog configuration                  |

**Examples:**

```
--analog gpio 35
--analog factor 2.0
--analog filter on
--button gpio 38
--setout 12,1
--io
```

---

### **Power & Battery**

*Verifiziert: Namen, `--volt on/off` und `--proz on/off` als Schalter. Nicht verifiziert: Wertebereiche von `--shunt`, `--imax`, `--isamp`.*

| Command         | Parameters   | Description                          |
| --------------- | ------------ | ------------------------------------ |
| `--volt`        | -            | Anzeige Spannung (`--volt on`) bzw. Prozent (`--volt off`); ein nacktes `--volt` ist kein Befehl         |
| `--proz`        | -            | Anzeige Prozent (`--proz on`) bzw. Spannung (`--proz off`); ein nacktes `--proz` ist kein Befehl              |
| `--maxv`        | `4.2`        | Set 100% battery voltage reference   |
| `--batt factor` | `2.0`        | Set battery voltage divider factor   |
| `--shunt`       | `0.1`        | Set current shunt resistance (Ω)     |
| `--imax`        | `3.0`        | Set maximum current (A)              |
| `--isamp`       | `10`         | Set current measurement sample count |
| `--ina226`      | `on` / `off` | Enable/disable INA226 current sensor |

**Examples:**

```
--volt on
--maxv 4.2
--batt factor 2.0
--ina226 on
```

---

### **Display & UI**

| Command     | Parameters   | Description                       |
| ----------- | ------------ | --------------------------------- |
| `--display` | `on` / `off` | Enable/disable display            |
| `--t5`      | `on` / `off` | Enable/disable T5 e-paper display |
| `--all`     | -            | Show all messages on display      |
| `--msg`     | -            | Show message information          |

**Examples:**

```
--display on
--all
```

---

### **Bluetooth**

| Command      | Parameters | Description                         |
| ------------ | ---------- | ----------------------------------- |
| `--btcode`   | `123456`   | Set BLE pairing PIN (6 digits, 100000–999999; `000000` schaltet die BLE-Absicherung ab, nur auf bereits authentifizierter Verbindung) |
| `--bleshort` | -          | Use short BLE message format        |
| `--blelong`  | -          | Use long BLE message format         |

**Examples:**

```
--btcode 987654
--blelong
```

---

### **Debug & Logging**

| Command          | Parameters   | Description                            |
| ---------------- | ------------ | -------------------------------------- |
| `--debug`        | `on` / `off` | Enable/disable general debug output    |
| `--bledebug`     | `on` / `off` | Enable/disable BLE debug messages      |
| `--loradebug`    | `on` / `off` | Enable/disable LoRa radio debug        |
| `--gpsdebug`     | `on` / `off` | Enable/disable GPS debug output        |
| `--wxdebug`      | `on` / `off` | Enable/disable weather sensor debug    |
| `--softserdebug` | `on` / `off` | Enable/disable SoftSerial debug        |
| `--softserread`  | `on` / `off` | Show/hide SoftSerial received messages |

**Examples:**

```
--loradebug on
--gpsdebug on
--debug off
```

---

### **Serial & SoftSerial**

*Verifiziert: Namen außer `--softser test` und `--softser xml`, die im Code nicht vorkommen.*

SoftSerial provides a second serial port for external sensors.

| Command               | Parameters   | Description                   |
| --------------------- | ------------ | ----------------------------- |
| `--softser`           | `on` / `off` | Enable/disable SoftSerial     |
| `--softser send`      | -            | Send test data via SoftSerial |
| `--softser app`       | -            | Enable application mode       |
| `--softser test`      | -            | nicht verifiziert: im Firmware-Stand 4.40a nicht gefunden           |
| `--softser baud`      | `9600`       | Set SoftSerial baud rate      |
| `--softser rxpin`     | `16`         | Set SoftSerial RX GPIO pin    |
| `--softser txpin`     | `17`         | Set SoftSerial TX GPIO pin    |
| `--softser fixpegel`  | `100.5`      | Set fixed water level value   |
| `--softser fixpegel2` | `50.0`       | Set second fixed water level  |
| `--softser fixtemp`   | `20.5`       | Set fixed temperature value   |
| `--softser xml`       | -            | nicht verifiziert: im Firmware-Stand 4.40a nicht gefunden       |
| `--seset`             | -            | Show serial settings          |

**Examples:**

```
--softser on
--softser baud 9600
--softser rxpin 16
--softser txpin 17
```

---

### **Time & Date**

| Command     | Parameters            | Description                                   |
| ----------- | --------------------- | --------------------------------------------- |
| `--utcoff`  | `+2.0` / `-5.5`       | Set UTC offset in hours (supports half hours); löscht eine gesetzte TZ-Regel |
| `--settime` | `2025.02.14 15:30:00` | Set date and time manually                    |
| `--settz`   | POSIX-TZ-Regel        | Zeitzonenregel des Knotens (Backend: `ble_session.py`, `--settz <rule>`); von `--utcoff` gelöscht |
| `--setrtc`  | `value`               | Set RTC register value                        |

**Examples:**

```
--utcoff +1.0
--settime 2025.02.14 12:00:00
```

---

### **Advanced Settings**

*Verifiziert gegen 4.40a. `--compress` ist entfernt: der Befehl kommt im Firmware-Code nicht vor.*

| Command       | Parameters   | Description                                 |
| ------------- | ------------ | ------------------------------------------- |
| `--setinfo`   | `on` / `off` | Include system info in transmitted messages |
| `--setcont`   | `on` / `off` | Enable/disable continuous transmission mode |
| `--setretx`   | `on` / `off` | Enable/disable automatic retransmission     |
| `--shortpath` | `on` / `off` | Use shortened path format in messages       |
| `--passwd`    | `password`   | Set system password                         |
| `--regex`     | -            | Show regex pattern configuration            |
| `--nodeset`   | -            | Show node configuration summary             |
| `--conffin`   | -            | Mark configuration as finished              |

**Examples:**

```
--setinfo on
--shortpath on
--nodeset
```

---

### **Firmware 4.40a: weitere Befehle aus `--help`**

Befehle, die 4.40a kennt, in keiner Tabelle oben stehen und im `--help`-Text der Firmware (`command_functions.cpp`) vorkommen. Beschreibung und Argumente sind der Hilfetext, nicht einzeln gegen die Ausführung geprüft.

| Command | Description |
| ------- | ----------- |
| `--id` | board identity (env, hwid, build tag) |
| `--neighbours` | neighbour matrix (alias --nbr) |
| `--msgid` | message-id counter |
| `--wifi on/off` | WiFi radio (T-Deck, saved) |
| `--ble on/off` | BLE advertising (T-Deck, saved) |
| `--wifistat` | WiFi link/counters |
| `--udpstat` | MeshCom UDP RX/TX counters |
| `--ethstat` | Ethernet link/counters |
| `--dumpsettings` | dump raw settings store |
| `--deepsleep` | enter deep sleep |
| `--cleanflash` | wipe settings flash (recovery) |
| `--dfu` | reboot into UF2 bootloader |
| `--ota-update` | Neustart in Safeboot-OTA |
| `--autoupdate notify` | firmware update mode (ESP32) |
| `--autoupdate status` | mode and planned install |
| `--autoupdate delay 0-7` | planned install: days after the release (default 3) |
| `--autoupdate day off\|mon..sun` | planned install on a weekday (replaces the delay) |
| `--autoupdate time HH:MM` | planned install time (default 03:00); shown only, nothing installs by itself |
| `--updchan <kanal>` | Update-Kanal (Werte siehe Firmware) |
| `--update check/install/status/apply` | firmware update now (ESP32) |
| `--store off/own/list/heard` | Store-Node-Modus (ohne Wert: Postfachstatus) |
| `--storecall <liste>` / `none` | Store-Node-Rufzeichenliste (Modus list) |
| `--storetime 1-168` | store-node hold hours (no value: show) |
| `--storeslots 1-50` | store-node mailbox slots (no value: show) |
| `--storenotice on/off` | sender-visible custody notice |
| `--stor on/off` | announce mailbox calls to the server (STOR, default off) |
| `--mbox` | store-node mailbox contents |
| `--blockcall <list>/none` | blocked calls, dropped and not relayed (CALL = all SSIDs) |
| `--remotemgmt on/off` | remote management via LoRa (RM1, needs --passwd) |
| `--rmstrictsecurity on/off` | RM-Brute-Force-Schutz (Standard aus): Sperre pro Absender |
| `--relay on/off` | board relay output (GPIO) |
| `--nbrreset` | clear matrix |
| `--nbrcheck` | consistency check |
| `--nbrrelay off/count/on` | Relay-Entscheidung: aus / nur protokollieren / aktiv |
| `--nbrsym on/off` | assume symmetric links |
| `--aprscomment <t>/none` | comment (alias --atxt) |
| `--aprsmc <call>` | APRS destination call (default APRSMC) |
| `--setublox <cmd>` | u-blox GPS passthrough |
| `--setl76k <cmd>` | L76K GPS passthrough |
| `--setownntp a.b.c.d` | NTP server |
| `--netmode wifi/eth` | network interface |
| `--setnet auto/inet/hamnet` | gateway network (auto: Hamnet if own IP is 44.x) |
| `--setnet` | show network mode |
| `--nopmother on/off` | no foreign DMs to EXTUDP peer |
| `--udplog on/off` | one [UDP] line per datagram |
| `--uv on/off` | UV sensor |
| `--wind on/off` | anemometer |
| `--windpin 99` | anemometer GPIO (ESP32) |
| `--windcal 2.4` | km/h per Hz (ESP32) |
| `--gpiocontrol on/off` | free GPIO control master (ESP32) |
| `--gpio list` | free GPIOs and levels |
| `--gpio 13 on/off/in/release` | drive, read, free a pin |
| `--dht 11` / `22` | 1-Wire DHT-Typ |
| `--contrast 1-255` | OLED contrast |
| `--rotate 0/90/180/270` | e-ink rotation |
| `--mute on/off` | sound off |
| `--persistflash on/off` | positions to flash |
| `--persistsd on/off` | positions to SD |
| `--immediatesave on/off` | save at once |
| `--persiststat` | NVS-only values |
| `--keylock on/off` | SYM+K lock |
| `--bl` | backlight state |
| `--bl auto on/off` | day/night switch |
| `--bl mode auto\|day\|night` | display mode |
| `--bl day\|night 1-16` | saved level |
| `--theme auto\|dark\|light` | display theme |
| `--chatfont s\|m\|l` | chat text 12/14/16 px |
| `--charge led on/off` | charge indicator LED |
| `--txcapture on/off` | TX capture |
| `--nbrdebug on/off` | neighbour debug |
| `--viadebug on/off` | via debug |
| `--gpsfilter on/off` | GPS position filter (not saved) |
| `--setlog on/off/<call>` | log one callsign |
| `--pingcall <call>` | ping target |
| `--pingtime 99` | ping interval s (fixed in release builds) |
| `--pingmax 99/max` | ping count limit (fixed in release builds) |
| `--ping start/stop` | start/stop pinging |
| `--spiffs reset` | format SPIFFS |
| `--injectmsg <grp\|call> <text>` | queue a text as if received via LoRa |
| `--injectraw <hex>` | feed a raw frame through the real RX path (decodeAPRS/dedup/relay) |
| `--loratx <n> <ms>` | queue n test TX frames (max 20) at ms intervals (min 100) |
| `--redrawlog on/off/refr/obj, --uistat, --tab list/<n>, --drawer on/off, --playtone start/msg/<file>, --tft on/off/state, --screencrc` | (siehe Firmware) |
| `--spitrace on/off, --touch tap <x> <y> [ms] / down <x> <y> / up` | (siehe Firmware) |
| `--ntpsync` | request an immediate NTP refresh now |
| `--battprobe [n]` | raw battery divider ADC capture, n cycles 1-10 (~7.5 s each) |
| `--udphold [status\|outage on\|off]` | server outage hold ring, outage = behave as if the server were dead |
| `--oledtext [-10] [-t<s>] <text>` | show UTF-8 test text on the OLED (fold + fonts as messages; -10 = 10x20; held 60 s or -t<s>), no TX |
| `--clockstat, --timesrc all\|cet\|none, --oledshot, --tdshot` | CLK-01 display-clock instrumentation (RAM only; --tdshot T-Deck only) |

Ohne Hilfetext-Eintrag in 4.40a (Name nur über die Befehlsprüfung belegt, nicht näher beschrieben): `airgap`, `audiodbg`, `autest`, `ball`, `balledge`, `balledges`, `blink`, `btn`, `disptest`, `drawer`, `ethdrop`, `ethmtu`, `flashpoke`, `flushfix`, `framedump`, `heap`, `heapeat`, `heaplog`, `injectpos`, `instr`, `instreset`, `invalidate`, `key`, `mapzoom`, `nbr`, `oledlog`, `oledshot`, `oledstat`, `operatorname`, `playtone`, `reflush`, `rxlatch`, `screencrc`, `scroll`, `sdtest`, `srvip`, `tab`, `tdpattern`, `tdshot`, `tft`, `timesrc`, `touch`, `uistat`, `wifidrop`, `wifips`.

---

## Complete BLE Command Example

*Nicht verifiziert: Beispielcode wurde nicht ausgeführt. Die Längenlogik (`len + 2`) stimmt mit `ble_types._frame()` überein.*

### Python Example (using `bleak` library)

```python
import asyncio
from bleak import BleakClient, BleakScanner

MESHCOM_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
TX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # Write to device


async def send_command(device_address, command):
    """Send a command to MeshCom device via BLE"""
    async with BleakClient(device_address) as client:
        # Prepare 0xA0 message
        cmd_bytes = command.encode("utf-8")
        length = len(cmd_bytes) + 2
        message = bytes([length, 0xA0]) + cmd_bytes

        # Send via BLE
        await client.write_gatt_char(TX_CHAR_UUID, message)
        print(f"Sent: {command}")


# Usage
asyncio.run(send_command("AA:BB:CC:DD:EE:FF", "--info"))
asyncio.run(send_command("AA:BB:CC:DD:EE:FF", "--sendpos"))
```

### Arduino/ESP32 Example

```cpp
#include <BLEDevice.h>

void sendBLECommand(BLEClient* pClient, const char* command) {
    BLERemoteCharacteristic* pTxChar = /* get TX characteristic */;

    uint8_t length = strlen(command) + 2;
    uint8_t message[length];
    message[0] = length;
    message[1] = 0xA0;
    memcpy(message + 2, command, strlen(command));

    pTxChar->writeValue(message, length);
}

// Usage
sendBLECommand(pClient, "--info");
sendBLECommand(pClient, "--sendpos");
```

---

## Message Flow

*Funktionsnamen und Zeilen nach DEV-Main 4.40a korrigiert. Der Ablauf selbst ist nicht verifiziert.*

```
Phone App
   ↓
[Length][0xA0][--command]
   ↓
BLE UART Service (NimBLE)
   ↓
readPhoneCommand() → phone_commands.cpp:233
   ↓
commandAction() → command_functions.cpp:1088
   ↓
Execute command & set flags
   ↓
Main loop processes flags
   ↓
Response sent back via BLE (if applicable)
```

---

## Important Notes

1. **Command Parsing:** Commands starting with `--` are treated as system commands; others are sent as APRS text messages to the mesh network.

2. **Case Sensitivity:** The command name is compared case-insensitively (`command_match.h`, `casecmp`/`tolower`). The `--` prefix is checked literally. A name without argument must match the whole token; a name ending in a space takes the rest of the line as argument.

3. **BLE Length Limit:** The backend (`ble_session.py`, `_BLE_MTU_LIMIT = 247`) rejects a callsign or WiFi frame over 247 bytes with `ValueError`; it does not truncate. The firmware receive buffer is `MAX_MSG_LEN_PHONE` (300). Whether the firmware truncates long text was not verified.

4. **Settings Persistence:** Toggles (`--setcont on/off`) with the save flag call `save_settings()` right after they apply (`command_functions.cpp`, `tgact.save`). For other setters, `--save` or the `0xF0` message is needed to persist. Which setters save on their own was not verified in full.

5. **Hello Handshake:** The `0x10` hello sets `isPhoneReady = 1` (`phone_commands.cpp`). Only the BLE output paths check it. Incoming frames (`0x20`, `0x50`, `0xA0` and the others) are not blocked before hello in `readPhoneCommand()`. With a PIN set (`--btcode` ≠ 0), hello must carry the SHA-256 digest, otherwise the link is dropped (`ble_disconnect_requested`).

6. **Timestamp Sync:** Send `0x20` with UNIX timestamp to synchronize device clock (especially important for devices without GPS or RTC battery).

7. **One `0xA0` per loop pass:** The firmware keeps one text buffer (`textbuff_phone`). Two `0xA0` frames in the same main-loop pass lose all but the last. The backend waits `A0_MIN_GAP_S = 0.3` s between `0xA0` writes (`ble_types.py`). The binary frames are not delayed.

---

## Source Code References

- **BLE Protocol Handler:** `src/phone_commands.cpp:233-645` (`readPhoneCommand`)
- **Command Parser:** `src/command_functions.cpp:1088-8443` (`commandAction(char*, bool)`)
- **Command Check Function:** `commandCheck()` (`command_functions.cpp:297`) → `commandMatches()` in `src/command_match.h`: case-insensitive, exact-token rule
- **BLE Service:** NimBLE UART service (UUID: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E)

---

## Revision History

| Date       | Version | Changes                                                                                                                                                                                               |
| ---------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-02-14 | 1.0     | Initial documentation based on firmware 4.35k                                                                                                                                                         |
| 2026-02-14 | 1.1     | Added "Querying Device State (TYP Responses)" section with JSON query commands                                                                                                                        |
| 2026-02-14 | 1.2     | Complete register mapping: all 12 TYP registers (G, I, SN, SA, SE, S1, SW, S2, AN, TM, W, IO) with detailed field descriptions, multi-part response handling, and source code references              |
| 2026-09-25 | 1.3     | Added IS1 (build date, follows I) and SN1 (via-routing state, follows SN) — both new firmware registers making `--info` and `--nodeset` multi-part too, see PRs icssw-org/MeshCom-Firmware#1156/#1155 |

---

**End of Document**
