"""Safety & Alarms: per-motor limits, global safety settings, firmware
limit enforcement, alarm journal."""

from __future__ import annotations

import csv
import math
import time
from dataclasses import fields

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...core.controllers import F_CONF_PERSIST, F_FW_WATCHDOG
from ...core.safety import HARD_CEILINGS, SafetyLimits, Severity, validate_limits
from ...core.types import finite
from ...security.auth import Perm
from .. import theme
from ..widgets import button, confirm, group, muted
from .base import HmiTab

LABELS = {
    "max_velocity": ("Max velocity", "rev/s"), "max_accel": ("Max acceleration", "rev/s²"),
    "max_torque": ("Max torque", "N·m"), "position_min": ("Soft limit min", "rev"),
    "position_max": ("Soft limit max", "rev"), "min_voltage": ("Min bus voltage", "V"),
    "max_voltage": ("Max bus voltage", "V"), "max_board_temp": ("Max board temp", "°C"),
    "max_motor_temp": ("Max motor temp", "°C"), "max_following_error": ("Max following error", "rev"),
    "overspeed_factor": ("Over-speed trip factor", "×"), "kp_scale_max": ("Max Kp scale", "×"),
    "kd_scale_max": ("Max Kd scale", "×"),
}
OPTIONAL = ("position_min", "position_max", "max_following_error")
SEV_COLOR = {Severity.INFO: theme.MUTED, Severity.WARNING: theme.WARN,
             Severity.TRIP: theme.ALARM, Severity.ESTOP: theme.ALARM}


