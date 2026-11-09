"""Runs the upstream ``moteus_tool`` (calibration, firmware flashing) as a
supervised child process and streams its output to the UI.

Why a child process: ``moteus_tool --calibrate`` is the procedure
maintained by mjbots; re-implementing it would drift from upstream.  The
HMI releases the CAN adapter, runs the tool, then re-acquires the bus
and immediately commands stop.

In a frozen (PyInstaller) build there is no ``python -m``; the HMI
executable re-launches itself with ``--moteus-tool``.  That passthrough
only runs when it receives a short-lived HMAC token issued by an
authenticated HMI session, so the EXE cannot be used to bypass login.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

TOKEN_ENV_NONCE = "MOTEUS_HMI_TOOL_NONCE"
TOKEN_ENV_MAC = "MOTEUS_HMI_TOOL_MAC"
TOKEN_TTL_S = 60


@dataclass
class CalibrationOptions:
    target: int = 1
    cal_motor_power: float = 7.5
    cal_bw_hz: float = 200.0
    encoder_bw_hz: Optional[float] = None
    cal_motor_poles: Optional[int] = None
    cal_force_kv: Optional[float] = None
    cal_hall: bool = False
    cal_invert: bool = False
    cal_no_update: bool = False
    cal_max_remainder: float = 0.1

    def to_args(self) -> list[str]:
        a = ["--target", str(int(self.target)), "--calibrate",
             "--cal-motor-power", f"{self.cal_motor_power:g}",
             "--cal-bw-hz", f"{self.cal_bw_hz:g}",
             "--cal-max-remainder", f"{self.cal_max_remainder:g}"]
        if self.encoder_bw_hz:
            a += ["--encoder-bw-hz", f"{self.encoder_bw_hz:g}"]
        if self.cal_motor_poles:
            a += ["--cal-motor-poles", str(int(self.cal_motor_poles))]
        if self.cal_force_kv:
            a += ["--cal-force-kv", f"{self.cal_force_kv:g}"]
        if self.cal_hall:
            a.append("--cal-hall")
        if self.cal_invert:
            a.append("--cal-invert")
        if self.cal_no_update:
            a.append("--cal-no-update")
        return a

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def issue_token(keystore) -> dict[str, str]:
    nonce = f"{int(time.time())}:{secrets.token_hex(16)}"
    return {TOKEN_ENV_NONCE: nonce, TOKEN_ENV_MAC: keystore.mac(nonce.encode(), "tool-passthrough")}


def verify_token(keystore, env=os.environ) -> bool:
    nonce, mac = env.get(TOKEN_ENV_NONCE, ""), env.get(TOKEN_ENV_MAC, "")
    if not nonce or not mac:
        return False
    try:
        ts = int(nonce.split(":", 1)[0])
    except ValueError:
        return False
    if abs(time.time() - ts) > TOKEN_TTL_S:
        return False
    return keystore.verify(nonce.encode(), mac, "tool-passthrough")


def tool_command(args: list[str]) -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, ["--moteus-tool", *args]
    return sys.executable, ["-u", "-m", "moteus.moteus_tool", *args]


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class ToolRunner(QObject):
    line = Signal(str)
    finished = Signal(int, object)   # exit code, parsed report (dict) or None

    def __init__(self, keystore, cal_dir: Path, parent=None):
        super().__init__(parent)
        self.ks = keystore
        self.cal_dir = Path(cal_dir)
        self.proc: Optional[QProcess] = None
        self._buf = ""
        self._lines: list[str] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def start(self, args: list[str]) -> None:
        if self.running:
            raise RuntimeError("A tool is already running")
        program, argv = tool_command(args)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("MOTEUS_CAL_DIR", str(self.cal_dir))
        for k, v in issue_token(self.ks).items():
            env.insert(k, v)
        self._buf, self._lines = "", []
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(lambda e: self.line.emit(f"[process error: {e}]"))
        self.line.emit("$ moteus_tool " + " ".join(args))
        self.proc.start(program, argv)

    def kill(self) -> None:
        if self.running:
            self.proc.kill()

    def _read(self) -> None:
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._buf += data.replace("\r", "\n")
        while "\n" in self._buf:
            ln, self._buf = self._buf.split("\n", 1)
            if ln.strip():
                self._lines.append(ln)
                self.line.emit(ln)

    def _done(self, code: int, _status) -> None:
        self._read()
        if self._buf.strip():
            self.line.emit(self._buf)
            self._lines.append(self._buf)
        self.finished.emit(int(code), parse_report(self._lines))
        self.proc = None


def parse_report(lines: list[str]) -> Optional[dict]:
    """Extract the JSON block printed after ``REPORT:`` by moteus_tool."""
    for i, ln in enumerate(lines):
        if ln.startswith("REPORT:"):
            body = [l for l in lines[i + 1:] if not l.startswith("----")]
            text = "\n".join(body)
            start = text.find("{")
            if start < 0:
                return None
            depth = 0
            for j, ch in enumerate(text[start:], start):
                depth += ch == "{"
                depth -= ch == "}"
                if depth == 0:
                    try:
                        return json.loads(text[start:j + 1])
                    except ValueError:
                        return None
    return None


def run_passthrough(argv: list[str]) -> int:
    """Entry point for ``MotorHMI --moteus-tool ...`` (frozen builds)."""
    from ..paths import AppPaths
    from ..security.keystore import KeyStore
    ks = KeyStore(AppPaths().key_file)
    if not verify_token(ks):
        sys.stderr.write("moteus_tool passthrough is only available from an authenticated HMI session.\n")
        return 2
    sys.argv = ["moteus_tool", *argv]
    from moteus import moteus_tool
    moteus_tool.main()
    return 0
