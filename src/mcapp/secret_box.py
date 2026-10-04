"""Install-bound encryption for secrets the backend must be able to read back.

The QRZ.com XML login needs the operator's password in plain text, so it can
only be encrypted, never hashed. The key that decrypts it is derived from two
inputs that do not travel together:

- an install key: 32 random bytes in `/var/lib/mcapp/secret.key` (mode 0600),
  generated on first use and never stored in the database;
- a board binding: the Raspberry Pi's SoC serial, which lives on the board and
  not on the SD card.

A copy of `messages.db` therefore yields only ciphertext, and a copy of the SD
card cannot decrypt on a different board. Code running as the service user on
the running box can do whatever the service does; no software-only scheme on a
Pi without a TPM changes that. Concept and threat model:
doc/2026-10-04_0848-qrz-callsign-lookup-plan.md §5.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .push_delivery import user_state_dir

logger = logging.getLogger(__name__)

SECRET_KEY_PATH = Path("/var/lib/mcapp/secret.key")
_KEY_BYTES = 32
_NONCE_BYTES = 12
_HKDF_INFO = b"mcapp-secret-box-v1"
_FORMAT_PREFIX = "v1:"

# Board serial first (on the SoC, survives an SD card swap the other way round),
# machine-id only as a dev-machine fallback: it lives on the SD card and so
# binds without protecting against an image copy.
_BOARD_SERIAL_SOURCES = (
    Path("/sys/firmware/devicetree/base/serial-number"),
    Path("/proc/cpuinfo"),
)
_MACHINE_ID_PATH = Path("/etc/machine-id")


class SecretBoxError(Exception):
    """A secret could not be decrypted: wrong board, lost key file, or tampering."""


def secret_key_path() -> Path:
    """Where the install key lives; resolved per call like `vapid_path()`.

    `MCAPP_SECRET_KEY_PATH` wins, a dev environment uses the per-user state
    dir, everything else `/var/lib/mcapp` (the systemd StateDirectory).
    """
    override = os.getenv("MCAPP_SECRET_KEY_PATH")
    if override:
        return Path(override)
    if os.getenv("MCAPP_ENV") == "dev":
        return user_state_dir() / SECRET_KEY_PATH.name
    return SECRET_KEY_PATH


def _read_board_serial(sources: tuple[Path, ...] = _BOARD_SERIAL_SOURCES) -> str:
    for source in sources:
        try:
            raw = source.read_bytes()
        except OSError:
            continue
        if source.name == "cpuinfo":
            for line in raw.decode("utf-8", errors="replace").splitlines():
                key, _, value = line.partition(":")
                if key.strip().lower() == "serial" and value.strip().strip("0"):
                    return value.strip()
            continue
        serial = raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()
        if serial.strip("0"):
            return serial
    return ""


def board_binding(
    sources: tuple[Path, ...] = _BOARD_SERIAL_SOURCES,
    machine_id_path: Path = _MACHINE_ID_PATH,
) -> bytes:
    """The machine-specific salt mixed into the key derivation.

    Empty when the host has neither a board serial nor a machine-id; the
    install key then stands alone, which is logged once by `SecretBox`.
    """
    serial = _read_board_serial(sources)
    if serial:
        return f"board:{serial}".encode()
    try:
        machine_id = machine_id_path.read_text(encoding="ascii").strip()
    except OSError:
        machine_id = ""
    return f"machine-id:{machine_id}".encode() if machine_id else b""


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        raw = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
        if len(raw) != _KEY_BYTES:
            msg = f"install key at {path} has {len(raw)} bytes, expected {_KEY_BYTES}"
            raise SecretBoxError(msg)
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
        return raw
    key = secrets.token_bytes(_KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL + 0600 at creation: the key is never world-readable, not even for
    # the instant between write and chmod.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(base64.b64encode(key).decode("ascii") + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    logger.info("Generated install secret key at %s", path)
    return key


class SecretBox:
    """AES-256-GCM over a key derived from the install key and the board.

    `associated_data` binds a ciphertext to its purpose (and, for QRZ, to the
    username), so a stored blob cannot be replayed under another one.
    """

    def __init__(self, key_path: Path | None = None, binding: bytes | None = None) -> None:
        self._key_path = key_path if key_path is not None else secret_key_path()
        self._binding = binding if binding is not None else board_binding()
        if not self._binding:
            logger.warning(
                "No board serial or machine-id found; secrets are bound to %s alone",
                self._key_path,
            )
        self._aead: AESGCM | None = None

    def _cipher(self) -> AESGCM:
        if self._aead is None:
            install_key = _load_or_create_key(self._key_path)
            kek = HKDF(
                algorithm=hashes.SHA256(),
                length=_KEY_BYTES,
                salt=self._binding or None,
                info=_HKDF_INFO,
            ).derive(install_key)
            self._aead = AESGCM(kek)
        return self._aead

    def encrypt(self, plaintext: str, associated_data: str) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = self._cipher().encrypt(nonce, plaintext.encode(), associated_data.encode())
        return _FORMAT_PREFIX + base64.b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, token: str, associated_data: str) -> str:
        if not token.startswith(_FORMAT_PREFIX):
            msg = "unknown secret format"
            raise SecretBoxError(msg)
        try:
            blob = base64.b64decode(token[len(_FORMAT_PREFIX) :], validate=True)
            cipher = self._cipher()
        except (ValueError, OSError) as exc:
            raise SecretBoxError(str(exc)) from exc
        nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            return cipher.decrypt(nonce, sealed, associated_data.encode()).decode()
        except InvalidTag as exc:
            msg = "secret cannot be decrypted with this install key and board"
            raise SecretBoxError(msg) from exc