class SafetyTab(HmiTab):
    title = "Safety && Alarms"
    active_alarms_changed = Signal(int, int)    # active count, highest severity

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QGridLayout(self)

        lf = QFormLayout()
        self.motor_lbl = QLabel("Defaults (no motor selected)")
        lf.addRow(self.motor_lbl)
        self.spins: dict[str, QDoubleSpinBox] = {}
        self.enables: dict[str, QCheckBox] = {}
        ceil = HARD_CEILINGS
        for f in fields(SafetyLimits):
            label, unit = LABELS[f.name]
            sp = QDoubleSpinBox()
            sp.setDecimals(4)
            sp.setRange(-1e4, 1e4)
            cv = getattr(ceil, f.name)
            if finite(cv) and f.name not in ("min_voltage",):
                sp.setMaximum(cv)
                sp.setToolTip(f"Hard ceiling {cv} {unit}")
            sp.setSuffix(f" {unit}")
            self.spins[f.name] = sp
            if f.name in OPTIONAL:
                chk = QCheckBox()
                chk.toggled.connect(sp.setEnabled)
                self.enables[f.name] = chk
                w = QWidget()
                h = QHBoxLayout(w)
                h.setContentsMargins(0, 0, 0, 0)
                h.addWidget(chk)
                h.addWidget(sp, 1)
                lf.addRow(label, w)
            else:
                lf.addRow(label, sp)
        row = QHBoxLayout()
        self.apply_btn = button("Apply to motor", "primary", self._apply)
        self.all_btn = button("Apply to all motors", slot=lambda: self._apply(all_motors=True))
        row.addWidget(self.apply_btn)
        row.addWidget(self.all_btn)
        lf.addRow(row)
        self.err = QLabel("")
        self.err.setWordWrap(True)
        self.err.setStyleSheet(f"color: {theme.ALARM};")
        lf.addRow(self.err)
        lay.addWidget(group("HMI safety limits (enforced on every command)", lf), 0, 0, 2, 1)

        gf = QFormLayout()
        self.estop_action = QComboBox()
        self.estop_action.addItem("Torque off (coast) – category 0", "stop")
        self.estop_action.addItem("Brake (phases shorted)", "brake")
        self.rate = QSpinBox(); self.rate.setRange(5, 500); self.rate.setSuffix(" Hz")
        self.wdog = QDoubleSpinBox(); self.wdog.setRange(0.02, 1.0); self.wdog.setDecimals(3); self.wdog.setSuffix(" s")
        self.comm = QSpinBox(); self.comm.setRange(2, 200); self.comm.setSuffix(" cycles")
        gf.addRow("E-stop action", self.estop_action)
        gf.addRow("Control loop rate", self.rate)
        gf.addRow("Controller watchdog", self.wdog)
        gf.addRow("Comm-loss trip after", self.comm)
        self.global_btn = button("Save (applies on next connect)", slot=self._save_global)
        gf.addRow(self.global_btn)
        self.wd_note = muted("")
        gf.addRow(self.wd_note)
        fw = QHBoxLayout()
        self.fw_btn = button("Write soft limits + watchdog to controller", "danger", self._push_firmware)
        fw.addWidget(self.fw_btn)
        gf.addRow(fw)
        gf.addRow(muted("Writes servopos.position_min/max and servo.default_timeout_s so the "
                        "controller enforces them even if this PC fails."))
        lay.addWidget(group("Global safety settings", gf), 0, 1)

        al = QVBoxLayout()
        self.alarms = QTableWidget(0, 6)
        self.alarms.setHorizontalHeaderLabels(["Time", "Motor", "Severity", "Code", "Message", "State"])
        self.alarms.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.alarms.setEditTriggers(QTableWidget.NoEditTriggers)
        al.addWidget(self.alarms)
        r2 = QHBoxLayout()
        r2.addWidget(button("Acknowledge all", slot=self._ack))
        r2.addWidget(button("Export CSV…", slot=self._export))
        r2.addStretch(1)
        al.addLayout(r2)
        lay.addWidget(group("Alarm journal", al), 1, 1)
        lay.setColumnStretch(1, 2)
        self._rows: list = []
        self._active: dict = {}
        self._load_global()
        self.on_motor(None)
        self.on_session()

    # ---- limits ---------------------------------------------------------
    def _limits_ui(self) -> SafetyLimits:
        lim = SafetyLimits()
        for name, sp in self.spins.items():
            v = sp.value()
            if name in self.enables and not self.enables[name].isChecked():
                v = math.nan
            setattr(lim, name, v)
        return lim

    def on_motor(self, motor_id) -> None:
        mid = motor_id if motor_id is not None else self.ctx.selected_motor
        lim = self.ctx.settings.limits_for(mid) if mid is not None else SafetyLimits.from_dict(self.ctx.settings.default_limits)
        self.motor_lbl.setText(f"Motor {mid}" if mid is not None else "Defaults (no motor selected)")
        for name, sp in self.spins.items():
            v = getattr(lim, name)
            if name in self.enables:
                self.enables[name].setChecked(finite(v))
                sp.setEnabled(finite(v))
            sp.setValue(v if finite(v) else 0.0)

    def _apply(self, all_motors: bool = False) -> None:
        if not self.ctx.require(Perm.EDIT_LIMITS, self):
            return
        lim = self._limits_ui()
        prof = self.ctx.profile
        errs = validate_limits(lim)
        if lim.max_voltage > prof.max_voltage:
            errs.append(f"max_voltage exceeds {prof.title} rating ({prof.max_voltage} V)")
        self.err.setText("\n".join(errs))
        if errs:
            return
        targets = [d.motor_id for d in self.ctx.devices] if all_motors else [self.ctx.selected_motor]
        targets = [t for t in targets if t is not None]
        if not targets:
            self.ctx.settings.default_limits = lim.to_dict()
        for mid in targets:
            self.ctx.settings.motor_limits[str(mid)] = lim.to_dict()
            if self.ctx.connected:
                self.ctx.run(self.ctx.worker.set_limits(mid, lim), what="Set limits")
        self.ctx.save_settings()
        self.ctx.audit("safety.limits", {"motors": targets or "default", "limits": lim.to_dict()})
        self.ctx.status_message.emit("Safety limits applied", 4000)

    # ---- global ---------------------------------------------------------
    def _load_global(self) -> None:
        s = self.ctx.settings
        self.estop_action.setCurrentIndex(max(0, self.estop_action.findData(s.estop_action)))
        self.rate.setValue(int(s.rate_hz))
        self.wdog.setValue(s.watchdog_s)
        self.comm.setValue(int(s.comm_loss_cycles))

    def _save_global(self) -> None:
        if not self.ctx.require(Perm.EDIT_LIMITS, self):
            return
        s = self.ctx.settings
        s.estop_action = self.estop_action.currentData()
        s.rate_hz = float(self.rate.value())
        s.watchdog_s = float(self.wdog.value())
        s.comm_loss_cycles = int(self.comm.value())
        if s.watchdog_s < 3.0 / s.rate_hz:
            QMessageBox.warning(self, "Watchdog", "The watchdog must cover at least 3 control periods.")
            return
        self.ctx.save_settings()
        sup = self.ctx.worker.supervisor
        sup.estop_action = s.estop_action
        sup.comm_loss_cycles = s.comm_loss_cycles
        self.ctx.audit("safety.global", {"estop_action": s.estop_action, "rate_hz": s.rate_hz,
                                         "watchdog_s": s.watchdog_s, "comm_loss_cycles": s.comm_loss_cycles})
        self.ctx.status_message.emit("Saved. Rate / watchdog apply at the next connect.", 5000)

    def _push_firmware(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.CONFIGURE, self):
            return
        lim = self.ctx.settings.limits_for(mid)
        cmds = [f"conf set servopos.position_min {lim.position_min if finite(lim.position_min) else 'nan'}",
                f"conf set servopos.position_max {lim.position_max if finite(lim.position_max) else 'nan'}",
                f"conf set servo.default_timeout_s {self.ctx.settings.watchdog_s:g}"]
        save = confirm(self, "Controller limits", "Write these to the controller:\n\n" + "\n".join(cmds) +
                       "\n\nThey will also be saved to flash.", typed="WRITE")
        if not save:
            return
        w = self.ctx.worker

        async def push():
            for c in cmds:
                await w._diagnostic(mid, c, False, 5.0)
            await w._diagnostic(mid, "conf write", False, 5.0)
            return True

        self.ctx.audit("safety.firmware_limits", {"motor": mid, "commands": cmds})
        self.ctx.run(w.run_coroutine(push), what="Write controller limits",
                     ok=lambda _: self.ctx.status_message.emit("Controller limits written and saved", 5000))

    def on_session(self) -> None:
        c = self.ctx
        edit = c.can(Perm.EDIT_LIMITS)
        for w in (self.apply_btn, self.all_btn, self.global_btn):
            w.setEnabled(edit)
        prof = c.profile
        self.fw_btn.setEnabled(c.connected and c.can(Perm.CONFIGURE) and prof.has(F_CONF_PERSIST))
        self.wd_note.setText("" if prof.has(F_FW_WATCHDOG) else
                             f"⚠ {prof.model}: no controller-side command watchdog. If this PC stops, "
                             "the drive keeps its last setpoint. Hard-wired E-stop required.")
        self.wd_note.setStyleSheet(f"color: {theme.WARN};")

    def on_connection(self, connected: bool) -> None:
        self.on_session()

    # ---- alarms ---------------------------------------------------------
    def on_alarm(self, a) -> None:
        key = a.key
        if a.active:
            self._active[key] = a
        else:
            self._active.pop(key, None)
        r = 0
        self.alarms.insertRow(r)
        vals = [time.strftime("%H:%M:%S", time.localtime(a.cleared_at or a.raised_at)),
                "-" if a.motor_id is None else str(a.motor_id), a.severity.label, a.code, a.message,
                "ACTIVE" if a.active else "cleared"]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(v)
            if a.active:
                it.setForeground(QColor(SEV_COLOR[a.severity]))
            self.alarms.setItem(r, c, it)
        if self.alarms.rowCount() > 2000:
            self.alarms.removeRow(self.alarms.rowCount() - 1)
        self._emit_summary()

    def clear_motor_alarms(self, motor_id=None) -> None:
        for k in [k for k in self._active if motor_id is None or k[1] == motor_id or k[1] is None]:
            self._active.pop(k)
        self._emit_summary()

    def _emit_summary(self) -> None:
        unacked = [a for a in self._active.values() if not a.acknowledged]
        sev = max((int(a.severity) for a in self._active.values()), default=-1)
        self.active_alarms_changed.emit(len(unacked), sev)

    def _ack(self) -> None:
        for a in self._active.values():
            a.acknowledged = True
        self.ctx.audit("alarm.acknowledge", {"count": len(self._active)})
        self._emit_summary()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export alarms", str(self.ctx.paths.logs / "alarms.csv"), "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow([self.alarms.horizontalHeaderItem(c).text() for c in range(self.alarms.columnCount())])
            for r in range(self.alarms.rowCount()):
                wr.writerow([self.alarms.item(r, c).text() for c in range(self.alarms.columnCount())])
        self.ctx.audit("alarm.export", {"file": path})
