#!/usr/bin/env python3
"""
Centralized configuration for MCProxy.

Provides dataclass-based configuration with defaults and validation.
Supports environment variable overrides for deployment flexibility.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

VERSION = "v0.50.0"

logger = get_logger(__name__)


@dataclass
class UDPConfig:
    """UDP transport configuration."""

    port_listen: int = 1799
    port_send: int = 1799
    target: str = "localhost"


@dataclass
class WebSocketConfig:
    """WebSocket transport configuration."""

    host: str = "127.0.0.1"
    port: int = 2980


@dataclass
class SSEConfig:
    """Server-Sent Events transport configuration."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 2981


@dataclass
class BLEConfig:
    """Bluetooth Low Energy configuration."""

    # Mode: "local" (D-Bus/BlueZ), "remote" (HTTP/SSE), "disabled" (no-op)
    mode: str = "local"

    # Remote service settings (only used when mode="remote")
    remote_url: str = ""
    api_key: str = ""

    # Device settings
    device_name: str = ""  # Auto-connect device name (e.g., "MC-XXXXXX")
    device_address: str = ""  # Auto-connect device MAC address

    # GATT UUIDs (Nordic UART Service)
    read_uuid: str = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
    write_uuid: str = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
    hello_bytes: bytes = field(default_factory=lambda: b"\x04\x10\x20\x30")


@dataclass
class StorageConfig:
    """Message storage configuration."""

    backend: str = "memory"  # "memory" or "sqlite"
    db_path: str = "/var/lib/mcproxy/messages.db"
    max_size_mb: int = 10
    prune_hours: int = 168  # 7 days
    dump_file: str = "mcdump.json"


@dataclass
class LocationConfig:
    """Geographic location configuration.

    DEPRECATED: latitude/longitude are now obtained from the GPS device at runtime.
    Only station_name is used from config. LAT/LONG keys in config.json are ignored.
    """

    latitude: float | None = None  # deprecated — use GPS device
    longitude: float | None = None  # deprecated — use GPS device
    station_name: str = ""


