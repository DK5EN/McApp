"""Pure utility functions for decoding LoRa/hardware data."""


def decode_lora_modulation(lora_mod):
    """Decode LoRa modulation value to readable format"""
    mod_map = {
        136: "EU8",
        # Add other mappings as needed
        # 137: "EU9", etc.
    }
    return mod_map.get(lora_mod, f"Mod{lora_mod}")


def decode_hardware_id(hw_id):
    """Decode hardware ID to readable format"""
    hw_map = {
        1: "TLoRa_V2",
        2: "TLoRa_V1",
        3: "TLora_V2_1_1p6",
        4: "TBeam",
        5: "TBeam_1268",
        6: "TBeam_0p7",
        7: "T_Echo",
        8: "T_Deck",
        9: "RAK_4631",
        10: "Heltec_V2_1",
        11: "Heltec_V1",
        12: "T-Beam_APX2101",
        39: "E22",
        43: "Heltec_V3",
        44: "Heltec_E290",
        45: "TBeam_1262",
        46: "T_Deck_Plus",
        47: "T-Beam_Supreme",
        48: "ESP32_S3_EByte_E22",
    }
    return hw_map.get(hw_id, f"HW{hw_id}")


def decode_maidenhead(lat, lon):
    """Convert lat/lon to Maidenhead locator"""
    lon180 = lon + 180
    lat90 = lat + 90

    A = int((lon180) / 20)
    B = int((lat90) / 10)

    C = int(((lon180) % 20) / 2)
    D = int((lat90) % 10)

    E = int(((lon180) % 2) * 12)
    F = int(((lat90) % 1) * 24)

    locator = (
        f"{chr(A + ord('A'))}{chr(B + ord('A'))}{C}{D}{chr(E + ord('a'))}{chr(F + ord('a'))}"
    )

    return locator
