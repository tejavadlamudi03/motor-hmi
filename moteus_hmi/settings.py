"""Application settings, stored as JSON and MAC-signed with the install
key (safety limits live here, so silent edits must be detectable)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .core.safety import SafetyLimits, validate_limits
from .core.types import TransportConfig
from .paths import restrict_permissions


@dataclass
class AppSettings:
    transport: dict = field(default_factory=lambda: TransportConfig().to_dict())
    rate_hz: float = 100.0
    watchdog_s: float = 0.25
    comm_loss_cycles: int = 10
    estop_action: str = "stop"            # "stop" (torque off) | "brake"
    session_timeout_min: float = 10.0
    plot_window_s: float = 10.0
    display_units: str = "rev"            # rev | rad | deg
    default_limits: dict = field(default_factory=lambda: SafetyLimits().to_dict())
    motor_limits: dict = field(default_factory=dict)   # "id" -> limits dict
    filters: dict = field(default_factory=dict)        # signal -> chain specs
    approved_firmware_sha256: list = field(default_factory=list)

    def limits_for(self, motor_id: int) -> SafetyLimits:
        d = self.motor_limits.get(str(motor_id), self.default_limits)
        return SafetyLimits.from_dict(d)

    def transport_config(self) -> TransportConfig:
        return TransportConfig.from_dict(self.transport)


class SettingsStore:
    def __init__(self, path: Path, keystore):
        self.path = Path(path)
        self.ks = keystore
        self.tampered = False

    def _canon(self, payload: dict) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()

    def load(self) -> AppSettings:
        self.tampered = False
        if not self.path.exists():
            return AppSettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            payload, mac = data["payload"], data["mac"]
        except (ValueError, KeyError):
            self.tampered = True
            return AppSettings()
        if not self.ks.verify(self._canon(payload), mac, "settings"):
            self.tampered = True
            return AppSettings()
        s = AppSettings()
        for k, v in payload.items():
            if hasattr(s, k):
                setattr(s, k, v)
        # Never trust stored limits blindly.
        if validate_limits(SafetyLimits.from_dict(s.default_limits)):
            s.default_limits = SafetyLimits().to_dict()
        for k in list(s.motor_limits):
            if validate_limits(SafetyLimits.from_dict(s.motor_limits[k])):
                del s.motor_limits[k]
        return s

    def save(self, s: AppSettings) -> None:
        payload: dict[str, Any] = asdict(s)
        data = {"payload": payload, "mac": self.ks.mac(self._canon(payload), "settings")}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        restrict_permissions(tmp)
        os.replace(tmp, self.path)
