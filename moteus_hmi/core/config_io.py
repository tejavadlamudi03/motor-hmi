"""moteus configuration: parse, diff, signed backup and restore files.

Backup files stay compatible with ``moteus_tool --restore-config``
(``key value`` lines, ``#`` comments), and additionally carry an
HMAC-SHA256 signature made with the installation key so the HMI can
detect files that were altered after export.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

# Keys that break communication or change the motor model if restored
# blindly.  They are excluded from restores unless explicitly selected.
COMMS_KEYS = ("id.id", "can.prefix")
SENSITIVE_PREFIXES = ("id.", "can.", "motor.", "motor_position.", "servo.max_current_A",
                      "servo.max_voltage", "servo.max_power_W", "servo.override_board_max_power",
                      "servo.pwm_rate_hz", "drv8323_conf.", "aux1.", "aux2.")


_KEY_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,96}$")
_VAL_RE = re.compile(r"^[-+0-9A-Za-z_.]{1,64}$")


def validate_kv(key: str, value: str) -> str | None:
    """Reject anything that is not a plain token.  The diagnostic channel is
    line oriented, so a value containing whitespace or a newline could
    smuggle a second command (e.g. a motion command) onto the controller."""
    if not _KEY_RE.match(key or ""):
        return f"invalid parameter name {key!r}"
    if not _VAL_RE.match(value or ""):
        return f"invalid value {value!r} for {key}"
    return None


def is_sensitive(key: str) -> bool:
    return any(key == p or key.startswith(p) for p in SENSITIVE_PREFIXES)


def parse_config_text(text: str) -> "OrderedDict[str, str]":
    out: "OrderedDict[str, str]" = OrderedDict()
    for line in text.splitlines():
        if "#" in line:
            line = line[: line.index("#")]
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key.startswith("conf") and value.startswith("set "):   # "conf set k v" style
            key, _, value = value[4:].partition(" ")
        out[key] = value.strip()
    return out


def _canon_number(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v.strip()
    if f != f:
        return "nan"
    return repr(round(f, 9))


def diff_configs(current: dict, new: dict) -> list[tuple[str, Optional[str], Optional[str]]]:
    """(key, current, new) for keys whose value differs or is missing."""
    rows = []
    for k, v in new.items():
        cur = current.get(k)
        if cur is None or _canon_number(cur) != _canon_number(v):
            rows.append((k, cur, v))
    return rows


class SignatureStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNSIGNED = "unsigned"


@dataclass
class BackupFile:
    config: "OrderedDict[str, str]"
    meta: dict
    status: SignatureStatus


def _body(config: dict) -> str:
    return "".join(f"{k} {v}\n" for k, v in config.items())


def write_backup(path: Path, config: dict, meta: dict, keystore) -> None:
    body = _body(config)
    header = {"created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **meta}
    header_lines = "".join(f"# {k}: {v}\n" for k, v in header.items())
    signed_part = header_lines + body
    mac = keystore.mac(signed_part.encode("utf-8"), "config-backup")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    text = ("# Motor HMI configuration backup (moteus_tool --restore-config compatible)\n"
            + header_lines + f"# sha256: {sha}\n" + f"# hmac-sha256: {mac}\n" + body)
    Path(path).write_text(text, encoding="utf-8")


def read_backup(path: Path, keystore) -> BackupFile:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    meta: dict = {}
    header_lines = []
    mac = None
    body_lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("# hmac-sha256:"):
            mac = line.split(":", 1)[1].strip()
        elif line.startswith("# sha256:") or line.startswith("# Motor HMI configuration backup"):
            continue
        elif line.startswith("# ") and ":" in line and not body_lines:
            header_lines.append(line)
            k, _, v = line[2:].partition(":")
            meta[k.strip()] = v.strip()
        elif not line.startswith("#"):
            body_lines.append(line)
    config = parse_config_text("".join(body_lines))
    if mac is None:
        status = SignatureStatus.UNSIGNED
    else:
        signed = "".join(header_lines) + _body(config)
        status = (SignatureStatus.VALID if keystore.verify(signed.encode("utf-8"), mac, "config-backup")
                  else SignatureStatus.INVALID)
    return BackupFile(config=config, meta=meta, status=status)
