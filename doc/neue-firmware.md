# MeshCom Firmware 4.35m — Community-Feedback (22.–24. Feb 2026)

Analyse der Chat-Nachrichten aus dem MeshCom-Netz über die McApp-Produktion.

## 1. Stimmung zur neuen Firmware

Die Stimmung ist **gemischt, aber überwiegend konstruktiv**. Viele OMs testen aktiv, berichten sowohl Probleme als auch Erfolge. Die Grundstimmung ist positiv — man erkennt die Verbesserungen an, akzeptiert aber auch die anfänglichen Kinderkrankheiten einer neuen Version. OE1KBC (der Firmware-Entwickler) reagiert schnell mit Hinweisen und eine Lösung für das Hauptproblem (Display) wird zeitnah kommuniziert.

### Positive Stimmen

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 08:13 | DJ8MEH-8 | "Guten Morgen aus Trostberg! Die Meshcom-Firmware 4.35m funktioniert hier im Test sehr gut. vy 73 de Helmut / DJ8MEH" |
| 23.02 13:14 | DJ8MEH-8 | "ABER wesentlich weniger ``Kollisionen``, dafür verzichte ich gerne auf das Display!" |
| 23.02 14:17 | DK8VW-99 | "hab gerade die 4.35m geupdated. Scheint zu funktionieren" |
| 23.02 19:11 | DJ8MEH-46 | "Merci für den Tipp - Problem kurz und schmerzlos gelöst!" (Display-Fix) |
| 23.02 22:28 | OE5HWN-12 | "hab alles auf 4.35m geflasht mit erase und keine Probleme" |
| 23.02 22:35 | OE5HWN-12 | "Ja habe heute wieder Update gemacht läuft alles Rund" |
| 24.02 14:38 | DK8VW-22 | "So, upgrade OTA ohne Erase und das Display am Heltec geht, puh!" |
| 24.02 15:07 | DK3ACH-11 | "Update auf v1.6.0 erfolgreich durchgeführt" |

### Zurückhaltende/Negative Stimmen

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 22:18 | DB4UW-12 | "Neu Version m. Aber Bugs. Ich habe wieder V k geflashed. Jetzt geht wieder alles, wie es soll." |
| 23.02 13:06 | OE6ATD-1 | "TBEAM AXP2101 nach 4.35m Display keine Reaktion, TLora T3_V1.6.1 ebenfalls - retour auf 4.35k 😞" |
| 23.02 13:52 | OE1EZA-12 | "downgrading 4.35k display on again" |

---

## 2. Gemeldete Probleme

### Problem 1: Display bleibt dunkel (Hauptproblem)

**Ursache:** Das Memory-Modell wurde in 4.35m modernisiert. Ohne Flash-Erase werden alte Memory-Layouts übernommen, was zu Display-Ausfällen führt.

**Betroffene Hardware:** T-Beam, T-LoRa, E22/433, Heltec V3 (teilweise)
**Nicht betroffen:** T-Beam Supreme, T-Beam AXP2101 V1.2, Heltec V3 (bei manchen)

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 00:05 | DG7FDL-12 | "Neue v4.35m für T-LoRa mit BME280: Kein Display! Schon seit v4.35.02.19!" |
| 23.02 12:46 | DK8JP-12 | "mein T-Lora board funktioniert mit Version m nicht, zurück auf 'k' alles wieder ok!" |
| 23.02 12:49 | DD3JN-12 | "@DK8JP-12 mein T-Lora funktioniert auch nicht. Display bleibt dunkel mit Version m" |
| 23.02 13:06 | OE6ATD-1 | "TBEAM AXP2101 nach 4.35m Display keine Reaktion, TLora T3_V1.6.1 ebenfalls - retour auf 4.35k 😞" |
| 23.02 13:17 | DF4ND-99 | "Auch mein E22/433 mit der 4.35m ist das Display aus." |
| 23.02 13:43 | OE1EZA-12 | "no display on t-beam after flashibg 4.35m" |
| 23.02 13:54 | OE1EZA-12 | "4.35m the display uder button in the app also does not work, no change after click" |
| 23.02 13:56 | OE1EZA-12 | "I have the \"normal\" T-Beam w/o suffixes, 4.35m display: dead" |
| 23.02 14:00 | DF3LZ-12 | "same here with 4.35m, display dead" |
| 23.02 18:28 | DL1HCI-98 | "Frage in die Runde: Bei ttgo_beam Update auf 4.35m bleibt Display dauerhaft aus. Lässt sich weder per App noch per Command einschalten?!" |
| 23.02 18:30 | DN9EL-7 | "ich lese mal mit, habe dass gleiche \"Problem\"" |
| 23.02 18:36 | DF4ND-99 | "Mein E22 hat das gleiche Problem." |

