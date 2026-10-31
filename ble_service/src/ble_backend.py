"""Backend factory: pick the BLE transport without importing the other one."""

import os
import sys
from typing import Any

from .ble_types import BleBackend


def create_backend(kind: str | None = None, **kwargs: Any) -> BleBackend:
    """Return a backend for `kind` (`bluez` | `bleak`).

    Default: `BLE_BACKEND` env, else `bleak` on macOS and `bluez` elsewhere. The chosen
    module is imported lazily, so a Pi never imports bleak and a Mac never dbus_next.
    """
    chosen = (kind or os.environ.get("BLE_BACKEND") or "").strip().lower()
    if not chosen:
        chosen = "bleak" if sys.platform == "darwin" else "bluez"
    if chosen == "bleak":
        from .ble_adapter_bleak import BleakAdapter  # noqa: PLC0415 - lazy by design

        return BleakAdapter(**kwargs)
    if chosen == "bluez":
        from . import ble_adapter  # noqa: PLC0415 - lazy by design

        # Any: BLEAdapter does not yet declare name/address_kind/capabilities (W4).
        bluez_cls: Any = ble_adapter.BLEAdapter
        return bluez_cls(**kwargs)  # type: ignore[no-any-return]  # Any-typed class call
    raise ValueError(f"unknown BLE backend {kind!r}: expected 'bluez' or 'bleak'")
