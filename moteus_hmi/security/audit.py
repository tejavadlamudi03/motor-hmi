"""Tamper-evident audit trail.

Each JSON line contains the HMAC of the previous entry (hash chain) and
its own HMAC made with the install key.  Editing, re-ordering or deleting
an entry in the middle breaks the chain; truncating the tail is detected
through a separately MAC'd anchor holding the last sequence number.

Every safety- or quality-relevant action is recorded: logins, failed
logins, connects, arm/disarm, motion commands, E-stops, trips, resets,
calibration, configuration writes, limit and filter changes, user
management.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..paths import restrict_permissions
from .keystore import KeyStore

GENESIS = "0" * 64


def _canon(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str).encode()


@dataclass
class VerifyResult:
    ok: bool
    entries: int
    message: str
    first_bad_seq: Optional[int] = None


class AuditLog:
    def __init__(self, path: Path, anchor: Path, keystore: KeyStore):
        self.path = Path(path)
        self.anchor = Path(anchor)
        self.ks = keystore
        self._lock = threading.Lock()
        self._seq, self._prev = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with open(self.path, "rb") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        try:
            e = json.loads(last)
            return int(e["seq"]), e["mac"]
        except (ValueError, KeyError):
            return 0, GENESIS

    def _write_anchor(self) -> None:
        payload = {"seq": self._seq, "mac": self._prev}
        data = {"payload": payload, "anchor_mac": self.ks.mac(_canon(payload), "audit-anchor")}
        tmp = self.anchor.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        restrict_permissions(tmp)
        os.replace(tmp, self.anchor)

    def record(self, user: str, action: str, details: Any = None,
               outcome: str = "ok", role: str = "") -> None:
        with self._lock:
            entry = {
                "seq": self._seq + 1,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "user": user or "-",
                "role": role,
                "action": action,
                "outcome": outcome,
                "details": details if details is not None else "",
                "prev": self._prev,
            }
            entry["mac"] = self.ks.mac(_canon(entry), "audit")
            new_file = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            if new_file:
                restrict_permissions(self.path)
            self._seq, self._prev = entry["seq"], entry["mac"]
            self._write_anchor()

    def entries(self, limit: int = 500) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"seq": "?", "action": "UNPARSEABLE LINE", "details": line[:200]})
        return out

    def verify(self) -> VerifyResult:
        with self._lock:
            prev = GENESIS
            n = 0
            expected_seq = 1
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            e = json.loads(line)
                            mac = e.pop("mac")
                        except (ValueError, KeyError):
                            return VerifyResult(False, n, f"Unparseable entry after seq {n}", n + 1)
                        seq = e.get("seq")
                        if seq != expected_seq:
                            return VerifyResult(False, n, f"Sequence gap: expected {expected_seq}, got {seq}", expected_seq)
                        if e.get("prev") != prev:
                            return VerifyResult(False, n, f"Chain broken at seq {seq}", seq)
                        if not self.ks.verify(_canon(e), mac, "audit"):
                            return VerifyResult(False, n, f"Entry {seq} was modified", seq)
                        prev, n, expected_seq = mac, n + 1, expected_seq + 1
            if self.anchor.exists():
                try:
                    a = json.loads(self.anchor.read_text(encoding="utf-8"))
                    payload = a["payload"]
                    if not self.ks.verify(_canon(payload), a["anchor_mac"], "audit-anchor"):
                        return VerifyResult(False, n, "Audit anchor was modified", None)
                    if payload["seq"] != n or payload["mac"] != prev:
                        return VerifyResult(False, n, f"Audit trail truncated or replaced: anchor expects "
                                                      f"{payload['seq']} entries, found {n}", n + 1)
                except (ValueError, KeyError):
                    return VerifyResult(False, n, "Audit anchor unreadable", None)
            elif n > 0:
                return VerifyResult(False, n, "Audit anchor missing", None)
            return VerifyResult(True, n, f"Audit trail intact ({n} entries)")