**Lösung gefunden:**

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 18:45 | OE3WAS | "wichtig! --contrast 255 --display on und alles ist wieder gut" |
| 23.02 19:09 | DL1HCI-99 | "Display Problem gelöst: --contrast 255 --display on und alles ist wieder gut (Tipp von OE3WAS)" |
| 23.02 19:17 | OE1KBC-1 | "WICHTIG! Version 4.35m muss einmal mit ERASE aufgespielt werden. Memory-Modell wurde modernisiert. 73 de OE1KBC" |
| 23.02 19:17 | OE1KBC-1 | "Sonst kommt es zu Display Fehlern usw." |
| 23.02 19:50 | OE5HWN-12 | "ich weiß nich ob ihr es gelesen habt bei der Umstellung auf 4.35m muß mit flash erase geflasht werden, steht jetzt auch auf esptool.oevsv.at" |

**Display OK ohne Erase (Hardware-abhängig):**

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 13:41 | OE5HWN-14 | "Meldung vom T-Beam Supreme FW 4.35m Display okay" |
| 23.02 13:54 | DF9ON-1 | "TBEAM_AXP2101 (V1.2) 1.3`` OLED SSD1306 Display ok..." |
| 23.02 19:20 | DL3YCW-12 | "Auf meinem Heltec V3 läuft es einwandfrei" |
| 24.02 14:38 | DK8VW-22 | "So, upgrade OTA ohne Erase und das Display am Heltec geht, puh!" |

### Problem 2: APRS.fi Einträge fehlen

Mehrere Stationen berichten, dass seit dem Update keine Positionsmeldungen mehr bei APRS.fi ankommen.

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 16:25 | DK9MS-12 | "Mir fällt auf, dass seit meinem Update auf 4.35 k das Meshcom Icon und Statusmeldung NICHT mehr bei APRS.fi angezeigt wird…" |
| 23.02 16:27 | DB4UW-12 | "Du meinst 4.35 m. Habe auch kein APRS Eintrag mehr" |
| 23.02 16:30 | DK9MS-12 | "bei mir ist seit ca 16 Stunden keine Statusmeldung mehr bei APRS.fi gekommen" |
| 23.02 16:33 | DB4UW-12 | "Auch mein letzter APRS Eintrag vom iGate ist von gestern Abend 22:17." |
| 23.02 16:34 | DB4UW-12 | "Auch mein Portabel-Node zeigt keinen APRS Standort an." |
| 23.02 16:35 | DK9MS-12 | "hab schon alles probiert, auch wenn ich händisch eine Bake setze, ist bei APRS.fi nix zu sehen" |
| 23.02 16:47 | DK5EN-99 | "mein -12 ist auf dem DL Server, der -99 ist am OE Server angemeldet. Und beide gehen, hab die Pakete mal verfolgt auf APRS.fi -sehe aber auch Lücken" |
| 23.02 17:41 | DK9MS-12 | "jetzt sehe ich mein Call wieder beim APRS.fi, habe mich in den OE Server eingeloggt" |

**Fazit:** Das Problem scheint DL-Server-spezifisch gewesen zu sein, nicht direkt firmware-bedingt. Wechsel auf den OE-Server löste es für DK9MS.

### Problem 3: Kommandos funktionieren nicht

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 18:43 | DB4UW-12 | "Kann es sein, daß es ein paar Bugs gibt? Die Kommandos gehen auch nicht bei mir und sind als Nachricht sichtbar." |

### Problem 4: WiFi / WebServer kritisch bei fehlender Erase

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 19:52 | OE1KBC-24 | "Display, WiFi, Messfühler - Fehler" |
| 23.02 19:55 | OE1KBC-24 | "WEBServer / WiFi ist besonders kritisch aber jetzt weist Du ja wenn es feigelt dann ERASE" |

