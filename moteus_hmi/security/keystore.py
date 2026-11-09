"""Per-installation secret used to MAC the user database, settings,
audit trail and configuration backups.

* Linux / macOS: 32 random bytes in ``security/install.key`` (mode 0600,
  directory 0700).
* Windows: the key is additionally wrapped with DPAPI
  (``CryptProtectData``, current-user scope) when pywin32 is available,
  so copying the file to another account or machine is useless.

Threat model (see docs/SECURITY.md): protects against casual or
accidental tampering by other OS users and against edits of HMI files
without the key.  It cannot protect against an attacker who already has
the same OS account as the operator - OS account security, disk
encryption and read-only install directories remain the trust anchor.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

from ..paths import restrict_permissions

_MAGIC_PLAIN = b"MHK1"
_MAGIC_DPAPI = b"MHK2"


def _dpapi():
    if sys.platform != "win32":
        return None
    try:
        import win32crypt  # type: ignore
        return win32crypt
    except Exception:
        return None


class KeyStore:
    def __init__(self, key_file: Path):
        self.key_file = Path(key_file)
        self._key: bytes | None = None

    @property
    def exists(self) -> bool:
        return self.key_file.exists()

    def _load_or_create(self) -> bytes:
        if self.key_file.exists():
            blob = self.key_file.read_bytes()
            magic, payload = blob[:4], blob[4:]
            if magic == _MAGIC_DPAPI:
                api = _dpapi()
                if api is None:
                    raise RuntimeError("Install key is DPAPI protected but pywin32 is unavailable")
                _, key = api.CryptUnprotectData(payload, None, None, None, 0)
                return bytes(key)
            if magic == _MAGIC_PLAIN and len(payload) == 32:
                return payload
            raise RuntimeError(f"Corrupt install key {self.key_file}")
        key = secrets.token_bytes(32)
        api = _dpapi()
        if api is not None:
            blob = _MAGIC_DPAPI + bytes(api.CryptProtectData(key, "MotorHMI", None, None, None, 0))
        else:
            blob = _MAGIC_PLAIN + key
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.key_file.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
        restrict_permissions(tmp)
        os.replace(tmp, self.key_file)
        restrict_permissions(self.key_file)
        return key

    def _root(self) -> bytes:
        if self._key is None:
            self._key = self._load_or_create()
        return self._key

    def subkey(self, purpose: str) -> bytes:
        """Domain-separated key so a MAC for one file type can never be
        replayed as a MAC for another."""
        return hmac.new(self._root(), b"moteus-hmi/" + purpose.encode(), hashlib.sha256).digest()

    def mac(self, data: bytes, purpose: str) -> str:
        return hmac.new(self.subkey(purpose), data, hashlib.sha256).hexdigest()

    def verify(self, data: bytes, mac_hex: str, purpose: str) -> bool:
        if not isinstance(mac_hex, str):
            return False
        return hmac.compare_digest(self.mac(data, purpose), mac_hex)
