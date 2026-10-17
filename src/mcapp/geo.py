"""Geodesy helpers shared by the QRZ lookup and the weather formatter."""

_SUBSQUARES_PER_AXIS = 4320  # 18 fields x 10 squares x 24 subsquares (both axes)


def latlon_to_locator(lat: float, lon: float) -> str:
    """6-char Maidenhead locator (JN59il); +90 / +180 clamp to the last cell."""
    x = min(int((lon + 180.0) * 12), _SUBSQUARES_PER_AXIS - 1)
    y = min(int((lat + 90.0) * 24), _SUBSQUARES_PER_AXIS - 1)
    return (
        chr(65 + x // 240)
        + chr(65 + y // 240)
        + str((x // 24) % 10)
        + str((y // 24) % 10)
        + chr(97 + x % 24)
        + chr(97 + y % 24)
    )