### Problem 5: App-Verbindungsprobleme (Cloud-Signal)

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 24.02 08:06 | OE1EZA-12 | "no cloud signal here in the app" |
| 24.02 08:33 | OE1EZA-12 | "habe kein Wölkchen um den grünen Haxl, und die nsg die ich im telegram sehe kommen in der app nicht an. komischerweise geht mein nsg aus ins telegram." |
| 24.02 08:53 | OE1EZA-12 | "jetzt ist wölkchen da" (nach Restart gelöst) |

---

## 3. Verbesserungen die bemerkt werden

### Weniger Kollisionen / besserer RX/TX

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 08:13 | DJ8MEH-8 | "Die Meshcom-Firmware 4.35m funktioniert hier im Test sehr gut." |
| 23.02 13:14 | DJ8MEH-8 | "wesentlich weniger ``Kollisionen``, dafür verzichte ich gerne auf das Display!" |
| 23.02 22:59 | OE5HWN-12 | "in der Firmware sind Änderungen im rx und tx damit weniger Nachrichten verschwinden also nicht durchkommen" |

### ACK-Zuverlässigkeit verbessert

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 22.02 18:17 | DK5EN-99 | "zweimal problemlos" (ACK-Test mit OE5HWN-12) |
| 23.02 19:16 | OE5HWN-12 | "bis jetzt immer ein ACK bekommen, einmal etwas verzögert aber gekommen" |
| 23.02 19:16 | DK5EN-99 | "ja, ACK kommt jetzt gut rein. Wir sind aber über Server verbunden - da kommt das ACK von der Zentrale" |

### Erklärung der Firmware-Änderungen veröffentlicht

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 10:55 | DJ8MEH-8 | "Eine sehr gute Erklärung von OM Martin, DK5EN: https://github.com/icssw-org/MeshCom-Firmware/pull/709" |

---

## 4. Paketverlust / Empfangsprobleme

### Grundsätzliche Verbesserung

Die Firmware 4.35m zielt explizit darauf ab, Paketverlust zu reduzieren:

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 23.02 22:59 | OE5HWN-12 | "in der Firmware sind Änderungen im rx und tx damit weniger Nachrichten verschwinden also nicht durchkommen" |

### CtC-Ping Tests (DK5EN, 23.02 ab 07:57)

Umfangreiche automatisierte Tests (TT14/TT15) zwischen DK5EN-12 und DK5EN-99 über mehrere Stunden. Ergebnis: Hohe Zuverlässigkeit — fast alle Pakete wurden bestätigt. Vereinzelt doppelte Zustellungen, aber keine erkennbaren Verluste.

### Signalwerte (positiv)

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 22.02 18:33 | DG3DJ-12 | "DO2FJ-12: Empfang mit -127 dB. 73 Andreas DG3DJ" |
| 23.02 22:53 | OE5HWN-12 | "21,3 Km RSSI -106 SNR -20 das geht noch gut" |

### Vereinzelte Empfangsprobleme

| Datum/Uhrzeit | Callsign | Nachricht |
|---|---|---|
| 22.02 22:47 | OE5HWN-62 | "es kann aber sein das dein Node das nicht empfangen hat" (zu DK3ACH Time-Sync Problem) |
| 24.02 08:06 | OE1EZA-12 | "no cloud signal here in the app" |

**Keine systematischen Paketverlust-Meldungen** — im Gegenteil: die neue Firmware wird explizit für weniger Verluste gelobt.

---

## 5. Zusammenfassung

| Thema | Status |
|---|---|
| **Display dunkel** | Hauptproblem — gelöst durch Flash-Erase oder `--contrast 255 --display on` |
| **APRS.fi-Ausfall** | Vermutlich DL-Server-Problem, nicht firmware-bedingt |
| **WiFi/WebServer** | Kritisch ohne Erase — laut OE1KBC durch Erase behoben |
| **Weniger Kollisionen** | Bestätigt von DJ8MEH und OE5HWN |
| **Bessere RX/TX** | Änderungen im Code, weniger Nachrichten gehen verloren |
| **ACK-Zuverlässigkeit** | Verbessert, bestätigt durch Tests |
| **Paketverlust** | Keine negativen Meldungen — Verbesserung gegenüber vorher |

**Nächste Version:** 4.35n bringt T-Beam 1 Watt Support (OE1KBC-33, 24.02 15:47)

**Empfehlung von OE1KBC:** Bei Update auf 4.35m **immer mit Flash-Erase** flashen — Memory-Modell wurde modernisiert.
