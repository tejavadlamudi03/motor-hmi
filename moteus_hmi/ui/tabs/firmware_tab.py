"""Firmware: version read-out and (moteus) controller firmware upgrade."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QVBoxLayout, QGridLayout)

from ...core.controllers import F_CAL_TOOL
from ...core.tools import ToolRunner, sha256_file
from ...core.types import ControlMode, MotorCommand
from ...security.auth import Perm
from .. import theme
from ..widgets import button, confirm, group, muted
from .base import HmiTab


def version_report(info: dict, motor_id: int, hmi_version: str) -> str:
    """Human readable version summary in the style of ``moteus_tool --info``."""
    import datetime
    lines = [f"Controller ID {motor_id}"]
    fw = info.get("firmware")
    if isinstance(fw, dict) and "version" in fw:
        v = int(fw["version"])
        lines.append(f"  Firmware ABI   : {v >> 16}.{(v >> 8) & 0xFF}.{v & 0xFF}   (0x{v:06x})")
        if "model" in fw:
            lines.append(f"  Model          : 0x{int(fw['model']):x}")
        sn = fw.get("serial_number")
        if isinstance(sn, (list, tuple)) and len(sn) == 3:
            try:
                from moteus.moteus_tool import _base64_serial_number
                lines.append(f"  Serial number  : {_base64_serial_number(*sn)}")
            except Exception:
                lines.append(f"  Serial number  : {sn}")
    elif isinstance(fw, dict) and "board_info" in fw:
        lines.append(f"  Board info     : {fw['board_info'] or '(none reported)'}")
    elif fw is not None:
        lines.append(f"  Firmware       : {fw}")
    git = info.get("git")
    if isinstance(git, dict) and "hash" in git:
        h = git["hash"]
        try:
            from moteus.moteus_tool import _make_git_hash
            hs = _make_git_hash(h)
        except Exception:
            hs = "".join(f"{b:02x}" for b in h)
        lines.append(f"  Git hash       : {hs}" + ("  (dirty build)" if git.get("dirty") else ""))
        ts = git.get("timestamp")
        if ts:
            lines.append("  Built          : " +
                         datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    elif git is not None:
        lines.append(f"  Git            : {git}")
    try:
        import moteus
        lib = moteus.VERSION
    except Exception:
        lib = "n/a"
    lines += ["", f"Motor HMI {hmi_version}  ·  moteus library {lib}"]
    return "\n".join(lines)


class FirmwareTab(HmiTab):
    title = "Firmware"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QGridLayout(self)
        info = QFormLayout()
        self.read_btn = button("Read version", slot=self._read)
        self.version = QPlainTextEdit()
        self.version.setReadOnly(True)
        self.version.setMinimumHeight(190)
        self.version.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.version.setStyleSheet("font-family: monospace;")
        info.addRow(self.read_btn)
        info.addRow(self.version)
        lay.addWidget(group("Controller firmware", info), 0, 0)

        up = QFormLayout()
        row = QHBoxLayout()
        self.file = QLineEdit()
        self.file.setReadOnly(True)
        row.addWidget(self.file, 1)
        self.open_btn = button("Open file…", slot=self._open)
        row.addWidget(self.open_btn)
        up.addRow("Firmware (.elf)", row)
        self.sha = QLabel("—")
        self.sha.setStyleSheet("font-family: monospace; font-size: 8pt;")
        self.sha.setWordWrap(True)
        up.addRow("SHA-256", self.sha)
        self.approval = QLabel("")
        up.addRow("Approval", self.approval)
        r2 = QHBoxLayout()
        self.approve_btn = button("Approve this hash", slot=self._approve,
                                  tip="Admin: add the file hash to the approved firmware list")
        self.flash_btn = button("Upgrade", "danger", self._flash)
        r2.addWidget(self.approve_btn)
        r2.addWidget(self.flash_btn)
        up.addRow(r2)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        up.addRow(self.progress)
        self.note = muted("")
        up.addRow(self.note)
        lay.addWidget(group("Firmware upgrade", up), 1, 0)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: monospace; font-size: 9pt;")
        l = QVBoxLayout()
        l.addWidget(self.log)
        lay.addWidget(group("Upgrade log", l), 0, 1, 2, 1)
        lay.setColumnStretch(1, 1)
        self.runner = ToolRunner(ctx.keystore, ctx.paths.calibration, self)
        self.runner.line.connect(self.log.appendPlainText)
        self.runner.finished.connect(self._done)
        self._hash = ""
        self.on_session()

    def on_connection(self, connected: bool) -> None:
        self.on_session()

    def on_session(self) -> None:
        c = self.ctx
        prof = c.profile
        moteus = prof.has(F_CAL_TOOL)
        approved = self._hash and self._hash in c.settings.approved_firmware_sha256
        sim = c.connected and c.worker.config.kind == "virtual_canfd"
        self.read_btn.setEnabled(c.connected)
        self.open_btn.setEnabled(moteus and c.can(Perm.CONFIGURE))
        self.approve_btn.setEnabled(bool(self._hash) and c.can(Perm.MANAGE_USERS) and not approved)
        self.flash_btn.setEnabled(bool(approved) and moteus and c.connected and not sim
                                  and c.can(Perm.CONFIGURE) and not self.runner.running)
        self.note.setText(
            "Runs moteus_tool --flash (config is preserved and migrated by moteus_tool). Only "
            "firmware whose SHA-256 was approved by an administrator can be flashed."
            if moteus else
            f"{prof.model}: build and flash firmware with STM32CubeIDE / STM32CubeProgrammer "
            "(ST-LINK). Not available from this HMI.")
        if self._hash:
            self.approval.setText("✔ approved" if approved else "✘ not approved")
            self.approval.setStyleSheet(f"color: {theme.OK if approved else theme.WARN};")

    def _read(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None:
            return
        out = {}
        from ... import __version__

        def show(name):
            def f(v):
                out[name] = v
                self.version.setPlainText(version_report(out, mid, __version__))
            return f

        for ch in ("firmware", "git"):
            self.ctx.run(self.ctx.worker.read_channel(mid, ch), ok=show(ch),
                         err=lambda e, ch=ch: show(ch)(f"unavailable: {e}"))

    def _open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Firmware", "", "ELF (*.elf);;All files (*)")
        if not path:
            return
        self.file.setText(path)
        self._hash = sha256_file(path)
        self.sha.setText(self._hash)
        self.ctx.audit("firmware.select", {"file": path, "sha256": self._hash})
        self.on_session()

    def _approve(self) -> None:
        if not self.ctx.require(Perm.MANAGE_USERS, self):
            return
        if confirm(self, "Approve firmware", f"Approve firmware with SHA-256\n{self._hash}\nfor flashing?"):
            self.ctx.settings.approved_firmware_sha256.append(self._hash)
            self.ctx.save_settings()
            self.ctx.audit("firmware.approve", {"sha256": self._hash})
            self.on_session()

    def _flash(self) -> None:
        mid = self.ctx.selected_motor
        path = self.file.text()
        if mid is None or not path or not self.ctx.require(Perm.CONFIGURE, self):
            return
        if sha256_file(path) != self._hash:
            self.log.appendPlainText("File changed since it was selected – aborting.")
            return
        if not confirm(self, "Upgrade firmware", f"Flash {Path(path).name} to controller {mid}? Do not "
                       "remove power or the USB cable until it completes.", typed="FLASH"):
            return
        from ...core.moteus_backend import MoteusBackend
        args = MoteusBackend(self.ctx.worker.config).moteus_tool_args() + ["--target", str(mid), "--flash", path]
        self.ctx.audit("firmware.flash_start", {"motor": mid, "file": path, "sha256": self._hash})
        self.progress.setVisible(True)

        def released(_):
            self.runner.start(args)
            self.on_session()

        self.ctx.run(self.ctx.worker.release_bus(), ok=released, what="Release bus")

    def _done(self, code: int, _report) -> None:
        self.progress.setVisible(False)
        self.ctx.audit("firmware.flash_finish", {"exit_code": code}, "ok" if code == 0 else "failed")
        w = self.ctx.worker
        self.ctx.run(w.reacquire_bus(), what="Re-connect",
                     ok=lambda ids: [w.set_command(i, MotorCommand(mode=ControlMode.STOP)) for i in ids])
        self.log.appendPlainText("Upgrade finished." if code == 0 else f"moteus_tool exited with {code}.")
        self.on_session()
