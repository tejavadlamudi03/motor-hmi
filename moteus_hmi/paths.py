"""Per-user, per-OS storage locations.

Windows : %LOCALAPPDATA%\\MotorHMI
Linux   : $XDG_DATA_HOME/moteus-hmi  (default ~/.local/share/moteus-hmi)
macOS   : ~/Library/Application Support/MotorHMI

``MOTEUS_HMI_HOME`` overrides everything (portable installs, tests).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    override = os.environ.get("MOTEUS_HMI_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(root) / "MotorHMI"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MotorHMI"
    root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(root) / "moteus-hmi"


class AppPaths:
    """Resolves and creates every directory the HMI writes to."""

    def __init__(self, base: Path | None = None):
        self.base = Path(base) if base else _base_dir()
        self.security = self.base / "security"
        self.logs = self.base / "logs"
        self.recordings = self.base / "recordings"
        self.calibration = self.base / "calibration"
        self.backups = self.base / "config_backups"
        for d in (self.base, self.security, self.logs, self.recordings,
                  self.calibration, self.backups):
            d.mkdir(parents=True, exist_ok=True)
            restrict_permissions(d, directory=True)

    @property
    def settings_file(self) -> Path:
        return self.base / "settings.json"

    @property
    def users_file(self) -> Path:
        return self.security / "users.json"

    @property
    def recovery_file(self) -> Path:
        return self.security / "recovery.json"

    @property
    def key_file(self) -> Path:
        return self.security / "install.key"

    @property
    def audit_file(self) -> Path:
        return self.security / "audit.jsonl"

    @property
    def audit_anchor(self) -> Path:
        return self.security / "audit.anchor"

    @property
    def app_log(self) -> Path:
        return self.logs / "hmi.log"


def restrict_permissions(path: Path, directory: bool = False) -> None:
    """Best effort: make a file/dir readable only by the current OS user.

    On POSIX this is chmod 0600/0700.  On Windows the per-user
    %LOCALAPPDATA% folder is already ACL-protected to the user, so
    nothing extra is done here.
    """
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        pass