@dataclass
class Config:
    """Main MCProxy configuration."""

    # Identity
    call_sign: str = ""
    user_info_text: str = ""

    # Transport configurations
    udp: UDPConfig = field(default_factory=UDPConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    sse: SSEConfig = field(default_factory=SSEConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)

    # Storage configuration
    storage: StorageConfig = field(default_factory=StorageConfig)

    # Location configuration
    location: LocationConfig = field(default_factory=LocationConfig)

    # Raw config for backward compatibility
    _raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """
        Load configuration from file.

        Args:
            path: Path to config file. If None, uses environment-based default.

        Returns:
            Config instance with loaded values.
        """
        if path is None:
            path = cls._get_default_path()

        path = Path(path)

        if not path.exists():
            logger.warning("Config file not found: %s, using defaults", path)
            return cls()

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        logger.info("Loaded config from %s", path)
        return cls._from_dict(data)

    @staticmethod
    def _get_default_path() -> Path:
        """Get default config path based on environment."""
        if os.getenv("MCADVCHAT_ENV") == "dev":
            logger.debug("DEV environment detected")
            return Path("/etc/mcadvchat/config.dev.json")
        return Path("/etc/mcadvchat/config.json")

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from dictionary (JSON data)."""
        # Map old config keys to new structure
        udp = UDPConfig(
            port_listen=data.get("UDP_PORT_list", 1799),
            port_send=data.get("UDP_PORT_send", 1799),
            target=data.get("UDP_TARGET", "localhost"),
        )

        websocket = WebSocketConfig(
            host=data.get("WS_HOST", "127.0.0.1"),
            port=data.get("WS_PORT", 2980),
        )

        sse = SSEConfig(
            enabled=data.get("SSE_ENABLED", False),
            host=data.get("SSE_HOST", "0.0.0.0"),
            port=data.get("SSE_PORT", 2981),
        )

        # BLE mode can also come from environment variable for easy override
        ble_mode = os.getenv("MCPROXY_BLE_MODE", data.get("BLE_MODE", "local"))
        ble_remote_url = os.getenv("MCPROXY_BLE_URL", data.get("BLE_REMOTE_URL", ""))
        ble_api_key = os.getenv("MCPROXY_BLE_API_KEY", data.get("BLE_API_KEY", ""))

        ble = BLEConfig(
            mode=ble_mode,
            remote_url=ble_remote_url,
            api_key=ble_api_key,
            device_name=data.get("BLE_DEVICE_NAME", ""),
            device_address=data.get("BLE_DEVICE_ADDRESS", ""),
            read_uuid=data.get("BLE_READ_UUID", "6e400003-b5a3-f393-e0a9-e50e24dcca9e"),
            write_uuid=data.get("BLE_WRITE_UUID", "6e400002-b5a3-f393-e0a9-e50e24dcca9e"),
            hello_bytes=bytes.fromhex(data.get("BLE_HELLO_BYTES", "04102030")),
        )

        storage = StorageConfig(
            backend=data.get("STORAGE_BACKEND", "memory"),
            db_path=data.get("DB_PATH", "/var/lib/mcproxy/messages.db"),
            max_size_mb=data.get("MAX_STORAGE_SIZE_MB", 10),
            prune_hours=data.get("PRUNE_HOURS", 168),
            dump_file=data.get("STORE_FILE_NAME", "mcdump.json"),
        )

        location = LocationConfig(
            latitude=data.get("LAT"),
            longitude=data.get("LONG"),
            station_name=data.get("STAT_NAME", ""),
        )

        return cls(
            call_sign=data.get("CALL_SIGN", ""),
            user_info_text=data.get("USER_INFO_TEXT", ""),
            udp=udp,
            websocket=websocket,
            sse=sse,
            ble=ble,
            storage=storage,
            location=location,
            _raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        """Export config to dictionary for saving."""
        return {
            "CALL_SIGN": self.call_sign,
            "USER_INFO_TEXT": self.user_info_text,
            "UDP_PORT_list": self.udp.port_listen,
            "UDP_PORT_send": self.udp.port_send,
            "UDP_TARGET": self.udp.target,
            "WS_HOST": self.websocket.host,
            "WS_PORT": self.websocket.port,
            "SSE_ENABLED": self.sse.enabled,
            "SSE_HOST": self.sse.host,
            "SSE_PORT": self.sse.port,
            "BLE_MODE": self.ble.mode,
            "BLE_REMOTE_URL": self.ble.remote_url,
            "BLE_API_KEY": self.ble.api_key,
            "BLE_DEVICE_NAME": self.ble.device_name,
            "BLE_DEVICE_ADDRESS": self.ble.device_address,
            "BLE_READ_UUID": self.ble.read_uuid,
            "BLE_WRITE_UUID": self.ble.write_uuid,
            "BLE_HELLO_BYTES": self.ble.hello_bytes.hex(),
            "STORAGE_BACKEND": self.storage.backend,
            "DB_PATH": self.storage.db_path,
            "MAX_STORAGE_SIZE_MB": self.storage.max_size_mb,
            "PRUNE_HOURS": self.storage.prune_hours,
            "STORE_FILE_NAME": self.storage.dump_file,
            "LAT": self.location.latitude,
            "LONG": self.location.longitude,
            "STAT_NAME": self.location.station_name,
        }

    def save(self, path: str | Path) -> None:
        """Save config to file."""
        path = Path(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info("Saved config to %s", path)

    def get_raw(self, key: str, default: Any = None) -> Any:
        """
        Get raw config value for backward compatibility.

        This allows gradual migration from old config access patterns.
        """
        return self._raw.get(key, default)


def hours_to_dd_hhmm(hours: int) -> str:
    """Convert hours to human-readable days/hours format."""
    days = hours // 24
    remainder_hours = hours % 24
    return f"{days:02d} day(s) {remainder_hours:02d}:00h"


# Singleton instance for easy access
_config: Config | None = None


def get_config(reload: bool = False) -> Config:
    """
    Get the global config instance.

    Args:
        reload: Force reload from file.

    Returns:
        Config instance.
    """
    global _config
    if _config is None or reload:
        _config = Config.load()
    return _config
