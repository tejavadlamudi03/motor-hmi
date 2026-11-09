"""Diagnostic console (engineers).  Commands are allow-listed: motion
commands (``d pos``, ``d pwm`` …) are refused because motion must go
through the supervised control path."""

from __future__ import annotations

import json

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QVBoxLayout

from ...core.controllers import F_CHANNELS
from ...security.auth import Perm
from ..widgets import button, confirm, group, muted
from .base import HmiTab

ALLOW = ("conf get ", "conf set ", "conf enumerate", "conf write", "tel list", "d stop",
         "d rezero", "d cfg-set-output ", "cmd ", "info")
CONFIRM = ("conf write", "d rezero", "d cfg-set-output", "cmd start", "cmd encoder_align")


def allowed(cmd: str) -> bool:
    c = cmd.strip()
    return any(c == a.strip() or c.startswith(a) for a in ALLOW)


class ConsoleTab(HmiTab):
    title = "Console"
    perm = Perm.CONSOLE

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMaximumBlockCount(5000)
        self.out.setStyleSheet("font-family: monospace;")
        lay.addWidget(self.out, 1)
        row = QHBoxLayout()
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("conf get servo.pid_position.kp   |   conf set …   |   tel list")
        self.inp.returnPressed.connect(self._send)
        row.addWidget(self.inp, 1)
        row.addWidget(button("Send", "primary", self._send))
        lay.addLayout(row)
        ch = QHBoxLayout()
        self.channel = QComboBox()
        self.channel.setEditable(True)
        ch.addWidget(self.channel, 1)
        ch.addWidget(button("Read channel", slot=self._read_channel))
        lay.addWidget(group("Telemetry channel", ch))
        lay.addWidget(muted("Allowed: " + ", ".join(a.strip() for a in ALLOW) + ". Every command is "
                            "recorded in the audit trail."))
        self._hist: list[str] = []
        self.on_connection(False)

    def on_connection(self, connected: bool) -> None:
        self.channel.clear()
        if self.ctx.profile.driver == "st_mcsdk":
            self.channel.addItems(["registers", "board_info"])
        else:
            self.channel.addItems(["servo_stats", "drv8323", "firmware", "git", "motor_position"])

    def _send(self) -> None:
        cmd = self.inp.text().strip()
        mid = self.ctx.selected_motor
        if not cmd or mid is None or not self.ctx.require(Perm.CONSOLE, self):
            return
        if "\n" in cmd or "\r" in cmd or not cmd.isprintable():
            self.out.appendPlainText("! rejected: control characters")
            return
        if not allowed(cmd):
            self.out.appendPlainText(f"! '{cmd}' is not allowed here (motion must use Test & Control)")
            self.ctx.audit("console.denied", {"motor": mid, "command": cmd}, "denied")
            return
        if any(cmd.startswith(c) for c in CONFIRM) and not confirm(self, "Console", f"Run '{cmd}' on {mid}?"):
            return
        self.inp.clear()
        self.out.appendPlainText(f"[{mid}]> {cmd}")
        self.ctx.audit("console.command", {"motor": mid, "command": cmd})
        any_resp = cmd.startswith("conf get")
        self.ctx.run(self.ctx.worker.diagnostic(mid, cmd, any_resp, 30.0),
                     ok=lambda r: self.out.appendPlainText((r or "OK").rstrip()),
                     err=lambda e: self.out.appendPlainText(f"! {e}"))

    def _read_channel(self) -> None:
        mid = self.ctx.selected_motor
        name = self.channel.currentText().strip()
        if mid is None or not name or not self.ctx.require(Perm.CONSOLE, self):
            return
        if not name.replace("_", "").isalnum():
            return
        self.ctx.run(self.ctx.worker.read_channel(mid, name),
                     ok=lambda d: self.out.appendPlainText(f"[{mid}] {name}:\n" + json.dumps(d, indent=2, default=str)),
                     err=lambda e: self.out.appendPlainText(f"! {e}"))
